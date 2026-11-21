"""Compact, lazily-decoded on-disk dependency map for the Blast Radius track.

Two queries have to be fast, and the program's CPU seconds are part of the
score, so the design goal is: near-zero cold start, and per-query cost
proportional to the data the patch actually touches (1-3 files out of 185).

    Q1  tests_for_file(path)         -> every test whose coverage touches path
    Q2  tests_for_lines(path, lines) -> every test whose coverage hits a line

FORMAT ("BRDM" v1, little-endian)
---------------------------------
The key observation is that coverage is block-structured: inside one source
file only a few thousand *distinct* test-sets exist across its covered lines
(7955 distinct sets across all 185 files, vs 35935 covered lines). So we
dedupe: every covered line points at a shared "block" and every block is a
flat bitset over test numbers.

    header        40 B   magic, version, n_files, n_tests, bitset_bytes,
                         and the offsets of the sections below
    directory     n_files * 28 B
                         min_line, n_lines, n_blocks,
                         lineidx_off, blocks_off, name_off, name_len
    names         '\n'-joined file paths
    file bitsets  n_files * bitset_bytes      (answers Q1 with one slice)
    global bitset bitset_bytes                (tests with any coverage at all)
    per file      uint16[n_lines]  line -> block id (0 = line not covered)
                  bitset_bytes * n_blocks     block id b lives at (b-1)

Nothing is decoded at open() time except the header, the names and the
directory: ~100 us of CPU. A query then slices the mmap for exactly the one
file it names, so a patch never pays for the other 180-odd files.

The default format is uncompressed on purpose: 1.82 MB of this file deflates
to 202 KB inside the git pack, which is what the 20 MB repo limit actually
counts, so zlib would buy nothing on disk while costing decompression CPU on
every touched file. encode(..., compress=True) produces the compressed variant
(247 KB on disk, read transparently) if a flat file ever has to be small.

Runtime dependencies: the Python standard library only. No writes anywhere,
no network, read-only filesystem safe (mmap is ACCESS_READ).
"""

from __future__ import annotations

import mmap
import struct
import sys
from array import array

# json / pathlib / zlib are only needed by the encoder (or by the optional
# compressed format), and `import pathlib` alone costs ~1 ms of CPU, so the
# read path deliberately imports nothing but cheap builtin C modules.

_LITTLE = sys.byteorder == "little"  # true on both amd64 and arm64

MAGIC = b"BRDM"
VERSION = 1
VERSION_Z = 2  # same layout, per-file payloads zlib-compressed

_HEADER = struct.Struct("<4sIIIIIIIII")
# min_line, n_lines, n_blocks, lineidx_off, blocks_off, name_off, name_len
_ENTRY = struct.Struct("<IIIIIII")
# compressed variant appends: payload_off, payload_zlen, payload_rawlen
_ENTRY_Z = struct.Struct("<IIIIIIIIII")


# ---------------------------------------------------------------- encoding


def encode(
    covmap_json_path,
    out_path,
    tests_json_path=None,
    compress: bool = False,
) -> dict:
    """Build the packed map from the raw covmap JSON.

    covmap_json_path: {"files": [...], "tests": {test_id: {file_idx: [lines]}}}
    tests_json_path:  sample/tests.json, used to turn test id strings into the
                      integer test "number"s the scorer expects. If omitted we
                      look next to the covmap and in ../sample/.

    Returns a dict of statistics (also useful for the benchmark harness).
    """
    import json
    import zlib
    from pathlib import Path

    covmap_json_path = Path(covmap_json_path)
    out_path = Path(out_path)
    tests_json_path = _find_tests_json(covmap_json_path, tests_json_path)

    covmap = json.loads(covmap_json_path.read_text())
    files = covmap["files"]
    per_test = covmap["tests"]

    meta = json.loads(Path(tests_json_path).read_text())["tests"]
    num_of = {t["id"]: t["number"] for t in meta}
    n_tests = max(t["number"] for t in meta) + 1
    bb = (n_tests + 7) // 8

    # file index -> {line: [test numbers]}
    lines_of_file: list[dict[int, list[int]]] = [{} for _ in files]
    unmapped = []
    for test_id, fmap in per_test.items():
        num = num_of.get(test_id)
        if num is None:
            unmapped.append(test_id)
            continue
        for fi, lines in fmap.items():
            table = lines_of_file[int(fi)]
            for line in lines:
                hit = table.get(line)
                if hit is None:
                    table[line] = [num]
                else:
                    hit.append(num)

    def bitset(nums) -> bytes:
        buf = bytearray(bb)
        for n in nums:
            buf[n >> 3] |= 1 << (n & 7)
        return bytes(buf)

    names = "\n".join(files).encode()
    file_bits: list[bytes] = []
    payloads: list[bytes] = []
    entries: list[tuple] = []
    global_nums: set[int] = set()
    n_blocks_total = 0

    for fi, table in enumerate(lines_of_file):
        if table:
            min_line = min(table)
            max_line = max(table)
            n_lines = max_line - min_line + 1
            # dedupe identical test-sets: one block id per distinct set
            block_of: dict[tuple, int] = {}
            line_idx = bytearray(2 * n_lines)
            seen: set[int] = set()
            for line, nums in table.items():
                key = tuple(sorted(nums))
                bid = block_of.get(key)
                if bid is None:
                    bid = len(block_of) + 1  # 0 is reserved for "not covered"
                    block_of[key] = bid
                off = 2 * (line - min_line)
                line_idx[off] = bid & 0xFF
                line_idx[off + 1] = bid >> 8
                seen.update(nums)
            if len(block_of) > 0xFFFF:
                raise ValueError(f"{files[fi]}: too many distinct line sets")
            blocks = bytearray()
            for key in block_of:  # dict preserves insertion == block id order
                blocks += bitset(key)
            n_blocks = len(block_of)
            n_blocks_total += n_blocks
            global_nums |= seen
            file_bits.append(bitset(seen))
            payloads.append(bytes(line_idx) + bytes(blocks))
        else:
            min_line = n_lines = n_blocks = 0
            file_bits.append(bytes(bb))
            payloads.append(b"")
        entries.append((min_line, n_lines, n_blocks))

    n_files = len(files)
    entry_struct = _ENTRY_Z if compress else _ENTRY
    dir_off = _HEADER.size
    names_off = dir_off + n_files * entry_struct.size
    filebits_off = names_off + len(names)
    globalbits_off = filebits_off + n_files * bb
    payload_base = globalbits_off + bb

    stored = [zlib.compress(p, 6) if (compress and p) else p for p in payloads]
    out = bytearray(payload_base)
    dir_bytes = bytearray()
    name_cursor = 0
    for fi in range(n_files):
        min_line, n_lines, n_blocks = entries[fi]
        name_len = len(files[fi].encode())
        blob = stored[fi]
        off = len(out) if blob else 0  # absolute byte offset in the file
        if blob:
            out += blob
        lineidx_off = off
        blocks_off = off + 2 * n_lines  # only meaningful for the raw format
        if compress:
            dir_bytes += entry_struct.pack(
                min_line, n_lines, n_blocks, lineidx_off, blocks_off,
                name_cursor, name_len, off, len(blob), len(payloads[fi]),
            )
        else:
            dir_bytes += entry_struct.pack(
                min_line, n_lines, n_blocks, lineidx_off, blocks_off,
                name_cursor, name_len,
            )
        name_cursor += name_len + 1

    out[0:_HEADER.size] = _HEADER.pack(
        MAGIC, VERSION_Z if compress else VERSION, n_files, n_tests, bb,
        dir_off, names_off, len(names), filebits_off, globalbits_off,
    )
    out[dir_off:names_off] = dir_bytes
    out[names_off:filebits_off] = names
    out[filebits_off:globalbits_off] = b"".join(file_bits)
    out[globalbits_off:payload_base] = bitset(global_nums)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(out))
    return {
        "path": str(out_path),
        "bytes": len(out),
        "n_files": n_files,
        "n_tests": n_tests,
        "bitset_bytes": bb,
        "n_blocks": n_blocks_total,
        "covered_lines": sum(len(t) for t in lines_of_file),
        "unmapped_test_ids": unmapped,
        "compressed": bool(compress),
    }


def _find_tests_json(covmap_json_path, given):
    from pathlib import Path

    if given:
        return Path(given)
    here = Path(__file__).resolve().parent
    for cand in (
        covmap_json_path.parent / "tests.json",
        here.parent / "sample" / "tests.json",
        Path("/data/tests.json"),
    ):
        if cand.exists():
            return cand
    raise FileNotFoundError("tests.json not found; pass tests_json_path=")


# ---------------------------------------------------------------- decoding

# bit position -> test number, memoised per (byte offset, byte value) so that
# turning a bitset into a set[int] is a handful of C-level set.update calls.
_BYTE_NUMS: dict[int, tuple] = {}


def _nums_for(key: int) -> tuple:
    pos, byte = key >> 8, key & 0xFF
    base = pos << 3
    out = tuple(base + b for b in range(8) if byte >> b & 1)
    _BYTE_NUMS[key] = out
    return out


class DepMap:
    """Read-only view over a packed map. Open once, query many times."""

    __slots__ = (
        "_buf", "_mv", "_dir", "_idx", "_names", "_bb", "_filebits",
        "_globalbits", "_n_tests", "_q1", "_payload", "_compressed", "_cast",
    )

    def __init__(self, path, use_mmap: bool = True):
        fh = open(path, "rb")
        try:
            if use_mmap:
                self._buf = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            else:
                self._buf = fh.read()
        finally:
            fh.close()
        mv = self._mv = memoryview(self._buf)
        (magic, version, n_files, n_tests, bb, dir_off, names_off, names_len,
         filebits_off, globalbits_off) = _HEADER.unpack_from(mv, 0)
        if magic != MAGIC:
            raise ValueError("not a BRDM map")
        if version not in (VERSION, VERSION_Z):
            raise ValueError(f"unsupported BRDM version {version}")
        self._compressed = version == VERSION_Z
        self._bb = bb
        self._n_tests = n_tests
        self._filebits = filebits_off
        self._globalbits = globalbits_off
        self._names = names = bytes(
            mv[names_off:names_off + names_len]
        ).decode().split("\n")
        entry = _ENTRY_Z if self._compressed else _ENTRY
        self._dir = list(entry.iter_unpack(
            bytes(mv[dir_off:dir_off + n_files * entry.size])
        ))
        self._idx = {name: i for i, name in enumerate(names)}
        self._q1: dict[int, set] = {}
        self._payload: dict[int, memoryview] = {}
        self._cast: dict[int, memoryview] = {}

    # ------------------------------------------------------------ queries

    def tests_for_file(self, path: str) -> set:
        """Q1: every test number whose coverage touches this file at all."""
        fi = self._idx.get(path)
        if fi is None:
            return set()
        hit = self._q1.get(fi)
        if hit is None:
            off = self._filebits + fi * self._bb
            hit = self._q1[fi] = self._expand(
                int.from_bytes(self._mv[off:off + self._bb], "little")
            )
        return set(hit)

    def tests_for_lines(self, path: str, lines) -> set:
        """Q2: every test number whose coverage intersects these lines."""
        fi = self._idx.get(path)
        if fi is None:
            return set()
        entry = self._dir[fi]
        min_line, n_lines, n_blocks = entry[0], entry[1], entry[2]
        if not n_lines:
            return set()
        idx = self._cast.get(fi)
        if idx is None:
            base = self._base(fi)
            raw = base[:2 * n_lines]
            if _LITTLE:
                idx = raw.cast("H")  # zero-copy; native order == file order
            else:  # big-endian host: swap once, then behave identically
                arr = array("H")
                arr.frombytes(bytes(raw))
                arr.byteswap()
                idx = arr
            self._cast[fi] = idx
        ids = set()
        add = ids.add
        for line in lines:
            k = line - min_line
            if 0 <= k < n_lines:
                add(idx[k])
        ids.discard(0)
        if not ids:
            return set()
        if len(ids) == n_blocks:  # the patch reaches every block: Q1 answers it
            return self.tests_for_file(path)
        bb = self._bb
        blocks = self._base(fi)
        start = 2 * n_lines
        acc = 0
        for bid in ids:
            off = start + (bid - 1) * bb
            acc |= int.from_bytes(blocks[off:off + bb], "little")
        return self._expand(acc)

    # ------------------------------------------------------------ extras

    @property
    def files(self) -> list:
        return list(self._names)

    @property
    def n_tests(self) -> int:
        return self._n_tests

    def has_file(self, path: str) -> bool:
        return path in self._idx

    def tests_with_coverage(self) -> set:
        off = self._globalbits
        return self._expand(
            int.from_bytes(self._mv[off:off + self._bb], "little")
        )

    def tests_without_coverage(self) -> set:
        """Tests no coverage row mentions -- always select these, blindly."""
        return set(range(self._n_tests)) - self.tests_with_coverage()

    def close(self) -> None:
        self._cast.clear()
        self._payload.clear()
        if isinstance(self._buf, mmap.mmap):
            self._mv.release()
            self._buf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ internals

    def _base(self, fi: int) -> memoryview:
        """The one file's payload: uint16 line->block table then the blocks."""
        got = self._payload.get(fi)
        if got is not None:
            return got
        entry = self._dir[fi]
        if self._compressed:
            import zlib

            off, zlen = entry[7], entry[8]
            got = memoryview(zlib.decompress(self._mv[off:off + zlen]))
        else:
            off = entry[3]
            size = 2 * entry[1] + entry[2] * self._bb
            got = self._mv[off:off + size]
        self._payload[fi] = got
        return got

    @staticmethod
    def _expand(bits: int) -> set:
        if not bits:
            return set()
        raw = bits.to_bytes((bits.bit_length() + 7) // 8, "little")
        table = _BYTE_NUMS
        out = set()
        update = out.update
        for pos, byte in enumerate(raw):
            if byte:
                key = pos << 8 | byte
                try:
                    update(table[key])
                except KeyError:
                    update(_nums_for(key))
        return out


def main(argv=None) -> int:
    import json

    args = list(sys.argv[1:] if argv is None else argv)
    compress = "--compress" in args
    args = [a for a in args if a != "--compress"]
    if len(args) < 2:
        print("usage: depmap.py COVMAP.json OUT.brdm [TESTS.json] [--compress]")
        return 2
    stats = encode(args[0], args[1], args[2] if len(args) > 2 else None,
                   compress=compress)
    stats["unmapped_test_ids"] = len(stats["unmapped_test_ids"])
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

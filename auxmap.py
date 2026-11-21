"""Auxiliary maps for the Blast Radius selector: fixtures, file-level coverage,
test-file membership, orphan tests, and module-level line ranges.

`depmap.py` (format `BRDM`) answers the two *line coverage* questions. This
module answers the five remaining ones, which together drive every rung of the
fallback ladder in `docs/SOUNDNESS.md`:

    a. FIXTURE   tests_for_fixture(path)          which tests READ this data file
    b. FILEMAP   filemap_tests(path)              per-test-file coverage, the blunt backstop
    c. TESTFILE  tests_in_test_file(path)         a test file implicates its own tests
    d. ORPHANS   orphan_tests()                   tests no coverage row mentions
    e. MODLINES  module_level_lines(path, lines)  which changed lines run at import time

Same design goals as `depmap.py`, for the same reason -- our CPU seconds are
added to the score 1:1: near-zero cold start, and per-query cost proportional
to what the patch actually names. `__init__` parses the 88-byte header and
nothing else; every name directory, bitset and line-range array is reached by
slicing the mmap on first use and then memoised.

FORMAT ("BRAX" v1, little-endian)
---------------------------------
All offsets are absolute byte offsets into the file. All bitsets are
`bitset_bytes = (max_test_number + 1 + 7) // 8` bytes wide (203 for the 1620
tests of the pinned suite), bit `n & 7` of byte `n >> 3` meaning "test number
`n` is in this set". All name blocks are '\n'-joined UTF-8 paths, **sorted**,
and a name's position in its block is the index of its fixed-size record in the
matching section -- so the sections need no per-entry directory at all, and a
sorted block also lets `fixture_paths_with_prefix` binary-search instead of
scanning.

    header       88 B   magic, version, n_tests, bitset_bytes, and the
                        offset/length of every section below

    names        4 sorted '\n'-joined blocks: fixture paths, filemap source
                 paths, test-file paths, module-level source paths

    fixture bits   fixture_count  * bitset_bytes
    filemap bits   filemap_count  * bitset_bytes
    testfile bits  testfile_count * bitset_bytes
    orphan bits    bitset_bytes                 (one set, not per name)

    modlines dir   mod_count * 8 B   n_runs, runs_off
    modlines runs  per file, 4-byte aligned: uint32[n_runs] run starts then
                   uint32[n_runs] run ends, both ascending, inclusive

Why MODLINES is shipped as data rather than computed at runtime: it is a pure
function of sqlglot at the pinned commit `eb11aec4`, the patches' line numbers
are in exactly that commit's coordinate system, and computing it at runtime
would mean shipping and `ast.parse`-ing the whole source tree (88 223 lines,
184 files). The ranges are long runs -- module-level code is imports, class
bodies, decorators, constants and the dialect registration tables, which come
in blocks -- so 184 files need only ~7 300 (start, end) pairs.

A line is "module level" here exactly when `astinfo.classify_lines` reports
`module_level=True` **and** `is_code=True`, i.e. the same answer as
`astinfo.module_level_lines(src, lines)` with its default `require_code=True`:
blank lines and pure comments are excluded, so reflowing a comment block at
module level does not fire the widening backstop. See `tools/astinfo.py` for
the semantics (class bodies, `def` headers with default arguments, decorators
and single-line lambdas are module level; multi-line lambda continuations are
not).

Nothing here is an answer worked out ahead of time: every section is keyed by a
path (and for MODLINES a line number) in the pinned tree, and no patch and no
label was an input. See docs/compliance.md, trap 5.

Runtime dependencies: the Python standard library only -- and from it only
`mmap`, `struct`, `sys` and (on a big-endian host) `array`. No writes
anywhere, no network, safe on a read-only filesystem: the mmap is ACCESS_READ.
"""

from __future__ import annotations

import mmap
import struct
import sys
from array import array

# json / pathlib / ast are needed only by the offline builder in
# tools/pack_aux.py. `import pathlib` alone costs ~1 ms of CPU, which is most
# of this module's whole budget, so the read path imports nothing but cheap
# builtin C modules.

_LITTLE = sys.byteorder == "little"  # true on both amd64 and arm64

MAGIC = b"BRAX"
VERSION = 1

_HEADER = struct.Struct(
    "<4s"       # magic
    "I"         # version
    "I"         # n_tests
    "I"         # bitset_bytes
    "IIII"      # FIXTURE  count, names_off, names_len, bits_off
    "IIII"      # FILEMAP  count, names_off, names_len, bits_off
    "IIII"      # TESTFILE count, names_off, names_len, bits_off
    "I"         # ORPHANS  bits_off
    "IIIII"     # MODLINES count, names_off, names_len, dir_off, n_runs_total
)
assert _HEADER.size == 88

#: MODLINES directory record: how many runs this file has, and where they live.
_MOD_ENTRY = struct.Struct("<II")

# Section ids, used only to key the lazily-built caches.
_FIXTURE, _FILEMAP, _TESTFILE = 0, 1, 2


# ---------------------------------------------------------------- decoding

# bit position -> test numbers, memoised per (byte offset, byte value), so that
# turning a 203-byte bitset into a set[int] is a handful of C-level set.update
# calls instead of 1620 shifts.
_BYTE_NUMS: dict[int, tuple] = {}


def _nums_for(key: int) -> tuple:
    pos, byte = key >> 8, key & 0xFF
    base = pos << 3
    out = tuple(base + b for b in range(8) if byte >> b & 1)
    _BYTE_NUMS[key] = out
    return out


def _expand(raw) -> set:
    """A bitset (any bytes-like) -> the set of test numbers it contains."""
    table = _BYTE_NUMS
    out: set = set()
    update = out.update
    for pos, byte in enumerate(raw):
        if byte:
            key = pos << 8 | byte
            try:
                update(table[key])
            except KeyError:
                update(_nums_for(key))
    return out


class AuxMaps:
    """Read-only view over a packed BRAX map. Open once, query many times.

    Every method is safe to call with a path the map has never heard of; the
    unknown-path answer is documented per method and is always the recall-safe
    direction (an empty set of *evidence*, or, for `module_level_lines`, every
    line, so that the caller widens rather than narrows).
    """

    __slots__ = (
        "_buf", "_mv", "_n_tests", "_bb", "_sec", "_names", "_idx", "_cache",
        "_mod", "_mod_names", "_mod_idx", "_mod_dir", "_mod_runs", "_orphan_off",
        "_orphans",
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
        self._mv = memoryview(self._buf)
        h = _HEADER.unpack_from(self._mv, 0)
        if h[0] != MAGIC:
            raise ValueError("not a BRAX map")
        if h[1] != VERSION:
            raise ValueError(f"unsupported BRAX version {h[1]}")
        self._n_tests = h[2]
        self._bb = h[3]
        # (count, names_off, names_len, bits_off) for the three bitset sections
        self._sec = (h[4:8], h[8:12], h[12:16])
        self._orphan_off = h[16]
        self._mod = h[17:22]  # count, names_off, names_len, dir_off, n_runs
        # Everything below is filled in on first use, per section / per file.
        self._names: list = [None, None, None]
        self._idx: list = [None, None, None]
        self._cache: list = [{}, {}, {}]
        self._mod_names = None
        self._mod_idx = None
        self._mod_dir = None
        self._mod_runs: dict = {}
        self._orphans = None

    # ------------------------------------------------------------ properties

    @property
    def n_tests(self) -> int:
        """One past the highest test number the map can represent."""
        return self._n_tests

    @property
    def fixture_paths(self) -> list:
        return list(self._name_list(_FIXTURE))

    @property
    def filemap_paths(self) -> list:
        return list(self._name_list(_FILEMAP))

    @property
    def test_file_paths(self) -> list:
        return list(self._name_list(_TESTFILE))

    @property
    def module_level_paths(self) -> list:
        return list(self._mod_name_list())

    # ------------------------------------------------------------ queries

    def tests_for_fixture(self, path: str) -> set:
        """(a) Tests that READ this fixture file. Empty set if unknown."""
        return self._bits(_FIXTURE, path) or set()

    def fixture_paths_with_prefix(self, prefix: str) -> list:
        """Known fixture paths starting with `prefix`, sorted.

        Used to widen on an unknown fixture: pass the parent directory and
        union the tests of everything under it.
        """
        names = self._name_list(_FIXTURE)
        i = _lower_bound(names, prefix)
        out = []
        n = len(names)
        while i < n and names[i].startswith(prefix):
            out.append(names[i])
            i += 1
        return out

    def filemap_tests(self, path: str):
        """(b) Per-test-file coverage: every test in every test file whose
        coverage touches this source file. `None` if the path is unknown --
        distinct from a known file with no covering test, which is `set()`.
        """
        return self._bits(_FILEMAP, path)

    def tests_in_test_file(self, path: str) -> set:
        """(c) The test numbers defined in this test file. Empty if unknown."""
        return self._bits(_TESTFILE, path) or set()

    def orphan_tests(self) -> set:
        """(d) Tests with no coverage row at all, so no coverage rule can ever
        select them. Ride them along on every commit.
        """
        hit = self._orphans
        if hit is None:
            off = self._orphan_off
            hit = self._orphans = _expand(self._mv[off:off + self._bb])
        return set(hit)

    def module_level_lines(self, path: str, lines) -> set:
        """(e) The subset of `lines` that executes at import time.

        If `path` is unknown, returns `set(lines)`: conservative, because a
        line we cannot place must not be allowed to suppress the module-level
        backstop.
        """
        idx = self._mod_index().get(path)
        if idx is None:
            return set(lines)
        starts, ends, n_runs = self._runs(idx)
        if not n_runs:
            return set()
        out: set = set()
        add = out.add
        for line in lines:
            lo, hi = 0, n_runs
            while lo < hi:  # rightmost run whose start is <= line
                mid = (lo + hi) >> 1
                if starts[mid] <= line:
                    lo = mid + 1
                else:
                    hi = mid
            if lo and line <= ends[lo - 1]:
                add(line)
        return out

    def has_module_level(self, path: str, lines) -> bool:
        """True when any of `lines` runs at import time (unknown path: True)."""
        return bool(self.module_level_lines(path, lines))

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._mod_runs.clear()
        for c in self._cache:
            c.clear()
        if isinstance(self._buf, mmap.mmap):
            self._mv.release()
            self._buf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ internals

    def _name_list(self, sec: int) -> list:
        got = self._names[sec]
        if got is None:
            count, off, length, _ = self._sec[sec]
            got = (
                bytes(self._mv[off:off + length]).decode().split("\n")
                if count else []
            )
            self._names[sec] = got
        return got

    def _index(self, sec: int) -> dict:
        got = self._idx[sec]
        if got is None:
            got = self._idx[sec] = {
                name: i for i, name in enumerate(self._name_list(sec))
            }
        return got

    def _bits(self, sec: int, path: str):
        """One section's bitset for one name, memoised; None if unknown."""
        i = self._index(sec).get(path)
        if i is None:
            return None
        cache = self._cache[sec]
        hit = cache.get(i)
        if hit is None:
            bb = self._bb
            off = self._sec[sec][3] + i * bb
            hit = cache[i] = _expand(self._mv[off:off + bb])
        return set(hit)  # a copy: callers union into their own sets

    def _mod_name_list(self) -> list:
        got = self._mod_names
        if got is None:
            count, off, length = self._mod[0], self._mod[1], self._mod[2]
            got = (
                bytes(self._mv[off:off + length]).decode().split("\n")
                if count else []
            )
            self._mod_names = got
        return got

    def _mod_index(self) -> dict:
        got = self._mod_idx
        if got is None:
            got = self._mod_idx = {
                name: i for i, name in enumerate(self._mod_name_list())
            }
        return got

    def _runs(self, idx: int):
        """(starts, ends, n_runs) for one file; uint32 views over the mmap."""
        got = self._mod_runs.get(idx)
        if got is not None:
            return got
        if self._mod_dir is None:
            count, dir_off = self._mod[0], self._mod[3]
            self._mod_dir = list(_MOD_ENTRY.iter_unpack(
                bytes(self._mv[dir_off:dir_off + count * _MOD_ENTRY.size])
            ))
        n_runs, off = self._mod_dir[idx]
        if not n_runs:
            got = ((), (), 0)
        else:
            raw = self._mv[off:off + 8 * n_runs]
            if _LITTLE:  # zero-copy: native order == file order
                both = raw.cast("I")
            else:  # big-endian host: swap once, then behave identically
                both = array("I")
                both.frombytes(bytes(raw))
                both.byteswap()
            got = (both[:n_runs], both[n_runs:], n_runs)
        self._mod_runs[idx] = got
        return got


def _lower_bound(names: list, prefix: str) -> int:
    """First index in the sorted `names` whose value is >= `prefix`."""
    lo, hi = 0, len(names)
    while lo < hi:
        mid = (lo + hi) >> 1
        if names[mid] < prefix:
            lo = mid + 1
        else:
            hi = mid
    return lo

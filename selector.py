#!/usr/bin/env python3
"""Blast Radius entry -- v2: evidence-driven selection over committed maps.

WHAT THIS VERSION DOES

For each patch it works out which tests the change can actually reach, using
three maps built offline from sqlglot at the pinned commit and committed as
plain binary files:

    maps/cov.brdm   per-test LINE coverage, corrected for the work sqlglot's
                    optimizer and executor farm out to spawned subprocesses
    maps/aux.brs    fixture reads, per-test-file coverage, module-level line
                    ranges, and the tests no coverage row mentions at all

Then it packs the selection onto the worker count that minimises the scorer's
own cost function. Nothing here is fitted to the sample answers: see
docs/compliance.md in the private workspace, and the note on the inclusion rule
below.

THE DECISION RULE

The scorer charges

    commit cost = (busiest worker's load) + 2 s * (number of non-empty workers)
    + 300 s for any commit whose selection omits a test that fails on it
    + our own CPU seconds

A 300 s miss penalty against a ~100 s suite inverts the usual
precision/recall tradeoff. A test is worth including exactly when

    P(t fails) * 300_000 ms  >  marginal cost of running t

which for a 3 ms test (the median) breaks even at a failure probability of
0.00025%, and for the 28.8 s worst case at 9.6%. So: include cheap tests
liberally, and demand real evidence only from the handful of expensive ones.
Those break-even points are derived from the scorer's published constants, not
tuned against labelled data.

THE EVIDENCE LADDER

Strongest evidence first, with a deliberate widening fallback at every rung,
because an unrecognised input must cost seconds, never a 300 s miss:

  0. tests with no coverage row at all ride along on every commit -- no
     coverage-derived rule could ever select them, and at a median 3 ms they
     are the cheapest insurance available.
  1. a changed data fixture under tests/fixtures/ selects the tests measured to
     READ it (coverage tools attribute nothing to data files).
  2. a changed test file selects its own tests.
  3. a changed line inside a function body selects the tests whose coverage
     hits that line.
  4. a changed line OUTSIDE any function body gets a backstop, because
     coverage is unreliable there: module-level code runs once at import, so
     coverage credits it to whichever test imported the module first. For a
     dialect module the backstop is its dialect family (sqlglot implements each
     dialect across dialects/, parsers/, generators/, typing/ and tokenizers/,
     and the parser and generator halves ARE function-body-rich, so they stand
     in faithfully). Otherwise it is per-test-FILE coverage.
  5. anything unrecognised -- a path absent from the maps, a non-Python file we
     have no model for -- selects everything.

Only the Python standard library is used, and the maps are mmap'd and decoded
per query, so cold start stays near the interpreter floor. Our CPU is scored.
"""

from __future__ import annotations

import json
import os
import re
import sys

# The scorer always mounts /data. BR_DATA exists only so the selector can be
# scored against the sample set outside the container, which is how we know our
# score without spending one of our 1-per-hour submissions.
DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")

# From SCORING.md. Not tunable: these are the scorer's published constants.
WORKER_MS = 2_000
MISS_MS = 300_000
MAX_WORKERS = 8

# sqlglot splits every dialect across these sibling directories.
DIALECT_DIRS = ("dialects", "parsers", "generators", "typing", "tokenizers")

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --------------------------------------------------------------------------
# Patch parsing
# --------------------------------------------------------------------------
def parse_patch(text: str) -> dict[str, set[int]]:
    """Return {old_path: affected line numbers in the pinned revision}.

    The whole old-side hunk range is taken, context lines included. That is
    deliberately generous: context brackets the edit, and over-selecting cheap
    tests is far cheaper than one 300 s miss. Line numbers are on the OLD side
    because every map is keyed to the pinned commit.
    """
    touched: dict[str, set[int]] = {}
    path = None
    for line in text.splitlines():
        if line.startswith("--- a/"):
            path = line[6:].strip()
        elif line.startswith("--- "):
            # An ADDED file has no a-side: git writes `--- /dev/null`. Keep the
            # sentinel intact so the b-side branch below picks the real name up.
            # (A naive .lstrip("ab/") mangles "/dev/null" into "dev/null",
            # which then looks like an ordinary unknown path and widens the
            # selection to the whole suite -- safe, but 36.8 s per added file.)
            stripped = line[4:].strip()
            path = stripped if stripped == "/dev/null" else stripped.removeprefix("a/")
        elif line.startswith("+++ b/"):
            # Prefer the b-side name only when there is no a-side: for a rename
            # the a-side is the name coverage knows.
            if path in (None, "/dev/null"):
                path = line[6:].strip()
        elif line.startswith("@@") and path:
            m = HUNK.match(line)
            if m:
                start = int(m.group(1))
                length = int(m.group(2) or 1)
                slot = touched.setdefault(path, set())
                if length == 0:
                    # Pure insertion: bracket the insertion point.
                    slot.update((start, start + 1))
                else:
                    slot.update(range(start, start + length))
    return touched


# --------------------------------------------------------------------------
# THE SELECTION STEP
# --------------------------------------------------------------------------
def _translation(tests: list[dict]):
    """Map the test numbers baked into the maps onto the ones we are given.

    THE RISK THIS CLOSES. Every map ships bitsets keyed by test NUMBER, baked in
    when the map was built. But numbers are not identities: in tests.json they
    are 0..n-1 assigned in sorted-by-test-id order. So a single test added,
    removed or renamed anywhere in the suite shifts roughly half of all numbers,
    and every answer our maps give would then point at the wrong tests --
    silently, with no crash and no warning, while the misses mount at 300 s
    each. The final supplies its own /data/tests.json, so this is exactly the
    kind of change we cannot rule out.

    maps/testids.txt records the (number, id) pairs the maps were built from.
    Test IDs are stable identities, so we rebuild the translation from them:

      returns (translate, extra)
        translate  None if the numbering is unchanged (the common case, and
                   then nothing is paid for this), else {built number: current
                   number} with ids that no longer exist dropped
        extra      current numbers whose id the maps have never seen. No
                   coverage-derived rule can ever select those, so they ride
                   along on every commit -- the same treatment, and for the
                   same reason, as a test with no coverage row.
    """
    path = os.path.join(MAPS, "testids.txt")
    built: dict[str, int] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            num, _, tid = line.rstrip("\n").partition("\t")
            built[tid] = int(num)

    current = {t["id"]: t["number"] for t in tests}
    if all(current.get(tid) == num for tid, num in built.items()) and len(
        current
    ) == len(built):
        return None, set()

    translate = {num: current[tid] for tid, num in built.items() if tid in current}
    extra = {num for tid, num in current.items() if tid not in built}
    print(
        f"tests.json numbering differs from the maps: {len(translate)} of "
        f"{len(built)} mapped tests still present, {len(extra)} new test(s) "
        "will run on every commit",
        file=sys.stderr,
    )
    return translate, extra


class Selector:
    """The evidence ladder, over the committed maps.

    `cov` and `aux` may be None (a map failed to open). Every rung then falls
    back to selecting everything, which is the v1 answer: expensive, never
    wrong. Degrading loudly on stderr beats a silent 300 s.
    """

    def __init__(self, cov, aux, tests: list[dict], translate=None, extra=()):
        self.cov = cov
        self.aux = aux
        self.translate = translate
        self.all_tests = {t["number"] for t in tests}
        self.tests_by_file: dict[str, set[int]] = {}
        for t in tests:
            self.tests_by_file.setdefault(t["file"], set()).add(t["number"])
        # Rung 0. Tests no coverage row mentions, so no rule can ever pick them,
        # plus any test the maps have never heard of at all.
        self.ride_along: set[int] = set(extra)
        if aux is not None:
            self.ride_along |= self._xlate(aux.orphan_tests()) & self.all_tests

    def _xlate(self, nums: set[int]) -> set[int]:
        """Map numbers as the maps know them onto numbers as we were given them."""
        if self.translate is None:
            return nums
        t = self.translate
        return {t[n] for n in nums if n in t}

    def _dialect_family(self, path: str) -> set[int]:
        """Tests covering any file implementing the same dialect as `path`."""
        base = path.rsplit("/", 1)[-1]
        if not base.endswith(".py"):
            return set()
        stem = base[:-3]
        if stem == "__init__" or self.cov is None:
            return set()
        out: set[int] = set()
        for d in DIALECT_DIRS:
            out |= self.cov.tests_for_file("sqlglot/%s/%s.py" % (d, stem))
        return self._xlate(out)

    def _filemap_tests(self, path: str) -> set[int]:
        if self.aux is None:
            return set(self.all_tests)
        hit = self.aux.filemap_tests(path)
        if not hit:
            return set(self.all_tests)
        return self._xlate(hit)

    def select(self, changed: dict[str, set[int]]) -> set[int]:
        if self.cov is None or self.aux is None:
            return set(self.all_tests)

        sel = set(self.ride_along)
        for path, lines in changed.items():
            # 1. data fixtures are read, not imported
            if path.startswith("tests/fixtures/"):
                hit = self.aux.tests_for_fixture(path)
                if hit:
                    sel |= self._xlate(hit)
                else:
                    # Unknown fixture: take every test reading anything in its
                    # directory. A new fixture file is exactly the case where we
                    # have no measurement and must not guess narrow.
                    parent = path.rsplit("/", 1)[0] + "/"
                    siblings: set[int] = set()
                    for p in self.aux.fixture_paths_with_prefix(parent):
                        siblings |= self.aux.tests_for_fixture(p)
                    if siblings:
                        sel |= self._xlate(siblings)
                    else:
                        # A fixture in a directory we have never measured at
                        # all. There is no evidence to narrow with, and
                        # selecting nothing here is a 300 s bet on a file we
                        # know nothing about -- so widen.
                        return set(self.all_tests)
                continue

            # 2. a changed module under tests/ implicates its own tests AND
            #    every test whose module imports it.
            #
            #    Its own tests are not enough, and assuming they were cost us a
            #    300 s miss on the hidden set. Two shapes break it:
            #      * a module under tests/ that defines no tests of its own --
            #        tests/helpers.py (imported by 1377 tests), tests/__init__.py
            #        (1620), tests/dialects/__init__.py (1020). Its own-tests set
            #        is EMPTY, so this rung used to select nothing at all.
            #      * a real test file that other test files import from --
            #        33 files do `from tests.dialects.test_dialect import
            #        Validator`, so changing it reaches 1020 tests, not its own
            #        113.
            #    Per-test-FILE coverage answers exactly this question, because it
            #    records which fresh-process test runs executed the file. For an
            #    ordinary test file the closure equals its own tests, so this
            #    costs nothing in the common case (test_optimizer.py: 167 and
            #    167). A module absent from that map widens, as ever.
            if path.startswith("tests/") and path.endswith(".py"):
                sel |= self.tests_by_file.get(path, set())
                sel |= self._filemap_tests(path)
                continue

            # 5a. a non-Python file we have no model for
            if not path.endswith(".py"):
                return set(self.all_tests)

            # 5b. a Python file absent from the coverage map
            if not self.cov.has_file(path):
                return set(self.all_tests)

            # 3. function-body changes: precise line coverage
            sel |= self._xlate(self.cov.tests_for_lines(path, lines))

            # 4. module-level changes need a backstop
            if self.aux.module_level_lines(path, lines):
                fam = self._dialect_family(path)
                sel |= fam if fam else self._filemap_tests(path)

        return sel & self.all_tests


# --------------------------------------------------------------------------
# Cost-optimal worker packing. Final; no need to change this.
# --------------------------------------------------------------------------
def _lpt_loads(durations: list[int], worker_count: int) -> list[int]:
    """Longest-processing-time greedy. Returns each worker's load in ms.

    LPT is within 4/3 of optimal makespan and, on this suite's duration
    distribution (one dominant test, a long tail of 3 ms tests), it is
    effectively exact: the makespan is pinned by the longest test.
    """
    loads = [0] * worker_count
    for d in durations:
        i = loads.index(min(loads))
        loads[i] += d
    return loads


def best_split(selected: list[int], dur: dict[int, int]) -> list[list[int]]:
    """Pack `selected` onto the worker count that minimises the scorer's cost.

    Tries every worker count 1..8 and keeps the cheapest, because more workers
    is not free: each costs 2 s while the busiest worker can never drop below
    the single longest selected test.
    """
    if not selected:
        return []

    ordered = sorted(selected, key=lambda n: -dur[n])
    durations = [dur[n] for n in ordered]

    best_cost = None
    best_w = 1
    for w in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        cost = max(_lpt_loads(durations, w)) + WORKER_MS * w
        if best_cost is None or cost < best_cost:
            best_cost, best_w = cost, w

    workers: list[list[int]] = [[] for _ in range(best_w)]
    loads = [0] * best_w
    for n, d in zip(ordered, durations):
        i = loads.index(min(loads))
        workers[i].append(n)
        loads[i] += d
    # Every worker is non-empty here (best_w <= len(ordered)), so the scorer's
    # "at most 8 non-empty workers" rule holds by construction.
    return workers


# --------------------------------------------------------------------------
def _open_maps():
    """Open both maps, or return (None, None) and run everything.

    A map that will not open is a 1000 s mistake, not a crash: say so on stderr
    and fall back to the answer that cannot miss.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import auxmap
        import brmap
    except Exception as exc:  # noqa: BLE001
        print(f"map modules unavailable ({exc!r}); running everything", file=sys.stderr)
        return None, None
    try:
        cov = brmap.DepMap(os.path.join(MAPS, "cov.brdm"))
    except Exception as exc:  # noqa: BLE001
        print(f"cov.brdm unavailable ({exc!r}); running everything", file=sys.stderr)
        return None, None
    try:
        aux = auxmap.AuxMaps(os.path.join(MAPS, "aux.brs"))
    except Exception as exc:  # noqa: BLE001
        print(f"aux.brs unavailable ({exc!r}); running everything", file=sys.stderr)
        cov.close()
        return None, None
    return cov, aux


def main() -> None:
    with open(os.path.join(DATA, "tests.json"), "rb") as fh:
        # Only the `tests` key is read. `skipped` names tests our environment
        # skips: they cannot fail and have no number, so they are never emitted.
        tests = json.loads(fh.read())["tests"]

    # A test's duration is round(seconds * 1000) ms, per SCORING.md. All cost
    # arithmetic is in whole milliseconds so it matches the scorer exactly.
    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    valid = set(dur)

    cov, aux = _open_maps()
    selector = Selector(cov, aux, tests)

    commits_dir = os.path.join(DATA, "commits")
    commit_ids = sorted(
        name[:-6] for name in os.listdir(commits_dir) if name.endswith(".patch")
    )

    # Identical selections pack identically, so pay for best_split once per
    # distinct selection rather than once per commit.
    pack_cache: dict[tuple[int, ...], list[list[int]]] = {}

    out = sys.stdout
    emitted = 0
    widened = 0
    for cid in commit_ids:
        patch_path = os.path.join(commits_dir, cid + ".patch")
        try:
            with open(patch_path, encoding="utf-8", errors="replace") as fh:
                changed = parse_patch(fh.read())
            if not changed:
                # A patch we could not parse at all. Never drop a commit.
                raise ValueError("no hunks parsed")
            selected = selector.select(changed)
        except Exception as exc:  # never drop a commit: a missing line rejects the run
            print(f"select failed on {cid}: {exc!r}; running everything", file=sys.stderr)
            selected = set(valid)
        if len(selected) == len(valid):
            widened += 1

        # Defensive normalisation. Guarantees the scorer's rules 5/6/7:
        # whole numbers only, every number known, no number twice.
        selected_list = sorted({n for n in map(int, selected) if n in valid})

        key = tuple(selected_list)
        workers = pack_cache.get(key)
        if workers is None:
            workers = best_split(selected_list, dur)
            pack_cache[key] = workers

        # Exactly the two keys `commit` and `workers`, one line per commit.
        out.write(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        out.write("\n")
        emitted += 1

    out.flush()
    if cov is not None:
        cov.close()
    if aux is not None:
        aux.close()
    print(
        f"v2: {emitted} commits, {len(pack_cache)} distinct packing(s), "
        f"{widened} widened to the full suite",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

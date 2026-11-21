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

#: A test at or above this duration is not run on backstop-only evidence. Its
#: break-even failure probability (duration / 300_000 ms) is then >= 1.7%, and
#: the module-level backstop -- which answers "which tests reference this name"
#: rather than "which tests this change can alter" -- is not that sharp.
EXPENSIVE_MS = 5_000

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

    def __init__(self, cov, aux, tests: list[dict], translate=None, extra=(), symref=None):
        self.cov = cov
        self.aux = aux
        self.symref = symref
        # Whether coverage is consulted about module-level changed lines. See
        # rung 3. BR_MODLEVEL_COV=0 selects the tighter, less defensive answer.
        self.modlevel_cov = os.environ.get("BR_MODLEVEL_COV", "1") != "0"
        # {dialect: itself and every dialect inheriting from it}. Missing file
        # degrades to "just this dialect", which is the old, thinner behaviour.
        # Tests expensive enough that backstop-only evidence does not pay for
        # them. Derived from tests.json's own durations and the scorer's 300 s
        # miss penalty -- no label, no fitted constant. At 5 s the break-even
        # failure probability is 5000/300000 = 1.7%.
        self.expensive = {n for t in tests if (n := t["number"]) is not None
                          and round(t["seconds"] * 1000) >= EXPENSIVE_MS}
        self.dialect_tree: dict[str, list[str]] = {}
        try:
            with open(os.path.join(MAPS, "dialect_tree.json"), "rb") as fh:
                self.dialect_tree = json.loads(fh.read())
        except Exception as exc:  # noqa: BLE001
            print(f"dialect_tree.json unavailable ({exc!r})", file=sys.stderr)
        self.translate = translate
        self.all_tests = {t["number"] for t in tests}
        self.num_of = {t["id"]: t["number"] for t in tests}
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
        """Tests of the dialect `path` implements, and of every dialect that
        inherits from it.

        sqlglot implements each dialect across dialects/, parsers/, generators/,
        typing/ and tokenizers/, so coverage of those five files is the first
        source. But coverage alone is demonstrably not enough here, and the gap
        is a 300 s miss waiting to happen:

          * A dialect module is a pure declarative class body, so its lines run
            once at import and coverage credits them to almost nothing --
            sqlglot/dialects/solr.py has exactly ONE covering test, and 36 of
            184 source files have three or fewer.
          * Dialects inherit. spark subclasses spark2 subclasses hive, so a
            spark test may execute only hive's lines and never touch spark.py
            at all. Measured: a module-level edit to spark.py selected 8 of the
            31 tests in tests/dialects/test_spark.py, and for solr it selected
            0 of 1.

        Both are closed structurally rather than by coverage. sqlglot's suite is
        organised one file per dialect, so the tests of dialect X are
        tests/dialects/test_X.py; and a change to X's class body changes every
        dialect that inherits those attribute tables, which maps/dialect_tree.json
        records (read from sqlglot at the pinned commit: hive -> databricks,
        hive, spark, spark2). Union both in.
        """
        base = path.rsplit("/", 1)[-1]
        if not base.endswith(".py"):
            return set()
        stem = base[:-3]
        if stem == "__init__" or self.cov is None:
            return set()
        out: set[int] = set()
        for d in DIALECT_DIRS:
            out |= self.cov.tests_for_file("sqlglot/%s/%s.py" % (d, stem))
        out = self._xlate(out)
        # The dialect's own tests, plus those of every dialect inheriting from it.
        for name in self.dialect_tree.get(stem, (stem,)):
            out |= self.tests_by_file.get("tests/dialects/test_%s.py" % name, set())
        return out

    def _filemap_tests(self, path: str) -> tuple[set[int], bool]:
        """(tests, is_widen). is_widen means we had NO per-file evidence.

        A path absent from the filemap -- sqlglot/__main__.py is the one such
        file, never imported under coverage -- yields the whole suite. That is
        a widen, not an answer, so the caller must not then trim expensive
        tests out of it: there is no evidence to trim against.
        """
        if self.aux is None:
            return set(self.all_tests), True
        hit = self.aux.filemap_tests(path)
        if not hit:
            return set(self.all_tests), True
        return self._xlate(hit), False

    def _is_dialect(self, path: str) -> bool:
        """Is `path` one of the five files implementing a dialect?"""
        parts = path.split("/")
        return len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_DIRS

    def _symref_tests(self, path: str, modlines: set[int]):
        """Tests that execute a line REFERENCING what the changed line binds.

        The sharp backstop for a module-level change. A module-level edit
        changes the value or shape of a named thing -- a class, a constant, a
        def, an import binding -- and code can only observe that change by
        reading the name. So the tests that can fail are a subset of the tests
        executing a line that references the name, and that is computable
        offline from the pinned tree.

        Why it is needed: per-test-FILE coverage, the previous backstop, says
        every one of the 58 test files touches sqlglot/expressions/ddl.py,
        because they all import it. It therefore returns ~1617 of 1620 tests --
        run-all -- on commits whose truth is frequently zero failing tests.
        Symref answers ktz3's ddl.py edit with 28 tests instead of 1617.

        Returns None whenever the index cannot answer soundly, and the caller
        then widens exactly as before. That happens when a changed line binds
        no name at all (a bare call, a docstring), when the name closure fans
        out pathologically, or when a reference sits in a shared tests/ module
        that declares no tests of its own -- tests/helpers.py has 1377
        importers and no tests, so resolving it to "tests in that file" would
        silently discard the evidence.
        """
        if self.symref is None or not self.symref.has_file(path):
            return None
        try:
            res = self.symref.resolve(path, sorted(modlines))
            if res.unresolved or res.empty:
                return None
            return self.symref.tests_for_locations(
                res,
                # Coverage answers in the numbers the map was BUILT with, so
                # translate; the other two sources are already current numbers.
                line_tests=lambda f, ls: self._xlate(self.cov.tests_for_lines(f, ls)),
                tests_in_file=lambda f: set(self.tests_by_file.get(f, ())),
                test_number=self.num_of.get,
            )
        except Exception as exc:  # noqa: BLE001 -- a backstop must never raise
            print(f"symref failed on {path}: {exc!r}; widening", file=sys.stderr)
            return None

    def select(self, changed: dict[str, set[int]]) -> set[int]:
        if self.cov is None or self.aux is None:
            return set(self.all_tests)

        # An EXPENSIVE test justified ONLY by the module-level backstop is not
        # worth running. Measured over 232 generated cases: of the times
        # test_execute_tpcds (21.45 s) was selected without failing, 39 came
        # from the backstop and only 4 from line coverage. The backstop is a
        # deliberate over-approximation -- it answers "which tests reference
        # this name", not "which tests this change can alter" -- so for a test
        # whose own break-even probability is high, that evidence is too thin
        # to pay for.
        #
        # The rule follows the scorer's constants, not the labels: include t iff
        # P(t fails) * 300_000 ms > marginal cost of t. At 5 s the break-even
        # probability is 1.7%; the measured rate for backstop-only selections of
        # such tests is 0 of 50. Cheap tests keep riding along as before, which
        # is where recall actually comes from.
        #
        # `strong` accumulates the same selection from evidence we trust:
        # fixture reads, a changed test module, and line coverage inside a
        # function body. Anything only in `sel` and not in `strong` reached us
        # via the backstop alone.
        strong: set[int] = set(self.ride_along)
        widened = False

        sel = set(self.ride_along)
        for path, lines in changed.items():
            # 1. data fixtures are read, not imported
            if path.startswith("tests/fixtures/"):
                hit = self.aux.tests_for_fixture(path)
                if hit:
                    sel |= self._xlate(hit)
                    strong |= self._xlate(hit)
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
                        strong |= self._xlate(siblings)
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
                fm, fm_widened = self._filemap_tests(path)
                own = self.tests_by_file.get(path, set()) | fm
                sel |= own
                strong |= own
                widened = widened or fm_widened
                continue

            # 5a. a non-Python file we have no model for
            if not path.endswith(".py"):
                return set(self.all_tests)

            # 5b. a Python file absent from the coverage map
            if not self.cov.has_file(path):
                return set(self.all_tests)

            # 3. function-body changes: precise line coverage
            modlines = self.aux.module_level_lines(path, lines)
            if self.modlevel_cov:
                cov_lines = lines
            else:
                # Ask coverage only about lines it can answer for. A
                # module-level line executes once, at import during collection,
                # so coverage credits it to no test or to one arbitrary test --
                # the answer is not wrong so much as meaningless, and symref
                # covers those lines properly on rung 4. Dropping them here is
                # worth ~31 s projected. It is OFF by default because
                # docs/SOUNDNESS.md records one sample commit (rszg) where that
                # arbitrary attribution happened to be load-bearing.
                cov_lines = lines - modlines
            strong_here = False
            if cov_lines:
                hit = self._xlate(self.cov.tests_for_lines(path, cov_lines))
                sel |= hit
                strong |= hit
                strong_here = bool(hit)

            # 4. module-level changes need a backstop
            if modlines:
                # _dialect_family unions in the dialect's own test file, so it
                # is non-empty for ANY sqlglot/<dir>/<stem>.py whose stem
                # happens to name a dialect test file. Require that it actually
                # be a dialect module, or __main__.py-shaped files take this
                # branch and never reach the widen below.
                fam = self._dialect_family(path) if self._is_dialect(path) else set()
                if fam:
                    sel |= fam
                else:
                    got = self._symref_tests(path, modlines)
                    if got is None:
                        fm, fm_widened = self._filemap_tests(path)
                        sel |= fm
                        widened = widened or fm_widened
                    else:
                        sel |= got
                        if not got and not strong_here:
                            # symref answered, but with nothing, and no rung
                            # claimed a test either. No evidence at all.
                            widened = True
                            sel |= set(self.all_tests)
            elif self._filemap_tests(path)[1]:
                # No module-level lines, no line coverage, AND the file has no
                # per-test-file coverage row either: no test has ever been
                # observed executing this file. sqlglot/__main__.py is the
                # standing example -- present in the coverage map but imported
                # by nothing. Selecting a handful of ride-along tests there is
                # a 300 s bet on code we have zero evidence about, so widen.
                # A comment-only edit does NOT come here: such a file still has
                # a filemap row, and a comment line is not code in either map.
                widened = True
                sel |= set(self.all_tests)

        # Trim expensive tests that only the backstop justified. Skipped
        # entirely if any path widened, because then we have no evidence to
        # reason from and the whole selection is already the safe answer.
        if not widened:
            sel -= (sel - strong) & self.expensive
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
        return None, None, None
    try:
        cov = brmap.DepMap(os.path.join(MAPS, "cov.brdm"))
    except Exception as exc:  # noqa: BLE001
        print(f"cov.brdm unavailable ({exc!r}); running everything", file=sys.stderr)
        return None, None, None
    try:
        aux = auxmap.AuxMaps(os.path.join(MAPS, "aux.brs"))
    except Exception as exc:  # noqa: BLE001
        print(f"aux.brs unavailable ({exc!r}); running everything", file=sys.stderr)
        cov.close()
        return None, None, None
    # The symbol-reference index is the one map we can run WITHOUT: losing it
    # only costs seconds, because the module-level rung falls back to
    # per-test-file coverage, which is what we shipped before it existed.
    try:
        import symref as symref_mod

        sym = symref_mod.SymRef(os.path.join(MAPS, "symref.json.gz"))
    except Exception as exc:  # noqa: BLE001
        print(f"symref unavailable ({exc!r}); using the wider backstop", file=sys.stderr)
        sym = None
    return cov, aux, sym


def main() -> None:
    with open(os.path.join(DATA, "tests.json"), "rb") as fh:
        # Only the `tests` key is read. `skipped` names tests our environment
        # skips: they cannot fail and have no number, so they are never emitted.
        tests = json.loads(fh.read())["tests"]

    # A test's duration is round(seconds * 1000) ms, per SCORING.md. All cost
    # arithmetic is in whole milliseconds so it matches the scorer exactly.
    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    valid = set(dur)

    cov, aux, sym = _open_maps()
    translate, extra = (None, set())
    try:
        translate, extra = _translation(tests)
    except Exception as exc:  # noqa: BLE001
        print(f"testids.txt unreadable ({exc!r}); assuming unchanged numbering", file=sys.stderr)
    selector = Selector(cov, aux, tests, translate, extra, sym)

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

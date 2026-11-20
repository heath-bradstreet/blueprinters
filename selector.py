#!/usr/bin/env python3
"""Blast Radius entry -- v1: safe baseline with cost-optimal worker packing.

WHAT THIS VERSION DOES

It selects *every* test on every commit, so it can never miss a failing test
(a miss costs 300 s, which is more than twice the cost of a perfect answer on
the whole sample set). The only thing it is clever about is **how many workers
to use**, which it chooses per commit by minimising the scorer's own cost
function instead of hard-coding 3 the way the starter kit does.

Why that alone is a win: the scorer charges

    commit cost = (busiest worker's load) + 2 s * (number of non-empty workers)

The suite holds one 28.8 s test out of 103 s total, so once that test is in the
selection the busiest worker can never drop below 28.8 s and extra workers only
add their 2 s each. Run-all is therefore cheapest at 4 workers, not 3 and
certainly not 8. Measured on the 30 sample commits: 1104 s here versus 1210 s
for the starter's fixed 3 workers, with 0 misses either way.

HOW TO EVOLVE IT

`select_tests()` below is the ONLY function that decides *which* tests run, and
it is the single seam to replace with the real selector (diff parsing plus a
coverage/import/fixture map built offline from the pinned commit). Everything
else -- input parsing, cost-optimal packing, output formatting -- is already
final and does not need to change. Keep `select_tests` returning an iterable of
test numbers and the rest of this file keeps working.

Only the Python standard library is used, so startup CPU stays near the
interpreter floor; our CPU seconds are added to the score.
"""

from __future__ import annotations

import json
import os
import sys

DATA = "/data"

# From SCORING.md. Not tunable: these are the scorer's published constants.
WORKER_MS = 2_000
MISS_MS = 300_000
MAX_WORKERS = 8


# --------------------------------------------------------------------------
# THE SELECTION STEP -- this is the swappable part.
# --------------------------------------------------------------------------
def select_tests(commit_id: str, patch_path: str, tests: list[dict]) -> list[int]:
    """Return the test numbers to run for one commit.

    *** THIS IS THE ONE FUNCTION TO REPLACE WITH THE REAL SELECTOR. ***

    v1 deliberately returns everything: it is the zero-risk answer whose job is
    to prove the pipeline end to end. A real implementation reads
    `patch_path` (a unified diff against the pinned sqlglot commit), maps the
    changed files and line ranges onto the tests that touch them, and returns a
    generous superset -- cheap tests included liberally, expensive ones gated by
    real evidence, per the expected-cost rule
    (include t iff P(t fails) * 300_000 ms > marginal cost of t).

    Tests named in tests.json's `skipped` key are never returned, because this
    only ever returns numbers from the `tests` list and skipped entries have no
    number.
    """
    return [t["number"] for t in tests]


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

    # Sorted longest-first once, and reused for every candidate worker count.
    ordered = sorted(selected, key=lambda n: -dur[n])
    durations = [dur[n] for n in ordered]

    best_cost = None
    best_w = 1
    for w in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        cost = max(_lpt_loads(durations, w)) + WORKER_MS * w
        if best_cost is None or cost < best_cost:
            best_cost, best_w = cost, w

    # Rebuild the real assignment for the winning worker count.
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
def main() -> None:
    with open(os.path.join(DATA, "tests.json"), "rb") as fh:
        # Only the `tests` key is read. `skipped` names tests our environment
        # skips: they cannot fail and have no number, so they are never emitted.
        tests = json.loads(fh.read())["tests"]

    # A test's duration is round(seconds * 1000) ms, per SCORING.md. All cost
    # arithmetic is in whole milliseconds so it matches the scorer exactly.
    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    valid = set(dur)

    commits_dir = os.path.join(DATA, "commits")
    commit_ids = sorted(
        name[:-6] for name in os.listdir(commits_dir) if name.endswith(".patch")
    )

    # Identical selections pack identically, so pay for best_split once per
    # distinct selection rather than once per commit. With v1 that is a single
    # call for the whole batch; with a real selector it still collapses the
    # commits that happen to agree.
    pack_cache: dict[tuple[int, ...], list[list[int]]] = {}

    out = sys.stdout
    emitted = 0
    for cid in commit_ids:
        patch_path = os.path.join(commits_dir, cid + ".patch")
        try:
            selected = select_tests(cid, patch_path, tests)
        except Exception as exc:  # never drop a commit: a missing line rejects the run
            print(f"select failed on {cid}: {exc!r}; running everything", file=sys.stderr)
            selected = [t["number"] for t in tests]

        # Defensive normalisation. Guarantees the scorer's rules 5/6/7:
        # whole numbers only, every number known, no number twice.
        selected = sorted({n for n in map(int, selected) if n in valid})

        key = tuple(selected)
        workers = pack_cache.get(key)
        if workers is None:
            workers = best_split(selected, dur)
            pack_cache[key] = workers

        # Exactly the two keys `commit` and `workers`, one line per commit.
        out.write(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        out.write("\n")
        emitted += 1

    out.flush()
    print(
        f"v1 run-all with optimal packing: {emitted} commits, "
        f"{len(dur)} tests each, {len(pack_cache)} distinct packing(s)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

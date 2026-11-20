# Blast Radius — CI test selection entry

Scorer version: `blast-radius-1`

For each patch against [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, this program prints one JSON line
saying which of the 1,620 tests to run and how to split them across up to 8
workers.

## What this version does

It runs **every test on every commit**, so it can never miss a failing test.
The one thing it optimises is the **number of workers**, chosen per commit by
minimising the scorer's own cost function rather than hard-coding 3 as the
starter kit does.

The scorer charges a commit:

    cost = (busiest worker's test seconds) + 2 s × (number of non-empty workers)

The suite is 103.0 s across 1,620 tests, but a single test takes 28.8 s. Once
that test is in the selection the busiest worker can never go below 28.8 s, so
additional workers only add their 2 s each. Run-all is therefore cheapest at
**4** workers — not 3, and definitely not 8. The selector tries worker counts
1…8, costs each with a longest-processing-time packing, and keeps the cheapest.

Measured on the 30 labelled sample commits with the kit's own `score.py`:

| | Simulated | Misses |
| --- | --- | --- |
| Starter kit (all tests, fixed 3 workers) | 1210.470 s | 0 |
| **This entry (all tests, cost-optimal worker count)** | **1104.292 s** | **0** |

This is deliberately an unambitious, zero-risk baseline — its job is to prove
the pipeline end to end. `select_tests()` in `selector.py` is the single
clearly-marked seam that decides *which* tests run; it is the only function to
replace with the real selector. Input parsing, packing and output formatting are
already final.

## Files

| | |
| --- | --- |
| `Dockerfile` | Image definition. Base pinned by digest; runs unprivileged. |
| `selector.py` | The whole program. `select_tests()` is the swappable selection step. |
| `README.md` | This file. |
| `LICENSE` | MIT. |

## Open-source libraries used

**None.** `selector.py` imports only the Python standard library — `json`, `os`
and `sys` — and the image installs no packages at all. This is a deliberate
choice: our program's CPU seconds are added to the score, so every avoidable
import is a cost. There is no `requirements.txt` and nothing is downloaded at
build time beyond the base image itself.

The base image is Docker Hub's official
[`python:3.13-slim`](https://hub.docker.com/_/python), pinned by digest
`sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`. That
is exactly the digest the starter kit pins, kept because the organisers labelled
every commit in that image, so its Python and SQLite are what decided which
sqlglot tests ran.

## Pre-existing code and attribution

- **The starter kit** (`blast-radius-sample.tar.gz`, supplied by the
  organisers). The `Dockerfile`'s base-image digest and `CMD` shape come from
  the kit's `Dockerfile`. The longest-processing-time greedy in
  `best_split()` is the same idea as the kit's `selector.py::split()`, extended
  to search worker counts 1…8 instead of using a fixed 3. The millisecond cost
  arithmetic mirrors the rules published in the kit's `SCORING.md`.
- **Our own scratch tooling**, written by this team and *not* shipped here:
  a local simulator of the scorer and an expected-cost policy module. This
  submission is self-contained and has no dependency on them — the packing
  logic is reimplemented inline in `selector.py`.
- No code is shared with any other team, and no part of this entry contains a
  precomputed patch-to-failing-tests table. The selection step is a pure
  function of the patch and of maps derivable from the pinned commit.

## Output contract

One line per commit on stdout, nothing else:

```json
{"commit":"k7q2","workers":[[12,845,901],[3,77]]}
```

Exactly the two keys `commit` and `workers`; at most 8 non-empty workers; no
test number twice within a commit; only numbers present in `/data/tests.json`.
Tests named in that file's `skipped` key are never emitted — they have no number
and the program only ever emits numbers from the `tests` list. All logging goes
to stderr.

## Running it locally

From the starter kit's root, with this directory's contents at the top:

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
make check    # output format only
```

On Apple Silicon, build and run with `--platform linux/amd64`, as the kit's
Makefile does.

## License

MIT — see `LICENSE`.

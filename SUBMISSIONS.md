# Mech Royale official submissions

Practice scorer: 48 matches vs 15 house bots, score = mean(16 - place). Scores look near-deterministic for the same code.
Best score is entered in the final automatically; we never pick anything else.

| Time (ET) | Receipt | Commit | Change | Local sim A/B | Official score |
|---|---|---|---|---|---|
| Fri 8:07 PM | BB-A57892F9 | 786b21d | Laser aimed as hitscan, time caps | laser hits 35% -> 87% | 2.63 |
| Fri 8:23 PM | BB-941E28F5 | 4a2e5f5 | Remember loot, farm crates, grab weapons/cubes mid-fight | 2.15 -> 2.83, 1.92 -> 2.23 | 3.63 |
| Fri 8:35 PM | BB-017B918E | a04d595 | Never carry a mortar (laser cook-off) | level (noise) | **3.71** (best) |
| Fri 8:54 PM | BB-3542E07D | 450a201 | Dash to cover when an enemy laser charges | 2.24 -> 2.42, 2.22 -> 2.16 | 3.71 |
| Fri 9:08 PM | BB-B6A2936D | 6ec0542 | Tighter gun trigger (1.4x tolerance) | +0.15, -0.08, +0.12 | 3.65 (reverted) |
| Fri 9:24 PM | BB-9BE3AB54 | f0ebb57 | Halve per-decision search budgets (path 6 ms, cover 5/8 ms, A* 1500) | 2.42 vs 2.39, p99 8.5 -> 5.4 ms | **3.81** (best) |
| Fri 9:37 PM | BB-FFAE974E | ce69674 | Smaller search budgets again (path 4 ms, cover 3/5 ms, peek 5 ms, A* 1000) + gc.freeze after hello | 2.38 vs 2.39, p99 5.4 -> 4.9 ms | **3.92** (best) |
| Fri 9:57 PM | BB-55FB5B96 | 0b7c10e | Same decisions, less compute (incremental LOS, pruned cover search, inlined A* heuristic) | 2.38 vs 2.36, p99 4.6 -> 3.6 ms | 3.90 (final stays ce69674) |
| Fri 10:09 PM | BB-E1545419 | baf3c05 | Reuse cover spot 2.5 s, replan paths every 1.0 s | 2.41 vs 2.38 (level) | 3.77 (reverted) |
| Fri 10:25 PM | BB-334F3BF6 | 43b8363 | Refresh cover every 1.0 s, replan paths every 0.35 s | 2.54 vs 2.41, 2.42 vs 2.32 | 3.42 (reverted) |
| Fri 10:39 PM | BB-412C3122 | 248f381 | Retreat earlier when outgunned (hp < 60% instead of 50%) | level (2.36) | 3.77 (reverted) |
| Fri 10:58 PM | BB-05EA9E01 | ce69674 | Repeat of best, unchanged (noise check) | - | **3.94** (final; repeat confirms scores reproduce within ~0.02) |
| Fri 11:10 PM | BB-57ED2D98 | 73a8010 | Retreat later when outgunned (hp < 40%) | - | 3.85 (reverted; scorer queue took 30 min) |
| Fri 11:46 PM | BB-B5FD5E16 | e5ab1a4 | Hold laser fights at 150 m instead of 120 m | - | 3.69 (reverted) |
| Fri 11:53 PM | BB-79E90D79 | e7f2d83 | Hold laser fights at 100 m instead of 120 m | - | 3.73 (reverted) |

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

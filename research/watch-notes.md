# Live feed notes (wss://watch.joinbuilders.org/live, format mechreplay/1)

Feed sends a header (bots, mechs, crates, npcs, zone plan), frames every 2 ticks
(m = [id,x,y,z,aim_yaw,aim_pitch,hp,energy,flags,cubes,max_hp,slot0,slot1]), events
(fire, hit, laser, zap, death with place, cube, crate, pickup, weapon, upgrade, short, ...)
and a `result` per match with per-mech kills, damage by weapon, cubes_max, upgrades,
loadout, first_weapon_t, death_t, killed_with, drops_taken.
Channel alternates: hourly "Top 16 Showdown" (4 FFA matches of top-16 submissions) and
"Real players" matches (humans + house bots: probe-astra-finalist, probe-red-best,
probe-red-honest, probe-astra-skirmish, expert).

## Top-16 showdown, 1 AM m3 + 2 AM m1-m4 (5 matches)
- Winners: gun+laser every time. Cubes_max of winners 12-39; they took supply drops (1-3).
- Upgrades of winners: mostly plating/armor/nanites/servos, some laser_damage/charge.
- Laser hits land at 100-200 m. Nearest enemy usually 100-250 m away; HP regens to full between fights.
- Lightning and mortar carriers finish mid/low. Bots with gun only die early.

## House bots (real-players match, FFA among themselves)
- Loadouts mostly gun+laser (some lightning/mortar early, they die early).
- Laser hit rate on mechs ~21-27% of shots; gun 18-50%.
- Laser hits at 45-130 m. Kill-cube snowball: winners reach 11-23 cubes.

## Official results that matter
- Fortress (hold low-exposure pockets) = 3.40: passive play loses. Practice score ~= our kills,
  since house bots only die to us (and the zone).

# Blueprinters: Mech Royale bot

Team Blueprinters' entry for the **Mech Royale** track of the Builders Buildathon (Nov 20-22, 2026).

This branch holds only the Mech Royale bot, with its `Dockerfile` at the top as the track requires.
The team's Blast Radius entry lives on `main`.

## What it does

A Python bot (standard library only) speaking `mechfight/1` JSON lines on stdin/stdout.
Placement is what scores, so it plays for survival first:

- **Map model** (`arena.py`): reads `/arena/v2/world.json` and `terrain.i16` at `hello`, rasterizes
  buildings into a 2 m occupancy grid (line of sight) and a 4 m nav grid (A* around buildings).
- **Zone**: moves into the next circle ahead of each shrink, with urgency based on travel time.
- **Combat** (`bot.py`): picks the enemy hurting it or the closest weak one, faces it (rear hits do
  1.5x), strafes in random bursts, keeps a range that suits its weapons, leads gun shots, charges
  lasers fully, fires lightning in its cone, lobs mortars over cover.
- **Survival**: breaks off and runs (staying in the zone) when hurt and outgunned, so regen kicks in.
- **Economy**: grabs a better second weapon (laser > lightning > mortar), cubes, and supply drops
  when nobody is in sight; farms NPC drones and crates when healthy; picks defensive upgrades.

## Run it

```sh
docker build -t mech .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech
```

(`check/` and `arena/` come from the organizers' `mechfight-kit.tar.gz`.)

## Credits and license

MIT licensed (see `LICENSE`). The stdin/stdout loop follows the pattern of the kit's
`python/mechfight.py` wrapper by Builders; everything else was written during the event.

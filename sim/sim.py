"""A rough local stand-in for the mechfight server, for testing our bot against a hunting squad.

Not the real game: physics, vision and weapons follow protocol.md closely enough to catch bugs
(getting stuck, leaving the zone, never hitting anything) and to compare bot versions.

    python3 sim/sim.py --kit /path/to/mechfight-kit --matches 8
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from arena import Arena  # noqa: E402
import bot as B  # noqa: E402

TAU = 2 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Mech:
    def __init__(self, i, pos, cfg):
        m = cfg["mech"]
        self.id = i
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd = {"move": [0, 0], "aim": [0, 0], "boost": False}
        self.hp = float(m["hp"])
        self.max_hp = float(m["hp"])
        self.energy = float(m["energy"])
        self.energy_t = 0.0
        self.grounded = True
        self.alive = True
        self.weapons = [{"kind": "gun", "cooldown": 0.0, "ammo": 12, "reload": 0.0, "stats": dict(cfg["weapons"]["gun"])}, None]
        self.last_combat = -99.0
        self.events = []
        self.cubes = 0
        self.kills = 0
        self.death_t = None
        self.seen = {}  # id -> last seen time
        self.charge_start = None
        self.sounds = []


class Sim:
    def __init__(self, kit, seed, ours_factory, skill_mix=None):
        with gzip.open(os.path.join(kit, "check", "stream.jsonl.gz"), "rt") as f:
            self.hello = json.loads(f.readline())
        self.cfg = self.hello["config"]
        self.hello["map"]["dir"] = os.path.join(kit, "arena")
        self.A = ARENA_CACHE.get(kit) or Arena(self.hello["map"]["dir"])
        ARENA_CACHE[kit] = self.A
        self.r = random.Random(seed)
        self.t = 0.0
        self.tick = 0
        z = self.cfg["zone"]
        self.zc = list(z["start_center"])
        self.zr = z["start_radius"]
        self.phase = 0
        self.phase_t0 = 0.0
        self._next_zone()
        # spawns
        self.mechs = []
        for i in range(16):
            for _ in range(200):
                ang = self.r.uniform(0, TAU)
                rr = math.sqrt(self.r.random()) * self.zr * 0.8
                p = (self.zc[0] + rr * math.cos(ang), self.zc[1] + rr * math.sin(ang))
                c = self.A.ncell(*p)
                if c < 0 or self.A.nblocked[c]:
                    continue
                if all(d2(p, m.pos) > 110 for m in self.mechs):
                    break
            self.mechs.append(Mech(i, (p[0], p[1], self.A.ground(*p)), self.cfg))
        self.ours = self.r.randrange(16)
        h = dict(self.hello)
        h["slot"] = self.ours
        self.bot = ours_factory(h)
        skills = skill_mix or (["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3)
        self.skill = {}
        k = 0
        for m in self.mechs:
            if m.id != self.ours:
                self.skill[m.id] = skills[k]
                k += 1
        # loot
        self.pickups = {}
        pid = 8000
        for _ in range(40):
            for _ in range(50):
                p = (self.r.uniform(self.A.minx + 40, self.A.maxx - 40), self.r.uniform(self.A.miny + 40, self.A.maxy - 40))
                c = self.A.ncell(*p)
                if c >= 0 and not self.A.nblocked[c]:
                    break
            self.pickups[pid] = {"id": pid, "kind": self.r.choice(["mortar", "laser", "lightning"]), "pos": [p[0], p[1], self.A.ground(*p)]}
            pid += 1
        self.pid = pid
        # some house bots start with a laser like the recorded hunter
        for m in self.mechs:
            if m.id != self.ours and self.r.random() < 0.5:
                m.weapons[1] = self.weap(self.r.choice(["laser", "lightning", "laser"]))
        self.projectiles = []
        self.order = []  # death order
        self.hunters = set()
        self.dt = 1.0 / self.cfg["match"]["tick_hz"]
        self.times = []
        self.stats = {}  # (weapon, 'shots'|'hits'|'dmg') for our mech
        self.failures = 0

    def stat(self, w, k, n=1):
        self.stats[(w, k)] = self.stats.get((w, k), 0) + n

    def weap(self, kind):
        w = {"kind": kind, "cooldown": 0.0, "stats": dict(self.cfg["weapons"][kind])}
        if kind == "laser":
            w["charge"] = 0.0
        return w

    def _next_zone(self):
        ph = self.cfg["zone"]["phases"]
        if self.phase >= len(ph):
            self.nc, self.nr = list(self.zc), self.zr
            self.s0 = self.s1 = 1e9
            self.dps = ph[-1]["dps"]
            return
        p = ph[self.phase]
        nr = p["radius"]
        ang = self.r.uniform(0, TAU)
        off = self.r.uniform(0, max(0.0, self.zr - nr)) * 0.8
        self.nc = [self.zc[0] + off * math.cos(ang), self.zc[1] + off * math.sin(ang)]
        self.nr = nr
        self.s0 = self.phase_t0 + p["hold_s"]
        self.s1 = self.s0 + p["shrink_s"]
        self.dps = p["dps"]
        self.zc0, self.zr0 = list(self.zc), self.zr

    def zone_step(self):
        if self.t >= self.s1:
            self.zc, self.zr = list(self.nc), self.nr
            self.phase += 1
            self.phase_t0 = self.t
            self._next_zone()
        elif self.t >= self.s0:
            f = (self.t - self.s0) / (self.s1 - self.s0)
            self.zc = [self.zc0[0] + (self.nc[0] - self.zc0[0]) * f, self.zc0[1] + (self.nc[1] - self.zc0[1]) * f]
            self.zr = self.zr0 + (self.nr - self.zr0) * f

    # ---------------------------------------------------------------- vision
    def sees(self, a, b):
        dd = d2(a.pos, b.pos)
        eye = (a.pos[0], a.pos[1], a.pos[2] + 8)
        if dd > 200:
            return False
        if dd > 45:
            yaw = math.atan2(b.pos[1] - a.pos[1], b.pos[0] - a.pos[0])
            if abs(wrap(yaw - a.aim[0])) > math.radians(60):
                return False
        return self.A.los(eye, (b.pos[0], b.pos[1], b.pos[2] + 4.5)) or self.A.los(eye, (b.pos[0], b.pos[1], b.pos[2] + 9))

    # ---------------------------------------------------------------- observation for our bot
    def observe(self, me):
        t = self.t
        mechs = []
        for o in self.mechs:
            if o is me or not o.alive:
                continue
            if self.sees(me, o):
                me.seen[o.id] = t
            if t - me.seen.get(o.id, -99) <= 5.0:
                mechs.append({"id": o.id, "kind": "mech", "pos": [round(v, 2) for v in o.pos], "vel": [round(v, 2) for v in o.vel],
                              "aim": list(o.aim), "hp": o.hp, "max_hp": o.max_hp, "cubes": o.cubes,
                              "weapons": [w["kind"] for w in o.weapons if w], "charging": o.charge_start is not None})
        projs = [{"id": i, "kind": "gun", "pos": p["pos"], "vel": p["vel"]} for i, p in enumerate(self.projectiles) if p["by"] != me.id and d2(p["pos"], me.pos) < 150]
        pick = [p for p in self.pickups.values() if d2(p["pos"], me.pos) < 120]
        ev, me.events = me.events, []
        snd, me.sounds = me.sounds, []
        w = []
        for x in me.weapons:
            if x is None:
                w.append(None)
            else:
                y = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in x.items()}
                w.append(y)
        st = dict(self.cfg["mech"])
        st["eye_height"] = 8
        st["muzzle_height"] = 7
        in_zone = d2(me.pos, self.zc) <= self.zr
        return {"type": "obs", "tick": self.tick, "t": round(t, 2), "alive": sum(m.alive for m in self.mechs),
                "self": {"id": me.id, "pos": [round(v, 2) for v in me.pos], "vel": [round(v, 2) for v in me.vel], "aim": [round(v, 3) for v in me.aim],
                         "hp": me.hp, "max_hp": me.max_hp, "energy": round(me.energy, 1), "max_energy": 100, "grounded": me.grounded,
                         "cubes": me.cubes, "upgrades": [], "weapons": w, "in_zone": in_zone, "stats": st},
                "mechs": mechs, "projectiles": projs, "crates": [], "cubes": [], "pickups": pick, "sounds": snd, "beams": [],
                "events": ev, "drops": [],
                "zone": {"center": [round(v, 2) for v in self.zc], "radius": round(self.zr, 2), "next_center": [round(v, 2) for v in self.nc],
                         "next_radius": self.nr, "shrink_start": self.s0, "shrink_end": self.s1, "dps": self.dps},
                "offer": None}

    # ---------------------------------------------------------------- house bot brain
    def house(self, m):
        t = self.t
        us = self.mechs[self.ours]
        skill = self.skill[m.id]
        aim_err = {"easy": 0.05, "medium": 0.03, "hard": 0.015, "expert": 0.008}[skill]
        hunting = m.id in self.hunters and us.alive
        cmd = {"move": [0, 0], "aim": m.cmd.get("aim", [0, 0]), "boost": False, "fire": [False, False]}
        # squad vision: anyone sees us -> all know
        squad_sees = any(o.alive and o.id != self.ours and self.sees(o, us) for o in self.mechs) if us.alive else False
        if squad_sees:
            self.last_us = (list(us.pos), t)
        goal = None
        tgt = None
        if hunting:
            goal = us.pos[:2]
            if self.sees(m, us) or (squad_sees and d2(m.pos, us.pos) < 200):
                tgt = us
        # zone
        if d2(m.pos, self.nc) > self.nr * 0.8 and (goal is None or d2(goal, self.nc) > self.nr):
            goal = self.nc
        if goal is None:
            if not hasattr(m, "wander") or d2(m.pos, m.wander) < 10 or self.r.random() < 0.002:
                ang = self.r.uniform(0, TAU)
                rr = math.sqrt(self.r.random()) * self.nr * 0.7
                m.wander = [self.nc[0] + rr * math.cos(ang), self.nc[1] + rr * math.sin(ang)]
            goal = m.wander
        dist = d2(m.pos, us.pos)
        if tgt is not None:
            want = 140 if (m.weapons[1] and m.weapons[1]["kind"] == "laser") else (30 if (m.weapons[1] and m.weapons[1]["kind"] == "lightning") else 100)
            dx, dy = us.pos[0] - m.pos[0], us.pos[1] - m.pos[1]
            n = math.hypot(dx, dy) or 1
            side = 1 if (int(t / 1.5) + m.id) % 2 else -1
            rad = 0.8 if dist > want + 20 else (-0.6 if dist < want - 20 else 0.0)
            mv = [dx / n * rad - dy / n * side * 0.8, dy / n * rad + dx / n * side * 0.8]
            if rad > 0 and not self.A.walk_clear(m.pos, us.pos):
                mv = list(self.steer_house(m, us.pos))
            cmd["move"] = mv
            # aim with lead
            gs = 252.0
            tt = dist / gs
            ax, ay = us.pos[0] + us.vel[0] * tt, us.pos[1] + us.vel[1] * tt
            yaw = math.atan2(ay - m.pos[1], ax - m.pos[0]) + self.r.gauss(0, aim_err)
            pitch = math.atan2(us.pos[2] + 4.5 - (m.pos[2] + 7), dist)
            cmd["aim"] = [yaw, pitch]
            err = abs(wrap(m.aim[0] - yaw))
            cmd["fire"][0] = err < 0.05 and dist < 225
            w1 = m.weapons[1]
            if w1:
                if w1["kind"] == "laser":
                    # charge, release when full and on target
                    if m.charge_start is not None and t - m.charge_start >= 0.75 and err < 0.03:
                        cmd["fire"][1] = False
                    elif w1["cooldown"] <= 0 and dist < 270:
                        cmd["fire"][1] = True
                elif w1["kind"] == "lightning":
                    cmd["fire"][1] = dist < 44
        else:
            cmd["move"] = list(self.steer_house(m, goal))
            yaw = math.atan2(cmd["move"][1], cmd["move"][0]) if any(cmd["move"]) else m.aim[0]
            cmd["aim"] = [yaw + 0.6 * math.sin(t * 0.8 + m.id), 0.0]
        m.cmd = cmd

    def steer_house(self, m, goal):
        if self.A.walk_clear(m.pos, goal):
            dx, dy = goal[0] - m.pos[0], goal[1] - m.pos[1]
        else:
            if not hasattr(m, "path") or m.path is None or self.t - m.path_t > 3:
                m.path = self.A.astar(m.pos[:2], goal, max_exp=1500)
                m.path_t = self.t
            if m.path:
                while len(m.path) > 1 and d2(m.pos, m.path[0]) < 4:
                    m.path.pop(0)
                dx, dy = m.path[0][0] - m.pos[0], m.path[0][1] - m.pos[1]
            else:
                dx, dy = goal[0] - m.pos[0], goal[1] - m.pos[1]
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n > 1 else (0.0, 0.0)

    # ---------------------------------------------------------------- physics and weapons
    def damage(self, victim, amount, attacker, weapon, src=None):
        if not victim.alive or amount <= 0:
            return
        rear = False
        if src is not None:
            yaw_from = math.atan2(src[1] - victim.pos[1], src[0] - victim.pos[0])
            if abs(wrap(yaw_from - victim.aim[0])) > math.pi / 2:
                rear = True
                amount *= 1.5
        victim.hp -= amount
        victim.last_combat = self.t
        bearing = math.atan2(src[1] - victim.pos[1], src[0] - victim.pos[0]) if src is not None else None
        victim.events.append({"type": "damaged", "amount": round(amount, 2), "weapon": weapon, "bearing": bearing,
                              "by": attacker.id if attacker else None, **({"rear": True} if rear else {})})
        if attacker is not None and attacker.id == self.ours and weapon in ("gun", "laser", "lightning"):
            self.stat(weapon, "hits")
            self.stat(weapon, "dmg", amount)
        if attacker is not None:
            attacker.last_combat = self.t
            attacker.events.append({"type": "hit", "target": victim.id, "amount": round(amount, 2), "weapon": weapon})
        if victim.hp <= 0:
            victim.hp = 0
            victim.alive = False
            victim.death_t = self.t
            self.order.append((self.t, victim.id))
            if attacker is not None:
                attacker.kills += 1
                attacker.events.append({"type": "kill", "target": victim.id})
            if victim.weapons[1]:
                self.pickups[self.pid] = {"id": self.pid, "kind": victim.weapons[1]["kind"], "pos": list(victim.pos)}
                self.pid += 1

    def shoot(self, m, slot):
        w = m.weapons[slot]
        if w is None:
            return
        k = w["kind"]
        st = w["stats"]
        yaw, pitch = m.aim
        dirv = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        org = [m.pos[0] + dirv[0] * 3, m.pos[1] + dirv[1] * 3, m.pos[2] + 7 + dirv[2] * 3]
        if k == "gun":
            if w["cooldown"] > 0 or w["reload"] > 0 or w["ammo"] <= 0:
                return
            sp = math.radians(st["spread_deg"])
            y2, p2 = yaw + self.r.gauss(0, sp), pitch + self.r.gauss(0, sp)
            v = st["speed"]
            if m.id == self.ours:
                self.stat("gun", "shots")
            self.projectiles.append({"by": m.id, "pos": org, "vel": [v * math.cos(p2) * math.cos(y2), v * math.cos(p2) * math.sin(y2), v * math.sin(p2)],
                                     "dmg": st["damage"], "left": st["range"]})
            w["cooldown"] = st["cooldown"]
            w["ammo"] -= 1
            if w["ammo"] == 0:
                w["reload"] = st["reload"]
            self.sound(m, "gun")
        elif k == "lightning":
            if w["cooldown"] > 0 or m.energy < st["energy"]:
                return
            m.energy -= st["energy"]
            m.energy_t = self.t
            w["cooldown"] = st["cooldown"]
            if m.id == self.ours:
                self.stat("lightning", "shots")
            best = None
            for o in self.mechs:
                if o is m or not o.alive:
                    continue
                dd = d2(m.pos, o.pos)
                if dd > st["range"]:
                    continue
                ang = math.atan2(o.pos[1] - m.pos[1], o.pos[0] - m.pos[0])
                if abs(wrap(ang - yaw)) > math.radians(st["cone_deg"]) / 2:
                    continue
                if not self.A.los((m.pos[0], m.pos[1], m.pos[2] + 7), (o.pos[0], o.pos[1], o.pos[2] + 4.5)):
                    continue
                if best is None or dd < best[0]:
                    best = (dd, o)
            if best:
                self.damage(best[1], st["damage"], m, "lightning", m.pos)
            self.sound(m, "lightning")
        elif k == "laser":
            pass  # handled by charge logic

    def laser_fire(self, m, w, frac):
        st = w["stats"]
        yaw, pitch = m.aim
        dirv = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        org = (m.pos[0], m.pos[1], m.pos[2] + 7)
        best = None
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            c = (o.pos[0], o.pos[1], o.pos[2] + 4.5)
            rel = (c[0] - org[0], c[1] - org[1], c[2] - org[2])
            along = rel[0] * dirv[0] + rel[1] * dirv[1] + rel[2] * dirv[2]
            if along < 0 or along > st["range"]:
                continue
            px = org[0] + dirv[0] * along
            py = org[1] + dirv[1] * along
            pz = org[2] + dirv[2] * along
            if math.hypot(px - c[0], py - c[1]) < 2.5 and abs(pz - c[2]) < 4.5:
                if self.A.los(org, (px, py, pz)) and (best is None or along < best[0]):
                    best = (along, o)
        if m.id == self.ours:
            self.stat("laser", "shots")
        if best:
            dmg = st["damage"] * (st["min_frac"] + (1 - st["min_frac"]) * frac)
            self.damage(best[1], dmg, m, "laser", m.pos)
        w["cooldown"] = st["cooldown"]
        w["charge"] = 0.0
        m.charge_start = None
        self.sound(m, "laser")

    def sound(self, m, kind):
        rng = self.cfg["sound"]["range"].get(kind, 100)
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            dd = d2(o.pos, m.pos)
            if dd < rng:
                o.sounds.append({"kind": kind, "bearing": math.atan2(m.pos[1] - o.pos[1], m.pos[0] - o.pos[0]) + self.r.gauss(0, 0.12),
                                 "dist": "near" if dd < 60 else ("mid" if dd < 160 else "far"), "age": 0.05})

    def apply(self, m, a, decide):
        """Apply a decision's continuous and one-shot parts."""
        if a is None:
            return
        if "aim" in a:
            m.cmd["aim"] = a["aim"]
        m.cmd["move"] = a.get("move", [0, 0])
        m.cmd["boost"] = bool(a.get("boost"))
        m.cmd["fire"] = a.get("fire", [False, False])
        m.cmd["jump"] = bool(a.get("jump"))
        if "take" in a:
            pk = self.pickups.get(a["take"])
            if pk and d2(pk["pos"], m.pos) <= 4.0 and abs(pk["pos"][2] - m.pos[2]) < 4:
                slot = a.get("drop")
                if slot is None:
                    slot = 1 if m.weapons[1] is None else (0 if m.weapons[0] is None else None)
                if slot is not None and all((w is None or w["kind"] != pk["kind"]) for w in m.weapons):
                    old = m.weapons[slot]
                    m.weapons[slot] = self.weap(pk["kind"])
                    m.weapons[slot]["cooldown"] = 1.0
                    del self.pickups[pk["id"]]
                    m.events.append({"type": "took", "id": pk["id"], "kind": pk["kind"], "slot": slot})
                    if old is not None:
                        self.pickups[self.pid] = {"id": self.pid, "kind": old["kind"], "pos": list(m.pos)}
                        self.pid += 1
            else:
                m.events.append({"type": "take_failed", "id": a["take"], "reason": "range"})
        if a.get("reload"):
            g = m.weapons[0]
            if g and g["kind"] == "gun" and g["reload"] <= 0 and g["ammo"] < g["stats"]["mag"]:
                g["reload"] = g["stats"]["reload"]

    def physics(self, m):
        dt = self.dt
        cfgm = self.cfg["mech"]
        mv = m.cmd.get("move", [0, 0])
        n = math.hypot(mv[0], mv[1])
        if n > 1:
            mv = [mv[0] / n, mv[1] / n]
        charging = m.charge_start is not None
        speed = cfgm["walk_speed"]
        boosting = m.cmd.get("boost") and m.energy > 0
        if boosting and m.grounded and n > 0.1:
            speed = cfgm["boost_speed"]
            m.energy -= cfgm["boost_drain"] * dt
            m.energy_t = self.t
        if charging:
            speed *= 0.5
        acc = cfgm["ground_accel"] if m.grounded else cfgm["air_accel"]
        for k in (0, 1):
            want = mv[k] * speed
            dv = want - m.vel[k]
            step = acc * dt
            m.vel[k] += max(-step, min(step, dv))
        if m.cmd.get("jump") and m.grounded:
            m.vel[2] = cfgm["jump_speed"]
            m.grounded = False
        if not m.grounded:
            m.vel[2] -= cfgm["gravity"] * dt
            if boosting and m.energy > 0:
                m.vel[2] = min(cfgm["max_rise_speed"], m.vel[2] + cfgm["jet_accel"] * dt)
                m.energy -= cfgm["jet_drain"] * dt
                m.energy_t = self.t
        m.energy = max(0.0, m.energy)
        if self.t - m.energy_t > cfgm["energy_regen_delay"]:
            m.energy = min(100.0, m.energy + cfgm["energy_regen"] * dt)
        # horizontal move with wall sliding
        for k in (0, 1):
            np_ = list(m.pos)
            np_[k] += m.vel[k] * dt
            if self.blocked(np_, m.pos[2]):
                m.vel[k] = 0.0
            else:
                m.pos[k] = np_[k]
        # vertical
        c = self.A.fcell(m.pos[0], m.pos[1])
        floor = self.A.ground(m.pos[0], m.pos[1])
        if c >= 0 and self.A.ftop[c] > floor and self.A.ftop[c] <= m.pos[2] + 1.2:
            floor = self.A.ftop[c]
        m.pos[2] += m.vel[2] * dt
        if m.pos[2] <= floor + 1.2 and (m.grounded or m.vel[2] <= 0):
            if m.pos[2] < floor or m.grounded:
                m.pos[2] = floor
            if m.pos[2] <= floor + 0.05:
                m.pos[2] = floor
                m.vel[2] = 0.0
                m.grounded = True
        else:
            if m.pos[2] > floor + 0.05:
                m.grounded = False
        # aim slew
        ty, tp = m.cmd.get("aim", m.aim)
        tp = max(cfgm["min_pitch"], min(cfgm["max_pitch"], tp))
        dy = wrap(ty - m.aim[0])
        mx = cfgm["turn_rate"] * dt
        m.aim[0] = wrap(m.aim[0] + max(-mx, min(mx, dy)))
        dp = tp - m.aim[1]
        mx = cfgm["pitch_rate"] * dt
        m.aim[1] += max(-mx, min(mx, dp))

    def blocked(self, p, z):
        A = self.A
        for ox, oy in ((2.0, 0), (-2.0, 0), (0, 2.0), (0, -2.0), (0, 0)):
            c = A.fcell(p[0] + ox, p[1] + oy)
            if c < 0:
                return True
            top = A.ftop[c]
            if top > z + 1.2 and A.fwall[c]:
                return True
            if A.fground[c] > z + 2.5:
                return True
        return False

    def weapons_step(self, m):
        dt = self.dt
        for w in m.weapons:
            if w is None:
                continue
            w["cooldown"] = max(0.0, w["cooldown"] - dt)
            if w["kind"] == "gun" and w["reload"] > 0:
                w["reload"] = max(0.0, w["reload"] - dt)
                if w["reload"] == 0:
                    w["ammo"] = w["stats"]["mag"]
        fire = m.cmd.get("fire", [False, False])
        for slot in (0, 1):
            w = m.weapons[slot]
            if w is None:
                continue
            on = slot < len(fire) and fire[slot]
            if w["kind"] == "laser":
                st = w["stats"]
                if on and m.charge_start is None and w["cooldown"] <= 0:
                    m.charge_start = self.t
                if m.charge_start is not None:
                    ch = self.t - m.charge_start
                    w["charge"] = ch
                    if (not on and ch >= st["min_charge"]) or ch >= st["charge"] + st["hold_s"]:
                        self.laser_fire(m, w, min(1.0, ch / st["charge"]))
            elif on:
                self.shoot(m, slot)

    def projectiles_step(self):
        dt = self.dt
        keep = []
        for p in self.projectiles:
            hit = False
            for _ in range(2):
                pos = p["pos"]
                for k in range(3):
                    pos[k] += p["vel"][k] * dt / 2
                p["left"] -= math.sqrt(sum(v * v for v in p["vel"])) * dt / 2
                if p["left"] <= 0 or self.A.solid_at(pos[0], pos[1], pos[2]):
                    hit = True
                    break
                for o in self.mechs:
                    if o.alive and o.id != p["by"] and math.hypot(pos[0] - o.pos[0], pos[1] - o.pos[1]) < 2.8 and o.pos[2] - 0.3 < pos[2] < o.pos[2] + 9.3:
                        src = (pos[0] - p["vel"][0], pos[1] - p["vel"][1])
                        self.damage(o, p["dmg"], self.mechs[p["by"]], "gun", src)
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                keep.append(p)
        self.projectiles = keep

    def run(self, max_t=480.0):
        us = self.mechs[self.ours]
        cfgm = self.cfg["mech"]
        while self.t < max_t:
            alive = [m for m in self.mechs if m.alive]
            if len(alive) <= 1 or not us.alive:
                break
            if self.tick % 2 == 0:
                # hunters: one early, more as the squad thins out, all late
                dead_house = sum(1 for m in self.mechs if not m.alive and m.id != self.ours)
                n_h = 1 + dead_house + (15 if self.t > 300 else (int((self.t - 120) / 30) if self.t > 120 else 0))
                self.hunters = {h for h in self.hunters if self.mechs[h].alive}
                if len(self.hunters) < n_h:
                    # weakest-first squad: send the closest idle house bots, one at a time
                    idle = sorted([m.id for m in alive if m.id != self.ours and m.id not in self.hunters], key=lambda i: ({'easy': 0, 'medium': 1, 'hard': 2, 'expert': 3}[self.skill[i]], d2(self.mechs[i].pos, us.pos)))
                    self.hunters.update(idle[: n_h - len(self.hunters)])
                for m in alive:
                    if m.id != self.ours:
                        self.house(m)
                obs = self.observe(us)
                t0 = time.perf_counter()
                try:
                    a = self.bot.act(obs)
                except Exception:
                    self.failures += 1
                    a = None
                self.times.append(time.perf_counter() - t0)
                json.dumps(a) if a is not None else None
                self.apply(us, a, True)
            for m in alive:
                self.physics(m)
                self.weapons_step(m)
            self.projectiles_step()
            # zone + regen
            for m in alive:
                if not m.alive:
                    continue
                inz = d2(m.pos, self.zc) <= self.zr
                if not inz:
                    m.hp -= self.dps * self.dt
                    m.last_combat = self.t
                    if m.hp <= 0:
                        self.damage(m, 0.0001, None, "zone")
                        m.hp = 0
                        m.alive = False
                        m.death_t = self.t
                        self.order.append((self.t, m.id))
                elif self.t - m.last_combat > cfgm["regen_delay"]:
                    m.hp = min(m.max_hp, m.hp + cfgm["regen_per_s"] * self.dt)
            self.zone_step()
            self.t += self.dt
            self.tick += 1
        # place
        if us.alive:
            alive = [m for m in self.mechs if m.alive]
            place = 1 + sum(1 for m in alive if m.hp > us.hp)
        else:
            place = sum(1 for m in self.mechs if m.alive) + 1 + sum(1 for (tt, i) in self.order if tt == us.death_t and i != us.id)
        return place, us.kills, self.t


ARENA_CACHE = {}


def load_bot(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("botmod_" + str(abs(hash(path))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Bot


def one(args):
    kit, seed, botpath, loadout = args
    s = Sim(kit, seed, load_bot(botpath))
    if loadout:
        s.mechs[s.ours].weapons[1] = s.weap(loadout)
    place, kills, t = s.run()
    return {"seed": seed, "place": place, "kills": kills, "t": t, "times": s.times, "stats": s.stats, "failures": s.failures}


def run_batch(kit, botpath, seeds, loadout=None, procs=4):
    from multiprocessing import Pool
    with Pool(procs) as pool:
        return pool.map(one, [(kit, sd, botpath, loadout) for sd in seeds])


def summarize(name, res):
    allt = sorted(x for r in res for x in r["times"])
    n = len(res)
    st = {}
    for r in res:
        for k, v in r["stats"].items():
            st[k] = st.get(k, 0) + v
    acc = []
    for w in ("gun", "laser", "lightning"):
        sh = st.get((w, "shots"), 0)
        if sh:
            acc.append(f"{w} {st.get((w, 'hits'), 0)}/{sh} hit ({100 * st.get((w, 'hits'), 0) / sh:.0f}%), {st.get((w, 'dmg'), 0) / n:.0f} dmg/match")
    return (f"{name}: score {sum(16 - r['place'] for r in res) / n:.2f}  mean place {sum(r['place'] for r in res) / n:.2f}  "
            f"kills/match {sum(r['kills'] for r in res) / n:.2f}  survived {sum(r['t'] for r in res) / n:.0f}s  "
            f"failures {sum(r['failures'] for r in res)}  act ms p99 {allt[int(len(allt) * 0.99)] * 1000:.1f} max {allt[-1] * 1000:.1f}\n    " + "; ".join(acc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", default="/tmp/claude-0/kitx/mechfight-kit")
    ap.add_argument("--matches", type=int, default=24)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--bot", action="append", help="bot.py file(s) to compare on the same seeds")
    ap.add_argument("--loadout", default=None, help="give our mech this second weapon at spawn (laser, lightning)")
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    bots = a.bot or [os.path.join(os.path.dirname(HERE), "bot.py")]
    seeds = list(range(a.seed, a.seed + a.matches))
    for b in bots:
        res = run_batch(a.kit, os.path.abspath(b), seeds, a.loadout, a.procs)
        print(summarize(os.path.basename(b), res), flush=True)


if __name__ == "__main__":
    main()

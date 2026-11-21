"""Blueprinters mech bot for mechfight/1.

Placement is what scores, so the bot plays for survival first:
- stay well inside the zone, moving early and along a path around buildings;
- face whoever is shooting (rear hits do 1.5x), strafe unpredictably, keep a weapon-appropriate range;
- shoot with lead, charge lasers fully, use lightning up close, lob mortars over cover;
- grab a better second weapon, cubes, and supply drops when it is safe;
- break off and heal (regen after 5 s) when hurt and outgunned.
"""
from __future__ import annotations

import gc
import json
import math
import queue
import random
import sys
import threading
import time
import traceback

from arena import Arena

NAME = "blueprinters"
TAU = 2 * math.pi

# preferred second weapons, best first
# no mortar: carrying one makes enemy lasers hit us 60% harder (cook-off), and we rarely land shells
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": -5, "gun": 0}
UPGRADE_RANK = [
    "armor", "plating", "nanites", "laser_damage", "laser_charge", "gun_rate", "gun_damage",
    "lightning_efficiency", "lightning_range", "gun_mag", "gun_velocity", "servos", "capacitor",
    "reactor", "jets", "gyros", "lightning_chain", "mortar_reload", "mortar_velocity",
    "mortar_splash", "laser_pierce", "sensors",
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(dx, dy):
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return 0.0, 0.0
    return dx / n, dy / n


class Track:
    """What we remember about another mech."""
    __slots__ = ("id", "pos", "vel", "aim", "hp", "max_hp", "weapons", "charging", "seen_t", "cubes", "last_dmg_t")

    def __init__(self, i):
        self.id = i
        self.pos = (0.0, 0.0, 0.0)
        self.vel = (0.0, 0.0, 0.0)
        self.aim = (0.0, 0.0)
        self.hp = 1400.0
        self.max_hp = 1400.0
        self.weapons = []
        self.charging = False
        self.seen_t = -99.0
        self.cubes = 0
        self.last_dmg_t = -99.0


class Bot:
    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = int(hello.get("slot", 0))
        self.rng = random.Random(int(hello.get("seed", 1)) ^ 0x9EAF)
        mp = hello.get("map") or {}
        t0 = time.perf_counter()
        self.arena = Arena(mp.get("dir", "/arena"), mp.get("files"))
        log(f"arena ok={self.arena.ok} in {time.perf_counter() - t0:.2f}s")
        self.tracks = {}
        self.t = 0.0
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.strafe_dir = 1 if self.rng.random() < 0.5 else -1
        self.strafe_until = 0.0
        self.last_damage_t = -99.0
        self.last_damage_bearing = None
        self.damage_log = []  # (t, amount, by)
        self.stuck_t = 0.0
        self.last_pos = None
        self.unstick_until = 0.0
        self.unstick_dir = (0.0, 0.0)
        self.scan_yaw = None
        self.sound_yaw = None
        self.sound_t = -99.0
        self.charge_t = 0.0
        self.jet_until = 0.0
        self.mode = "start"
        self.target_id = None
        self.bad_pickups = {}
        self.wander_goal = None
        self.cover_spot = None
        self.peek_spot = None
        self.peek_t = -99.0
        self.known_pickups = {}  # id -> (kind, pos, seen_t)
        self.known_crates = {}  # id -> (pos, seen_t)
        self.known_cubes = {}  # id -> (pos, seen_t)
        self.cover_t = -99.0
        self.t_start = time.perf_counter()

    # ------------------------------------------------------------------ helpers
    def wstat(self, kind, key, default=0.0):
        try:
            return float(self.cfg["weapons"][kind][key])
        except Exception:
            return default

    def zone_target(self, obs, me):
        """Where we want to be with respect to the zone, and how urgently (0..1)."""
        z = obs["zone"]
        t = obs["t"]
        cx, cy = z["center"]
        r = z["radius"]
        nx, ny = z["next_center"]
        nr = z["next_radius"]
        p = me["pos"]
        start, end = z["shrink_start"], z["shrink_end"]
        speed = me["stats"].get("walk_speed", 11.7)
        # radius of the circle we must be in when we arrive: plan for the next circle ahead of time
        dn = d2(p, (nx, ny))
        margin_n = max(6.0, min(nr * 0.35, 40.0))
        need_next = dn > nr - margin_n
        # time we have before the shrink reaches us (roughly)
        if t < start:
            time_left = start - t + (end - start) * 0.5
        else:
            time_left = max(0.0, end - t)
        travel = max(0.0, dn - (nr - margin_n)) / max(1.0, speed * 0.75)
        urgency = 0.0
        if need_next:
            urgency = max(0.0, min(1.0, 1.2 - (time_left - travel) / 40.0))
        # current circle (interpolated during the shrink)
        if start <= t <= end and end > start:
            f = (t - start) / (end - start)
            ccx = cx + (nx - cx) * f
            ccy = cy + (ny - cy) * f
            cr = r + (nr - r) * f
        else:
            ccx, ccy, cr = cx, cy, r
        dc = d2(p, (ccx, ccy))
        if dc > cr - 5 or not me["in_zone"]:
            urgency = 1.0
        # aim point: towards the next centre, stopping a bit inside
        if need_next or urgency >= 1.0:
            ux, uy = unit(nx - p[0], ny - p[1])
            stop = min(dn, max(0.0, dn - (nr * 0.55)))
            gx, gy = p[0] + ux * stop, p[1] + uy * stop
            if not need_next:
                gx, gy = nx, ny
            return (gx, gy), urgency, need_next
        return None, 0.0, False

    def inside_future(self, obs, pt, margin=10.0):
        z = obs["zone"]
        return d2(pt, z["next_center"]) < z["next_radius"] - margin

    def in_current(self, obs, pt, margin=10.0):
        z = obs["zone"]
        t = obs["t"]
        start, end = z["shrink_start"], z["shrink_end"]
        cx, cy = z["center"]
        r = z["radius"]
        if start <= t <= end and end > start:
            f = (t - start) / (end - start)
            cx += (z["next_center"][0] - cx) * f
            cy += (z["next_center"][1] - cy) * f
            r += (z["next_radius"] - r) * f
        return d2(pt, (cx, cy)) < r - margin

    # ------------------------------------------------------------------ navigation
    def steer(self, me, goal, t, avoid_pts=()):
        """Unit move vector from me toward goal, around buildings."""
        p = me["pos"]
        if goal is None:
            return 0.0, 0.0
        dist = d2(p, goal)
        if dist < 1.5:
            return 0.0, 0.0
        A = self.arena
        if not A.ok:
            return unit(goal[0] - p[0], goal[1] - p[1])
        if A.walk_clear(p, goal):
            self.path = None
            return unit(goal[0] - p[0], goal[1] - p[1])
        replan = (self.path is None or self.path_goal is None or d2(self.path_goal, goal) > 12
                  or t - self.path_t > 4.0)
        if replan and t - self.path_t > 0.5 and self.spent() < 0.004:
            self.path_t = t
            self.path_goal = goal
            self.path = A.astar((p[0], p[1]), goal, max_exp=1000)
        if self.path:
            # drop reached waypoints, then head for the furthest one we can walk to straight
            while len(self.path) > 1 and d2(p, self.path[0]) < 4.0:
                self.path.pop(0)
            wp = self.path[0]
            if len(self.path) > 1 and A.walk_clear(p, self.path[1]):
                self.path.pop(0)
                wp = self.path[0]
            return unit(wp[0] - p[0], wp[1] - p[1])
        return unit(goal[0] - p[0], goal[1] - p[1])

    def check_stuck(self, me, want_move, t):
        p = me["pos"]
        v = me["vel"]
        sp = math.hypot(v[0], v[1])
        if want_move and sp < 2.0 and me["grounded"]:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.2)
        if self.stuck_t > 0.8 and t > self.unstick_until:
            self.stuck_t = 0.0
            self.unstick_until = t + 1.2
            self.jet_until = t + 1.2
            ang = self.rng.uniform(0, TAU)
            self.unstick_dir = (math.cos(ang), math.sin(ang))
            self.path = None
            self.path_t = -99.0
            return True
        return False

    def find_cover(self, obs, me, threats_xyz, t, radius=70.0, short=False):
        """A nearby walkable spot (inside the zone) that none of `threats_xyz` can see. Cached ~1.5 s."""
        A = self.arena
        if not A.ok:
            return None
        p = me["pos"]
        if short:
            return self.find_short_cover(obs, me, threats_xyz, t, radius)
        if self.cover_spot is not None and (t - self.cover_t < 1.5 or self.spent() > 0.003):
            return self.cover_spot
        if self.spent() > 0.005:
            return None
        self.cover_t = t
        best = None
        cands = []
        for r in (14.0, 26.0, 40.0, 55.0, radius):
            n = 12 if r < 30 else 16
            off = self.rng.uniform(0, TAU)
            for k in range(n):
                a = off + TAU * k / n
                cands.append((p[0] + r * math.cos(a), p[1] + r * math.sin(a), r))
        if self.cover_spot is not None:
            cands.append((self.cover_spot[0], self.cover_spot[1], d2(p, self.cover_spot)))
        for (x, y, r) in cands:
            c = A.ncell(x, y)
            if c < 0 or A.nblocked[c]:
                continue
            if not self.in_current(obs, (x, y), 12.0):
                continue
            if obs["zone"]["shrink_start"] - t < 25 and not self.inside_future(obs, (x, y), 10.0):
                continue
            gz = A.ground(x, y)
            seen = 0
            for q in threats_xyz:
                if A.los((q[0], q[1], q[2] + 8.0), (x, y, gz + 7.0)):
                    seen += 1
            s = seen * 1000 + r
            # avoid running past the threats to get there
            for q in threats_xyz:
                if d2(q, (x, y)) < d2(q, p) - 10:
                    s += 40
            if best is None or s < best[0]:
                best = (s, (x, y), seen)
        if best is None or best[2] > 0:
            self.cover_spot = None
            return None
        self.cover_spot = best[1]
        return best[1]

    def find_short_cover(self, obs, me, threats_xyz, t, radius):
        """Closest spot within a short dash that the given threats can't see. Cached 0.4 s."""
        A = self.arena
        p = me["pos"]
        if self.peek_spot is not None and t - self.peek_t < 0.4:
            return self.peek_spot
        if self.spent() > 0.005:
            return self.peek_spot
        self.peek_t = t
        best = None
        for r in (6.0, 11.0, 16.0, radius):
            for k in range(12):
                a = TAU * k / 12
                x, y = p[0] + r * math.cos(a), p[1] + r * math.sin(a)
                c = A.ncell(x, y)
                if c < 0 or A.nblocked[c] or not A.walk_clear(p, (x, y)):
                    continue
                if not self.in_current(obs, (x, y), 5.0):
                    continue
                gz = A.ground(x, y)
                if any(A.los((q[0], q[1], q[2] + 8.0), (x, y, gz + 4.5)) or A.los((q[0], q[1], q[2] + 8.0), (x, y, gz + 8.5)) for q in threats_xyz):
                    continue
                best = (x, y)
                break
            if best is not None:
                break
        self.peek_spot = best
        return best

    # ------------------------------------------------------------------ perception
    def remember_items(self, obs, me):
        """Items are only listed while in view; remember them so goals survive a scan or a turn."""
        t = obs["t"]
        p = me["pos"]
        for pk in obs["pickups"]:
            self.known_pickups[pk["id"]] = (pk["kind"], tuple(pk["pos"]), t)
        for c in obs["crates"]:
            self.known_crates[c["id"]] = (tuple(c["pos"]), t)
        for c in obs["cubes"]:
            self.known_cubes[c["id"]] = (tuple(c["pos"]), t)
        vis_p = {x["id"] for x in obs["pickups"]}
        vis_c = {x["id"] for x in obs["crates"]}
        vis_q = {x["id"] for x in obs["cubes"]}
        # forget things that should be visible from here but aren't (taken/destroyed), or are old
        for store, vis, ttl in ((self.known_pickups, vis_p, 90.0), (self.known_crates, vis_c, 90.0), (self.known_cubes, vis_q, 40.0)):
            for i in list(store):
                v = store[i]
                pos, seen = v[-2], v[-1]
                if i in vis:
                    continue
                if t - seen > ttl or d2(p, pos) < 20:
                    del store[i]

    def update_tracks(self, obs):
        t = obs["t"]
        for m in obs["mechs"]:
            if m.get("kind") != "mech":
                continue
            tr = self.tracks.get(m["id"])
            if tr is None:
                tr = self.tracks[m["id"]] = Track(m["id"])
            tr.pos = tuple(m["pos"])
            tr.vel = tuple(m.get("vel", (0, 0, 0)))
            tr.aim = tuple(m.get("aim", (0, 0)))
            if "hp" in m:
                tr.hp = m["hp"]
            if "max_hp" in m:
                tr.max_hp = m["max_hp"]
            tr.weapons = m.get("weapons") or []
            tr.charging = bool(m.get("charging"))
            tr.cubes = m.get("cubes", 0)
            tr.seen_t = t
        for e in obs["events"]:
            ty = e.get("type")
            if ty == "damaged":
                self.last_damage_t = t
                if e.get("by") is not None and e.get("weapon") != "npc":
                    self.last_damage_bearing = e.get("bearing")
                    self.damage_log.append((t, e.get("amount", 0), e.get("by")))
                    tr = self.tracks.get(e["by"])
                    if tr is not None:
                        tr.last_dmg_t = t
                elif e.get("weapon") == "npc":
                    self.last_damage_bearing = e.get("bearing")
            elif ty == "kill":
                self.tracks.pop(e.get("target"), None)
            elif ty == "take_failed":
                self.bad_pickups[e.get("id")] = t
        self.damage_log = [d for d in self.damage_log if t - d[0] < 6.0]
        # loud sounds tell us where to look
        best = None
        for s in obs["sounds"]:
            k = s.get("kind")
            if k in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet"):
                pri = {"near": 3, "mid": 2, "far": 1}.get(s.get("dist"), 0)
                if k in ("laser_charge", "step", "boost", "jet"):
                    pri += 1
                if best is None or pri > best[0]:
                    best = (pri, s["bearing"])
        if best is not None and best[0] >= 2:
            self.sound_yaw = best[1]
            self.sound_t = t

    def visible_enemies(self, obs):
        return [m for m in obs["mechs"] if m.get("kind") == "mech"]

    # ------------------------------------------------------------------ aiming
    def lead(self, me, tgt_pos, tgt_vel, speed, gravity=0.0):
        """Aim [yaw, pitch] to hit a target moving at tgt_vel with a projectile of `speed`."""
        p = me["pos"]
        mh = me["stats"].get("muzzle_height", 7.0)
        ox, oy, oz = p[0], p[1], p[2] + mh
        tx, ty, tz = tgt_pos[0], tgt_pos[1], tgt_pos[2] + 4.5
        vx, vy, vz = tgt_vel
        if abs(vz) > 15:
            vz = 0.0
        tt = 0.0
        for _ in range(3):
            px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt * 0.5
            dist = math.sqrt((px - ox) ** 2 + (py - oy) ** 2 + (pz - oz) ** 2)
            tt = max(0.0, (dist - 3.0) / speed)
        px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt * 0.5
        # keep the aim point above the ground under it
        A = self.arena
        if A.ok:
            gz = A.ground(px, py)
            pz = max(pz, gz + 3.0)
        yaw = math.atan2(py - oy, px - ox)
        hd = math.hypot(px - ox, py - oy)
        pitch = math.atan2(pz - oz, hd)
        return yaw, pitch, hd, (px, py, pz)

    def hitscan_aim(self, me, tgt_pos, tgt_vel, dt):
        """Aim [yaw, pitch] for an instant weapon: the target's center `dt` seconds from now."""
        p = me["pos"]
        ox, oy, oz = p[0], p[1], p[2] + me["stats"].get("muzzle_height", 7.0)
        tx = tgt_pos[0] + tgt_vel[0] * dt
        ty = tgt_pos[1] + tgt_vel[1] * dt
        tz = tgt_pos[2] + 4.5 + (tgt_vel[2] * dt if abs(tgt_vel[2]) < 15 else 0.0)
        hd = math.hypot(tx - ox, ty - oy)
        return math.atan2(ty - oy, tx - ox), math.atan2(tz - oz, hd), hd, (tx, ty, tz)

    def mortar_aim(self, me, tgt, tvel):
        p = me["pos"]
        v = self.wstat("mortar", "speed", 72.0)
        g = self.wstat("mortar", "gravity", 22.0)
        for w in me["weapons"]:
            if w and w["kind"] == "mortar":
                v = w["stats"].get("speed", v)
                g = w["stats"].get("gravity", g)
        oz = p[2] + me["stats"].get("height", 9.0) + 0.5
        tx, ty, tz = tgt[0], tgt[1], tgt[2] + 1.0
        tt = 2.0
        for _ in range(3):
            px, py = tx + tvel[0] * tt * 0.6, ty + tvel[1] * tt * 0.6
            x = math.hypot(px - p[0], py - p[1])
            y = tz - oz
            v2 = v * v
            disc = v2 * v2 - g * (g * x * x + 2 * y * v2)
            if disc < 0:
                return None
            # high arc when lobbing over cover, low arc otherwise
            ang = math.atan2(v2 + math.sqrt(disc), g * x)
            if ang > me["stats"].get("max_pitch", 1.3):
                ang = math.atan2(v2 - math.sqrt(disc), g * x)
            tt = x / max(1e-3, v * math.cos(ang))
        yaw = math.atan2(py - p[1], px - p[0])
        return yaw, ang

    # ------------------------------------------------------------------ main decision
    def spent(self):
        """Seconds of this decision's 50 ms budget already used."""
        return time.perf_counter() - self.t_start

    def act(self, obs):
        self.t_start = time.perf_counter()
        t = obs["t"]
        self.t = t
        me = obs["self"]
        p = me["pos"]
        self.update_tracks(obs)
        self.remember_items(obs, me)
        act = {"tick": obs["tick"]}

        # upgrades
        off = obs.get("offer")
        if off and off.get("choices"):
            best_i, best_r = 0, 999
            for i, ch in enumerate(off["choices"]):
                r = UPGRADE_RANK.index(ch["id"]) if ch["id"] in UPGRADE_RANK else 50
                if r < best_r:
                    best_i, best_r = i, r
            act["pick"] = best_i

        weapons = me["weapons"]
        kinds = [w["kind"] if w else None for w in weapons]
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        energy = me["energy"]
        enemies = self.visible_enemies(obs)
        # threats seen recently (server tracks memory_s for us; also keep our own short memory)
        threats = []
        for m in enemies:
            dd = d2(p, m["pos"])
            threats.append((dd, m))
        threats.sort(key=lambda x: x[0])

        # -------------------------------------------------- target selection
        target = None
        if threats:
            def score(item):
                dd, m = item
                hpv = m.get("hp", 1400)
                s = dd + hpv * 0.06
                tr = self.tracks.get(m["id"])
                if tr is not None and t - tr.last_dmg_t < 3.0:
                    s -= 60  # whoever is hurting us
                if m["id"] == self.target_id:
                    s -= 25  # stickiness
                # someone standing in a landed supply drop's ring freezes our capture: shoot them first
                for dr in obs.get("drops") or []:
                    if dr.get("lands_in", 0) <= 0 and d2(m["pos"], dr["pos"]) < dr.get("radius", 8.0) + 2 and d2(p, dr["pos"]) < 120:
                        s -= 120
                return s
            target = min(threats, key=score)[1]
            self.target_id = target["id"]
        else:
            self.target_id = None

        n_threats = sum(1 for dd, m in threats if dd < 200)
        recent_dmg = sum(a for (tt, a, b) in self.damage_log if t - tt < 4.0)

        # -------------------------------------------------- movement goal
        zone_goal, urgency, need_next = self.zone_target(obs, me)
        goal = None
        move = (0.0, 0.0)
        boost = False
        jump = False
        mode = "roam"

        retreat = False
        if target is not None:
            # outgunned: low HP vs healthier enemy, or several enemies on us
            ehp = target.get("hp", 1400)
            if (hp_frac < 0.5 and ehp > me["hp"] * 1.3) or (n_threats >= 3 and hp_frac < 0.6) or \
               (n_threats >= 2 and hp_frac < 0.4) or (hp_frac < 0.22 and ehp > 200):
                retreat = True

        dgoal = self.drop_goal(obs, me)
        if target is not None:
            tp = target["pos"]
            dist = d2(p, tp)
            dx, dy = unit(tp[0] - p[0], tp[1] - p[1])
            # preferred range by weapon
            if "lightning" in kinds and energy > 15:
                want = 28.0
            elif "laser" in kinds:
                want = 120.0
            else:
                want = 90.0
            tw = target.get("weapons") or []
            if "lightning" in tw and "lightning" not in kinds:
                want = max(want, 70.0)  # stay out of their arc
            if t > self.strafe_until:
                self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.7 else self.strafe_dir
                self.strafe_until = t + self.rng.uniform(0.5, 1.4)
            px_, py_ = -dy * self.strafe_dir, dx * self.strafe_dir
            if retreat:
                mode = "retreat"
                # run away from the centroid of threats, but stay inside the zone
                sx = sy = 0.0
                for dd, m in threats:
                    w = 1.0 / max(20.0, dd)
                    sx += (p[0] - m["pos"][0]) * w
                    sy += (p[1] - m["pos"][1]) * w
                ax, ay = unit(sx, sy)
                cand = (p[0] + ax * 60, p[1] + ay * 60)
                if not self.in_current(obs, cand, 15.0) or (need_next and urgency > 0.5):
                    # bend toward the zone centre
                    zc = obs["zone"]["next_center"]
                    zx, zy = unit(zc[0] - p[0], zc[1] - p[1])
                    ax, ay = unit(ax + zx * 1.5, ay + zy * 1.5)
                    cand = (p[0] + ax * 60, p[1] + ay * 60)
                cov = self.find_cover(obs, me, [m["pos"] for dd, m in threats], t)
                if cov is not None:
                    cand = cov
                    mode = "cover"
                if dgoal is not None:
                    # a supply drop is a full repair: retreat onto it rather than just away
                    cand = dgoal
                    mode = "drop"
                mv = self.steer(me, cand, t)
                move = (mv[0] * 0.85 + px_ * 0.35, mv[1] * 0.85 + py_ * 0.35)
                boost = energy > 10
            else:
                mode = "fight"
                radial = 0.0
                if dist > want + 25:
                    radial = 0.8
                elif dist < want - 15:
                    radial = -0.8
                # zone pressure overrides spacing
                if urgency > 0.6 and zone_goal is not None:
                    zv = self.steer(me, zone_goal, t)
                    move = (zv[0] * 0.8 + px_ * 0.6, zv[1] * 0.8 + py_ * 0.6)
                else:
                    if radial > 0 and self.arena.ok and not self.arena.walk_clear(p, tp):
                        av = self.steer(me, (tp[0], tp[1]), t)
                        move = (av[0] * radial + px_ * 0.7, av[1] * radial + py_ * 0.7)
                    else:
                        move = (dx * radial + px_, dy * radial + py_)
                    # don't strafe out of the zone
                    nxt = (p[0] + move[0] * 20, p[1] + move[1] * 20)
                    if not self.in_current(obs, nxt, 5.0):
                        zc = obs["zone"]["next_center"]
                        zx, zy = unit(zc[0] - p[0], zc[1] - p[1])
                        move = (move[0] + zx * 1.2, move[1] + zy * 1.2)
                # a better weapon or a cube close by is worth a detour mid-fight (still facing the target)
                grab = self.fight_grab(obs, me, kinds, dist)
                if grab is not None:
                    gv = self.steer(me, grab, t)
                    move = (gv[0] + px_ * 0.35, gv[1] + py_ * 0.35)
                    mode = "grab"
                in_ring = False
                if dgoal is not None:
                    landed = any(d2(dr["pos"], dgoal) < 1.0 and dr.get("lands_in", 0) <= 0 for dr in obs.get("drops") or [])
                    if d2(p, dgoal) > 5.5 or not landed:
                        gv = self.steer(me, dgoal, t)
                        move = (gv[0] + px_ * 0.25, gv[1] + py_ * 0.25)
                        mode = "drop"
                    else:
                        # capturing: keep moving side to side inside the 8 m ring so we are harder to hit
                        in_ring = True
                        sx, sy = dgoal[0] + px_ * 4.0, dgoal[1] + py_ * 4.0
                        ux, uy = unit(sx - p[0], sy - p[1])
                        k = min(1.0, d2(p, (sx, sy)) / 2.0)
                        move = (ux * k, uy * k)
                        mode = "ring"
                # boost-strafe when under fire and we can afford it (keep energy for lightning)
                reserve = 35 if "lightning" in kinds else 20
                boost = energy > reserve and (recent_dmg > 0 or dist < 160)
                # dodge an aimed laser: if they charge at us, break the line sideways hard
                charging = [m for dd, m in threats if m.get("charging") and dd < 290]
                if in_ring:
                    boost = False
                    charging = []  # never leave the ring mid-capture
                if charging:
                    # an enemy laser is charging: get out of its line of sight if cover is a short dash away
                    cov = self.find_cover(obs, me, [m["pos"] for m in charging], t, radius=22.0, short=True)
                    if cov is not None:
                        cv = self.steer(me, cov, t)
                        move = cv
                        mode = "peek"
                    else:
                        move = (px_ * 1.0, py_ * 1.0)
                    boost = energy > 5
        else:
            # no one in sight
            goal = None
            mode = "roam"
            if zone_goal is not None and urgency > 0.6:
                goal = zone_goal
                mode = "zone"
            if dgoal is not None and (urgency <= 0.6 or zone_goal is None):
                goal = dgoal
                mode = "drop"
            if goal is None and hp_frac < 0.75:
                recent = [tr.pos for tr in self.tracks.values() if t - tr.seen_t < 25.0]
                if recent:
                    cov = self.find_cover(obs, me, recent, t)
                    if cov is not None:
                        goal = cov
                        mode = "heal"
            if goal is None:
                goal, mode = self.loot_goal(obs, me, kinds, hp_frac)
            if goal is None and zone_goal is not None:
                goal = zone_goal
                mode = "zone"
            if goal is None:
                # hold a spot inside the next circle; drift toward its centre-ish when far away
                z = obs["zone"]
                nc, nr = z["next_center"], z["next_radius"]
                if d2(p, nc) > nr * 0.6:
                    goal = (nc[0], nc[1])
                    mode = "center"
            if goal is not None:
                move = self.steer(me, goal, t)
                boost = urgency > 0.7 and energy > 20
            # healing: walk slowly (silent under 5 m/s) when hurt and not in a hurry
            if goal is None or (hp_frac < 0.7 and urgency < 0.5 and mode != "zone"):
                if goal is None:
                    move = (0.0, 0.0)

        # unstick
        want_move = math.hypot(*move) > 0.3
        if self.check_stuck(me, want_move, t) or t < self.unstick_until:
            if t < self.jet_until:
                jump = me["grounded"]
                boost = energy > 5
                if move == (0.0, 0.0):
                    move = self.unstick_dir
            else:
                move = (move[0] * 0.3 + self.unstick_dir[0], move[1] * 0.3 + self.unstick_dir[1])
        # in the air with jets: keep boosting only if we're climbing over something
        if not me["grounded"] and t < self.jet_until:
            boost = energy > 3

        mx, my = move
        n = math.hypot(mx, my)
        if n > 1:
            mx, my = mx / n, my / n
        act["move"] = [round(mx, 3), round(my, 3)]
        act["boost"] = bool(boost and n > 0.1)
        if jump:
            act["jump"] = True

        # -------------------------------------------------- aim and fire
        self.aim_and_fire(obs, me, act, target, kinds, energy)

        # -------------------------------------------------- loot actions (take weapons, swap)
        self.loot_actions(obs, me, act, kinds, target)

        self.mode = mode
        return act

    # ------------------------------------------------------------------ loot
    def loot_goal(self, obs, me, kinds, hp_frac):
        p = me["pos"]
        t = obs["t"]
        best = None
        cur_rank = WEAPON_RANK.get(kinds[1], -1) if len(kinds) > 1 else -1
        want_weapon = cur_rank < WEAPON_RANK["laser"]
        for pid, (kind, pos, seen) in self.known_pickups.items():
            if t - self.bad_pickups.get(pid, -99) < 5:
                continue
            if kind in kinds:
                continue
            r = WEAPON_RANK.get(kind, 0)
            if r <= cur_rank:
                continue
            dd = d2(p, pos)
            reach = 260 if cur_rank < 0 else 160
            if dd > reach or abs(pos[2] - p[2]) > 6:
                continue
            if not self.in_current(obs, pos, 10):
                continue
            s = dd - r * 30 - (60 if cur_rank < 0 else 0)
            if best is None or s < best[0]:
                best = (s, (pos[0], pos[1]), "loot")
        for cid, (pos, seen) in self.known_cubes.items():
            dd = d2(p, pos)
            if dd > 140 or abs(pos[2] - p[2]) > 6 or not self.in_current(obs, pos, 8):
                continue
            s = dd - 20
            if best is None or s < best[0]:
                best = (s, (pos[0], pos[1]), "cube")
        # crates: a cube each and often a weapon; walk into gun range of one we remember
        if hp_frac > 0.5:
            for cid, (pos, seen) in self.known_crates.items():
                dd = d2(p, pos)
                if dd > 200 or abs(pos[2] - p[2]) > 6 or not self.in_current(obs, pos, 10):
                    continue
                if dd < 35:
                    continue  # close enough to shoot it (alt_target does that)
                s = dd + (0 if want_weapon else 40)
                if best is None or s < best[0]:
                    best = (s, (pos[0], pos[1]), "crate")
        for dr in obs.get("drops") or []:
            dd = d2(p, dr["pos"])
            if dd > 220 or dr.get("lands_in", 0) > 12:
                continue
            if dr.get("holder") is not None and dr.get("holder") != me["id"]:
                continue
            s = dd - 60 - (1 - hp_frac) * 120
            if best is None or s < best[0]:
                best = (s, (dr["pos"][0], dr["pos"][1]), "drop")
        if best is None:
            return None, "roam"
        return best[1], best[2]

    def drop_goal(self, obs, me):
        """Supply drop worth heading for now (full HP + 3 cubes): leave so we arrive as it lands."""
        p = me["pos"]
        speed = me["stats"].get("walk_speed", 11.7) * 0.8
        best = None
        for dr in obs.get("drops") or []:
            hold = dr.get("holder")
            if hold is not None and hold != me["id"]:
                continue
            q = dr["pos"]
            dd = d2(p, q)
            if dd > 320 or not self.in_current(obs, q, 5.0):
                continue
            eta = dd / max(1.0, speed)
            if dr.get("lands_in", 0) - eta > 6.0:
                continue
            if best is None or dd < best[0]:
                best = (dd, (q[0], q[1]), dr)
        if best is None:
            return None
        dd, q, dr = best
        # don't stand in the open ring before it lands: wait out of sight close by, then rush in
        rush = me["stats"].get("boost_speed", 19.5) * 0.8
        if dr.get("lands_in", 0) > dd / rush + 0.8 and dd < 90:
            st = self.stage_spot(obs, me, q)
            if st is not None:
                return st
        return q

    def stage_spot(self, obs, me, q):
        t = obs["t"]
        if getattr(self, "stage_t", -99) > t - 1.0 and getattr(self, "stage_q", None) == q:
            return self.stage_pt
        if self.spent() > 0.006 or not self.arena.ok:
            return getattr(self, "stage_pt", None) if getattr(self, "stage_q", None) == q else None
        A = self.arena
        p = me["pos"]
        th = sorted((tr.pos for tr in self.tracks.values() if t - tr.seen_t < 10.0), key=lambda z: d2(q, z))[:4]
        best = None
        for r in (14.0, 24.0, 36.0):
            for k in range(14):
                if self.spent() > 0.020:
                    break  # time guard: keep well inside the 50 ms budget
                a = TAU * k / 14
                x, y = q[0] + r * math.cos(a), q[1] + r * math.sin(a)
                c = A.ncell(x, y)
                if c < 0 or A.nblocked[c] or not self.in_current(obs, (x, y), 8.0):
                    continue
                gz = A.ground(x, y)
                seen = sum(1 for z in th if A.los((z[0], z[1], z[2] + 8.0), (x, y, gz + 7.0)))
                # walls nearby make a spot safer even against enemies we have not seen yet
                open_dirs = 0
                for j in range(6):
                    b = TAU * j / 6
                    if A.los((x, y, gz + 7.0), (x + 60 * math.cos(b), y + 60 * math.sin(b), gz + 7.0)):
                        open_dirs += 1
                sc = seen * 1000 + open_dirs * 25 + r + 0.3 * d2(p, (x, y))
                if best is None or sc < best[0]:
                    best = (sc, (x, y))
        self.stage_t, self.stage_q = t, q
        self.stage_pt = None if best is None else best[1]
        return self.stage_pt

    def fight_grab(self, obs, me, kinds, tdist):
        p = me["pos"]
        t = obs["t"]
        cur_rank = WEAPON_RANK.get(kinds[1], -1) if len(kinds) > 1 else -1
        best = None
        lim = 70.0 if cur_rank < 0 else 35.0
        for pid, (kind, pos, seen) in self.known_pickups.items():
            if kind in kinds or WEAPON_RANK.get(kind, 0) <= cur_rank or t - self.bad_pickups.get(pid, -99) < 5:
                continue
            dd = d2(p, pos)
            if dd < lim and abs(pos[2] - p[2]) < 4 and (best is None or dd < best[0]):
                best = (dd, pos)
        for cid, (pos, seen) in self.known_cubes.items():
            dd = d2(p, pos)
            if dd < 25 and abs(pos[2] - p[2]) < 4 and (best is None or dd < best[0]):
                best = (dd, pos)
        if best is None:
            return None
        return (best[1][0], best[1][1])

    def loot_actions(self, obs, me, act, kinds, target):
        p = me["pos"]
        t = obs["t"]
        reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        best = None
        for pk in obs["pickups"]:
            if t - self.bad_pickups.get(pk["id"], -99) < 3:
                continue
            if d2(p, pk["pos"]) > reach - 0.3 or abs(pk["pos"][2] - p[2]) > 3.8:
                continue
            k = pk["kind"]
            if k in kinds:
                continue
            r = WEAPON_RANK.get(k, 0)
            if r < 0:
                continue
            if best is None or r > best[0]:
                best = (r, pk)
        if best is None:
            return
        r, pk = best
        w1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
        w0 = me["weapons"][0]
        if w0 is None:
            act["take"] = pk["id"]
            return
        if w1 is None:
            act["take"] = pk["id"]
            return
        # both full: swap slot 1 if this one is better, unless its laser is charging
        if r > WEAPON_RANK.get(w1["kind"], 0) and not (w1["kind"] == "laser" and w1.get("charge", 0) > 0):
            # don't swap mid-fight against a close target (equip delay)
            if target is None or d2(p, target["pos"]) > 60:
                act["take"] = pk["id"]
                act["drop"] = 1

    # ------------------------------------------------------------------ aiming and firing
    def aim_and_fire(self, obs, me, act, target, kinds, energy):
        p = me["pos"]
        t = obs["t"]
        cur = me["aim"]
        fire = [False, False]
        aimv = None
        A = self.arena
        eye = (p[0], p[1], p[2] + me["stats"].get("eye_height", 8.0))
        muzzle = (p[0], p[1], p[2] + me["stats"].get("muzzle_height", 7.0))

        # NPC or crate as a fallback target when nothing else is around
        alt = None
        if target is None:
            alt = self.alt_target(obs, me)

        tgt = target or alt
        if tgt is not None:
            tp = tgt["pos"]
            tv = tgt.get("vel", (0, 0, 0))
            dist = d2(p, tp)
            gun = None
            gun_i = None
            for i, w in enumerate(me["weapons"]):
                if w and w["kind"] == "gun":
                    gun, gun_i = w, i
            speed = gun["stats"].get("speed", 252.0) if gun else 252.0
            # gun: lead by bullet flight time
            yaw, pitch, hd, aimpt = self.lead(me, tp, tv, speed)
            ang_err = math.hypot(abs(wrap(cur[0] - yaw)), abs(cur[1] - pitch))
            # laser: hitscan, so aim at where the target is when the beam fires (one decision ahead, ~0.05 s)
            lyaw, lpitch, lhd, _ = self.hitscan_aim(me, tp, tv, 0.05)
            l_err = math.hypot(abs(wrap(cur[0] - lyaw)), abs(cur[1] - lpitch))
            laser = None
            for w in me["weapons"]:
                if w and w["kind"] == "laser":
                    laser = w
            laser_first = (laser is not None and tgt.get("kind") in ("mech", "npc")
                           and dist < laser["stats"].get("range", 280) - 10
                           and (laser.get("charge", 0) > 0 or laser.get("cooldown", 0) < 0.4))
            aimv = [lyaw, lpitch] if laser_first else [yaw, pitch]
            radius = 2.5 if tgt.get("kind") == "mech" else (1.8 if tgt.get("kind") == "npc" else 1.4)
            tol = max(0.006, (radius * 0.7) / max(1.0, hd))
            ltol = max(0.005, (radius * 0.6) / max(1.0, lhd))
            center = (tp[0], tp[1], tp[2] + (4.5 if tgt.get("kind") == "mech" else 1.5))
            clear = A.los(muzzle, center) or A.los(muzzle, (tp[0], tp[1], tp[2] + 8.0))
            for i, w in enumerate(me["weapons"]):
                if not w:
                    continue
                k = w["kind"]
                st = w.get("stats", {})
                if k == "gun":
                    rng_ = st.get("range", 230)
                    tspd = math.hypot(tv[0], tv[1])
                    reach_ok = dist < 125 or tspd < 4.0 or (dist < 175 and tspd < 9.0) or tgt.get("kind") != "mech"
                    if dist < rng_ - 5 and reach_ok and clear and ang_err < tol * 2.5 and w.get("reload", 0) <= 0 and w.get("ammo", 1) > 0:
                        fire[i] = True
                elif k == "lightning":
                    rng_ = st.get("range", 45)
                    cone = math.radians(st.get("cone_deg", 30)) * 0.5
                    if dist < rng_ - 1.5 and abs(wrap(cur[0] - math.atan2(tp[1] - p[1], tp[0] - p[0]))) < cone * 0.85 \
                            and energy > st.get("energy", 3) + 4 and clear:
                        fire[i] = True
                elif k == "laser":
                    full = st.get("charge", 0.75)
                    ch = w.get("charge", 0.0)
                    rng_ = st.get("range", 280)
                    if tgt.get("kind") != "mech" and tgt.get("kind") != "npc":
                        continue  # don't waste the laser on crates
                    if ch > 0:
                        # charging: keep holding until full; release when the hitscan aim is on target
                        # (or just before the hold limit, when it would fire on its own anyway)
                        if ch < full or (not (clear and l_err < ltol) and ch < full + 1.6):
                            fire[i] = True
                        else:
                            fire[i] = False
                    elif w.get("cooldown", 0) <= 0.05 and dist < rng_ - 10 and clear and l_err < 0.25:
                        fire[i] = True
                elif k == "mortar":
                    if tgt.get("kind") != "mech":
                        continue
                    if 30 < dist < 220 and w.get("cooldown", 0) <= 0.05:
                        ma = self.mortar_aim(me, tp, tv)
                        if ma is not None and not clear:
                            # lob over cover: aim with the mortar instead of the gun
                            aimv = [ma[0], ma[1]]
                            if abs(wrap(cur[0] - ma[0])) < 0.03 and abs(cur[1] - ma[1]) < 0.03:
                                fire[i] = True
                        elif ma is not None and clear:
                            # in the open: fire the low arc when the gun aim roughly agrees
                            if abs(wrap(cur[0] - ma[0])) < 0.03 and abs(cur[1] - ma[1]) < 0.04:
                                fire[i] = True
            # reload during a lull
            if gun and gun.get("ammo", 12) == 0 and gun.get("reload", 0) <= 0:
                act["reload"] = True
        else:
            # nothing to shoot: look toward danger, otherwise scan
            look = None
            if t - self.last_damage_t < 3.0 and self.last_damage_bearing is not None:
                look = self.last_damage_bearing
            elif t - self.sound_t < 2.0 and self.sound_yaw is not None:
                look = self.sound_yaw
            else:
                # most recently seen enemy
                rec = [tr for tr in self.tracks.values() if t - tr.seen_t < 12.0]
                if rec:
                    tr = max(rec, key=lambda r: r.seen_t)
                    look = math.atan2(tr.pos[1] - p[1], tr.pos[0] - p[0])
            if look is None:
                if self.scan_yaw is None:
                    self.scan_yaw = cur[0]
                # sweep: our FOV is 120 deg; rotate steadily so every direction is covered every ~4 s
                self.scan_yaw = wrap(self.scan_yaw + 0.16)
                look = self.scan_yaw
            else:
                self.scan_yaw = look
            aimv = [look, 0.0]
            for i, w in enumerate(me["weapons"]):
                if w and w["kind"] == "gun" and w.get("ammo", 12) < w["stats"].get("mag", 12) and w.get("reload", 0) <= 0:
                    act["reload"] = True
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = True  # keep holding a charge we started (it will auto-fire eventually)
        if aimv is not None:
            act["aim"] = [round(aimv[0], 4), round(max(-0.69, min(1.29, aimv[1])), 4)]
        act["fire"] = fire

    def alt_target(self, obs, me):
        """An NPC drone or crate worth shooting when no mech is in sight."""
        p = me["pos"]
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        best = None
        if hp_frac > 0.6:
            for m in obs["mechs"]:
                if m.get("kind") != "npc":
                    continue
                dd = d2(p, m["pos"])
                if dd < 110:
                    s = dd
                    if best is None or s < best[0]:
                        best = (s, m)
        if best is None:
            for c in obs["crates"]:
                dd = d2(p, c["pos"])
                if dd < 90:
                    s = dd + 30
                    if best is None or s < best[0]:
                        best = (s, {"pos": c["pos"], "vel": (0, 0, 0), "kind": "crate"})
        return best[1] if best else None


# ---------------------------------------------------------------------- protocol loop
def send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def reader(q):
    for line in sys.stdin:
        if line.strip():
            q.put(line)
    q.put(None)


def main():
    q = queue.Queue()
    threading.Thread(target=reader, args=(q,), daemon=True).start()
    bot = None
    last = {}
    errors = 0
    while True:
        batch = [q.get()]
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest = None
        for raw in batch:
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "hello":
                try:
                    bot = Bot(msg)
                except Exception:
                    log(traceback.format_exc())
                # the map is built once: move it out of the GC's way so collections stay short
                gc.collect()
                gc.freeze()
                gc.set_threshold(50000, 50, 100)
                send({"type": "ready", "name": NAME})
            elif kind == "obs":
                latest = msg
            elif kind == "end":
                log(f"end: place {msg.get('place')} kills {msg.get('kills')}")
                return
        if latest is None:
            continue
        action = {}
        if bot is not None:
            try:
                action = bot.act(latest) or {}
                last = {k: action[k] for k in ("move", "aim", "boost") if k in action}
            except Exception:
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log(f"act failed at tick {latest.get('tick')}:\n{traceback.format_exc()}")
                action = dict(last)
        action["tick"] = latest["tick"]
        try:
            send(action)
        except (TypeError, ValueError):
            send({"tick": latest["tick"], **last})


if __name__ == "__main__":
    main()

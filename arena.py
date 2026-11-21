"""Static map model: terrain heights, building footprints, a coarse nav grid, line-of-sight and A*.

Everything is built once at `hello` from /arena (v2/world.json + v2/terrain.i16). If the map can't be
read, `Arena.ok` is False and the bot falls back to steering without a map.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import sys
from array import array

FINE = 2.0     # meters per cell of the occupancy grid (LOS, walls)
NAV = 4.0      # meters per cell of the nav grid (A*)
HEAD = 9.0     # mech height: anything with a base lower than ground + HEAD is a wall


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Arena:
    def __init__(self, mapdir: str, files=None):
        self.ok = False
        try:
            self._load(mapdir)
            self.ok = True
        except Exception as e:  # never die at hello: the bot can still play without a map
            log("arena load failed:", repr(e))

    # ------------------------------------------------------------------ loading
    def _load(self, mapdir: str):
        with open(os.path.join(mapdir, "v2", "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        self.tnx, self.tny = int(t["nx"]), int(t["ny"])
        self.tminx, self.tminy = float(t["minX"]), float(t["minY"])
        self.tstep = float(t.get("step", 1))
        scale = float(t.get("scale", 0.01))
        raw = array("h")
        with open(os.path.join(mapdir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            raw.frombytes(f.read())
        if sys.byteorder != "little":
            raw.byteswap()
        self.terr = raw
        self.tscale = scale
        b = w["bounds"]
        self.minx, self.miny, self.maxx, self.maxy = b["minX"], b["minY"], b["maxX"], b["maxY"]

        # fine grid
        self.fx = int(math.ceil((self.maxx - self.minx) / FINE)) + 1
        self.fy = int(math.ceil((self.maxy - self.miny) / FINE)) + 1
        fx, fy = self.fx, self.fy
        ground = array("f", [0.0]) * (fx * fy)
        for j in range(fy):
            y = self.miny + (j + 0.5) * FINE
            row = j * fx
            for i in range(fx):
                ground[row + i] = self.ground(self.minx + (i + 0.5) * FINE, y)
        self.fground = ground
        # top of the highest wall in each cell (-1e9 = open)
        top = array("f", [-1e9]) * (fx * fy)
        for p in w["prisms"]:
            self._raster(p, top, ground)
        self.ftop = top

        # nav grid: blocked where a wall (inflated by the mech radius) or a steep slope is
        k = int(NAV / FINE)
        self.nx = (fx + k - 1) // k
        self.ny = (fy + k - 1) // k
        nx, ny = self.nx, self.ny
        blocked = bytearray(nx * ny)
        nground = array("f", [0.0]) * (nx * ny)
        wall = bytearray(fx * fy)
        for c in range(fx * fy):
            if top[c] - ground[c] > 1.2:
                wall[c] = 1
        # inflate fine walls by 1 cell (2 m) then map to nav cells
        infl = bytearray(fx * fy)
        for j in range(fy):
            for i in range(fx):
                if wall[j * fx + i]:
                    for dj in (-1, 0, 1):
                        jj = j + dj
                        if 0 <= jj < fy:
                            for di in (-1, 0, 1):
                                ii = i + di
                                if 0 <= ii < fx:
                                    infl[jj * fx + ii] = 1
        for J in range(ny):
            for I in range(nx):
                cnt = 0
                tot = 0
                gs = 0.0
                for dj in range(k):
                    j = J * k + dj
                    if j >= fy:
                        continue
                    for di in range(k):
                        i = I * k + di
                        if i >= fx:
                            continue
                        tot += 1
                        cnt += infl[j * fx + i]
                        gs += ground[j * fx + i]
                c = J * nx + I
                nground[c] = gs / max(1, tot)
                # blocked if more than a quarter of it is (inflated) wall
                if tot == 0 or cnt * 4 > tot:
                    blocked[c] = 1
        # map edge margin
        for J in range(ny):
            for I in range(nx):
                if I < 2 or J < 2 or I >= nx - 2 or J >= ny - 2:
                    blocked[J * nx + I] = 1
        self.nblocked = blocked
        self.nground = nground
        self.fwall = wall

    def _raster(self, p, top, ground):
        """Scanline-fill a prism's ring (minus holes) into the fine grid."""
        ring = p["ring"]
        if len(ring) < 6:
            return
        polys = [ring] + [h for h in p.get("holes") or [] if len(h) >= 6]
        base, ptop = float(p["base"]), float(p["top"])
        xs = ring[0::2]
        ys = ring[1::2]
        j0 = max(0, int((min(ys) - self.miny) / FINE))
        j1 = min(self.fy - 1, int((max(ys) - self.miny) / FINE))
        fx = self.fx
        for j in range(j0, j1 + 1):
            y = self.miny + (j + 0.5) * FINE
            cross = []
            for poly in polys:
                n = len(poly) // 2
                for a in range(n):
                    x1, y1 = poly[2 * a], poly[2 * a + 1]
                    b = (a + 1) % n
                    x2, y2 = poly[2 * b], poly[2 * b + 1]
                    if (y1 <= y < y2) or (y2 <= y < y1):
                        cross.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            cross.sort()
            for a in range(0, len(cross) - 1, 2):
                i0 = max(0, int(math.ceil((cross[a] - self.minx) / FINE - 0.5)))
                i1 = min(fx - 1, int(math.floor((cross[a + 1] - self.minx) / FINE - 0.5)))
                for i in range(i0, i1 + 1):
                    c = j * fx + i
                    if base < ground[c] + HEAD and ptop > top[c]:
                        top[c] = ptop

    # ------------------------------------------------------------------ queries
    def ground(self, x: float, y: float) -> float:
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = int(math.floor(fx))
        j = int(math.floor(fy))
        if i < 0:
            i, fx = 0, 0.0
        if j < 0:
            j, fy = 0, 0.0
        if i >= self.tnx - 1:
            i, fx = self.tnx - 2, float(self.tnx - 1)
        if j >= self.tny - 1:
            j, fy = self.tny - 2, float(self.tny - 1)
        u = fx - i
        v = fy - j
        T = self.terr
        nx = self.tnx
        a = T[j * nx + i]
        b = T[j * nx + i + 1]
        c = T[(j + 1) * nx + i]
        d = T[(j + 1) * nx + i + 1]
        return ((a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v) * self.tscale

    def fcell(self, x, y):
        i = int((x - self.minx) / FINE)
        j = int((y - self.miny) / FINE)
        if 0 <= i < self.fx and 0 <= j < self.fy:
            return j * self.fx + i
        return -1

    def solid_at(self, x, y, z) -> bool:
        c = self.fcell(x, y)
        if c < 0:
            return True
        return z < self.ftop[c] or z < self.fground[c]

    def los(self, a, b, step=2.0) -> bool:
        """True if the segment a->b (3D points) clears buildings and terrain (coarse)."""
        if not self.ok:
            return True
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = int(d / step)
        if n < 2:
            return True
        ftop, fg = self.ftop, self.fground
        minx, miny, fxn = self.minx, self.miny, self.fx
        for s in range(1, n):
            t = s / n
            x = a[0] + dx * t
            y = a[1] + dy * t
            z = a[2] + dz * t
            i = int((x - minx) / FINE)
            j = int((y - miny) / FINE)
            if i < 0 or j < 0 or i >= fxn or j >= self.fy:
                return False
            c = j * fxn + i
            if z < ftop[c] - 0.3 or z < fg[c] - 0.5:
                return False
        return True

    def walk_clear(self, a, b) -> bool:
        """Straight walk a->b (2D) crosses no nav-blocked cell."""
        if not self.ok:
            return True
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy)
        n = max(1, int(d / 2.0))
        for s in range(1, n + 1):
            t = s / n
            c = self.ncell(a[0] + dx * t, a[1] + dy * t)
            if c < 0 or self.nblocked[c]:
                return False
        return True

    def ncell(self, x, y):
        I = int((x - self.minx) / NAV)
        J = int((y - self.miny) / NAV)
        if 0 <= I < self.nx and 0 <= J < self.ny:
            return J * self.nx + I
        return -1

    def ncenter(self, c):
        J, I = divmod(c, self.nx)
        return (self.minx + (I + 0.5) * NAV, self.miny + (J + 0.5) * NAV)

    def nearest_free(self, c, maxr=6):
        if c >= 0 and not self.nblocked[c]:
            return c
        if c < 0:
            return -1
        J0, I0 = divmod(c, self.nx)
        for r in range(1, maxr + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    I, J = I0 + di, J0 + dj
                    if 0 <= I < self.nx and 0 <= J < self.ny:
                        cc = J * self.nx + I
                        if not self.nblocked[cc]:
                            return cc
        return -1

    def astar(self, start, goal, max_exp=3500, avoid=None):
        """A* on the nav grid from 2D start to goal. Returns a list of 2D waypoints or None.
        `avoid(x, y) -> extra cost` can weight cells (e.g. near enemies)."""
        if not self.ok:
            return None
        s = self.nearest_free(self.ncell(*start))
        g = self.nearest_free(self.ncell(*goal))
        if s < 0 or g < 0:
            return None
        if s == g:
            return [tuple(goal)]
        nx = self.nx
        blocked = self.nblocked
        ng = self.nground
        gJ, gI = divmod(g, nx)
        SQ2 = 1.41421356
        def h(c):
            J, I = divmod(c, nx)
            a = abs(I - gI)
            b = abs(J - gJ)
            return (a + b) + (SQ2 - 2) * min(a, b)
        dist = {s: 0.0}
        prev = {}
        openq = [(h(s), s)]
        closed = set()
        exp = 0
        best, bestc = h(s), s
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))
        found = False
        while openq:
            f, c = heapq.heappop(openq)
            if c in closed:
                continue
            if c == g:
                found = True
                break
            closed.add(c)
            exp += 1
            if exp > max_exp:
                break
            J, I = divmod(c, nx)
            dc = dist[c]
            gc = ng[c]
            for di, dj, w in nbrs:
                I2, J2 = I + di, J + dj
                if I2 < 0 or J2 < 0 or I2 >= nx or J2 >= self.ny:
                    continue
                c2 = J2 * nx + I2
                if blocked[c2] or c2 in closed:
                    continue
                if di and dj and (blocked[J * nx + I2] or blocked[J2 * nx + I]):
                    continue
                dh = ng[c2] - gc
                if dh > 3.5 * w:  # too steep to walk up
                    continue
                cost = w + (0.15 * dh if dh > 0 else 0.0)
                if avoid is not None:
                    cost += avoid(c2)
                nd = dc + cost
                if nd < dist.get(c2, 1e18):
                    dist[c2] = nd
                    prev[c2] = c
                    hh = h(c2)
                    heapq.heappush(openq, (nd + hh, c2))
                    if hh < best:
                        best, bestc = hh, c2
        end = g if found else bestc
        if end == s:
            return None
        cells = [end]
        while cells[-1] in prev:
            cells.append(prev[cells[-1]])
        cells.reverse()
        pts = [self.ncenter(c) for c in cells[1:]]
        if found:
            pts[-1] = (goal[0], goal[1])
        return self._smooth(start, pts)

    def _smooth(self, start, pts):
        """Drop waypoints that can be skipped with a clear straight walk."""
        if not pts:
            return pts
        out = []
        cur = start
        i = 0
        while i < len(pts):
            j = min(len(pts) - 1, i + 12)
            while j > i and not self.walk_clear(cur, pts[j]):
                j -= 1
            out.append(pts[j])
            cur = pts[j]
            i = j + 1
        return out

"""FarmerOne: a survive-first fighter for mechfight/1.

Ideas:
- Build a 2 m height grid of the map (terrain + buildings) for line of sight and walking.
- Stay inside the zone, loot weapons and cubes while nobody is around.
- Fight with a leading gun, plus laser or lightning, while strafing to dodge.
- Back off when hurt, face attackers (rear hits do 1.5x), and get unstuck with jets.
"""
import json
import math
import os
import random
import array

import mechfight as mf

CELL = 2.0
TAU = math.tau


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def norm2(x, y):
    d = math.hypot(x, y)
    if d < 1e-6:
        return 0.0, 0.0
    return x / d, y / d


def pip(px, py, ring):
    inside = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class Map:
    def __init__(self, map_dir):
        self.ok = False
        try:
            self._load(map_dir)
            self.ok = True
        except Exception as e:  # never die on the map; play blind instead
            mf.log("map load failed:", repr(e))

    def _load(self, map_dir):
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        tnx, tny, tminx, tminy, tstep = t["nx"], t["ny"], t["minX"], t["minY"], t["step"]
        terr = array.array("h")
        with open(os.path.join(map_dir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            terr.frombytes(f.read())
        if array.array("h", [1]).tobytes() != b"\x01\x00":
            terr.byteswap()
        scale = t.get("scale", 0.01)
        b = w["bounds"]
        self.minx, self.miny = b["minX"], b["minY"]
        self.maxx, self.maxy = b["maxX"], b["maxY"]
        self.nx = int((self.maxx - self.minx) / CELL) + 1
        self.ny = int((self.maxy - self.miny) / CELL) + 1
        nx, ny = self.nx, self.ny
        ground = array.array("f", [0.0]) * (nx * ny)
        for j in range(ny):
            y = self.miny + (j + 0.5) * CELL
            tj = min(tny - 1, max(0, int(round((y - tminy) / tstep))))
            row = tj * tnx
            for i in range(nx):
                x = self.minx + (i + 0.5) * CELL
                ti = min(tnx - 1, max(0, int(round((x - tminx) / tstep))))
                ground[j * nx + i] = terr[row + ti] * scale
        surf = array.array("f", ground)
        for p in w["prisms"]:
            ring = p["ring"]
            holes = p.get("holes") or []
            xs, ys = ring[0::2], ring[1::2]
            i0 = max(0, int((min(xs) - self.minx) / CELL))
            i1 = min(nx - 1, int((max(xs) - self.minx) / CELL))
            j0 = max(0, int((min(ys) - self.miny) / CELL))
            j1 = min(ny - 1, int((max(ys) - self.miny) / CELL))
            base, top = p["base"], p["top"]
            for j in range(j0, j1 + 1):
                y = self.miny + (j + 0.5) * CELL
                for i in range(i0, i1 + 1):
                    x = self.minx + (i + 0.5) * CELL
                    if not pip(x, y, ring):
                        continue
                    if any(pip(x, y, h) for h in holes):
                        continue
                    k = j * nx + i
                    # a bridge high enough to walk under is not a wall
                    if base > ground[k] + 9.5:
                        continue
                    if top > surf[k]:
                        surf[k] = top
        self.surf = surf

    def h(self, x, y):
        if not self.ok:
            return -1e9
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return 1e9
        return self.surf[j * self.nx + i]

    def los(self, a, b):
        """True if the straight line a->b (3D) clears buildings and terrain."""
        if not self.ok:
            return True
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = int(d / 1.5)
        if n < 2:
            return True
        for s in range(1, n):
            f = s / n
            if f * d < 3.0 or (1 - f) * d < 3.0:
                continue
            if a[2] + dz * f < self.h(a[0] + dx * f, a[1] + dy * f) - 0.2:
                return False
        return True

    def walkable(self, x, y, z):
        if x < self.minx + 6 or x > self.maxx - 6 or y < self.miny + 6 or y > self.maxy - 6:
            return False
        return self.h(x, y) <= z + 1.1


UPGRADE_PRIORITY = [
    "plating", "armor", "nanites", "laser_damage", "gun_rate", "gun_damage", "laser_charge",
    "lightning_efficiency", "gyros", "gun_velocity", "lightning_range", "servos", "reactor",
    "capacitor", "gun_mag", "jets", "lightning_chain", "sensors", "laser_pierce",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]
WANT_WEAPON = {"laser": 3, "lightning": 2}


class Bot:
    name = "FarmerOne-RAT"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.map = Map(hello.get("map", {}).get("dir", "/arena"))
        random.seed(hello.get("seed", 1))
        self.reach = cfg["loot"]["pickup_radius"]
        self.strafe_sign = 1
        self.strafe_until = 0.0
        self.hist = []
        self.stuck_until = 0.0
        self.stuck_dir = None
        self.look_bearing = None
        self.look_until = 0.0
        self.last_dmg_t = -99.0
        self.steer_bias = 1
        self.prev_hp = None
        self.cover = None
        self.cover_t = -99.0
        self.hold = None
        self.hold_t = -99.0

    # ---------- helpers ----------
    def zone_goal(self, obs, me):
        z = obs["zone"]
        x, y = me["pos"][0], me["pos"][1]
        t = obs["t"]
        cx, cy = z["center"]
        nx_, ny_ = z["next_center"]
        r, nr = z["radius"], z["next_radius"]
        dcur = math.hypot(x - cx, y - cy)
        dnext = math.hypot(x - nx_, y - ny_)
        speed = me["stats"].get("walk_speed", 11.7)
        # time we have before the circle we are in no longer covers us
        need = max(0.0, dnext - nr * 0.65) / (speed * 0.7)
        urgent = (not me["in_zone"]) or dcur > r - 12
        soon = t + need + 15 > z["shrink_start"]
        if urgent:
            return (nx_, ny_) if dnext < dcur + 50 else (cx, cy), True
        if soon and dnext > nr * 0.65:
            return (nx_, ny_), dnext > nr * 0.9
        return None, False

    def steer(self, me, dx, dy):
        """Pick a walkable direction near (dx, dy)."""
        if not self.map.ok or (dx == 0 and dy == 0):
            return dx, dy, False
        x, y, z = me["pos"]
        base = math.atan2(dy, dx)
        for off_deg in (0, 20, 40, 60, 85, 110, 140, 170):
            for sgn in ((self.steer_bias, -self.steer_bias) if off_deg else (1,)):
                a = base + math.radians(off_deg) * sgn
                c, s = math.cos(a), math.sin(a)
                ok = True
                zz = z
                for d in (3.0, 6.0, 10.0):
                    px, py = x + c * d, y + s * d
                    if not self.map.walkable(px, py, zz):
                        ok = False
                        break
                    zz = max(self.map.h(px, py), zz - 6)
                if ok:
                    if off_deg:
                        self.steer_bias = sgn
                    return c, s, off_deg > 0
        return dx, dy, True

    def find_cover(self, obs, me, threats):
        """A nearby walkable spot, inside the zone, that the threats cannot see."""
        if not self.map.ok:
            return None
        x, y, z = me["pos"]
        best, best_s = None, -1e9
        tx = sum(e[0]["pos"][0] for e in threats) / len(threats)
        ty = sum(e[0]["pos"][1] for e in threats) / len(threats)
        away = math.atan2(y - ty, x - tx)
        for k in range(16):
            a = k * TAU / 16
            for d in (14.0, 26.0, 40.0):
                px, py = x + math.cos(a) * d, y + math.sin(a) * d
                gz = self.map.h(px, py)
                if gz > z + 1.1 or gz < z - 8:
                    continue
                if not self.inside_safe(obs, px, py, 15):
                    continue
                hidden = 0
                for e in threats:
                    m = e[0]
                    eye = [m["pos"][0], m["pos"][1], m["pos"][2] + 8]
                    if not self.map.los(eye, [px, py, gz + 4.5]):
                        hidden += 1
                s = hidden * 100 - d * 0.6 + 25 * math.cos(wrap(a - away))
                if s > best_s:
                    best, best_s = (px, py, hidden), s
        if best and best[2] > 0:
            return best[:2]
        return None

    def find_hold(self, obs, me):
        """A corner spot near the zone's next circle: walls on some sides, open on others."""
        if not self.map.ok:
            return None
        x, y, z = me["pos"]
        zn = obs["zone"]
        ncx, ncy = zn["next_center"]
        nr = zn["next_radius"]
        best, best_s = None, -1e9
        rng = random.Random(int(obs["t"] * 10))
        for _ in range(70):
            # half near us, half near the next circle's middle
            if rng.random() < 0.5:
                a, r = rng.uniform(0, TAU), rng.uniform(5, 90)
                px, py = x + math.cos(a) * r, y + math.sin(a) * r
            else:
                a, r = rng.uniform(0, TAU), rng.uniform(0, nr * 0.6)
                px, py = ncx + math.cos(a) * r, ncy + math.sin(a) * r
            if math.hypot(px - ncx, py - ncy) > nr * 0.7 or not self.inside_safe(obs, px, py, 20):
                continue
            gz = self.map.h(px, py)
            if abs(gz - z) > 6:
                continue
            blocked, opens = 0, []
            for k in range(16):
                a = k * TAU / 16
                c, s = math.cos(a), math.sin(a)
                hit = False
                for d in (3.0, 5.0, 7.5, 10.0):
                    if self.map.h(px + c * d, py + s * d) > gz + 6:
                        hit = True
                        break
                if hit:
                    blocked += 1
                else:
                    opens.append(a)
            if blocked < 7 or blocked > 13:
                continue
            s_ = -abs(blocked - 10) * 8 - math.hypot(px - x, py - y) * 0.35 - math.hypot(px - ncx, py - ncy) * 0.1
            if s_ > best_s:
                ox = sum(math.cos(a) for a in opens)
                oy = sum(math.sin(a) for a in opens)
                best, best_s = (px, py, math.atan2(oy, ox)), s_
        return best

    def hold_goal(self, obs, me, t):
        zn = obs["zone"]
        ok = self.hold is not None and \
            math.hypot(self.hold[0] - zn["next_center"][0], self.hold[1] - zn["next_center"][1]) < zn["next_radius"] * 0.75 \
            and self.inside_safe(obs, self.hold[0], self.hold[1], 15)
        if not ok or (t - self.hold_t > 40 and math.hypot(self.hold[0] - me["pos"][0], self.hold[1] - me["pos"][1]) > 30):
            if t - self.hold_t > 3.0 or not ok:
                if t - self.hold_t > 1.0:
                    self.hold = self.find_hold(obs, me)
                    self.hold_t = t
        if self.hold is None:
            return None
        return (self.hold[0], self.hold[1])

    def inside_safe(self, obs, px, py, margin=10):
        z = obs["zone"]
        return math.hypot(px - z["center"][0], py - z["center"][1]) < z["radius"] - margin

    # ---------- main ----------
    def act(self, obs):
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        stats = me["stats"]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        muzzle = [x, y, z + stats.get("muzzle_height", 7)]
        eye = [x, y, z + stats.get("eye_height", 8)]
        energy = me["energy"]

        # upgrades
        offer = obs.get("offer")
        if offer:
            best, bi = 999, 0
            for i, ch in enumerate(offer["choices"]):
                p = UPGRADE_PRIORITY.index(ch["id"]) if ch["id"] in UPGRADE_PRIORITY else 50
                if p < best:
                    best, bi = p, i
            act["pick"] = bi

        # events
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                if ev.get("weapon") != "zone":
                    self.last_dmg_t = t
                    if ev.get("bearing") is not None:
                        self.look_bearing = ev["bearing"]
                        self.look_until = t + 1.5

        weapons = me["weapons"]
        slot_of = {}
        for i, w in enumerate(weapons):
            if w:
                slot_of[w["kind"]] = i

        # ---------- targets ----------
        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]

        def center(m):
            return [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * (1 + 0.05 * m.get("cubes", 0)) if m["kind"] == "mech" else m["pos"][2] + 1.75]

        def dist(m):
            return math.hypot(m["pos"][0] - x, m["pos"][1] - y)

        enemies = []
        for m in mechs:
            d = dist(m)
            vis = self.map.los(muzzle, center(m))
            enemies.append((m, d, vis))
        enemies.sort(key=lambda e: (not e[2], e[1] * 0.5 + e[0].get("hp", 1400) * 0.08))
        threats = [e for e in enemies if e[1] < 240]
        target = threats[0] if threats else None

        fire = [False, False]
        aim = None
        move = (0.0, 0.0)
        boost = False
        jump = False
        mode = "roam"

        # ---------- gun ----------
        gun_i = slot_of.get("gun")
        gun = weapons[gun_i] if gun_i is not None else None

        def lead_aim(m, speed):
            c = center(m)
            v = m.get("vel", [0, 0, 0])
            px, py, pz = c
            for _ in range(3):
                tt = math.dist(muzzle, (px, py, pz)) / speed
                px, py = c[0] + v[0] * tt, c[1] + v[1] * tt
                pz = c[2] + max(-3.0, min(3.0, v[2] * tt))
            yaw = math.atan2(py - muzzle[1], px - muzzle[0])
            pitch = math.atan2(pz - muzzle[2], math.hypot(px - muzzle[0], py - muzzle[1]))
            return yaw, pitch, (px, py, pz)

        def aim_ok(yaw, pitch, d, tol=2.0):
            ay, ap = me["aim"]
            return abs(wrap(ay - yaw)) * max(d, 1) < tol and abs(ap - pitch) * max(d, 1) < tol + 1.5

        def gun_shoot(m, d, tol=2.0):
            nonlocal aim
            if gun is None:
                return
            sp = gun["stats"].get("speed", 252)
            rng = gun["stats"].get("range", 230)
            yaw, pitch, pt = lead_aim(m, sp)
            if aim is None:
                aim = [yaw, pitch]
            if d < rng - 5 and gun.get("ammo", 1) > 0 and gun.get("reload", 0) <= 0 and aim_ok(yaw, pitch, d, tol) \
                    and self.map.los(muzzle, pt):
                fire[gun_i] = True

        # ---------- combat ----------
        laser_i = slot_of.get("laser")
        light_i = slot_of.get("lightning")
        if target:
            m, d, vis = target
            mode = "fight"
            c = center(m)
            ex, ey = m["pos"][0], m["pos"][1]
            bear = math.atan2(ey - y, ex - x)
            if vis:
                gun_shoot(m, d)
                # laser: hold to charge, release when full and on target
                if laser_i is not None:
                    lw = weapons[laser_i]
                    ls = lw["stats"]
                    charge = lw.get("charge", 0) or 0
                    if d < ls.get("range", 280) - 5:
                        yaw = math.atan2(c[1] - muzzle[1], c[0] - muzzle[0])
                        pitch = math.atan2(c[2] - muzzle[2], d)
                        if gun is None or gun.get("ammo", 0) <= 0 or charge > 0:
                            aim = [yaw, pitch]
                        if charge > 0:
                            ready = charge >= ls.get("charge", 0.75) - 0.06
                            on = aim_ok(yaw, pitch, d, 1.8)
                            fire[laser_i] = not (ready and on)
                        elif lw["cooldown"] <= 0:
                            fire[laser_i] = True
                # lightning: instant, nearest in cone
                if light_i is not None:
                    lw = weapons[light_i]
                    ls = lw["stats"]
                    if d < ls.get("range", 45) - 1 and energy > ls.get("energy", 3) + 2:
                        yaw = math.atan2(c[1] - muzzle[1], c[0] - muzzle[0])
                        if aim is None or gun is None or gun.get("ammo", 0) <= 0:
                            aim = [yaw, math.atan2(c[2] - muzzle[2], d)]
                        if abs(wrap(me["aim"][0] - yaw)) < math.radians(ls.get("cone_deg", 30)) * 0.8:
                            fire[light_i] = True
            else:
                # tracked but behind cover: keep facing it, ready to shoot
                aim = [bear, 0.0]

            # range keeping
            pref = 85.0
            if laser_i is not None:
                pref = 110.0
            if light_i is not None and energy > 35 and len(threats) == 1:
                pref = 30.0
            if "lightning" in m.get("weapons", []) and light_i is None:
                pref = max(pref, 65.0)
            if "mortar" in m.get("weapons", []):
                pref = min(pref, 45.0) if light_i is not None or d < 60 else pref
            n_vis = len([e for e in threats if e[2]])
            if n_vis >= 2:
                pref += 35
            elif m.get("hp", 1400) < me["hp"] * 0.8:
                pref = min(pref, 55.0)  # winning a 1v1: press in and finish it
            e_hp = m.get("hp", 1400)
            if n_vis <= 1:
                retreat = hp_frac < 0.25 or (hp_frac < 0.45 and e_hp > me["hp"] * 1.8)
            else:
                retreat = hp_frac < 0.5 or (hp_frac < 0.7 and n_vis >= 3)
            my_pow = me["hp"] * (1 + 0.04 * min(10, me.get("cubes", 0))) * (1.3 if (laser_i is not None or light_i is not None) else 1.0)
            en_pow = e_hp * (1 + 0.04 * min(10, m.get("cubes", 0))) * (1.3 if len(m.get("weapons", [])) > 1 else 1.0)
            high = z > m["pos"][2] + 5
            edge = high or my_pow > en_pow * 1.15
            if n_vis >= 2 and not (high and hp_frac > 0.6):
                edge = False
            if not edge and d > 25:
                retreat = True
            if retreat:
                pref = 200.0
                mode = "retreat"
            if t > self.strafe_until:
                self.strafe_sign = random.choice((-1, 1))
                self.strafe_until = t + random.uniform(0.5, 1.3)
            sx, sy = -math.sin(bear) * self.strafe_sign, math.cos(bear) * self.strafe_sign
            radial = max(-1.0, min(1.0, (d - pref) / 30.0))
            mx = sx * 0.85 + math.cos(bear) * radial * 0.8
            my = sy * 0.85 + math.sin(bear) * radial * 0.8
            # several threats: move away from their middle
            if len(threats) >= 2:
                cx = sum(e[0]["pos"][0] for e in threats) / len(threats)
                cy = sum(e[0]["pos"][1] for e in threats) / len(threats)
                ax, ay = norm2(x - cx, y - cy)
                w = 0.6 if not retreat else 1.2
                mx += ax * w
                my += ay * w
            move = norm2(mx, my)
            if self.hold is not None and not retreat and len(threats) <= 2:
                hx, hy = self.hold[0] - x, self.hold[1] - y
                hd = math.hypot(hx, hy)
                if hd < 25:
                    # peek around our corner: small side steps, pulled back to the spot
                    ux, uy = norm2(hx, hy)
                    pull = min(1.0, hd / 6.0)
                    move = norm2(sx * 0.6 + ux * pull * 1.2, sy * 0.6 + uy * pull * 1.2)
                    mode = "hold-fight"
            if retreat:
                if t - self.cover_t > 0.6:
                    self.cover = self.find_cover(obs, me, threats)
                    self.cover_t = t
                if self.cover:
                    cx_, cy_ = self.cover[0] - x, self.cover[1] - y
                    if math.hypot(cx_, cy_) > 2.5:
                        move = norm2(cx_, cy_)
                    else:
                        move = (sx * 0.3, sy * 0.3)
                    mode = "cover"
            laser_charging = laser_i is not None and (weapons[laser_i].get("charge", 0) or 0) > 0
            boost = (energy > 55 or (retreat and energy > 15)) and not laser_charging
            if light_i is not None and d < 50 and not retreat:
                boost = boost and energy > 70

        # threats we cannot see but heard / got hit by
        if not target and self.look_bearing is not None and t < self.look_until:
            aim = [self.look_bearing, 0.05]
            mode = "alert"

        # ---------- non-mech shooting (npcs, crates) ----------
        if not target:
            best = None
            for n in npcs:
                d = dist(n)
                if d < 120 and (best is None or d < best[1]):
                    best = (n, d)
            if best and hp_frac > 0.45:
                n, d = best
                if self.map.los(muzzle, center(n)):
                    gun_shoot(n, d, 1.2)
                    if light_i is not None and d < 40 and energy > 40:
                        fire[light_i] = True
                    mode = "npc"
            if best is None or mode != "npc":
                cr = None
                for c_ in obs.get("crates", []):
                    d = math.hypot(c_["pos"][0] - x, c_["pos"][1] - y)
                    if d < 90 and abs(c_["pos"][2] - z) < 6 and (cr is None or d < cr[1]):
                        cr = (c_, d)
                if cr and gun is not None:
                    c_, d = cr
                    cc = [c_["pos"][0], c_["pos"][1], c_["pos"][2] + 1.2]
                    yaw = math.atan2(cc[1] - muzzle[1], cc[0] - muzzle[0])
                    pitch = math.atan2(cc[2] - muzzle[2], d)
                    if aim is None:
                        aim = [yaw, pitch]
                    if aim_ok(yaw, pitch, d, 1.0) and self.map.los(muzzle, cc) and gun.get("ammo", 0) > 2:
                        fire[gun_i] = True
                    if mode == "roam":
                        mode = "crate"

        # ---------- movement goals out of combat ----------
        zgoal, zurgent = self.zone_goal(obs, me)
        if not mode.startswith(("fight", "retreat", "cover", "hold-fight")):
            goal = None
            # supply drop
            for dr in obs.get("drops", []):
                d = math.hypot(dr["pos"][0] - x, dr["pos"][1] - y)
                if d < 450 and self.inside_safe(obs, dr["pos"][0], dr["pos"][1], 20) and (
                        dr.get("holder") in (None, me["id"])):
                    if d < dr["radius"] - 2.5 and dr["lands_in"] <= 0:
                        goal = (x, y)  # stand still to capture
                        mode = "capture"
                    elif d >= dr["radius"] - 2.5:
                        goal = (dr["pos"][0], dr["pos"][1])
                        mode = "drop"
                    break
            # weapons we want
            if goal is None:
                best = None
                for p in obs.get("pickups", []):
                    k = p["kind"]
                    if k not in WANT_WEAPON:
                        continue
                    cur1 = weapons[1]["kind"] if weapons[1] else None
                    if k in slot_of:
                        continue
                    if cur1 is not None and WANT_WEAPON.get(cur1, 0) >= WANT_WEAPON[k]:
                        continue
                    if abs(p["pos"][2] - z) > 5:
                        continue
                    d = math.hypot(p["pos"][0] - x, p["pos"][1] - y)
                    if d < 150 and (best is None or d < best[1]):
                        best = (p, d)
                if best:
                    p, d = best
                    goal = (p["pos"][0], p["pos"][1])
                    mode = "loot"
                    if d < self.reach - 0.3:
                        act["take"] = p["id"]
                        if weapons[1] is not None:
                            act["drop"] = 1
            # cubes
            if goal is None:
                best = None
                for c_ in obs.get("cubes", []):
                    if abs(c_["pos"][2] - z) > 5:
                        continue
                    d = math.hypot(c_["pos"][0] - x, c_["pos"][1] - y)
                    if d < 130 and (best is None or d < best[1]):
                        best = (c_, d)
                if best:
                    goal = (best[0]["pos"][0], best[0]["pos"][1])
                    mode = "cube"
            if zurgent and zgoal:
                goal = zgoal
                mode = "zone"
            if goal is None:
                # hold a corner near the next circle: silent, shoot first
                hold_goal = self.hold_goal(obs, me, t)
                if hold_goal is not None:
                    goal = hold_goal
                    mode = "hold"
                elif zgoal:
                    goal = zgoal
                else:
                    zc = obs["zone"]["next_center"]
                    if math.hypot(zc[0] - x, zc[1] - y) > obs["zone"]["next_radius"] * 0.5:
                        goal = (zc[0], zc[1])
            if goal is not None:
                gx, gy = goal[0] - x, goal[1] - y
                gd = math.hypot(gx, gy)
                if gd > 2.0:
                    move = norm2(gx, gy)
                    if gd < 6 or (mode == "hold" and gd < 25):
                        move = (move[0] * 0.4, move[1] * 0.4)  # under step speed: silent
                if mode == "hold" and gd < 3.0:
                    move = (0.0, 0.0)
            # low hp and nobody around: walk slowly (silent) to regen
            if hp_frac < 0.65 and mode in ("roam", "crate") and not zurgent and t - self.last_dmg_t > 2.0:
                move = (0.0, 0.0)  # stand still: silent, and regen kicks in
                mode = "heal"
        else:
            # in a fight the zone still wins if we are in danger
            if zgoal is not None and zurgent:
                zx, zy = norm2(zgoal[0] - x, zgoal[1] - y)
                move = norm2(move[0] * 0.4 + zx * 1.2, move[1] * 0.4 + zy * 1.2)
                boost = boost or (not me["in_zone"] and energy > 10)

        # cubes close by: grab them even mid-fight
        if mode in ("fight", "hold-fight"):
            reach_c = 35 if (len(threats) == 1 and hp_frac > 0.5) else 18
            for c_ in obs.get("cubes", []):
                d = math.hypot(c_["pos"][0] - x, c_["pos"][1] - y)
                if d < reach_c and abs(c_["pos"][2] - z) < 4:
                    cx_, cy_ = norm2(c_["pos"][0] - x, c_["pos"][1] - y)
                    move = norm2(move[0] * 0.5 + cx_, move[1] * 0.5 + cy_)
                    break
        # pickup in reach anytime
        if "take" not in act and weapons[1] is None:
            for p in obs.get("pickups", []):
                if p["kind"] in WANT_WEAPON and p["kind"] not in slot_of and \
                        math.hypot(p["pos"][0] - x, p["pos"][1] - y) < self.reach - 0.3 and abs(p["pos"][2] - z) < 3.5:
                    act["take"] = p["id"]
                    break

        # ---------- dodge visible projectiles ----------
        cz = z + 4.5
        dodge_x = dodge_y = 0.0
        for pr in obs.get("projectiles", []):
            px, py, pz = pr["pos"]
            vx, vy, vz = pr["vel"]
            rx, ry, rz = x - px, y - py, cz - pz
            vv = vx * vx + vy * vy + vz * vz
            if vv < 1:
                continue
            ts = (rx * vx + ry * vy + rz * vz) / vv
            if ts <= 0 or ts > 1.2:
                continue
            mx_, my_ = rx - vx * ts, ry - vy * ts
            miss = math.hypot(mx_, my_)
            if miss < 4.5:
                if miss < 0.3:
                    mx_, my_ = -vy, vx
                ux, uy = norm2(mx_, my_)
                w = (4.5 - miss) / 4.5 + 0.5
                dodge_x += ux * w
                dodge_y += uy * w
        if dodge_x or dodge_y:
            move = norm2(move[0] * 0.5 + dodge_x, move[1] * 0.5 + dodge_y)
            if energy > 25:
                boost = True
        # an enemy laser charging at us: juke
        for m in mechs:
            if m.get("charging"):
                b = math.atan2(y - m["pos"][1], x - m["pos"][0])
                if abs(wrap(m["aim"][0] - b)) < 0.25:
                    if energy > 15:
                        boost = True
                    if t > self.strafe_until - 0.2:
                        self.strafe_sign *= -1
                        self.strafe_until = t + random.uniform(0.3, 0.6)

        # ---------- steering + stuck handling ----------
        mx, my = move
        moving = math.hypot(mx, my) > 0.3
        self.hist.append((t, x, y))
        while self.hist and self.hist[0][0] < t - 1.0:
            self.hist.pop(0)
        if t < self.stuck_until and self.stuck_dir is not None:
            mx, my = self.stuck_dir
            if energy > 8:
                boost = True
            if me["grounded"]:
                jump = True
            mode += "+unstick"
        else:
            if moving:
                mx, my, blocked = self.steer(me, mx, my)
                if len(self.hist) >= 8 and self.hist[-1][0] - self.hist[0][0] > 0.75:
                    moved = math.hypot(x - self.hist[0][1], y - self.hist[0][2])
                    if moved < 1.5:
                        if energy > 35:
                            self.stuck_dir = (mx, my)
                        else:
                            a = random.uniform(0, TAU)
                            self.stuck_dir = (math.cos(a), math.sin(a))
                        self.stuck_until = t + 1.3
                        self.hist = []
        # in the air: only jet when unsticking or climbing onto something
        if not me["grounded"] and not (t < self.stuck_until):
            boost = False

        # ---------- default aim: look around ----------
        if aim is None:
            sounds = [s for s in obs.get("sounds", []) if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar", "reload") and s["dist"] != "far"]
            if sounds:
                s = min(sounds, key=lambda s: s["age"])
                self.look_bearing = s["bearing"]
                self.look_until = t + 1.2
                aim = [s["bearing"], 0.05]
            elif self.look_bearing is not None and t < self.look_until:
                aim = [self.look_bearing, 0.05]
            elif self.hold is not None and math.hypot(self.hold[0] - x, self.hold[1] - y) < 8:
                aim = [wrap(self.hold[2] + 0.9 * math.sin(t * 0.9)), 0.0]
            else:
                base = math.atan2(my, mx) if moving else me["aim"][0]
                aim = [wrap(base + 1.0 * math.sin(t * 1.3)), 0.0]

        # empty magazine: always reload
        if gun is not None and gun.get("ammo", 1) <= 0 and gun.get("reload", 0) <= 0:
            act["reload"] = True
        # reload when quiet
        if gun is not None and not target and gun.get("ammo", 0) < gun["stats"].get("mag", 12) * 0.5 \
                and gun.get("reload", 0) <= 0 and not fire[gun_i]:
            act["reload"] = True

        act["move"] = [round(mx, 3), round(my, 3)]
        act["aim"] = [round(wrap(aim[0]), 4), round(max(-0.69, min(1.29, aim[1])), 4)]
        act["fire"] = fire
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        mf.draw.label(f"{mode} hp{me['hp']:.0f} e{energy:.0f} c{me.get('cubes', 0)}")
        return act


mf.run(Bot)

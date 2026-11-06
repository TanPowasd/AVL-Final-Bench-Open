# -*- coding: utf-8 -*-
# hark_strong: calibration opponent for kaggriculture (hark).
# Single file, whitelisted stdlib only; reads nothing but the observation.
#
# Idea: every unit-step is paid for (farmer + Fibonacci-priced day hands), and most
# markets are thin, so value comes from picking which markets to feed and how much.
#  - Animals are visited once a day (FEED + CARE doubles output; COLLECT_FERTILIZER
#    sells near 100 early).  Cows and sheep feed rich but thin markets (milk, wool)
#    and are bought only while the projected end-of-game price, counting our herd,
#    the opponent's visible herd and expected town demand, stays above a floor.
#    Geese are capped low: two eggs barely beat the wheat they eat.
#  - Melon tiles are sized to the melon market (ours + opponent's visible melons).
#  - Once bought wheat gets dear, fertilized wheat fields feed our own animals
#    (fertilizer is nearly worthless by then); wheat/fertilizer are sold at the
#    last step of each day so the night drop never overflows the shed.
#  - Each morning the day's work is cut into angular sectors around the shed, one
#    per unit; units steal from other routes once theirs is done.
import math

CROPS = {
    "WHEAT": {"seed": 10, "fy": 2, "my": 4, "ongoing": False, "max": 6},
    "CARROT": {"seed": 20, "fy": 2, "my": 3, "ongoing": False, "max": 4},
    "TOMATO": {"seed": 50, "fy": 8, "my": 8, "ongoing": True, "max": 4},
    "STRAWBERRY": {"seed": 100, "fy": 10, "my": 10, "ongoing": True, "max": 4},
    "MELON": {"seed": 80, "fy": 10, "my": 12, "ongoing": False, "max": 6},
}
ANIMALS = {
    "GOOSE": {"cost": 300, "st": "COOP", "fy": 4, "iv": 1, "held": 4, "prod": "EGG", "rate": 2.0},
    "COW": {"cost": 400, "st": "PASTURE", "fy": 8, "iv": 2, "held": 6, "prod": "MILK", "rate": 1.5},
    "SHEEP": {"cost": 500, "st": "PASTURE", "fy": 6, "iv": 3, "held": 6, "prod": "WOOL", "rate": 1.33},
}
MP = {
    "WHEAT": (25, 400, "sqrt", 0.8, "log", 0.2),
    "CARROT": (35, 450, "hinge", 1.0, "sqrt", 0.7),
    "TOMATO": (60, 200, "hinge", 0.4, "sqrt", 0.6),
    "STRAWBERRY": (120, 100, "sqrt", 0.7, "linear", 1.6),
    "MELON": (250, 300, "log", 0.2, "sq", 3.6),
    "EGG": (50, 332, "hinge", 0.4, "log", 0.2),
    "MILK": (160, 122, "sqrt", 0.6, "linear", 1.6),
    "WOOL": (200, 105, "log", 0.2, "sq", 3.2),
    "FERTILIZER": (100, 200, "linear", 0.4, "linear", 0.4),
}
SHOPS = {
    "BAKERY": ["EGG", "WHEAT"], "PIZZA_SHOP": ["MILK", "TOMATO", "WHEAT"],
    "BRUNCH_SPOT": ["EGG", "WHEAT", "STRAWBERRY"], "YARN_STORE": ["WOOL"],
    "ICE_CREAM_SHOP": ["STRAWBERRY", "MILK", "WHEAT"], "PET_CAFE": ["CARROT"],
    "SMOOTHIE_SHOP": ["STRAWBERRY", "MILK"], "FARMERS_MARKET": ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY"],
}
I0 = 10000
# expected per-day demand one more random shop unlock adds to each market
EXP_UNLOCK = {"EGG": 1.5, "MILK": 2.25, "WOOL": 1.5, "WHEAT": 3.75, "CARROT": 2.25,
              "TOMATO": 1.5, "STRAWBERRY": 3.0, "MELON": 0.0, "FERTILIZER": 0.0}
SHED = ((4, 4), (5, 4), (4, 5), (5, 5))
SHED_SET = set(SHED)
LAND_COST = (1000, 2000, 4000)
LAST_DAY = 29
NON_SELL = ("WHEAT", "GOOSE", "COW", "SHEEP")

P = {
    "ANIMAL_MAX": 60,
    "ANIMAL_WORK": 6.0,
    "SHOP_TRUST": 0.8,
    "GOOSE_MIN_GAIN": 150,
    "GOOSE_MAX": 4,
    "GOOSE_EARLY_MAX": 4,
    "GOOSE_LATE_DAY": 8,
    "USE_DRIFT": 0,
    "PRICE_RULE": 1,
    "PR_COW": 170,
    "PR_SHEEP": 230,
    "PR_GOOSE": 999,
    "PR_IMM": 3,
    "WOOL_TRUST": 0.4,
    "GOOSE_LAST_DAY": 21,
    "COW_LAST_DAY": 17,
    "SHEEP_LAST_DAY": 18,
    "BIG_FIRST_DAY": 0,
    "MELON_FIRST_DAY": 2,
    "MILK_FLOOR": 75,
    "WOOL_FLOOR": 90,
    "MELON_FLOOR": 130,
    "MELON_LAST_PLANT": 19,
    "MELON_MAX_TILES": 20,
    "HMAX": 13,
    "HIRE_CAP_COST": 400,
    "UNIT_CAP": 21.0,
    "RESERVE": 60,
    "FERT_MIN": 8,
    "OVF_LIMIT": 95,
    "DEPOSIT2": 1,
    "DEP_HOUR": 12,
    "NIGHT_SELL": 1,
    "EARLY_DEPOSIT": 1,
    "WHEAT_GROW_PRICE": 34,
    "CROP_FIRST_DAY": 2,
    "STRAW_FLOOR": 70,
    "STRAW_MAX": 0,
    "TOMATO_FLOOR": 38,
    "TOMATO_MAX": 0,
    "CARROT_FLOOR": 22,
    "CARROT_MAX": 0,
    "WHEAT_TILES_MAX": 26,
}


def _shape(f, x, T):
    x = max(0.0, x)
    if f == "linear":
        return x
    if f == "sq":
        return x * x
    if f == "sqrt":
        return math.sqrt(x)
    if f == "log":
        return math.log(1.0 + x)
    u = x / T
    return u + 8.0 * max(0.0, u - 1.0) ** 2


def mprice(item, inv):
    base, T, bf, bt, af, at = MP[item]
    if inv < I0:
        amp = bt * base / _shape(bf, T, T)
        p = base + amp * _shape(bf, I0 - inv, T)
    else:
        amp = at * base / _shape(af, T, T)
        p = base - amp * _shape(af, inv - I0, T)
    return max(1, int(round(p)))


def fib(n):
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def md(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def sd(p):
    return min(md(p, s) for s in SHED)


def near_shed(p):
    return min(SHED, key=lambda s: (md(p, s), s[1], s[0]))


def step_to(p, t):
    dx = t[0] - p[0]
    dy = t[1] - p[1]
    if dx == 0 and dy == 0:
        return "PASS"
    if abs(dx) >= abs(dy):
        return "EAST" if dx > 0 else "WEST"
    return "SOUTH" if dy > 0 else "NORTH"


def G(o, k, d=None):
    try:
        v = o[k]
    except Exception:
        v = getattr(o, k, d)
    return d if v is None else v


def ang(p):
    return math.atan2(p[1] - 4.5, p[0] - 4.5)


def consumption(shops):
    """Units per day the town removes from each market."""
    c = {k: 0.0 for k in MP}
    for k in MP:
        if k != "FERTILIZER":
            c[k] += 1.0
    for s in shops:
        items = SHOPS.get(s, [])
        mult = 2 if len(items) == 1 else 1
        for it in items:
            c[it] += 6.0 * mult
    return c


def herd(tiles):
    n = {"GOOSE": 0, "COW": 0, "SHEEP": 0, "MELON": 0, "TOMATO": 0, "STRAWBERRY": 0, "CARROT": 0, "WHEAT": 0}
    for row in tiles:
        for t in row:
            if isinstance(t, dict):
                a = t.get("animal") if hasattr(t, "get") else None
                if a in n:
                    n[a] += 1
                elif t.get("kind") == "PLANT" and t.get("crop") in n:
                    n[t.get("crop")] += 1
    return n


class Agent:
    def __init__(self):
        self.day = -1
        self.routes = {}
        self.planned = False
        self.plan_units = 0
        self.hired_at = {}
        self.minv_hist = []
        self.immature_mine = {}
        self.mode = {}
        self.bplan = {}
        self.pplan = {}

    def act(self, obs):
        try:
            return self._act(obs)
        except Exception:
            me = G(obs, "player", 0)
            n = len(G(G(obs, "farms")[me], "hands", []) or [])
            return {"farmer": ["PASS"], "hands": [["PASS"] for _ in range(n)], "market": []}

    # ================================================================== main
    def _act(self, obs):
        me = int(G(obs, "player", 0))
        farm = G(obs, "farms")[me]
        ofarm = G(obs, "farms")[1 - me]
        tiles = G(farm, "tiles")
        step = int(G(obs, "step", 0))
        day = step // 24
        hour = step % 24
        priv = G(obs, "private")
        shed = {k: int(v) for k, v in dict(G(priv, "shed", {})).items()}
        seeds = {k: int(v) for k, v in dict(G(priv, "seeds", {})).items()}
        invs = list(G(priv, "inventories", [{}]) or [{}])
        money = float(G(farm, "money", 0))
        mkt = G(obs, "market")
        minv = {k: int(v) for k, v in dict(G(mkt, "inventory", {})).items()}
        hands = list(G(farm, "hands", []) or [])
        unlocked = list(G(farm, "unlocked_quadrants", ["NW"]))
        shops = list(G(G(obs, "town"), "unlocked_shops", []) or [])
        if day != self.day:
            self.minv_hist.append((day, dict(minv)))
            self.day = day
            self.routes = {}
            self.planned = False
            self.plan_units = 0
            self.mode = {}
            self.bplan = {}
            self.pplan = {}
        final = day == LAST_DAY
        self.hour = hour
        steps_left = (23 - hour) if final else (24 - hour)
        days_left = LAST_DAY - day

        units = [(0, tuple(G(farm, "farmer")), {k: int(v) for k, v in dict(invs[0]).items()} if invs else {})]
        for i, h in enumerate(hands):
            inv = {k: int(v) for k, v in dict(invs[i + 1]).items()} if i + 1 < len(invs) else {}
            units.append((i + 1, tuple(h), inv))
        price = {k: mprice(k, minv.get(k, I0)) for k in MP}
        wheat_buy = mprice("WHEAT", minv.get("WHEAT", I0) - 1)

        # ------------------------------------------------------------ survey
        animals = []
        plants = []
        empties = []
        e_struct = {"COOP": [], "PASTURE": []}
        weeds = []
        n_kind = {"GOOSE": 0, "COW": 0, "SHEEP": 0}
        for y in range(10):
            row = tiles[y]
            for x in range(10):
                t = row[x]
                if t == "LOCKED":
                    continue
                if t is None:
                    empties.append((x, y))
                    continue
                k = G(t, "kind")
                if k == "PLANT":
                    plants.append((x, y, t))
                elif k == "WEED":
                    weeds.append((x, y))
                elif k in ("COOP", "PASTURE"):
                    a = G(t, "animal")
                    if a:
                        animals.append((x, y, t))
                        n_kind[a] = n_kind.get(a, 0) + 1
                    else:
                        e_struct[k].append((x, y))
        held = {a: shed.get(a, 0) + sum(inv.get(a, 0) for _, _, inv in units) for a in ANIMALS}
        tot_kind = {a: n_kind.get(a, 0) + held[a] for a in ANIMALS}
        n_anim = len(animals)
        n_held = sum(held.values())
        melon_alive = sum(1 for _, _, t in plants if G(t, "crop") == "MELON")
        opp = herd(G(ofarm, "tiles"))
        immature = {"GOOSE": 0, "COW": 0, "SHEEP": 0}
        for tl in (tiles, G(ofarm, "tiles")):
            for row in tl:
                for t in row:
                    if isinstance(t, dict) and t.get("animal") in immature:
                        a = t.get("animal")
                        if day - int(t.get("placed_day", day)) < ANIMALS[a]["fy"] - 1:
                            immature[a] += 1
        immature_mine = {"GOOSE": 0, "COW": 0, "SHEEP": 0}
        for row in tiles:
            for t in row:
                if isinstance(t, dict) and t.get("animal") in immature_mine:
                    a = t.get("animal")
                    if day - int(t.get("placed_day", day)) < ANIMALS[a]["fy"]:
                        immature_mine[a] += 1
        for a in ANIMALS:
            immature[a] += held[a]
            immature_mine[a] += held[a]
        self.immature_mine = immature_mine
        drift = {}
        if len(self.minv_hist) >= 2:
            d0, m0 = self.minv_hist[max(0, len(self.minv_hist) - 4)]
            d1, m1 = self.minv_hist[-1]
            if d1 > d0:
                for k in MP:
                    drift[k] = (m1.get(k, I0) - m0.get(k, I0)) / float(d1 - d0)
        cons = consumption(shops)

        # ------------------------------------------------------------ purchases (decided early: they shape today's work)
        buy = {}
        if not final and hour <= 12 and day <= P["GOOSE_LAST_DAY"]:
            buy = self.plan_purchases(day, hour, money, price, minv, cons, tot_kind, opp,
                                      n_anim + n_held, empties, e_struct, weeds, unlocked,
                                      len(plants) + len(self.pplan), len(shops), wheat_buy,
                                      drift, immature)
        need_st = {"COOP": 0, "PASTURE": 0}
        for a in ANIMALS:
            need_st[ANIMALS[a]["st"]] += held[a] + buy.get(a, 0)
        need_build = {s: max(0, need_st[s] - len(e_struct[s])) for s in need_st}
        cand = sorted(empties + weeds, key=lambda p: (sd(p), p[1], p[0]))
        cand_set = set(cand)
        # persistent (per day) build plan so targets do not flicker
        build_plan = {p: s for p, s in self.bplan.items() if p in cand_set}
        for s in ("PASTURE", "COOP"):
            have_b = sum(1 for v in build_plan.values() if v == s)
            for p in cand:
                if have_b >= need_build[s]:
                    break
                if p in build_plan or p in self.pplan:
                    continue
                build_plan[p] = s
                have_b += 1
        self.bplan = build_plan
        free = [p for p in cand if p not in build_plan and p not in self.pplan]
        # keep the next-nearest tiles for future animals
        future = 0
        if day <= P["GOOSE_LAST_DAY"] - 1:
            future = max(0, min(8, P["ANIMAL_MAX"] - n_anim - n_held - sum(buy.values())))
        free = free[future:] if len(free) > future else []
        # melons: size the patch to the market (ours + opponent's visible melons)
        plant_plan = {p: c for p, c in self.pplan.items() if p in cand_set}
        if P["MELON_FIRST_DAY"] <= day <= P["MELON_LAST_PLANT"] and not final:
            cap_units = math.sqrt(max(0.0, (250.0 - P["MELON_FLOOR"]) / 0.01))
            x_now = minv.get("MELON", I0) - I0 + shed.get("MELON", 0)
            room_units = cap_units + 1.0 * days_left - x_now - 6.0 * (melon_alive + opp["MELON"])
            new_tiles = int(room_units // 6)
            new_tiles = max(0, min(new_tiles, P["MELON_MAX_TILES"] - melon_alive) - len(plant_plan))
            for p in free:
                if new_tiles <= 0:
                    break
                plant_plan[p] = "MELON"
                new_tiles -= 1
        # thin crop markets with town demand: size each patch to what sells above a floor
        mine = herd(tiles)
        fut_u = 0.0
        cnt_u = 0
        for t_ in range(day, LAST_DAY + 1):
            fut_u += max(0, min(8, t_ // 3) - len(shops))
            cnt_u += 1
        fut_u /= max(1, cnt_u)
        for c, floor, last, yld, cmax in (("STRAWBERRY", P["STRAW_FLOOR"], 18, 6.0, P["STRAW_MAX"]),
                                          ("TOMATO", P["TOMATO_FLOOR"], 19, 6.0, P["TOMATO_MAX"]),
                                          ("CARROT", P["CARROT_FLOOR"], 26, 3.0, P["CARROT_MAX"])):
            if final or day > last or day < P["CROP_FIRST_DAY"] or cmax <= 0:
                continue
            x_now = minv.get(c, I0) + shed.get(c, 0)
            # largest inventory that still prices at or above the floor
            x_floor = x_now
            while mprice(c, x_floor + 1) >= floor and x_floor < x_now + 2000:
                x_floor += 1
            demand = (cons.get(c, 0.0) + 0.5 * fut_u * EXP_UNLOCK.get(c, 0.0)) * max(0, days_left - 3)
            cycles = 1.0 if c != "CARROT" else max(1.0, (days_left - 1) / 3.0)
            supply = (mine.get(c, 0) + opp.get(c, 0)) * yld * (0.6 if c != "CARROT" else cycles)
            room_c = (x_floor - x_now) + demand - supply
            planned_c = sum(1 for v in plant_plan.values() if v == c)
            n_new = int(room_c // (yld * cycles))
            n_new = max(0, min(n_new, cmax - mine.get(c, 0) - planned_c))
            for p in free:
                if n_new <= 0:
                    break
                if p in plant_plan:
                    continue
                plant_plan[p] = c
                n_new -= 1
        # wheat fields: feed our own animals once bought wheat gets dear (fertilizer is cheap by then)
        wheat_alive = sum(1 for _, _, t in plants if G(t, "crop") == "WHEAT")
        grow_wheat = (wheat_buy >= P["WHEAT_GROW_PRICE"] and day <= LAST_DAY - 3 and not final)
        if grow_wheat:
            target_w = min(P["WHEAT_TILES_MAX"], int(math.ceil((n_anim + n_held) / 1.6)))
            planned_w = sum(1 for c in plant_plan.values() if c == "WHEAT")
            add = target_w - wheat_alive - planned_w
            for p in free:
                if add <= 0:
                    break
                if p in plant_plan:
                    continue
                plant_plan[p] = "WHEAT"
                add -= 1
        self.pplan = plant_plan
        seed_need = {}
        for c in plant_plan.values():
            seed_need[c] = seed_need.get(c, 0) + 1
        for (x, y, t) in plants:
            c = G(t, "crop")
            if c == "WHEAT" and grow_wheat and day - int(G(t, "planted_day", day)) >= 2:
                seed_need["WHEAT"] = seed_need.get("WHEAT", 0) + 1
            if c == "CARROT" and day <= 26 and day - int(G(t, "planted_day", day)) >= 2:
                seed_need["CARROT"] = seed_need.get("CARROT", 0) + 1
        use_fert = price["FERTILIZER"] < 2.2 * wheat_buy

        fert_stock = shed.get("FERTILIZER", 0) + sum(inv.get("FERTILIZER", 0) for _, _, inv in units)
        fert_ok = price["FERTILIZER"] >= P["FERT_MIN"] or (use_fert and wheat_alive > 0 and fert_stock < wheat_alive)

        def tile_jobs(p):
            x, y = p
            t = tiles[y][x]
            out = []
            if t is None or (isinstance(t, dict) and G(t, "kind") == "WEED"):
                if t is not None and (p in build_plan or p in plant_plan):
                    out.append(("DIG", None, 30))
                    return out
                if p in build_plan:
                    out.append(("BUILD_" + build_plan[p], None, 50))
                elif p in plant_plan:
                    out.append(("PLANT", "SEED:" + plant_plan[p], 60))
                return out
            k = G(t, "kind")
            if k in ("COOP", "PASTURE"):
                a = G(t, "animal")
                if not a:
                    for an in ANIMALS:
                        if ANIMALS[an]["st"] == k:
                            out.append(("PLACE", an, 160))
                    return out
                if not final:
                    if not G(t, "fed_today"):
                        out.append(("FEED", "WHEAT", 120 + 80 * int(G(t, "consecutive_unfed", 0))))
                    if not G(t, "cared_today"):
                        out.append(("CARE", None, 60 if a != "GOOSE" else 40))
                if G(t, "fertilizer_available") and fert_ok:
                    out.append(("COLLECT_FERTILIZER", None, max(10, price["FERTILIZER"])))
                yu = int(G(t, "yield_units", 0))
                if yu > 0:
                    info = ANIMALS[a]
                    dsf = day + 1 - int(G(t, "placed_day", day)) - info["fy"]
                    nxt = 0
                    if dsf >= 0 and dsf % info["iv"] == 0:
                        nxt = 1 + int(G(t, "pending_care_bonus", 0))
                    if final or yu + nxt > info["held"] or day >= LAST_DAY - 1:
                        out.append(("HARVEST", None, 0.5 * price[info["prod"]] * yu))
                return out
            if k == "PLANT":
                crop = G(t, "crop")
                cd = CROPS.get(crop)
                if cd is None:
                    return out
                pd = int(G(t, "planted_day", day))
                age = day - pd
                yu = int(G(t, "yield_units", 0))
                watered = bool(G(t, "watered_today"))
                fud0 = int(G(t, "fertilized_until_day", -1))
                if cd["ongoing"]:
                    if yu > 0 and age >= cd["fy"]:
                        out.append(("HARVEST", None, 0.6 * price[crop] * yu + 10))
                    if (not watered) and use_fert and age >= cd["fy"] - 1 and fud0 < day and not final:
                        out.append(("FERTILIZE", "FERTILIZER", 60))
                    if not watered and not final:
                        out.append(("WATER", None, 90))
                    return out
                hage = {"WHEAT": 3, "MELON": 10, "CARROT": 3}.get(crop, cd["my"])
                harvestable = age >= cd["fy"] and yu > 0
                window = (cd["my"] + 1) // 2 <= age <= cd["my"]
                if harvestable and (final or (age >= hage and (watered or not window))):
                    out.append(("HARVEST", None, 300 if crop == "MELON" else 70))
                    return out
                fud = int(G(t, "fertilized_until_day", -1))
                if crop in ("WHEAT", "CARROT") and use_fert and not watered and 1 <= age <= 2 and fud < pd + 3:
                    out.append(("FERTILIZE", "FERTILIZER", 85))
                if not watered and not final:
                    out.append(("WATER", None, 130 if crop == "MELON" else 80))
                return out
            return out

        jobs = {}
        for y in range(10):
            for x in range(10):
                if tiles[y][x] == "LOCKED":
                    continue
                js = tile_jobs((x, y))
                if final:
                    js = [j for j in js if j[0] in ("HARVEST", "COLLECT_FERTILIZER")]
                if js:
                    jobs[(x, y)] = js

        def tile_cost(js):
            c = 0
            for op, need, val in js:
                c += 2 if op.startswith("BUILD_") else 1
            return c

        # ------------------------------------------------------------ hires (before planning)
        hires_now = 0
        work = sum(tile_cost(js) + 1.0 + 0.3 * sd(p) for p, js in jobs.items())
        work += 2.0 * sum(buy.values())
        want_h = int(math.ceil((work + 4.0) / P["UNIT_CAP"])) - 1
        want_h = max(0, min(P["HMAX"], want_h))
        if final:
            want_h = min(want_h, 10)
        hires_so_far = int(G(farm, "hires_today", 0))
        hire_budget = money - 20
        if hour <= 1 and not (final and hour > 0):
            k = hires_so_far
            have = len(hands)
            while have + hires_now < want_h and hires_now < (9 if hour == 0 else 6):
                cost = fib(k)
                if cost > hire_budget or cost > P["HIRE_CAP_COST"]:
                    break
                hire_budget -= cost
                k += 1
                hires_now += 1

        # ------------------------------------------------------------ route planning
        seeds_left = dict(seeds)
        shed_sim = dict(shed)

        def need_ok(need, inv):
            if need is None:
                return True
            if need.startswith("SEED:"):
                return seeds_left.get(need[5:], 0) > 0
            return inv.get(need, 0) > 0

        if hour >= 1 and (not self.planned or len(units) > self.plan_units):
            virt = hires_now if hour == 1 else 0
            plan_units = list(units) + [(len(units) + v, (4, 4), {}) for v in range(virt)]
            self.plan_routes(plan_units, jobs, tile_cost)
            self.planned = True
            self.plan_units = len(plan_units)

        def route_needs(idx):
            need = {}
            for p in self.routes.get(idx, []):
                for op, nd, val in jobs.get(p, []):
                    if nd in ("WHEAT", "FERTILIZER"):
                        need[nd] = need.get(nd, 0) + 1
                if p in build_plan:
                    s = build_plan[p]
                    need["ST:" + s] = need.get("ST:" + s, 0) + 1
                else:
                    t = tiles[p[1]][p[0]]
                    if isinstance(t, dict) and G(t, "kind") in ("COOP", "PASTURE") and not G(t, "animal"):
                        need["ST:" + G(t, "kind")] = need.get("ST:" + G(t, "kind"), 0) + 1
            return need

        def sellable_count(inv):
            return sum(v for k, v in inv.items() if k not in NON_SELL)

        claimed = {}
        for idx, r in self.routes.items():
            for p in r:
                claimed[p] = idx

        actions = {}
        carry_total = sum(sellable_count(inv) for _, _, inv in units)
        night_load = sum(v for k, v in shed.items() if k in ("GOOSE", "COW", "SHEEP"))
        overflow_risk = carry_total + night_load > 92
        # expected pick-ups still to come today (harvests on pending tiles)
        exp_more = 0
        for p_, js in jobs.items():
            for j in js:
                if j[0] == "HARVEST":
                    t_ = tiles[p_[1]][p_[0]]
                    exp_more += int(G(t_, "yield_units", 0)) if isinstance(t_, dict) else 0
                elif j[0] == "COLLECT_FERTILIZER":
                    exp_more += 1
        heavy = carry_total + exp_more + night_load > P["OVF_LIMIT"]

        for idx, pos, inv in units:
            carry = sellable_count(inv)
            dist_home = sd(pos)
            route = self.routes.setdefault(idx, [])

            def go_home():
                if pos in SHED_SET:
                    self.mode.pop(idx, None)
                    for k, v in inv.items():
                        shed_sim[k] = shed_sim.get(k, 0) + v
                    inv.clear()
                    return ["DROP"]
                self.mode[idx] = "HOME"
                return [step_to(pos, near_shed(pos))]

            if self.mode.get(idx) == "HOME" and carry > 0:
                actions[idx] = go_home()
                continue
            if self.mode.get(idx) == "HOME":
                self.mode.pop(idx, None)

            # ---------------- must-go-home cases
            if carry > 0:
                if final and dist_home + 1 >= steps_left - 1:
                    actions[idx] = go_home()
                    continue
                if (not final) and overflow_risk and ((carry >= 3 and dist_home + 2 >= steps_left) or
                                                      (P["EARLY_DEPOSIT"] and hour >= 17 and carry >= 10 and dist_home <= 3)):
                    actions[idx] = go_home()
                    continue
            # ---------------- supplies for the route (only near the shed)
            if not final and (dist_home <= 2 or self.mode.get(idx) == "FETCH") and route:
                need = route_needs(idx)
                want = {}
                if need.get("WHEAT", 0) > inv.get("WHEAT", 0):
                    want["WHEAT"] = need["WHEAT"] + 1 - inv.get("WHEAT", 0)
                if need.get("FERTILIZER", 0) > inv.get("FERTILIZER", 0):
                    want["FERTILIZER"] = need["FERTILIZER"] - inv.get("FERTILIZER", 0)
                for s in ("COOP", "PASTURE"):
                    k = need.get("ST:" + s, 0)
                    for an in ("SHEEP", "COW", "GOOSE"):
                        if ANIMALS[an]["st"] != s or k <= 0:
                            continue
                        have = inv.get(an, 0)
                        take = min(max(0, k - have), shed_sim.get(an, 0))
                        if take > 0:
                            want[an] = take
                        k -= have + take
                fetch = {k: v for k, v in want.items() if shed_sim.get(k, 0) > 0}
                if fetch:
                    if pos in SHED_SET:
                        k = max(fetch, key=lambda z: (z != "WHEAT", fetch[z]))
                        n = min(fetch[k], shed_sim.get(k, 0))
                        shed_sim[k] -= n
                        inv[k] = inv.get(k, 0) + n
                        actions[idx] = ["PICKUP", k, n]
                        if len(fetch) <= 1:
                            self.mode.pop(idx, None)
                        continue
                    self.mode[idx] = "FETCH"
                    actions[idx] = [step_to(pos, near_shed(pos))]
                    continue
                self.mode.pop(idx, None)
            # ---------------- deposit on the way when the night drop would overflow the shed
            if (P["DEPOSIT2"] and heavy and not final and hour >= P["DEP_HOUR"] and carry >= 6
                    and (dist_home <= 1 or (dist_home <= 3 and not route))):
                actions[idx] = go_home()
                continue
            # ---------------- work the current tile
            here = jobs.get(pos)
            if here and claimed.get(pos, idx) == idx:
                dj = [j for j in here if need_ok(j[1], inv)]
                if dj:
                    op, nd, val = max(dj, key=lambda j: j[2])
                    if op == "PLANT":
                        c = nd[5:]
                        seeds_left[c] = seeds_left.get(c, 0) - 1
                        act = ["PLANT", c]
                        rest = [("WATER", None, 130)]
                    elif op == "PLACE":
                        inv[nd] -= 1
                        act = ["PLACE", nd]
                        rest = []
                    elif op.startswith("BUILD_"):
                        act = [op]
                        s = op[6:]
                        rest = [("PLACE", an, 160) for an in ANIMALS if ANIMALS[an]["st"] == s]
                    elif op == "DIG":
                        act = ["DIG"]
                        if pos in build_plan:
                            rest = [("BUILD_" + build_plan[pos], None, 50)]
                        elif pos in plant_plan:
                            rest = [("PLANT", "SEED:" + plant_plan[pos], 60)]
                        else:
                            rest = []
                    else:
                        act = [op]
                        if op == "FEED":
                            inv["WHEAT"] -= 1
                        rest = [j for j in here if j[0] != op]
                        t = tiles[pos[1]][pos[0]]
                        if op == "HARVEST" and G(t, "kind") == "PLANT":
                            crop = G(t, "crop")
                            rest = []
                            if crop == "WHEAT" and grow_wheat:
                                plant_plan[pos] = "WHEAT"
                                self.pplan[pos] = "WHEAT"
                            if crop == "CARROT" and day <= 26 and price["CARROT"] >= P["CARROT_FLOOR"]:
                                plant_plan[pos] = "CARROT"
                                self.pplan[pos] = "CARROT"
                            if not CROPS[crop]["ongoing"] and pos in plant_plan:
                                rest = [("PLANT", "SEED:" + plant_plan[pos], 60)]
                    if rest:
                        jobs[pos] = rest
                    else:
                        jobs.pop(pos, None)
                    if not [j for j in rest if need_ok(j[1], inv)]:
                        if route and route[0] == pos:
                            route.pop(0)
                        claimed.pop(pos, None)
                    actions[idx] = act
                    continue
            # ---------------- deposit when standing at the shed with goods
            if pos in SHED_SET and carry > 0 and (carry >= 6 or not route or hour >= 20):
                if not inv.get("WHEAT") and not any(inv.get(a) for a in ANIMALS):
                    actions[idx] = go_home()
                else:
                    k = max((z for z in inv if z not in NON_SELL), key=lambda z: inv[z])
                    actions[idx] = ["PLACE", k, inv[k]]
                    shed_sim[k] = shed_sim.get(k, 0) + inv[k]
                    inv[k] = 0
                continue
            # ---------------- carrying an animal: place it first
            carried_an = [a for a in ANIMALS if inv.get(a, 0) > 0]
            if carried_an and not final:
                tgt = None
                bd = 99
                for p, js in jobs.items():
                    if not any((j[0] == "PLACE" and j[1] in carried_an) or (j[0].startswith("BUILD_") and
                               any(ANIMALS[a]["st"] == j[0][6:] for a in carried_an)) or
                               (j[0] == "DIG" and p in build_plan) for j in js):
                        continue
                    o = claimed.get(p)
                    if o is not None and o != idx:
                        continue
                    d = md(pos, p)
                    if d < bd:
                        bd = d
                        tgt = p
                if tgt is not None:
                    claimed[tgt] = idx
                    if tgt in route:
                        route[:] = [q for q in route if q != tgt]
                    route.insert(0, tgt)
                    actions[idx] = [step_to(pos, tgt)]
                    continue
            # ---------------- follow the route
            nxt = None
            while route:
                p = route[0]
                js = jobs.get(p, [])
                if any(need_ok(j[1], inv) for j in js):
                    nxt = p
                    break
                claimed.pop(p, None)
                route.pop(0)
            if nxt is None:
                if carry >= 8 and not final and overflow_risk:
                    actions[idx] = go_home()
                    continue
                nxt = self.steal(idx, pos, inv, jobs, claimed, need_ok, final, steps_left, dist_home)
                if nxt is None:
                    if carry > 0 or (not final and pos not in SHED_SET):
                        actions[idx] = go_home() if carry > 0 else [step_to(pos, near_shed(pos))]
                    else:
                        actions[idx] = ["PASS"]
                    continue
                route.insert(0, nxt)
                claimed[nxt] = idx
            if final and md(pos, nxt) + 2 + sd(nxt) > steps_left:
                route.clear()
                actions[idx] = go_home() if carry > 0 else ["PASS"]
                continue
            actions[idx] = [step_to(pos, nxt)]

        # ------------------------------------------------------------ market
        fert_keep = 0
        if use_fert and (hour != 23 or not P["NIGHT_SELL"]):
            fert_keep = min(40, int(0.7 * (wheat_alive + sum(1 for c in plant_plan.values() if c == "WHEAT"))) + 4)
        orders = []
        cash = money
        pre_sells = hour == 0 and money < 1500
        if pre_sells:
            orders, cash = self.sells(orders, cash, day, price, shed_sim, animals, n_held, final, fert_keep)
        room = max(0, 100 - sum(max(0, v) for v in shed_sim.values()))
        late = final or day >= LAST_DAY - 1
        # wheat for today's feeding (arrives next step, units pick it up at the shed)
        carried = sum(inv.get("WHEAT", 0) for _, _, inv in units)
        unfed = sum(1 for (_, _, t) in animals if not G(t, "fed_today"))
        if not final:
            need_w = unfed + n_held + sum(buy.values()) - carried - shed_sim.get("WHEAT", 0)
            if need_w > 0 and len(orders) < 10:
                n = int(min(need_w + 2, room, max(0.0, cash - 20) // (wheat_buy + 3)))
                if n > 0:
                    orders.append(["BUY_PRODUCT", "WHEAT", n])
                    cash -= n * (wheat_buy + 3)
                    room -= n
        if use_fert and not final and hour <= 2:
            need_f = sum(1 for js in jobs.values() for j in js if j[0] == "FERTILIZE")
            need_f -= shed_sim.get("FERTILIZER", 0) + sum(inv.get("FERTILIZER", 0) for _, _, inv in units)
            pf = mprice("FERTILIZER", minv.get("FERTILIZER", I0) - 1)
            if need_f > 0 and pf < 2 * wheat_buy and len(orders) < 10:
                n = int(min(need_f, room, max(0.0, cash - 20) // (pf + 2)))
                if n > 0:
                    orders.append(["BUY_PRODUCT", "FERTILIZER", n])
                    cash -= n * (pf + 2)
                    room -= n
        k = 0
        while k < hires_now and len(orders) < 10:
            c = fib(hires_so_far + k)
            if c > cash - 10:
                break
            orders.append(["HIRE"])
            cash -= c
            k += 1
        if not pre_sells:
            orders, cash = self.sells(orders, cash, day, price, shed_sim, animals, n_held, final, fert_keep)
        for a in ("SHEEP", "COW", "GOOSE"):
            n = min(buy.get(a, 0), room - 5, int((cash - 40) // ANIMALS[a]["cost"]))
            if n > 0 and len(orders) < 10:
                orders.append(["BUY_ANIMAL", a, n])
                cash -= n * ANIMALS[a]["cost"]
                room -= n
                n_held += n
        spend = -1.0 if late else cash - P["RESERVE"] - (35 * len(animals) if hour >= 2 else 0)
        # land when short of tiles for what we want to place
        n_unl = len(unlocked)
        free_tiles = len(empties) + len(weeds) + len(e_struct["COOP"]) + len(e_struct["PASTURE"])
        if n_unl < 4 and day <= 20 and len(orders) < 10:
            lc = LAND_COST[n_unl - 1]
            want_tiles = sum(buy.values()) + held_total(held) + len(plant_plan) + 3
            if free_tiles < want_tiles + 4 and spend >= lc + 300 and len(animals) + n_held < P["ANIMAL_MAX"]:
                orders.append(["BUY_LAND"])
                spend -= lc
                cash -= lc
        for c, k in seed_need.items():
            need = k - seeds.get(c, 0)
            if need > 0 and spend > 0 and len(orders) < 10:
                n = int(min(need, spend // CROPS[c]["seed"]))
                if n > 0:
                    orders.append(["BUY_SEED", c, n])
                    spend -= n * CROPS[c]["seed"]
                    cash -= n * CROPS[c]["seed"]
        out_hands = [actions.get(i + 1, ["PASS"]) for i in range(len(hands))]
        return {"farmer": actions.get(0, ["PASS"]), "hands": out_hands, "market": orders[:10]}

    # ================================================================== routing
    def plan_routes(self, units, jobs, tile_cost):
        """Split pending tiles into angular sectors with balanced estimated effort."""
        tl = list(jobs.keys())
        self.routes = {idx: [] for idx, _, _ in units}
        if not tl:
            return
        tl.sort(key=lambda p: (ang(p), sd(p)))
        if len(tl) > 1:
            best_j, best_g = 0, -1.0
            for i in range(len(tl)):
                g = ang(tl[i]) - ang(tl[i - 1])
                if g < 0:
                    g += 2 * math.pi
                if g > best_g:
                    best_g, best_j = g, i
            tl = tl[best_j:] + tl[:best_j]
        cost = [tile_cost(jobs[p]) + 1.0 + 0.3 * sd(p) for p in tl]
        k = len(units)
        total = sum(cost) + 3.0 * k
        per = total / k
        groups = []
        cur = []
        acc = 3.0
        for p, c in zip(tl, cost):
            cur.append(p)
            acc += c
            if acc >= per and len(groups) < k - 1:
                groups.append(cur)
                cur = []
                acc = 3.0
        if cur:
            groups.append(cur)
        while len(groups) < k:
            groups.append([])
        free_units = list(units)
        order = sorted(range(len(groups)), key=lambda i: -len(groups[i]))
        for gi in order:
            g = groups[gi]
            if not free_units:
                break
            if g:
                c = (sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g))
                u = min(free_units, key=lambda u: abs(u[1][0] - c[0]) + abs(u[1][1] - c[1]))
            else:
                u = free_units[0]
            free_units = [v for v in free_units if v[0] != u[0]]
            self.routes[u[0]] = self.order_route(u[1], g)

    def order_route(self, start, g):
        rest = list(g)
        out = []
        pos = start
        while rest:
            p = min(rest, key=lambda q: (md(pos, q), sd(q)))
            rest = [q for q in rest if q != p]
            out.append(p)
            pos = p
        return out

    def steal(self, idx, pos, inv, jobs, claimed, need_ok, final, steps_left, dist_home):
        best = None
        best_sc = 0.0
        for p, js in jobs.items():
            dj = [j for j in js if need_ok(j[1], inv)]
            if not dj:
                continue
            if inv.get("WHEAT", 0) <= 0 and any(j[0] == "FEED" for j in js):
                continue
            owner = claimed.get(p)
            if owner is not None and owner != idx:
                r = self.routes.get(owner, [])
                if r and r[0] == p:
                    continue
                pos_in = r.index(p) if p in r else 0
                if pos_in < 2:
                    continue
            d = md(pos, p)
            if final and d + 2 + sd(p) > steps_left:
                continue
            if d + 1 > steps_left:
                continue
            val = sum(j[2] for j in dj)
            sc = val / (d + 2.0)
            if sc > best_sc:
                best_sc = sc
                best = (p, owner)
        if best is None:
            return None
        p, owner = best
        if owner is not None and owner != idx:
            r = self.routes.get(owner, [])
            self.routes[owner] = [q for q in r if q != p]
        return p

    # ================================================================== economy
    def plan_purchases(self, day, hour, money, price, minv, cons, tot_kind, opp, n_animals,
                       empties, e_struct, weeds, unlocked, n_plants, n_shops, wheat_buy,
                       drift, immature):
        buy = {}
        immature_mine = self.immature_mine
        labor = (P["HMAX"] + 1) * 22.0 - 3.0 * n_plants - 25.0
        cap_total = min(P["ANIMAL_MAX"], int(labor / P["ANIMAL_WORK"]))
        cap_left = cap_total - n_animals
        if cap_left <= 0:
            return buy
        budget = money - P["RESERVE"] - 30 * n_animals
        if budget < 320:
            return buy
        days_left = LAST_DAY - day
        # expected extra town demand from shops that will still unlock
        fut = 0.0
        cnt = 0
        for t in range(day, LAST_DAY + 1):
            fut += max(0, min(8, t // 3) - n_shops)
            cnt += 1
        fut = fut / max(1, cnt)

        def proj_price(item, extra_rate, start_delay):
            x = minv.get(item, I0)
            dl = max(0, days_left - start_delay)
            trust = P["SHOP_TRUST"] * (P["WOOL_TRUST"] if item == "WOOL" else 1.0)
            if item in drift and P["USE_DRIFT"]:
                # observed net flow already contains producing herds and current shops
                rate = drift[item] + extra_rate
                for a, info in ANIMALS.items():
                    if info["prod"] == item:
                        rate += info["rate"] * immature.get(a, 0)
                c = trust * fut * EXP_UNLOCK.get(item, 0.0)
            else:
                rate = extra_rate
                for a, info in ANIMALS.items():
                    if info["prod"] == item:
                        rate += info["rate"] * (tot_kind.get(a, 0) + opp.get(a, 0))
                c = cons.get(item, 0.0) + trust * fut * EXP_UNLOCK.get(item, 0.0)
            x_end = x + (rate - c) * dl
            return mprice(item, int(x_end))

        if day >= P["BIG_FIRST_DAY"] or money > 4000:
            for a, floor, last in (("SHEEP", P["WOOL_FLOOR"], P["SHEEP_LAST_DAY"]),
                                   ("COW", P["MILK_FLOOR"], P["COW_LAST_DAY"])):
                if day > last:
                    continue
                info = ANIMALS[a]
                n = 0
                pr_rule = (P["PRICE_RULE"] and price[info["prod"]] >= P["PR_" + a]
                           and day <= last - 2)
                imm_mine = immature_mine.get(a, 0)
                while n < 4 and cap_left > 0 and budget >= info["cost"] + 40:
                    ok = proj_price(info["prod"], info["rate"] * (n + 1), 4) >= floor
                    if not ok and pr_rule and imm_mine + n < P["PR_IMM"]:
                        ok = True
                    if not ok:
                        break
                    n += 1
                    budget -= info["cost"] + 40
                    cap_left -= 1
                if n > 0:
                    buy[a] = n
        if cap_left > 0 and budget >= 330 and day <= P["GOOSE_LAST_DAY"]:
            # geese only while two eggs clearly beat the wheat they eat
            n_geese = tot_kind.get("GOOSE", 0)
            d_now = max(0.0, I0 - minv.get("WHEAT", I0))
            n = 0
            gmax = P["GOOSE_MAX"] if day >= P["GOOSE_LATE_DAY"] else P["GOOSE_EARLY_MAX"]
            nmax = min(int(budget // 330), cap_left, 14, gmax - n_geese)
            while n < nmax:
                rate = n_animals + n + 1
                d_mid = d_now + rate * days_left * 0.5
                w_mid = 25.0 + math.sqrt(d_mid)
                margin = 2.0 * price["EGG"] * 0.95 - w_mid
                if margin * max(0, days_left - 3) < P["GOOSE_MIN_GAIN"]:
                    break
                n += 1
            if n > 0:
                buy["GOOSE"] = n
        slots = len(empties) + len(weeds) + len(e_struct["COOP"]) + len(e_struct["PASTURE"])
        if len(unlocked) < 4:
            slots += 25 if money > LAND_COST[len(unlocked) - 1] + 600 else 0
        tot = sum(buy.values())
        if tot > slots:
            over = tot - slots
            for a in ("GOOSE", "COW", "SHEEP"):
                c = min(buy.get(a, 0), over)
                buy[a] = buy.get(a, 0) - c
                over -= c
        return {a: n for a, n in buy.items() if n > 0}

    def sells(self, orders, cash, day, price, shed_sim, animals, n_held, final, fert_keep=0):
        for item in ("EGG", "MILK", "WOOL", "MELON", "FERTILIZER", "CARROT", "TOMATO", "STRAWBERRY", "WHEAT"):
            if len(orders) >= 10:
                break
            n = shed_sim.get(item, 0)
            if n <= 0:
                continue
            if item == "WHEAT":
                keep = 0 if (final or (self.hour == 23 and P["NIGHT_SELL"])) else len(animals) + n_held + 4
                n -= keep
            if item == "FERTILIZER" and not final:
                n -= fert_keep
            if n <= 0:
                continue
            if price[item] <= 1 and not final and item != "FERTILIZER":
                continue
            orders.append(["SELL", item, int(n)])
            cash += 0.9 * n * price[item]
            shed_sim[item] = shed_sim.get(item, 0) - n
        return orders, cash


def held_total(h):
    return sum(h.values())


_AGENT = Agent()


def act(observation):
    return _AGENT.act(observation)

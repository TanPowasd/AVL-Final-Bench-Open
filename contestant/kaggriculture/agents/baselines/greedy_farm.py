# 贪心启发式锚点：种麦-浇水-收割-回棚卸货-卖出，雇 2 个工人，末期清仓。
# 不是最优，只是给教练模型一个"及格线"参照。


def act(observation):
    me = observation["player"]
    farm = observation["farms"][me]
    priv = observation["private"]
    tiles = farm["tiles"]
    bs = len(tiles)
    day = observation["day"]
    hands = farm.get("hands") or []
    invs = priv.get("inventories") or [{}]
    shed = priv.get("shed") or {}
    money = float(farm["money"])
    unlocked = farm.get("unlocked_quadrants") or ["NW"]
    half = bs // 2

    def quad(x, y):
        return ("N" if y < half else "S") + ("W" if x < half else "E")

    def in_area(x, y):
        return quad(x, y) in unlocked

    shed_adj = [(half - 1, half - 1), (half, half - 1), (half - 1, half), (half, half)]

    def carrying(inv):
        return sum(int(n) for k, n in (inv or {}).items() if k != "FERTILIZER")

    def dist(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def step_toward(p, target):
        dx = target[0] - p[0]
        dy = target[1] - p[1]
        cands = []
        if abs(dx) >= abs(dy):
            if dx:
                cands.append(("EAST" if dx > 0 else "WEST", (p[0] + (1 if dx > 0 else -1), p[1])))
            if dy:
                cands.append(("SOUTH" if dy > 0 else "NORTH", (p[0], p[1] + (1 if dy > 0 else -1))))
        else:
            if dy:
                cands.append(("SOUTH" if dy > 0 else "NORTH", (p[0], p[1] + (1 if dy > 0 else -1))))
            if dx:
                cands.append(("EAST" if dx > 0 else "WEST", (p[0] + (1 if dx > 0 else -1), p[1])))
        for name, np_ in cands:
            if 0 <= np_[0] < bs and 0 <= np_[1] < bs and in_area(*np_):
                return name
        # 无合法方向：随便挑一个界内方向（多半在锁区外，先爬回自己地盘）
        for name, np_ in cands:
            if 0 <= np_[0] < bs and 0 <= np_[1] < bs:
                return name
        return "PASS"

    def best_target(pos, want):
        best = None
        bd = 999
        for y in range(bs):
            for x in range(bs):
                if not in_area(x, y):
                    continue
                if want(tiles[y][x]) and dist(pos, (x, y)) < bd:
                    best = (x, y)
                    bd = dist(pos, best)
        return best

    FIRST_YIELD = {"WHEAT": 2, "CARROT": 2, "TOMATO": 8, "STRAWBERRY": 10, "MELON": 10}

    def home_target(pos):
        pool = [q for q in shed_adj if in_area(*q)]
        away = [q for q in pool if tuple(q) != tuple(pos)]
        cand = away or pool or shed_adj
        return min(cand, key=lambda q: dist(pos, q))

    def unit(pos, inv):
        # 出了自己地盘：先回去
        if not in_area(*pos):
            return [step_toward(pos, home_target(pos))]
        tile = tiles[pos[1]][pos[0]]

        def ready(tl):
            if not isinstance(tl, dict) or tl.get("kind") != "PLANT":
                return False
            fy = FIRST_YIELD.get(tl.get("crop"), 99)
            return int(tl.get("yield_units", 0)) > 0 and (day - int(tl.get("planted_day", -99))) >= fy
        if ready(tile):
            return ["HARVEST"]
        if carrying(inv) > 0:
            if tuple(pos) in shed_adj:
                return ["DROP"]
            return [step_toward(pos, home_target(pos))]
        if isinstance(tile, dict) and tile.get("kind") == "WEED":
            return ["DIG"]
        if isinstance(tile, dict) and tile.get("kind") == "PLANT":
            if not tile.get("watered_today"):
                return ["WATER"]
        if tile is None:
            seeds = priv.get("seeds") or {}
            for crop in ("WHEAT", "CARROT"):
                if seeds.get(crop, 0) > 0:
                    return ["PLANT", crop]
        t = best_target(pos, lambda tl: isinstance(tl, dict) and tl.get("kind") == "PLANT"
                        and not tl.get("watered_today"))
        if t is None:
            t = best_target(pos, ready)
        if t is None and priv.get("seeds", {}).get("WHEAT", 0) > 0:
            t = best_target(pos, lambda tl: tl is None)
        if t is None:
            t = best_target(pos, lambda tl: isinstance(tl, dict) and tl.get("kind") == "PLANT")
        if t is None:
            return ["PASS"]
        return [step_toward(pos, t)]

    market = []
    for item in ("WHEAT", "CARROT", "TOMATO", "STRAWBERRY", "MELON", "EGG", "MILK", "WOOL"):
        n = int(shed.get(item, 0))
        if n > 0:
            if day >= 28:
                market.append(["SELL", item, n])
            else:
                market.append(["SELL", item, min(n, 5)])
    seeds = priv.get("seeds") or {}
    if day < 24 and money > 120 and sum(seeds.values()) < 6:
        market.append(["BUY_SEED", "WHEAT", 4])
    if len(hands) < 2 and money > 400 and int(farm.get("hires_today", 0)) == 0:
        market.append(["HIRE"])
    if len(unlocked) < 2 and money > 3000 and day > 4:
        market.append(["BUY_LAND"])
    return {"farmer": unit(list(farm["farmer"]), invs[0] if invs else {}),
            "hands": [unit(list(h), invs[i + 1] if i + 1 < len(invs) else {}) for i, h in enumerate(hands)],
            "market": market[:10]}

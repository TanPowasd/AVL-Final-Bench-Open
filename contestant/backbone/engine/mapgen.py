"""骨干争夺 地图生成器（只用标准库）。

地图由"半张图"镜像而成，保证 A、B 起始位置严格对称：
- 半图：3 个大区、n_half ∈ [50,70] 个城市、陆缆+微波若干；
- 另一半 = 半图沿 x 轴镜像（x' = WORLD_W - x），城市属性相同，大区编号 +3；
- 两半之间用成对对称的海缆相连。
城市总数 100–140，链路总数 260–380，6 个大区。

generate(seed) 返回纯 JSON 可序列化的 dict：
  cities: [{id,x,y,region,pop,elec,land}]
  links:  [{id,u,v,type,cap,lat,base}]     type ∈ land/sub/mw；lat 毫秒；base 每单位每 tick 基础租价
  regions:[{id,tz}]                        tz 为小时偏移
  start:  {"A":[c1,c2],"B":[c1',c2']}      起始 PoP 城市
  mirror: [city -> 镜像城市]
"""
from __future__ import annotations

import math
import random

HALF_W = 3000.0          # 半图宽（km）
HALF_H = 2000.0
GAP = 3000.0             # 两半之间的海洋宽度
WORLD_W = 2 * HALF_W + GAP
REGION_TZ_HALF0 = [-2, 0, 2]
TZ_SHIFT_HALF1 = 10


def _dist(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _land_lat(d):
    return round(1.0 + d / 200.0, 2)        # 光纤约 5µs/km + 设备 1ms


def _mw_lat(d):
    return round(0.5 + d / 290.0, 2)


def _base_price(typ, lat):
    # 每单位容量每 tick 的基础租价
    if typ == "sub":
        return round(0.0008 + lat * 0.000015, 6)
    if typ == "mw":
        return round(0.0024 + lat * 0.00008, 6)
    return round(0.0006 + lat * 0.00008, 6)


def generate(seed: int) -> dict:
    rng = random.Random(seed * 1000003 + 17)
    n_half = rng.randint(50, 70)
    centers = [(500 + 1000 * k + rng.uniform(-150, 150), HALF_H / 2 + rng.uniform(-400, 400))
               for k in range(3)]
    half = []
    for i in range(n_half):
        r = i % 3 if i < 3 else rng.choices([0, 1, 2], weights=[1.0, 1.0, 1.0])[0]
        cx, cy = centers[r]
        while True:
            x = rng.gauss(cx, 330)
            y = rng.gauss(cy, 330)
            if 30 < x < HALF_W - 30 and 30 < y < HALF_H - 30:
                if all(math.hypot(x - c["x"], y - c["y"]) > 60 for c in half):
                    break
        pop = min(25.0, rng.paretovariate(1.25))     # 帕累托，截断 25 倍
        half.append({"x": round(x, 1), "y": round(y, 1), "region": r, "pop": pop,
                     "elec": round(rng.uniform(0.7, 1.4), 3)})
    tot = sum(c["pop"] for c in half)
    for c in half:
        c["pop"] = round(c["pop"] / tot / 2, 6)       # 全图人口和 = 1
        # 地价：人口越大越贵，0.5–2.5
        c["land"] = round(min(2.5, 0.5 + 60 * c["pop"] + rng.uniform(0, 0.3)), 3)

    # ---- 半图链路：MST + 近邻补边 ----
    n = n_half
    in_tree = [False] * n
    best = [(1e18, -1)] * n
    best[0] = (0.0, -1)
    edges = set()
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i][0])
        in_tree[u] = True
        if best[u][1] >= 0:
            edges.add((min(u, best[u][1]), max(u, best[u][1])))
        for v in range(n):
            if not in_tree[v]:
                d = _dist(half[u], half[v])
                if d < best[v][0]:
                    best[v] = (d, u)
    tree = set(edges)
    target_half = rng.randint(125, 165)
    cand = []
    for u in range(n):
        near = sorted((v for v in range(n) if v != u), key=lambda v: _dist(half[u], half[v]))[:6]
        for v in near:
            cand.append((_dist(half[u], half[v]) * rng.uniform(0.8, 1.25), min(u, v), max(u, v)))
    cand.sort()
    for _, u, v in cand:
        if len(edges) >= target_half:
            break
        edges.add((u, v))
    half_links = []
    for u, v in sorted(edges):
        d = _dist(half[u], half[v])
        if (u, v) not in tree and d < 450 and rng.random() < 0.3:   # 生成树边一律陆缆
            typ, cap, lat = "mw", rng.randint(10, 40), _mw_lat(d)
        else:
            typ, cap, lat = "land", rng.randint(40, 400), _land_lat(d)
        half_links.append((u, v, typ, cap, lat))

    # ---- 镜像 ----
    cities = []
    for i, c in enumerate(half):
        cities.append({"id": i, **c})
    for i, c in enumerate(half):
        cities.append({"id": n + i, "x": round(WORLD_W - c["x"], 1), "y": c["y"],
                       "region": 3 + (2 - c["region"]), "pop": c["pop"],
                       "elec": c["elec"], "land": c["land"]})
    mirror = [i + n for i in range(n)] + [i for i in range(n)]
    links = []
    for u, v, typ, cap, lat in half_links:
        links.append((u, v, typ, cap, lat))
    for u, v, typ, cap, lat in half_links:
        links.append((u + n, v + n, typ, cap, lat))

    # ---- 海缆：沿岸城市成对对称连接 ----
    coast = sorted(range(n), key=lambda i: -half[i]["x"])[:max(8, n // 5)]
    n_sub_pairs = rng.randint(10, 20)
    subs = set()
    tries = 0
    while len(subs) < n_sub_pairs and tries < 1000:
        tries += 1
        a, b = rng.choice(coast), rng.choice(coast)
        key = (min(a, b), max(a, b))
        if key in subs:
            continue
        subs.add(key)
    for a, b in sorted(subs):
        lat = round(rng.uniform(40, 120), 2)
        cap = rng.randint(400, 1600)
        links.append((a, b + n, "sub", cap, lat))
        if a != b:
            links.append((b, a + n, "sub", cap, lat))

    out_links = []
    for k, (u, v, typ, cap, lat) in enumerate(links):
        out_links.append({"id": k, "u": u, "v": v, "type": typ, "cap": cap, "lat": lat,
                          "base": _base_price(typ, lat)})

    regions = [{"id": r, "tz": REGION_TZ_HALF0[r]} for r in range(3)]
    regions += [{"id": 3 + (2 - r), "tz": REGION_TZ_HALF0[r] + TZ_SHIFT_HALF1} for r in range(3)]
    regions.sort(key=lambda r: r["id"])

    # 起始 PoP：半图中人口最大的大区里人口前二的城市；B 取镜像
    reg_pop = [sum(c["pop"] for c in half if c["region"] == r) for r in range(3)]
    r0 = max(range(3), key=lambda r: reg_pop[r])
    top = sorted((i for i in range(n) if half[i]["region"] == r0), key=lambda i: -half[i]["pop"])[:2]
    start = {"A": top, "B": [mirror[i] for i in top]}
    return {"seed": seed, "cities": cities, "links": out_links, "regions": regions,
            "start": start, "mirror": mirror}


def small_map() -> dict:
    """GUIDE 与单元测试用的 8 城市小地图（手工，不对称也无所谓）。"""
    cities = [
        {"id": 0, "x": 0, "y": 0, "region": 0, "pop": 0.020, "elec": 1.0, "land": 1.5},
        {"id": 1, "x": 400, "y": 0, "region": 0, "pop": 0.010, "elec": 1.0, "land": 1.0},
        {"id": 2, "x": 800, "y": 100, "region": 1, "pop": 0.015, "elec": 0.9, "land": 1.2},
        {"id": 3, "x": 1200, "y": 0, "region": 1, "pop": 0.005, "elec": 1.1, "land": 0.8},
        {"id": 4, "x": 0, "y": 600, "region": 0, "pop": 0.010, "elec": 1.0, "land": 1.0},
        {"id": 5, "x": 400, "y": 600, "region": 0, "pop": 0.005, "elec": 1.0, "land": 0.7},
        {"id": 6, "x": 800, "y": 700, "region": 1, "pop": 0.020, "elec": 0.8, "land": 1.5},
        {"id": 7, "x": 1200, "y": 600, "region": 1, "pop": 0.015, "elec": 1.0, "land": 1.2},
    ]
    raw = [(0, 1, "land", 200), (1, 2, "land", 120), (2, 3, "land", 200), (0, 4, "land", 100),
           (1, 5, "mw", 30), (2, 6, "land", 80), (3, 7, "land", 150), (4, 5, "land", 200),
           (5, 6, "land", 120), (6, 7, "land", 200)]
    links = []
    for k, (u, v, typ, cap) in enumerate(raw):
        d = _dist(cities[u], cities[v])
        lat = _mw_lat(d) if typ == "mw" else _land_lat(d)
        links.append({"id": k, "u": u, "v": v, "type": typ, "cap": cap, "lat": lat,
                      "base": _base_price(typ, lat)})
    return {"seed": 0, "cities": cities, "links": links,
            "regions": [{"id": 0, "tz": 0}, {"id": 1, "tz": 0}, {"id": 2, "tz": 0},
                        {"id": 3, "tz": 0}, {"id": 4, "tz": 0}, {"id": 5, "tz": 0}],
            "start": {"A": [0, 1], "B": [6, 7]}, "mirror": [6, 7, 5, 4, 3, 2, 0, 1]}


if __name__ == "__main__":
    import sys
    for s in map(int, sys.argv[1:] or ["1", "2", "3"]):
        m = generate(s)
        types = {}
        for l in m["links"]:
            types[l["type"]] = types.get(l["type"], 0) + 1
        print(s, "cities", len(m["cities"]), "links", len(m["links"]), types, "start", m["start"])

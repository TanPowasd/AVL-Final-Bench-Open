import random

MOVES = ["NORTH", "SOUTH", "EAST", "WEST", "PASS"]
TILE_OPS = ["WATER", "HARVEST", "DIG", "DROP", "PLANT WHEAT", "PICKUP WHEAT 1"]
MKT = [[], [], [], ["HIRE"], ["BUY_SEED", "WHEAT", "1"], ["SELL", "WHEAT", "1"], ["BUY_LAND"]]


def _unit():
    r = random.random()
    if r < 0.40:
        return [random.choice(MOVES)]
    if r < 0.70:
        op = random.choice(TILE_OPS).split()
        return op
    return ["PASS"]


def act(observation):
    me = observation["player"]
    farm = observation["farms"][me]
    n = len(farm.get("hands") or [])
    market = []
    if random.random() < 0.15:
        m = random.choice(MKT)
        if m:
            market = [[m[0]] + m[1:] if m[0] in ("BUY_SEED", "SELL") else list(m)]
    return {"farmer": _unit(), "hands": [_unit() for _ in range(n)], "market": market}

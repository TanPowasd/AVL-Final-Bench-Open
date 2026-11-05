def act(observation):
    me = observation["player"]
    n = len(observation["farms"][me].get("hands") or [])
    return {"farmer": ["PASS"], "hands": [["PASS"] for _ in range(n)], "market": []}

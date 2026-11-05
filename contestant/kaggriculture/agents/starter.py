# -*- coding: utf-8 -*-
# 农场对战 agent 起步模板：每一步返回 PASS 形状的合法动作。
# 只允许 import: math, random, itertools, collections, heapq, bisect, functools, statistics, json
# 接口： act(observation) -> {"farmer":[op,...], "hands":[[op,...],...], "market":[[op,...],...]}
# hands 列表长度必须等于当前工人数量；market 最多 10 条订单。


def act(observation):
    me = observation["player"]
    farm = observation["farms"][me]
    n_hands = len(farm.get("hands") or [])
    return {
        "farmer": ["PASS"],
        "hands": [["PASS"] for _ in range(n_hands)],
        "market": [],
    }

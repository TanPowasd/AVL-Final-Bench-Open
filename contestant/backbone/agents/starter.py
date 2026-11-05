"""骨干争夺协议起步模板（详细规则与字段见 README.md）。"""
import heapq
import json
import math
import sys


class Net:
    """地图邻接表、最短路与引力需求计算工具（公式与 engine/engine.py 一致）。"""

    def __init__(self, init):
        m = init["map"]
        self.cities = m["cities"]
        self.links = m["links"]
        self.n, self.m = len(self.cities), len(self.links)
        self.rules = init["rules"]
        self.adj = [[] for _ in range(self.n)]
        self.edge = {}
        for l in self.links:
            self.adj[l["u"]].append((l["v"], l["id"]))
            self.adj[l["v"]].append((l["u"], l["id"]))
            self.edge[(l["u"], l["v"])] = l["id"]
            self.edge[(l["v"], l["u"])] = l["id"]
        self.region = [c["region"] for c in self.cities]

    def other(self, l, u):
        L = self.links[l]
        return L["v"] if L["u"] == u else L["u"]

    def dijkstra(self, src, weight=None, down=()):
        down = set(down)
        dist = [math.inf] * self.n
        prev = [None] * self.n
        dist[src] = 0.0
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, l in self.adj[u]:
                if l in down:
                    continue
                w = self.links[l]["lat"] if weight is None else weight(l)
                if w is None:
                    continue
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, l)
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    @staticmethod
    def walk(prev, src, dst):
        if src != dst and prev[dst] is None:
            return None, None
        nodes, links = [dst], []
        v = dst
        while v != src:
            u, l = prev[v]
            nodes.append(u)
            links.append(l)
            v = u
        return nodes[::-1], links[::-1]

    def gravity(self, a, b):
        ca, cb = self.cities[a], self.cities[b]
        d = math.hypot(ca["x"] - cb["x"], ca["y"] - cb["y"]) / 1000.0
        return self.rules["DEM_K"] * ca["pop"] * cb["pop"] / max(d, 0.3) ** self.rules["DEM_ALPHA"]


class Bot:
    def __init__(self, init):
        self.net = Net(init)
        self.seat = init["seat"]
        self.my_pops = list(init["start"]["you"])

    def step(self, obs):
        return {}


def make_bot(init):
    return Bot(init)


if __name__ == "__main__":
    bot = make_bot(json.loads(sys.stdin.readline()))
    print("{}", flush=True)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "end":
            print("{}", flush=True)
            break
        try:
            act = bot.step(msg)
        except Exception:
            act = {}
        sys.stdout.write(json.dumps(act, separators=(",", ":")) + "\n")
        sys.stdout.flush()

"""基线：防守 FIFO；攻击 = 热点 + 冷扫描混合（80% 访问 C/2 个热 key，20% 扫冷 key）。"""
import json, sys, random
from collections import deque
init = json.loads(sys.stdin.readline()); role, C, K = init["role"], init["cache_size"], init["universe"]
rng = random.Random(init["seed"] * 7 + 1)
print("{}", flush=True)
q, cold = deque(), 0
for line in sys.stdin:
    m = json.loads(line)
    if m.get("end"): print("{}", flush=True); break
    if role == "attacker":
        if rng.random() < 0.8: k = rng.randrange(max(1, C // 2))
        else: cold = (cold + 1) % max(1, K - C // 2); k = C // 2 + cold
        print(json.dumps({"key": k}), flush=True); continue
    k, out = m["key"], {}
    if not m["hit"]:
        if m["need_evict"]: out["evict"] = q.popleft()
        q.append(k)
    print(json.dumps(out), flush=True)

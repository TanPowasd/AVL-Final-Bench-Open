"""基线：防守随机驱逐；攻击 = 在 2C 个 key 上均匀随机。"""
import json, sys, random
init = json.loads(sys.stdin.readline()); role, C, K = init["role"], init["cache_size"], init["universe"]
rng = random.Random(init["seed"] * 13 + 5)
print("{}", flush=True)
cache = []
for line in sys.stdin:
    m = json.loads(line)
    if m.get("end"): print("{}", flush=True); break
    if role == "attacker":
        print(json.dumps({"key": rng.randrange(min(K, 2 * C))}), flush=True); continue
    k, out = m["key"], {}
    if not m["hit"]:
        if m["need_evict"]:
            i = rng.randrange(len(cache)); out["evict"] = cache[i]; cache[i] = cache[-1]; cache.pop()
        cache.append(k)
    print(json.dumps(out), flush=True)

"""基线：防守 随机化 Marking（理论竞争比 O(log C)）；
攻击 = 自适应：在 C+1 个 key 上，优先重复请求刚 miss 过的邻居、否则按随机排列扫描。"""
import json, sys, random
init = json.loads(sys.stdin.readline()); role, C = init["role"], init["cache_size"]
rng = random.Random(init["seed"] * 31 + 17)
print("{}", flush=True)
cache, marked = set(), set()
perm, pi, last = list(range(C + 1)), 0, None
rng.shuffle(perm)
for line in sys.stdin:
    m = json.loads(line)
    if m.get("end"): print("{}", flush=True); break
    if role == "attacker":
        if pi >= len(perm): rng.shuffle(perm); pi = 0
        k = perm[pi]; pi += 1
        print(json.dumps({"key": k}), flush=True); continue
    k, out = m["key"], {}
    if m["hit"]: marked.add(k)
    else:
        if m["need_evict"]:
            un = [x for x in cache if x not in marked]
            if not un: marked.clear(); un = list(cache)
            e = rng.choice(sorted(un)); cache.discard(e); out["evict"] = e
        cache.add(k); marked.add(k)
    print(json.dumps(out), flush=True)

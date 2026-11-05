"""cache_duel bot 模板：同一个文件同时负责攻击和防守。"""
import json, sys, random

def main():
    init = json.loads(sys.stdin.readline())
    role, C, K, seed = init["role"], init["cache_size"], init["universe"], init["seed"]
    rng = random.Random(seed)
    print("{}", flush=True)                      # 握手
    cache = set()
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("end"):
            print("{}", flush=True); break
        if role == "attacker":
            # msg = {"t", "last_hit"}；回 {"key": 0..K-1}
            print(json.dumps({"key": rng.randrange(K)}), flush=True)
        else:
            # msg = {"t", "key", "hit", "need_evict"}；需要驱逐时回 {"evict": 缓存里的某个 key}
            k = msg["key"]; out = {}
            if not msg["hit"]:
                if msg["need_evict"]:
                    e = rng.choice(sorted(cache)); cache.discard(e); out["evict"] = e
                cache.add(k)
            print(json.dumps(out), flush=True)

main()

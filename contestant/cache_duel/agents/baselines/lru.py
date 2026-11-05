"""基线：防守 LRU；攻击 = 在 C+1 个 key 上循环扫描。"""
import json, sys
from collections import OrderedDict
init = json.loads(sys.stdin.readline()); role, C = init["role"], init["cache_size"]
print("{}", flush=True)
od = OrderedDict()
for line in sys.stdin:
    m = json.loads(line)
    if m.get("end"): print("{}", flush=True); break
    if role == "attacker":
        print(json.dumps({"key": m["t"] % (C + 1)}), flush=True); continue
    k, out = m["key"], {}
    if m["hit"]: od.move_to_end(k)
    else:
        if m["need_evict"]: out["evict"] = od.popitem(last=False)[0]
        od[k] = 1
    print(json.dumps(out), flush=True)

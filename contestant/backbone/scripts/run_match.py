# -*- coding: utf-8 -*-
"""单局骨干争夺对战与回放导出（受工作区边界守卫约束）。

用法：
  python scripts/run_match.py agents/submissions/my_agent.py agents/baselines/greedy.py --seed 1 --out replays/s1.json
"""
from __future__ import annotations

import argparse
import json
import random
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
import engine  # noqa: E402
import policy  # noqa: E402


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="骨干争夺单局对局与回放导出")
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--seed", type=int, default=random.SystemRandom().randrange(1, 2**31), help="默认随机；指定整数可重放")
    ap.add_argument("--ticks", type=int, default=engine.TICKS)
    ap.add_argument("--out", default="", help="可选：保存回放 JSON（选手工作区限 replays/ 下）")
    ap.add_argument("--unrestricted", action="store_true", help=argparse.SUPPRESS)
    ns = ap.parse_args()

    if ns.unrestricted and policy.is_contestant_workspace():
        print("选手工作区不支持 --unrestricted。", file=sys.stderr)
        sys.exit(2)

    try:
        if not ns.unrestricted:
            policy.check_pair(ns.a, ns.b)
        if ns.out and policy.is_contestant_workspace():
            policy.check_output_dir(os.path.dirname(os.path.abspath(ns.out)), "replays")
    except policy.PolicyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    def log(t, g):
        d = g.replay["days"][-1]
        print(
            f"day {d['day']:>2}  A nw {d['A']['nw']:>10.0f} pops {len(d['A']['pops']):>2}   "
            f"B nw {d['B']['nw']:>10.0f} pops {len(d['B']['pops']):>2}",
            flush=True,
        )

    r = engine.play(ns.a, ns.b, ns.seed, ticks=ns.ticks, log=log)
    print(json.dumps({k: v for k, v in r.items() if k != "replay"}, ensure_ascii=False, indent=1))
    s = engine.s_share(r["nw"][0], r["nw"][1])
    print(f"A 对 B 的份额 S = {s:.4f}（NW 差 {r['nw'][0] - r['nw'][1]:.0f}）")
    if ns.out:
        os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
        with open(ns.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False)
        print("replay ->", ns.out)


if __name__ == "__main__":
    main()

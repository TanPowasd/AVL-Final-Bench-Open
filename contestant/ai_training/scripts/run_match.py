# -*- coding: utf-8 -*-
"""单局前沿实验室对战/单人赛与回放导出（受工作区边界守卫约束）。

用法：
  python scripts/run_match.py agents/submissions/my_agent.py agents/baselines/merchant.py --seed 1 --out replays/s1.json
  python scripts/run_match.py agents/submissions/my_agent.py --solo --seed 1
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
    ap = argparse.ArgumentParser(description="前沿实验室单局对局与回放导出")
    ap.add_argument("a")
    ap.add_argument("b", nargs="?", default="")
    ap.add_argument("--solo", action="store_true")
    ap.add_argument("--seed", type=int, default=random.SystemRandom().randrange(1, 2**31), help="默认随机；指定整数可重放")
    ap.add_argument("--ticks", type=int, default=engine.TICKS)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", default="", help="可选：保存回放 JSON（选手工作区限 replays/ 下）")
    ap.add_argument("--organizer", action="store_true", help=argparse.SUPPRESS)
    ns = ap.parse_args()

    if ns.organizer and policy.is_contestant_workspace():
        print("独立选手工作区禁止 --organizer。", file=sys.stderr)
        sys.exit(2)
    if not ns.solo and not ns.b:
        ap.error("对抗赛需指定第二个对手文件；单人赛请加 --solo")

    try:
        if ns.solo:
            policy.check_agent_path(ns.a)
        elif not ns.organizer:
            policy.check_pair(ns.a, ns.b)
        if ns.out and policy.is_contestant_workspace():
            policy.check_output_dir(os.path.dirname(os.path.abspath(ns.out)), "replays")
    except policy.PolicyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    def log(t, g):
        d = g.replay["days"][-1]
        a, b_info = d["A"], d["B"]
        print(
            f"月 {(t + 1) // engine.MONTH:>2}  A 价值 {a['value']:>8.0f} 现金 {a['cash']:>7.0f} C {a['C']:.3f} "
            f"算力 {a['compute']:>6} 股权 {a['eq']:.2f}  |  B 价值 {b_info['value']:>8.0f} 现金 {b_info['cash']:>7.0f} "
            f"C {b_info['C']:.3f} 算力 {b_info['compute']:>6}  |  份额 {d['shares']}",
            flush=True,
        )

    b = engine.SOLO_NPC if ns.solo else ns.b
    r = engine.play(ns.a, b, ns.seed, ticks=ns.ticks, inproc_b=ns.solo, log=None if ns.quiet else log)
    print(json.dumps({k: v for k, v in r.items() if k != "replay"}, ensure_ascii=False, indent=1))
    if ns.solo:
        print(f"单人赛：A 的最高已上线模型真实能力 C = {r['C'][0]:.4f}")
    else:
        s = engine.s_share(r["value"][0], r["value"][1])
        print(f"A 对 B 的份额 S = {s:.4f}（价值差 {r['value'][0] - r['value'][1]:.0f}M，tanh 尺度 {engine.VALUE_SCALE:.0f}M）")
    if ns.out:
        os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
        with open(ns.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False)
        print("replay ->", ns.out)


if __name__ == "__main__":
    main()

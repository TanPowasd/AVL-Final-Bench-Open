# -*- coding: utf-8 -*-
"""公开陪练自测脚本（交换座位对局）。

用法：
  python scripts/spar.py --list
  python scripts/spar.py agents/submissions/my_agent.py scaling --seed-count 1
  python scripts/spar.py agents/submissions/my_agent.py --all --seed-count 1
"""
from __future__ import annotations

import argparse
import json
import random
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
import engine  # noqa: E402
import policy  # noqa: E402

PRESETS = {
    'blank': os.path.join(ROOT, 'agents/baselines/blank.py'),
    'scaling': os.path.join(ROOT, 'agents/baselines/scaling.py'),
    'merchant': os.path.join(ROOT, 'agents/baselines/merchant.py'),
}
ORDER = ['blank', 'scaling', 'merchant']
DESC = {
    'blank': '白板单轮盲训',
    'scaling': '重探针缩放律策略',
    'merchant': '重商业化 API 扩张策略',
}
SCALE = engine.VALUE_SCALE
METRIC_KEY = 'value'
HAS_SOLO = True


def resolve(name: str) -> str:
    if name in PRESETS:
        policy.check_agent_path(PRESETS[name])
        return PRESETS[name]
    policy.check_agent_path(name)
    if os.path.isfile(name):
        return os.path.abspath(name)
    raise SystemExit(f"未知陪练：{name}（用 --list 查看可选公开陪练）")


def _play_one(a: str, b: str, seed: int, ticks: int | None):
    kw = {}
    if ticks:
        kw["ticks"] = ticks
    r = engine.play(a, b, seed, **kw)
    r.pop("replay", None)
    return r


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="公开陪练自测")
    ap.add_argument("mine", nargs="?", default="")
    ap.add_argument("opponent", nargs="?", default="greedy" if "greedy" in PRESETS else ORDER[-1])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--solo", action="store_true", help="跑单人赛模式（若支持）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seeds", type=int, nargs="+", help="指定种子重放；默认每次随机抽样")
    ap.add_argument("--seed-count", type=int, default=None, help="随机种子数，默认 64")
    ap.add_argument("--ticks", type=int, default=0, help="可选：缩短局数快速调试接口")
    ap.add_argument("--organizer", action="store_true", help=argparse.SUPPRESS)
    ns = ap.parse_args()

    if ns.organizer and policy.is_contestant_workspace():
        print("独立选手工作区禁止 --organizer。", file=sys.stderr)
        sys.exit(2)

    if ns.list:
        print("可选公开陪练：")
        for k in ORDER:
            print(f"  {k:<10s} {DESC[k]}  <{os.path.relpath(PRESETS[k], ROOT)}>")
        if HAS_SOLO:
            print("  --solo     单人赛模式（挑战内置固定脚本 NPC 实验室，检验最高真实能力 C）")
        return
    if not ns.mine:
        ns.mine = policy.default_submission_path()
        if not ns.mine:
            print("可选公开陪练：")
            for k in ORDER:
                print(f"  {k:<10s} {DESC[k]}  <{os.path.relpath(PRESETS[k], ROOT)}>")
            print("\n未检测到提交文件；请传入路径，或保存至 ../submissions/ai_training.py（或 agents/submissions/my_agent.py）。")
            return

    cfg = json.load(open(os.path.join(ROOT, "config", "format.json"), encoding="utf-8"))
    if ns.seed_count is None:
        ns.seed_count = cfg["scoring"]["default_seed_count"]
    if ns.seed_count < 1:
        ap.error("--seed-count 必须为正整数")
    seeds = ns.seeds if ns.seeds is not None else random.SystemRandom().sample(range(1, 2**31), ns.seed_count)
    if not seeds or len(set(seeds)) != len(seeds):
        ap.error("种子列表不能为空或重复")
    print("本次场景种子：", seeds)
    ticks = ns.ticks if ns.ticks > 0 else None

    try:
        policy.check_agent_path(ns.mine)
    except policy.PolicyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    if not os.path.isfile(ns.mine):
        print(f"找不到文件：{ns.mine}", file=sys.stderr)
        sys.exit(2)
    mine_abs = os.path.abspath(ns.mine)

    rows = []
    if HAS_SOLO and ns.solo:
        for s in seeds:
            t0 = time.time()
            kw = {"inproc_b": True}
            if ticks:
                kw["ticks"] = ticks
            r = engine.play(mine_abs, engine.SOLO_NPC, s, **kw)
            r.pop("replay", None)
            dt = time.time() - t0
            c_val = r["C"][0]
            print(f"[solo] seed={s}  score={100 * c_val:.2f}/100  C={c_val:.4f}  value={r['value'][0]:.1f}M  ({dt:.1f}s)")
            rows.append({"mode": "solo", "seed": s, "C": c_val, "score": 100 * c_val, "value": r["value"][0], "sec": round(dt, 1)})
    else:
        targets = ORDER if ns.all else [ns.opponent]
        for t in targets:
            try:
                policy.check_opponent(mine_abs, resolve(t))
            except policy.PolicyError as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)

        for name in targets:
            opp_abs = resolve(name)
            s_vals, my_vals, opp_vals = [], [], []
            t0 = time.time()
            for s in seeds:
                for seat in (0, 1):
                    a, b = (mine_abs, opp_abs) if seat == 0 else (opp_abs, mine_abs)
                    r = _play_one(a, b, s, ticks)
                    mv = r[METRIC_KEY][seat]
                    ov = r[METRIC_KEY][1 - seat]
                    my_vals.append(mv)
                    opp_vals.append(ov)
                    s_vals.append(50.0 + 50.0 * math.tanh((mv - ov) / SCALE))
            dt = time.time() - t0
            s_avg = sum(s_vals) / len(s_vals)
            m_avg = sum(my_vals) / len(my_vals)
            o_avg = sum(opp_vals) / len(opp_vals)
            print(f"vs {name:<9s}  score={s_avg:.2f}/100  我均值={m_avg:,.1f}  对手均值={o_avg:,.1f}  ({dt:.1f}s)")
            rows.append({"opp": name, "score": s_avg, "mine_mean": m_avg, "opp_mean": o_avg, "sec": round(dt, 1)})

    out_dir = os.path.join(ROOT, "results", f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}")
    policy.check_output_dir(out_dir, "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "spar.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"scoring_version": "scoring_v2", "official": False, "max_score": 100, "mine": os.path.relpath(mine_abs, ROOT), "seeds": seeds, "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"\n记录已保存：{os.path.relpath(out_path, ROOT)}")


if __name__ == "__main__":
    main()

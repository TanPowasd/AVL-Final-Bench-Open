# -*- coding: utf-8 -*-
"""农场经营本地自测工具：与公开基线交换座位对局。

用法：
  python scripts/spar.py --list
  python scripts/spar.py agents/submissions/my_agent.py greedy
  python scripts/spar.py agents/submissions/my_agent.py --all --seed-count 2
"""
import argparse
import glob
import json
import math
import random
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
import arena  # noqa: E402
from loader import load_agent, load_trusted  # noqa: E402
import policy  # noqa: E402

BASELINE_DIR = os.path.join(ROOT, "agents", "baselines")
PRESETS = {
    "passive": os.path.join(BASELINE_DIR, "passive.py"),
    "random": os.path.join(BASELINE_DIR, "random_legal.py"),
    "greedy": os.path.join(BASELINE_DIR, "greedy_farm.py"),
}
ORDER = ["passive", "random", "greedy"]
DESC = {
    "passive": "全员待机基线",
    "random": "随机合法动作基线",
    "greedy": "基础贪心经营基线",
}


def load_any(path):
    policy.check_agent_path(path)
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        raise SystemExit("找不到文件：%s" % path)
    if policy.is_public_champion(p):
        return load_trusted(p)
    return load_agent(p)


def resolve(name):
    if name in PRESETS:
        policy.check_agent_path(PRESETS[name])
        return PRESETS[name]
    policy.check_agent_path(name)
    if os.path.isfile(name):
        return os.path.abspath(name)
    hits = glob.glob(name)
    if hits:
        policy.check_agent_path(hits[0])
        return os.path.abspath(hits[0])
    raise SystemExit("未知陪练：%s（用 --list 查看可选公开基线）" % name)


def list_opponents():
    print("可选公开基线（短名 / 路径）：")
    for k in ORDER:
        print("  %-9s %s   <%s>" % (k, DESC[k], os.path.relpath(PRESETS[k], ROOT)))


def run_one(mine_path, opp_name, seeds, cfg_all):
    opp_path = resolve(opp_name)
    policy.check_opponent(mine_path, opp_path)
    cfg = dict(cfg_all["match"])
    scale = cfg_all["scoring"]["margin_scale"]
    t0 = time.time()
    scores, mine_cash, opp_cash = [], [], []
    tally = {"w": 0, "d": 0, "l": 0}
    for seed in seeds:
        for seat in (0, 1):
            paths = [mine_path, opp_path] if seat == 0 else [opp_path, mine_path]
            policies = []
            for i, path in enumerate(paths):
                random.seed(seed * 2 + i)
                fn = load_any(path)[1]
                policies.append(arena.isolate_policy_rng(fn, seed * 2 + i))
            game = arena.play_game(policies, seed, cfg=cfg)
            mine, other = game["cash"][seat], game["cash"][1-seat]
            dead = game["forfeited"]
            score = (0.0 if dead[seat] else 100.0 if dead[1-seat]
                     else 50.0 + 50.0 * math.tanh((mine-other) / scale))
            scores.append(score)
            mine_cash.append(mine)
            opp_cash.append(other)
            tally["d" if game["winner"] == -1 else "w" if game["winner"] == seat else "l"] += 1
    score = sum(scores) / len(scores)
    mine = sum(mine_cash) / len(mine_cash)
    other = sum(opp_cash) / len(opp_cash)
    dt = time.time() - t0
    print("vs %s score=%.2f/100 | %d胜 %d平 %d负 | 我 %.0f vs 对手 %.0f | %.1fs"
          % (opp_name, score, tally["w"], tally["d"], tally["l"], mine, other, dt))
    return {"opp": opp_name, "score": score, **tally, "mine_cash": mine,
            "opp_cash": other, "sec": round(dt, 1), "games": len(scores)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="公开基线陪练自测")
    ap.add_argument("mine", nargs="?", help="你的 agent 文件")
    ap.add_argument("opponent", nargs="?", default="greedy", help="短名或路径（默认 greedy）")
    ap.add_argument("--all", action="store_true", help="依次对阵全部公开基线")
    ap.add_argument("--list", action="store_true", help="列出公开基线")
    ap.add_argument("--seeds", type=int, nargs="+", help="指定种子重放；默认每次随机抽样")
    ap.add_argument("--seed-count", type=int, default=None, help="随机种子数，默认 64")
    ap.add_argument("--organizer", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.organizer and policy.is_contestant_workspace():
        print("独立选手工作区禁止 --organizer。", file=sys.stderr)
        sys.exit(2)

    if args.list:
        list_opponents()
        return
    if not args.mine:
        args.mine = policy.default_submission_path()
        if not args.mine:
            list_opponents()
            print("\n未检测到提交文件；请传入路径，或保存至 ../submissions/kaggriculture.py（或 agents/submissions/my_agent.py）。")
            return

    cfg_all = json.load(open(os.path.join(ROOT, "config", "format.json"), encoding="utf-8"))
    if args.seed_count is None:
        args.seed_count = cfg_all["scoring"]["default_seed_count"]
    if args.seed_count < 1:
        ap.error("--seed-count 必须为正整数")
    seeds = args.seeds if args.seeds is not None else random.SystemRandom().sample(range(1, 2**31), args.seed_count)
    if not seeds or len(set(seeds)) != len(seeds):
        ap.error("种子列表不能为空或重复")
    print("本次场景种子：", seeds)
    targets = ORDER if args.all else [args.opponent]

    try:
        policy.check_agent_path(args.mine)
        for name in targets:
            policy.check_opponent(args.mine, resolve(name))
        mine_label, mine_fn = load_any(args.mine)
    except policy.PolicyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    print("我的 agent：%s（%d 种子 × 双座位 = %d 局/对手）\n"
          % (mine_label, len(seeds), len(seeds) * 2))
    rows = []
    for name in targets:
        try:
            rows.append(run_one(args.mine, name, seeds, cfg_all))
        except policy.PolicyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)

    out = os.path.join(ROOT, "results", f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}")
    policy.check_output_dir(out, "results")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "spar.json")
    policy.check_output_dir(path, "results")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"scoring_version": "scoring_v2", "official": False, "max_score": 100, "mine": mine_label, "seed_count": len(seeds), "seeds": seeds, "rows": rows}, f,
                  ensure_ascii=False, indent=2)
    print("\n记录已保存：", os.path.relpath(path, ROOT))


if __name__ == "__main__":
    main()

import argparse
import glob
import json
import random
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
import arena  # noqa: E402
from loader import load_agent, load_trusted  # noqa: E402
import policy  # noqa: E402


def pick(path):
    policy.check_agent_path(path)
    if policy.is_public_champion(path):
        return load_trusted(path)
    return load_agent(path)


def expand(p):
    policy.check_agent_path(p)
    hits = glob.glob(p)
    return hits[0] if hits else p


def main():
    ap = argparse.ArgumentParser(description="单场对抗：A vs B，多种子+座位交换")
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True, help="路径或 --self")
    ap.add_argument("--seeds", default="", help="逗号分隔；默认随机抽样")
    ap.add_argument("--seed-count", type=int, default=None, help="默认 64 个随机种子")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--organizer", action="store_true",
                    help="组委会模式：放行 选手vs选手（参赛自测请勿使用）")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="覆盖单步基础时限秒数；0=官方口径（1s/步 + 60s 超时池）")
    ap.add_argument("--overage", type=float, default=0.0,
                    help="覆盖每局超时池秒数；0=官方 60s")
    args = ap.parse_args()

    cfg_all = json.load(open(os.path.join(ROOT, "config", "format.json"), encoding="utf-8"))
    if args.seed_count is None:
        args.seed_count = cfg_all["scoring"]["default_seed_count"]
    if args.seed_count < 1:
        ap.error("--seed-count 必须为正整数")
    if args.seeds:
        seeds = [int(x) for x in args.seeds.split(",")]
    else:
        seeds = random.SystemRandom().sample(range(1, 2**31), args.seed_count)

    try:
        pa = expand(args.a)
        pb = pa if args.b == "--self" else expand(args.b)
        policy.check_pair(pa, pb, organizer=args.organizer)
    except policy.PolicyError as e:
        print(str(e))
        sys.exit(2)

    a = pick(pa)
    if args.b == "--self":
        b = pick(pa)
        b = (b[0] + "(镜像)", b[1])
    else:
        b = pick(pb)
    trace_dir = os.path.join(ROOT, "results", "traces") if args.trace else None
    cfg = {}
    if args.timeout:
        cfg["actTimeoutS"] = float(args.timeout)
    if args.overage:
        cfg["overageBudgetS"] = float(args.overage)
    cfg = cfg or None
    res = arena.run_match(a, b, seeds=seeds, cfg=cfg, trace_dir=trace_dir)
    path = arena.save_result(res)
    print(arena.summarize_match(res))
    for g in res["games"]:
        print("  seed=%4d %s A@seat%d -> cash %8.0f vs %8.0f  winner=%s %s" % (
            g["seed"], "swap" if g["seat_a"] else "fwd ", g["seat_a"],
            g["cash"][0], g["cash"][1], g["winner"], g["reason"]))
    print("saved:", path)


if __name__ == "__main__":
    main()

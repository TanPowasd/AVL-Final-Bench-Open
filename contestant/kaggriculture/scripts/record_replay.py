"""录制对局回放（本地可视化用）。

用法：
    python scripts/record_replay.py --a agents/submissions/mine.py --b greedy --seed 11
    python scripts/record_replay.py --a agents/baselines/greedy_farm.py --b passive --seeds 11,23 --delta
    python scripts/record_replay.py --a agents/submissions/mine.py --b greedy --seed 11 --serve

说明：
- 录制在裁判计时区间之外，不影响 1s/步 与 60s 超时池统计。
- --verify（默认开）会用同一配置对局**复算一次**，比对终局现金与回放 meta 一致。
- 选手之间的红线依旧生效（不能拿别的选手当对手、不能读别人的文件）。
"""
import argparse
import json
import random
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import arena  # noqa: E402
import policy  # noqa: E402
import replay as replay_mod  # noqa: E402
import spar  # noqa: E402


def rel(p):
    """相对仓库根显示；跨盘（C: vs F:）时 relpath 会抛 ValueError，退化成绝对路径。"""
    try:
        return os.path.relpath(p, ROOT)
    except ValueError:
        return os.path.abspath(p)


def main():
    ap = argparse.ArgumentParser(description="录制对抗赛回放（k4u-arena-replay/1）")
    ap.add_argument("--a", required=True, help="选手 A：路径或短名")
    ap.add_argument("--b", default="greedy", help="对手 B：公开基线或路径（短名见 spar.py --list）")
    ap.add_argument("--seed", type=int, default=random.SystemRandom().randrange(1, 2**31), help="默认随机；指定整数可重放")
    ap.add_argument("--seeds", default="", help="逗号分隔的多个种子（覆盖 --seed）")
    ap.add_argument("--swap", action="store_true", help="额外录一局座位交换")
    ap.add_argument("--every", type=int, default=1, help="每 N 步录一帧（默认 1=每步）")
    ap.add_argument("--steps", type=int, default=0, help="只录前 N 步（预览用；0=官方整局）")
    ap.add_argument("--delta", action="store_true", help="直接使用增量帧（体积更小）")
    ap.add_argument("--budget-kb", type=int, default=400, help="单局体积预算 KB，超了自动切换增量")
    ap.add_argument("--out", default=os.path.join(ROOT, "replays"))
    ap.add_argument("--timeout", type=float, default=0.0, help="覆盖单步时限；0=官方 1s")
    ap.add_argument("--overage", type=float, default=0.0, help="覆盖超时池；0=官方 60s")
    ap.add_argument("--no-verify", action="store_true", help="跳过复算校验（更快）")
    ap.add_argument("--organizer", action="store_true", help="组委会模式：放行 选手vs选手")
    ap.add_argument("--serve", action="store_true", help="录完后起本地 http 服务并打印播放地址")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()

    cfg_all = json.load(open(os.path.join(ROOT, "config", "format.json"), encoding="utf-8"))
    cfg = {"actTimeoutS": 1.0, "overageBudgetS": 60.0, "errorBudget": 30, "drawMarginPct": 1.0}
    if args.timeout:
        cfg["actTimeoutS"] = float(args.timeout)
    if args.overage:
        cfg["overageBudgetS"] = float(args.overage)
    cfg["episodeSteps"] = int(args.steps) if args.steps else cfg_all.get("episodeSteps", 720)
    cfg["boardSize"] = cfg_all.get("boardSize", 10)
    cfg["turnsPerDay"] = cfg_all.get("turnsPerDay", 24)
    cfg["startingMoney"] = cfg_all.get("startingMoney", 3000)

    seeds = [int(x) for x in args.seeds.split(",")] if args.seeds else [args.seed]

    try:
        policy.check_output_dir(args.out, "replays")
        if args.serve and policy.is_contestant_workspace():
            raise policy.PolicyError("选手工作区不提供 HTTP 共享服务；用离线播放器读取自己的回放。")
        a_path = spar.resolve(args.a)
        b_path = spar.resolve(args.b)
        policy.check_pair(a_path, b_path, organizer=args.organizer)
    except policy.PolicyError as e:
        print(str(e))
        sys.exit(2)

    label_a, fn_a = spar.load_any(a_path)
    label_b, fn_b = spar.load_any(b_path)
    engine_sha = replay_mod.sha256_file(os.path.join(ROOT, "engine", "rules.py"))
    os.makedirs(args.out, exist_ok=True)
    budget = args.budget_kb * 1024
    written = []

    for seed in seeds:
        for swap in ((False, True) if args.swap else (False,)):
            pols = [fn_b, fn_a] if swap else [fn_a, fn_b]
            labels = [label_b, label_a] if swap else [label_a, label_b]
            paths = [b_path, a_path] if swap else [a_path, b_path]
            seats = [{"seat": i, "label": labels[i], "file": rel(paths[i]),
                      "file_sha256": replay_mod.sha256_file(paths[i])} for i in range(2)]
            rec = replay_mod.ReplayRecorder(cfg, seats, seed, every=args.every, delta=args.delta,
                                           budget_bytes=budget, engine_sha=engine_sha, labels=labels)
            res = arena.play_game(pols, seed, cfg, recorder=rec)
            name = "replay_seed%d_%s.json" % (seed, "sw" if swap else "fw")
            path = os.path.join(args.out, name)
            policy.check_output_dir(path, "replays")
            rec.save(path, res)
            size = os.path.getsize(path)
            data = json.load(open(path, encoding="utf-8"))
            good, msg = replay_mod.verify_against_engine(data, res["cash"])
            print("[回放] %-24s %6.1f KB  帧=%d  终局 %s vs %s  %s" % (
                name, size / 1024.0, len(data["frames"]),
                int(res["cash"][0]), int(res["cash"][1]),
                "自校验 " + ("OK" if good else "失败: " + msg)))
            if not good:
                sys.exit(3)
            if not args.no_verify:
                again = arena.play_game(pols, seed, cfg)
                same = [round(c, 1) for c in again["cash"]] == [round(c, 1) for c in res["cash"]]
                print("        复算校验：%s（%s）" % ("一致" if same else "不一致",
                      "同一随机种子重跑终局现金相同" if same else "重跑结果不同，回放不可复现"))
                if not same:
                    sys.exit(4)
            meta = data["meta"]
            if meta.get("delta_forced"):
                print("        体积超预算，已自动切为增量帧（delta）")
            written.append(path)

    print("\n产出：")
    for p in written:
        print("  " + rel(p).replace(os.sep, "/"))

    if args.serve:
        import http.server
        import socketserver
        shown = rel(written[-1])
        if os.path.isabs(shown) or shown.startswith(".."):
            print("提示：回放不在仓库内，--serve 只服务仓库根目录，请把回放放进 replays/ 再播放。")
        url = "http://127.0.0.1:%d/viz/player.html#src=/%s" % (args.port, shown.replace(os.sep, "/"))
        print("\n播放地址（Ctrl+C 停止）：\n  " + url)
        os.chdir(ROOT)
        with socketserver.TCPServer(("127.0.0.1", args.port), http.server.SimpleHTTPRequestHandler) as httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")


if __name__ == "__main__":
    main()

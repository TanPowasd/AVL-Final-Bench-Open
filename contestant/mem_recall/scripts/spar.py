# -*- coding: utf-8 -*-
"""记忆检索本地开发集（data/dev）自测工具。

用法：
  python scripts/spar.py --list
  python scripts/spar.py agents/submissions/my_agent.py --sets cpzh,hive,scifact
  python scripts/spar.py agents/submissions/my_agent.py --all --jobs 2
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
import policy  # noqa: E402

DEV_DIR = os.path.join(ROOT, "data", "dev")
QUICK_SETS = "cpzh,hive,scifact"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="记忆检索开发集自测")
    ap.add_argument("mine", nargs="?", default="")
    ap.add_argument("--list", action="store_true", help="列出开发集与公开基线参考分")
    ap.add_argument("--sets", default=QUICK_SETS, help=f"指定评测集（逗号分隔，默认快速冒烟 {QUICK_SETS}）")
    ap.add_argument("--all", action="store_true", help="评测 data/dev 下全部 23 个开发集")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--per-q", action="store_true")
    ap.add_argument("--organizer", action="store_true", help=argparse.SUPPRESS)
    ns = ap.parse_args()

    if ns.organizer and policy.is_contestant_workspace():
        print("独立选手工作区禁止 --organizer。", file=sys.stderr)
        sys.exit(2)

    if not ns.list and not ns.mine:
        ns.mine = policy.default_submission_path()
    if ns.list or not ns.mine:
        print("公开基线（18 个计分开发集等权宏平均）：")
        print("  bm25        0.564  (mem=0.606, en=0.492, zh=0.545)  <agents/baselines/bm25.py>")
        print("  charoverlap 0.433  (mem=0.519, en=0.272, zh=0.408)  <agents/baselines/charoverlap.py>")
        if os.path.isdir(DEV_DIR):
            files = sorted(f for f in os.listdir(DEV_DIR) if f.endswith((".json", ".json.gz")))
            print(f"\n本地开发集 data/dev/（共 {len(files)} 个文件）：")
            print("  " + ", ".join(f.replace(".json.gz", "").replace(".json", "") for f in files))
        return

    try:
        policy.check_agent_path(ns.mine)
    except policy.PolicyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    if not os.path.isfile(ns.mine):
        print(f"找不到文件：{ns.mine}", file=sys.stderr)
        sys.exit(2)

    out_dir = os.path.join(ROOT, "results", time.strftime("%Y%m%d-%H%M%S"))
    policy.check_output_dir(out_dir, "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "spar.json")

    cmd = [
        sys.executable, "-X", "utf8",
        os.path.join(ROOT, "engine", "runner.py"),
        "--bot", os.path.abspath(ns.mine),
        "--data", DEV_DIR,
        "--jobs", str(ns.jobs),
        "--out", out_path,
    ]
    if not ns.all and ns.sets:
        cmd += ["--sets", ns.sets]
    if ns.per_q:
        cmd.append("--per-q")

    rc = subprocess.call(cmd, cwd=ROOT)
    if rc == 0:
        print(f"\n记录已保存：{os.path.relpath(out_path, ROOT)}")
    sys.exit(rc)


if __name__ == "__main__":
    main()

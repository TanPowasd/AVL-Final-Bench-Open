# -*- coding: utf-8 -*-
"""公开样例与本地自定义输入自测脚本。

用法：
  python scripts/spar.py --list
  python scripts/spar.py agents/submissions/my_solution.cpp
  python scripts/spar.py agents/submissions/my_solution.cpp --input logs/custom.in
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
import policy  # noqa: E402
import runner  # noqa: E402

SAMPLES_DIR = os.path.join(ROOT, "data", "samples")


def list_samples() -> list[dict]:
    ins = sorted(f for f in os.listdir(SAMPLES_DIR) if f.endswith(".in"))
    cases = []
    for fn in ins:
        stem = fn[:-3]
        out_fn = stem + ".out"
        c = {"name": f"sample_{stem}", "in": fn, "is_sample": True}
        if os.path.isfile(os.path.join(SAMPLES_DIR, out_fn)):
            c["out"] = out_fn
        cases.append(c)
    return cases


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="公开样例本地编译与自测")
    ap.add_argument("src", nargs="?", default="", help="C++20 源文件路径（如 agents/submissions/my_solution.cpp）")
    ap.add_argument("--list", action="store_true", help="列出公开样例")
    ap.add_argument("--input", default="", help="可选：测试单个自定义输入文件（需放在 data/samples/ 或 logs/ 下）")
    ap.add_argument("--organizer", action="store_true", help=argparse.SUPPRESS)
    ns = ap.parse_args()

    if ns.organizer and policy.is_contestant_workspace():
        print("独立选手工作区禁止 --organizer；正式评测只能在组委会目录运行。", file=sys.stderr)
        sys.exit(2)

    samples = list_samples()
    if not ns.list and not ns.src:
        ns.src = policy.default_submission_path()
    if ns.list or not ns.src:
        print(f"公开自测样例（共 {len(samples)} 组）：")
        for c in samples:
            print(f"  - {c['name']}  (data/samples/{c['in']})")
        print("\n也可将自定义测试输入写入 logs/custom.in 并用 --input logs/custom.in 运行。")
        return

    try:
        policy.check_source_path(ns.src)
        if ns.input:
            policy.check_input_path(ns.input)
    except policy.PolicyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    if not os.path.isfile(ns.src):
        print(f"找不到源文件：{ns.src}", file=sys.stderr)
        sys.exit(2)

    with tempfile.TemporaryDirectory(prefix="afb_spar_cpp_") as build_dir:
        comp = runner.compile_submission(ns.src, build_dir, ROOT)
        if not comp.ok:
            print(f"[CE] 编译失败 ({comp.elapsed_ms:.0f} ms):\n{comp.log}")
            sys.exit(1)

        import calib
        _f = calib.speed_factor(verbose=True)
        print(f"编译成功 ({comp.elapsed_ms:.0f} ms) · 时限 {runner.TIME_LIMIT_MS} 标准ms（本机实际 {runner.TIME_LIMIT_MS * _f:.0f} ms，速度系数 {_f:.3f}） · 内存 {runner.MEMORY_LIMIT_MB} MB")
        rows = []
        if ns.input:
            in_dir = os.path.dirname(os.path.abspath(ns.input))
            in_fn = os.path.basename(ns.input)
            case = {"name": in_fn, "in": in_fn, "is_sample": True}
            out_cand = os.path.splitext(os.path.abspath(ns.input))[0] + ".out"
            if os.path.isfile(out_cand):
                case["out"] = os.path.basename(out_cand)
            res = runner.run_sample_or_custom(comp.exe_path, case, in_dir)
            rows.append(res)
            print(f"  {res['name']:<24s} {res['verdict']:<4s} {res.get('time_ms', 0.0):7.1f} ms  {res.get('mem_mb', 0.0):6.1f} MB  {res.get('msg', '')}")
        else:
            for c in samples:
                res = runner.run_sample_or_custom(comp.exe_path, c, SAMPLES_DIR)
                rows.append(res)
                print(f"  {res['name']:<24s} {res['verdict']:<4s} {res.get('time_ms', 0.0):7.1f} ms  {res.get('mem_mb', 0.0):6.1f} MB  {res.get('msg', '')}")

    out_dir = os.path.join(ROOT, "results", time.strftime("%Y%m%d-%H%M%S"))
    policy.check_output_dir(out_dir, "results")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "spar.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"src": os.path.relpath(os.path.abspath(ns.src), ROOT), "cases": rows}, f, ensure_ascii=False, indent=2)
    print(f"\n自测记录已保存：{os.path.relpath(out_file, ROOT)}")


if __name__ == "__main__":
    main()

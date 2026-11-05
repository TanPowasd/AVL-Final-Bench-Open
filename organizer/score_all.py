# -*- coding: utf-8 -*-
r"""组委会自动分发与单模型统一评分工具（organizer/score_all.py）。

核心流程：
  AI 在选手区完成各题测试后，将最终代码以 <题目名>.py / <题目名>.cpp 放入统一提交文件夹
  （默认 ../contestant/submissions/，也可传入任意外部提交文件夹）。
  运行本脚本会自动清空旧残留、按题目名将文件分发至 organizer/<problem>/agents/submissions/，
  依次调用各题正式评测脚本，并在末尾打印 8 题成绩与等权总分（同步保存至 organizer/results/summary.json）。

典型用法：
  # 1. 直接分发默认 ../contestant/submissions/ 下的提交并运行正式评分
  python organizer/score_all.py

  # 2. 指定某个 AI 提交文件夹（内含 ai_training.py, ball_machine.cpp 等）自动分发并评分
  python organizer/score_all.py D:\runs\model_submissions --name gpt5

  # 3. 仅自动分发到各题（不立即启动评分）
  python organizer/score_all.py D:\runs\model_submissions --dispatch-only

  # 4. 只分发并评测部分指定赛题
  python organizer/score_all.py --problems ball_machine fruehlingsbeginn partition cache_duel

  # 5. 查看当前各题已分发的待评文件状态
  python organizer/score_all.py --list
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ORGANIZER_ROOT = Path(__file__).resolve().parent
DEFAULT_SUBMISSIONS_DIR = ORGANIZER_ROOT.parent / "contestant" / "submissions"

PROBLEM_EXT = {
    "ai_training": ".py",
    "backbone": ".py",
    "ball_machine": ".cpp",
    "cache_duel": ".py",
    "fruehlingsbeginn": ".cpp",
    "kaggriculture": ".py",
    "mem_recall": ".py",
    "partition": ".cpp",
}
PROBLEMS = list(PROBLEM_EXT.keys())

RESERVED_STEMS = {
    "blank", "ref", "strong", "champion", "starter",
    "fifo", "lru", "marking", "rand",
    "basic", "greedy", "shortest",
    "passive", "random", "random_legal", "greedy_farm",
    "merchant", "scaling", "bm25", "charoverlap",
}


def sanitize_model_name(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw.strip()).strip("_")
    if not cleaned or cleaned.lower() in {"submissions", "contestant", "submission"}:
        cleaned = "candidate"
    if cleaned.lower() in RESERVED_STEMS:
        cleaned = f"candidate_{cleaned}"
    return cleaned


def resolve_source_dir(raw_dir: Path) -> Path:
    resolved = raw_dir.resolve()
    if (resolved / "submissions").is_dir() and not any(
        (resolved / f"{prob}{ext}").is_file() for prob, ext in PROBLEM_EXT.items()
    ):
        return resolved / "submissions"
    return resolved


def clear_problem_submissions(problems: list[str]) -> None:
    for prob in problems:
        ext = PROBLEM_EXT[prob]
        sub_dir = ORGANIZER_ROOT / prob / "agents" / "submissions"
        if sub_dir.is_dir():
            for p in sub_dir.glob(f"*{ext}"):
                if not p.name.startswith("_"):
                    p.unlink()


def dispatch_submissions(src_dir: Path, model_stem: str, problems: list[str]) -> dict[str, Path | None]:
    dispatched: dict[str, Path | None] = {}
    print(f"统一提交源目录：{src_dir}")
    print(f"分发目标标识名：{model_stem}")
    print("-" * 82)
    print(f"{'赛题':<18s} {'期望文件名':<24s} {'状态':<10s} 分发目标路径")
    print("-" * 82)
    for prob in problems:
        ext = PROBLEM_EXT[prob]
        expected_name = f"{prob}{ext}"
        src_file = src_dir / expected_name
        if not src_file.is_file():
            dispatched[prob] = None
            print(f"{prob:<18s} {expected_name:<24s} {'[未提交]':<10s} -")
            continue
        dst_dir = ORGANIZER_ROOT / prob / "agents" / "submissions"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_file = dst_dir / f"{model_stem}{ext}"
        shutil.copy2(src_file, dst_file)
        dispatched[prob] = dst_file
        print(f"{prob:<18s} {expected_name:<24s} {'[已分发]':<10s} {dst_file.relative_to(ORGANIZER_ROOT).as_posix()}")
    print("-" * 82)
    ok_cnt = sum(1 for v in dispatched.values() if v is not None)
    print(f"共分发 {ok_cnt} / {len(problems)} 道赛题。\n")
    return dispatched


def list_current_dispatched(problems: list[str]) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    print(f"{'赛题':<18s} {'类型':<6s} {'当前待评文件数':<14s} 文件列表")
    print("-" * 78)
    for prob in problems:
        ext = PROBLEM_EXT[prob]
        sub_dir = ORGANIZER_ROOT / prob / "agents" / "submissions"
        files = sorted(p for p in sub_dir.glob(f"*{ext}") if not p.name.startswith("_")) if sub_dir.is_dir() else []
        found[prob] = files
        names = ", ".join(p.name for p in files) if files else "(空)"
        print(f"{prob:<18s} {ext:<6s} {len(files):<14d} {names}")
    return found


def extract_problem_metric(prob: str, model_stem: str) -> dict[str, Any]:
    lb_path = ORGANIZER_ROOT / prob / "results" / "leaderboard.json"
    if not lb_path.is_file():
        return {"status": "missing_result", "display": "无结果文件"}
    try:
        data = json.loads(lb_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "parse_error", "display": f"解析失败: {exc}"}

    if prob in ("ball_machine", "fruehlingsbeginn", "partition"):
        rows = data.get("leaderboard", [data] if "submission" in data else [])
        for r in rows:
            if r.get("submission") == model_stem:
                sc = float(r.get("score", 0.0))
                mx = float(r.get("max_score", 100.0))
                st = r.get("status", "?")
                return {"status": st, "score": sc, "max_score": mx, "display": f"{sc:.2f} / {mx:.0f} ({st})"}

    elif prob in ("ai_training", "backbone", "cache_duel", "kaggriculture"):
        import math
        from strategy_scoring import VERSION, digest, load_profile, profile_fingerprint

        if data.get("scoring_version") != VERSION or data.get("official") is not True:
            return {"status": "incompatible_scoring", "display": "旧版或练习成绩，须使用 scoring_v2 正式重测"}
        try:
            expected_profile = profile_fingerprint(load_profile(prob))
        except (OSError, ValueError, KeyError) as exc:
            return {"status": "invalid_profile", "display": f"评分配置校验失败：{exc}"}
        if data.get("profile_id") != expected_profile:
            return {"status": "stale_profile", "display": "评分配置或引擎已变化，须重新评测"}
        for row in data.get("table", []):
            if row.get("bot") != model_stem:
                continue
            try:
                score = float(row["score"])
                if not math.isfinite(score) or not 0 <= score <= 100:
                    raise ValueError("score outside [0,100]")
                source = ORGANIZER_ROOT / prob / "agents/submissions" / f"{model_stem}.py"
                if not source.is_file() or row.get("submission_sha256") != digest(source):
                    return {"status": "stale_submission", "display": "提交代码与成绩指纹不符，须重新评测"}
                components = row["components"]
                if not isinstance(components, dict) or not components:
                    raise ValueError("missing components")
                if any(not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 100
                       for v in components.values()):
                    raise ValueError("invalid component score")
                detail = ", ".join(f"{k}={v:.2f}" for k, v in components.items())
                ci = row.get("ci95")
                if ci is not None and (not isinstance(ci, list) or len(ci) != 2
                                       or any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in ci)
                                       or not 0 <= ci[0] <= ci[1] <= 100):
                    raise ValueError("invalid confidence interval")
                interval = f"; 95% CI [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "; CI 样本不足"
                return {"status": row.get("status", "OK"), "score": score, "max_score": 100,
                        "scoring_version": VERSION, "profile_id": expected_profile,
                        "components": components, "ci95": ci, "raw": row.get("raw", {}),
                        "sampling": data.get("sampling"), "seeds": data.get("seeds"),
                        "execution": data.get("execution"),
                        "fault_games": row.get("fault_games", 0), "n_games": row.get("n_games", 0),
                        "display": f"{score:.2f} / 100 ({detail}{interval})"}
            except (KeyError, ValueError, TypeError) as exc:
                return {"status": "invalid_result", "display": f"新版成绩字段非法：{exc}"}

    elif prob == "mem_recall":
        if model_stem in data:
            entry = data[model_stem]
            m_off = entry.get("_macro_official") or {}
            m_dev = entry.get("_macro_dev") or {}
            chosen = m_off if m_off.get("n_sets") else m_dev
            split_tag = "official" if m_off.get("n_sets") else "dev"
            if chosen:
                sc = float(chosen.get("score", 0.0))
                return {"status": "OK", "score": sc, "split": split_tag,
                        "display": f"{sc:.4f} ({split_tag} 宏平均, {chosen.get('n_sets', 0)} 集)"}

    return {"status": "not_found", "display": f"未在结果中找到 {model_stem}"}


def overall_score(problems: list[str], rows: dict[str, Any]) -> float:
    """总分：8 题百分制得分等权平均；未提交或无有效成绩的题计 0，子集评测同样除以 8。"""
    total = 0.0
    for prob in problems:
        score = rows[prob].get("score")
        if score is not None:
            total += float(score) * (100.0 if prob == "mem_recall" else 1.0)
    return round(total / len(PROBLEMS), 2)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="AVL Final Bench 自动分发统一提交文件夹并执行单模型评分")
    ap.add_argument(
        "submissions_dir",
        nargs="?",
        type=Path,
        default=None,
        help="统一提交文件夹路径（内含 <题目名>.py / <题目名>.cpp，默认 ../contestant/submissions）",
    )
    ap.add_argument("--from", dest="from_dir", type=Path, default=None, help="同位置参数：指定统一提交文件夹路径")
    ap.add_argument("--name", default="", help="本次评测的模型标识名（默认自动取文件夹名或 candidate）")
    ap.add_argument("--problems", nargs="*", choices=PROBLEMS, default=PROBLEMS, help="指定要分发与评测的赛题子集")
    ap.add_argument("--dispatch-only", "--collect-only", dest="dispatch_only", action="store_true", help="仅自动分发提交文件，不立即启动评分")
    ap.add_argument("--score-existing", action="store_true", help="跳过分发步骤，直接对各题 agents/submissions/ 中现有文件运行评分")
    ap.add_argument("--keep-existing", action="store_true", help="分发前不清空各题 agents/submissions/ 中的旧文件")
    ap.add_argument("--list", action="store_true", help="仅查看当前各题 agents/submissions/ 中已分发的文件")
    ap.add_argument("--strategy-seed-count", type=int, help="策略题本次随机场景数（默认各题配置；增加场景后取平均）")
    ap.add_argument("--jobs", "--strategy-jobs", dest="strategy_jobs", type=int,
                    help="策略题期望并行对局数；自动预留 CPU/内存，手动值也受安全上限约束")
    ap.add_argument("--mem-allow-dev", action="store_true", help="mem_recall 缺隐藏集时改用开发集，总分不是正式成绩")
    ap.add_argument("--out", type=Path, default=ORGANIZER_ROOT / "results" / "summary.json", help="总成绩单 JSON 保存路径")
    ns = ap.parse_args()
    if ns.strategy_seed_count is not None and ns.strategy_seed_count < 1:
        ap.error("--strategy-seed-count 必须为正整数")
    if ns.strategy_jobs is not None and ns.strategy_jobs < 1:
        ap.error("--jobs 必须为正整数")

    if ns.list:
        list_current_dispatched(ns.problems)
        return

    raw_src = ns.from_dir or ns.submissions_dir or DEFAULT_SUBMISSIONS_DIR
    src_dir = resolve_source_dir(raw_src)
    inferred_name = ns.name or raw_src.resolve().name
    model_stem = sanitize_model_name(inferred_name)

    if not ns.score_existing:
        if not src_dir.is_dir():
            raise SystemExit(f"找不到统一提交文件夹：{src_dir}")
        if not ns.keep_existing:
            clear_problem_submissions(ns.problems)
        dispatched = dispatch_submissions(src_dir, model_stem, ns.problems)
        if ns.dispatch_only:
            return
        active_problems = [p for p in ns.problems if dispatched.get(p) is not None]
    else:
        found = list_current_dispatched(ns.problems)
        active_problems = [p for p in ns.problems if found.get(p)]
        if active_problems and not ns.name:
            first_file = found[active_problems[0]][0]
            model_stem = first_file.stem

    if not active_problems:
        print("没有任何已提交的赛题文件可供评测（请将 <题目名>.py / <题目名>.cpp 放入统一提交目录后再运行）。")
        return

    summary_rows: dict[str, Any] = {}
    failures: list[tuple[str, int]] = []
    t_all = time.time()

    for prob in ns.problems:
        if prob not in active_problems:
            summary_rows[prob] = {"submitted": False, "status": "unsubmitted", "display": "未提交"}
            continue
        print(f"\n{'=' * 78}\n[正式评分] {prob}  (提交标识: {model_stem})\n{'=' * 78}")
        t0 = time.time()
        score_script = ORGANIZER_ROOT / prob / "scripts" / "score.py"
        command = [sys.executable, "-X", "utf8", str(score_script)]
        if ns.strategy_seed_count is not None and prob in ("ai_training", "backbone", "cache_duel", "kaggriculture"):
            command.extend(["--seed-count", str(ns.strategy_seed_count)])
        if ns.strategy_jobs is not None and prob in ("ai_training", "backbone", "cache_duel", "kaggriculture"):
            command.extend(["--jobs", str(ns.strategy_jobs)])
        if prob == "mem_recall" and ns.mem_allow_dev:
            command.append("--allow-dev")
        rc = subprocess.call(command, cwd=ORGANIZER_ROOT / prob)
        dt = round(time.time() - t0, 1)
        if rc != 0:
            failures.append((prob, rc))
            summary_rows[prob] = {"submitted": True, "status": f"error_rc_{rc}", "wall_s": dt, "display": f"评测异常 (exit={rc})"}
        else:
            metric = extract_problem_metric(prob, model_stem)
            if prob in ("ai_training", "backbone", "cache_duel", "kaggriculture") and metric.get("status") != "OK":
                failures.append((prob, metric.get("status", "invalid_result")))
            metric["submitted"] = True
            metric["wall_s"] = dt
            summary_rows[prob] = metric

    total_wall = round(time.time() - t_all, 1)
    print(f"\n{'=' * 78}")
    print(f"AVL Final Bench 单模型正式评分汇总 · 模型标识: {model_stem} · 总耗时: {total_wall:.1f}s")
    print("=" * 78)
    print(f"{'赛题':<18s} {'状态':<10s} {'耗时(s)':<9s} 正式成绩")
    print("-" * 78)
    for prob in ns.problems:
        info = summary_rows[prob]
        st = "已评测" if info.get("submitted") else "未提交"
        ws = f"{info.get('wall_s', 0.0):.1f}" if info.get("submitted") else "-"
        print(f"{prob:<18s} {st:<10s} {ws:<9s} {info.get('display', '-')}")
    print("-" * 78)
    total_score = overall_score(ns.problems, summary_rows)
    print(f"总分（8 题等权平均，满分 100）：{total_score:.2f}")
    if summary_rows.get("mem_recall", {}).get("split") == "dev":
        print("警告：mem_recall 用的是公开开发集，本次总分不是正式成绩。")
    print("=" * 78)

    ns.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model_stem,
        "source_dir": str(src_dir),
        "total_wall_s": total_wall,
        "total_score": total_score,
        "problems": summary_rows,
    }
    ns.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"汇总成绩已保存：{ns.out}")

    if failures:
        print("\n部分赛题评分返回非零退出码：", failures, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

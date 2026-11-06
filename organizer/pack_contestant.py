# -*- coding: utf-8 -*-
"""导出完整干净的参赛选手工作区（含顶层统一交卷目录 submissions/，默认输出至 ../contestant）。

用法：
  python organizer/pack_contestant.py --out ../players/model_a
  python organizer/pack_contestant.py --force
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ORGANIZER_ROOT = Path(__file__).resolve().parent
DEFAULT_DEST = ORGANIZER_ROOT.parent / "contestant"

PROBLEMS = ['ai_training', 'backbone', 'ball_machine', 'cache_duel', 'fruehlingsbeginn', 'kaggriculture', 'mem_recall', 'partition']

SUITE_DOCS_DIR = ORGANIZER_ROOT / "contestant_suite"
SUITE_DOCS = ("README.md", "AGENTS.md")

# data/dev 是公开语料，文本里可能自然出现这些词，不做词语检查。
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".html", ".js", ".cpp", ".h"}
FORBIDDEN_WORDS = re.compile(r"organizer|组委会|champion|OPEN_ISSUES|HEAVY_TESTS|TanPowasd|Powasd|/data/ws|/data/home")
FORBIDDEN_NAMES = re.compile(r"(^|/)(\.git|__pycache__|hidden|official|reference)(/|$)|\.(pyc|so|log)$|problem_mod\.py$")


def prepare_suite_root(dest: Path, reset_submissions: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in SUITE_DOCS:
        shutil.copyfile(SUITE_DOCS_DIR / name, dest / name)
    sub_dir = dest / "submissions"
    if reset_submissions and sub_dir.exists():
        shutil.rmtree(sub_dir)
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / ".gitkeep").write_text("", encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_hashes() -> dict[str, Path]:
    """隐藏数据、参考对手等不得进入选手包的文件。公开样例除外。"""
    hashes: dict[str, Path] = {}
    for prob in PROBLEMS:
        base = ORGANIZER_ROOT / prob
        for directory in (base / "data" / "official", base / "hidden", base / "agents" / "reference",
                          base / "engine" / "champion"):
            for path in directory.rglob("*") if directory.is_dir() else ():
                if path.is_file() and path.stat().st_size > 32 and not path.name.startswith("sample"):
                    hashes[_sha256(path)] = path
        problem_mod = base / "engine" / "problem_mod.py"
        if problem_mod.is_file():
            hashes[_sha256(problem_mod)] = problem_mod
    return hashes


def check_purity(dest: Path) -> list[str]:
    """返回选手包的纯净性问题。空列表表示通过。"""
    protected = _protected_hashes()
    problems: list[str] = []
    for path in sorted(p for p in dest.rglob("*") if p.is_file()):
        relative = path.relative_to(dest)
        if FORBIDDEN_NAMES.search(relative.as_posix()):
            problems.append(f"禁止的文件：{relative}")
        if path.stat().st_size > 32 and _sha256(path) in protected:
            problems.append(f"与保密文件相同：{relative}")
        if path.suffix in TEXT_SUFFIXES and "data/dev" not in relative.as_posix():
            match = FORBIDDEN_WORDS.search(path.read_text(encoding="utf-8", errors="replace"))
            if match:
                problems.append(f"含禁用词 {match[0]}：{relative}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description="导出干净的 AVL Final Bench 参赛选手工作区")
    ap.add_argument("--out", type=Path, default=DEFAULT_DEST, help="输出目标目录（默认 ../contestant）")
    ap.add_argument("--problems", nargs="*", choices=PROBLEMS, default=PROBLEMS)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--root-only", action="store_true")
    ap.add_argument("--zip", action="store_true")
    ns = ap.parse_args()

    dest = ns.out.resolve()
    if dest.is_relative_to(ORGANIZER_ROOT):
        raise SystemExit(f"选手目录不能位于 organizer/ 内部：{dest}")
    if dest.exists() and not ns.force and not ns.root_only:
        raise SystemExit(f"目标目录已存在：{dest}（如需覆盖刷新请加 --force，或用 --out 指定新目录）")

    prepare_suite_root(dest, reset_submissions=(ns.force and not ns.root_only))
    if ns.root_only:
        print(f"已刷新顶层 README.md、AGENTS.md 与 submissions/：{dest}")
        return

    for prob in ns.problems:
        pack_script = ORGANIZER_ROOT / prob / "scripts" / "pack_contestant.py"
        prob_dest = dest / prob
        cmd = [sys.executable, "-X", "utf8", str(pack_script), "--out", str(prob_dest)]
        if ns.force:
            cmd.append("--force")
        subprocess.run(cmd, cwd=ORGANIZER_ROOT / prob, check=True)
        print(f"  [OK] {prob:<18s} -> {prob_dest}")

    issues = check_purity(dest)
    if issues:
        raise SystemExit("选手包未通过纯净检查，未打包：\n" + "\n".join(issues))
    print(f"纯净检查通过：{dest}")

    if ns.zip:
        archive = dest.with_name(dest.name + ".zip")
        if archive.exists():
            if not ns.force:
                raise SystemExit(f"压缩包已存在：{archive}")
            archive.unlink()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(dest.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(dest).as_posix())
        print(f"已生成压缩包：{archive}")

    print(f"\n选手工作区已就绪：{dest}（统一交卷目录：{dest / 'submissions'}）")


if __name__ == "__main__":
    main()

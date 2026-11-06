# -*- coding: utf-8 -*-
"""选手工作区边界守卫：禁止跨目录访问、禁止选手互测、禁止 --unrestricted，兼容顶层 ../submissions/mem_recall.py。"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBLEM_ID = 'mem_recall'
SUBMISSION_DIR = os.path.join(ROOT, "agents", "submissions")
SUITE_SUBMISSIONS_DIR = os.path.join(os.path.dirname(ROOT), "submissions")
UNIFIED_SUBMISSION_FILE = os.path.join(SUITE_SUBMISSIONS_DIR, f"{PROBLEM_ID}.py")
WORKSPACE_FILE = os.path.join(ROOT, "config", "workspace.json")

PUBLIC_AGENTS = (
    os.path.join(ROOT, *['agents', 'starter.py']),
    os.path.join(ROOT, *['agents', 'baselines', 'bm25.py']),
    os.path.join(ROOT, *['agents', 'baselines', 'charoverlap.py']),
)
PUBLIC_OPPONENTS = PUBLIC_AGENTS[1:] if len(PUBLIC_AGENTS) > 1 else PUBLIC_AGENTS


class PolicyError(Exception):
    pass


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(directory)]) == _norm(directory)
    except ValueError:
        return False


def is_contestant_workspace() -> bool:
    return os.path.isfile(WORKSPACE_FILE)


def is_unified_submission(path: str) -> bool:
    return os.path.isdir(SUITE_SUBMISSIONS_DIR) and _norm(path) == _norm(UNIFIED_SUBMISSION_FILE)


def default_submission_path() -> str:
    for cand in (
        UNIFIED_SUBMISSION_FILE if os.path.isdir(SUITE_SUBMISSIONS_DIR) else "",
        os.path.join(SUBMISSION_DIR, f"{PROBLEM_ID}.py"),
        os.path.join(SUBMISSION_DIR, "my_agent.py"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def is_submission(path: str) -> bool:
    return _within(path, SUBMISSION_DIR) or is_unified_submission(path)


def check_workspace_path(path: str) -> None:
    if is_contestant_workspace() and not (_within(path, ROOT) or is_unified_submission(path)):
        raise PolicyError(f"选手自测工具只能访问当前题目工作区或 ../submissions/{PROBLEM_ID}.py，禁止访问外部路径。")


def check_agent_path(path: str) -> None:
    if not is_contestant_workspace():
        return
    check_workspace_path(path)
    if not is_submission(path) and _norm(path) not in {_norm(p) for p in PUBLIC_AGENTS}:
        raise PolicyError(f"只能加载自己的提交（../submissions/{PROBLEM_ID}.py 或 agents/submissions/）或公开基线文件。")


def check_opponent(mine: str, opponent: str) -> None:
    check_agent_path(mine)
    check_agent_path(opponent)
    mine_abs, opp_abs = _norm(mine), _norm(opponent)
    if opp_abs == mine_abs:
        return
    if opp_abs not in {_norm(p) for p in PUBLIC_OPPONENTS}:
        raise PolicyError("赛制红线：自测对手必须是公开基线（或自身镜像），禁止与其他选手提交互测。")


def check_pair(path_a: str, path_b: str, unrestricted: bool = False) -> None:
    if is_contestant_workspace():
        if unrestricted:
            raise PolicyError("选手工作区不支持 --unrestricted。")
        check_agent_path(path_a)
        check_agent_path(path_b)
    if unrestricted or _norm(path_a) == _norm(path_b):
        return
    if is_submission(path_a) and is_submission(path_b):
        raise PolicyError("赛制红线：禁止两个不同提交文件互测，请以公开基线作为对手（或自身镜像）。")
    if is_contestant_workspace():
        pub = {_norm(p) for p in PUBLIC_AGENTS}
        if _norm(path_a) not in pub and _norm(path_b) not in pub:
            raise PolicyError("赛制红线：对局至少一方须为公开基线（或自身镜像）。")


def check_output_dir(path: str, category: str = "results") -> None:
    if not is_contestant_workspace():
        return
    if not _within(path, ROOT) or not _within(path, os.path.join(ROOT, category)):
        raise PolicyError(f"{category} 只能写入当前工作区的 {category}/ 目录。")

# -*- coding: utf-8 -*-
"""选手工作区路径与自测边界守卫（兼容顶层统一提交文件 ../submissions/ball_machine.cpp）。"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBLEM_ID = 'ball_machine'
SUBMISSION_DIR = os.path.join(ROOT, "agents", "submissions")
SUITE_SUBMISSIONS_DIR = os.path.join(os.path.dirname(ROOT), "submissions")
UNIFIED_SUBMISSION_FILE = os.path.join(SUITE_SUBMISSIONS_DIR, f"{PROBLEM_ID}.cpp")
STARTER_FILE = os.path.join(ROOT, "agents", "starter.cpp")
SAMPLES_DIR = os.path.join(ROOT, "data", "samples")
WORKSPACE_FILE = os.path.join(ROOT, "config", "workspace.json")


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
        os.path.join(SUBMISSION_DIR, f"{PROBLEM_ID}.cpp"),
        os.path.join(SUBMISSION_DIR, "my_solution.cpp"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def check_workspace_path(path: str) -> None:
    if is_contestant_workspace() and not (_within(path, ROOT) or is_unified_submission(path)):
        raise PolicyError(f"选手工具只能访问当前独立工作区或 ../submissions/{PROBLEM_ID}.cpp，禁止访问外部路径。")


def check_source_path(path: str) -> None:
    if not is_contestant_workspace():
        return
    check_workspace_path(path)
    if not (_within(path, SUBMISSION_DIR) or is_unified_submission(path) or _norm(path) == _norm(STARTER_FILE)):
        raise PolicyError(f"只能评测 ../submissions/{PROBLEM_ID}.cpp、agents/submissions/ 下的自有提交或 agents/starter.cpp。")


def check_input_path(path: str) -> None:
    if not is_contestant_workspace():
        return
    if not _within(path, ROOT) or not (_within(path, SAMPLES_DIR) or _within(path, os.path.join(ROOT, "logs"))):
        raise PolicyError("自定义测试输入只能放在当前工作区的 data/samples/ 或 logs/ 下。")


def check_output_dir(path: str, category: str) -> None:
    if not is_contestant_workspace():
        return
    if not _within(path, ROOT) or not _within(path, os.path.join(ROOT, category)):
        raise PolicyError(f"{category} 只能写入当前选手工作区的 {category}/。")

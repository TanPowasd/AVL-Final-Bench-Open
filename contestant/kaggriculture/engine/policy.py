# -*- coding: utf-8 -*-
"""赛制红线：参赛 agent 之间互相隔离，同时支持顶层统一交卷目录 ../submissions/kaggriculture.py。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBLEM_ID = "kaggriculture"
SUBMISSION_DIR = os.path.join(ROOT, "agents", "submissions")
SUITE_SUBMISSIONS_DIR = os.path.join(os.path.dirname(ROOT), "submissions")
UNIFIED_SUBMISSION_FILE = os.path.join(SUITE_SUBMISSIONS_DIR, f"{PROBLEM_ID}.py")
BASELINE_DIR = os.path.join(ROOT, "agents", "baselines")
REFERENCE_DIR = os.path.join(ROOT, "agents", "reference")
WORKSPACE_FILE = os.path.join(ROOT, "config", "workspace.json")

CONTESTANT_PUBLIC_AGENTS = (
    os.path.join(ROOT, "agents", "starter.py"),
    os.path.join(BASELINE_DIR, "passive.py"),
    os.path.join(BASELINE_DIR, "random_legal.py"),
    os.path.join(BASELINE_DIR, "greedy_farm.py"),
)
CHAMPION_FILE = os.path.join(REFERENCE_DIR, "champion.py")


class PolicyError(Exception):
    pass


def _norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _within(path, directory):
    try:
        return os.path.commonpath([_norm(path), _norm(directory)]) == _norm(directory)
    except ValueError:
        return False


def is_contestant_workspace():
    return os.path.isfile(WORKSPACE_FILE)


def is_unified_submission(path):
    return os.path.isdir(SUITE_SUBMISSIONS_DIR) and _norm(path) == _norm(UNIFIED_SUBMISSION_FILE)


def default_submission_path():
    for cand in (
        UNIFIED_SUBMISSION_FILE if os.path.isdir(SUITE_SUBMISSIONS_DIR) else "",
        os.path.join(SUBMISSION_DIR, f"{PROBLEM_ID}.py"),
        os.path.join(SUBMISSION_DIR, "my_agent.py"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def public_agents():
    if is_contestant_workspace():
        return CONTESTANT_PUBLIC_AGENTS
    if os.path.isfile(CHAMPION_FILE):
        return CONTESTANT_PUBLIC_AGENTS + (CHAMPION_FILE,)
    return CONTESTANT_PUBLIC_AGENTS


def public_opponents():
    return public_agents()[1:]


def check_workspace_path(path):
    if is_contestant_workspace() and not (_within(path, ROOT) or is_unified_submission(path)):
        raise PolicyError("选手工具只能访问当前独立工作区（或本工作区 submissions/kaggriculture.py），禁止访问外部路径。")


def check_agent_path(path):
    if not is_contestant_workspace():
        return
    check_workspace_path(path)
    if not is_submission(path) and _norm(path) not in {_norm(p) for p in public_agents()}:
        raise PolicyError("只能加载自己的提交文件（../submissions/kaggriculture.py 或 agents/submissions/）或公开基线陪练。")


def check_output_dir(path, category):
    if not is_contestant_workspace():
        return
    if not _within(path, ROOT) or not _within(path, os.path.join(ROOT, category)):
        raise PolicyError("%s 只能写入当前选手工作区的 %s/。" % (category, category))


def is_public_champion(path):
    return (not is_contestant_workspace()) and os.path.isfile(CHAMPION_FILE) and _norm(path) == _norm(CHAMPION_FILE)


def is_submission(path):
    return _within(path, SUBMISSION_DIR) or is_unified_submission(path)


def rel(path):
    try:
        return os.path.relpath(path, ROOT)
    except Exception:
        return str(path)


def check_opponent(mine, opponent):
    check_agent_path(mine)
    check_agent_path(opponent)
    mine_abs, opp_abs = _norm(mine), _norm(opponent)
    if opp_abs == mine_abs:
        return
    if opp_abs not in {_norm(p) for p in public_opponents()}:
        raise PolicyError(
            "赛制红线：对手必须是公开基线（passive / random / greedy），也可镜像自搏；不得读取其他选手提交（%s）。"
            % rel(opponent)
        )


def check_pair(path_a, path_b, organizer=False):
    if is_contestant_workspace():
        if organizer:
            raise PolicyError("独立选手工作区禁止 --organizer；正式比赛只能在组委会工作区运行。")
        check_agent_path(path_a)
        check_agent_path(path_b)
    if organizer:
        return
    if _norm(path_a) == _norm(path_b):
        return
    if is_submission(path_a) and is_submission(path_b):
        raise PolicyError(
            "赛制红线：本命令默认禁止 选手 vs 选手（%s vs %s）。\n"
            "参赛自测请使用公开基线：python scripts/spar.py <你的文件> --list" % (rel(path_a), rel(path_b))
        )
    if is_contestant_workspace():
        pub = {_norm(p) for p in public_agents()}
        if _norm(path_a) not in pub and _norm(path_b) not in pub:
            raise PolicyError("赛制红线：对局至少一方须为公开基线（或自身镜像）。")

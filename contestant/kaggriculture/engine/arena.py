"""裁判：受预算与判罚约束的对局执行器（单场与多场聚合并落盘 JSON）。

关键实现事实（来自研究库对拍，勿改）：
- rules.interpreter 不维护 observation.step，须在步进前后由裁判写入；
- 初始状态必须自建并把 farms 塞进观测，否则 interpreter 首步 NameError；
- 引擎对非法动作静默忽略；结构非法（缺字段/hands 数不符/未知 op/参数类型错误）由裁判替换为 PASS 并计入异常预算。
"""
import copy
import json
import os
import random
import time
from types import SimpleNamespace

import rules as _rules
import policy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_CONFIG = {
    "episodeSteps": 720, "boardSize": 10, "turnsPerDay": 24, "startingMoney": 3000,
    "maxMarketOrdersPerTurn": 10, "farmHandCostMult": 1, "shedCapacity": 100,
    "weedSpawnChance": 0.005, "townShopUnlockInterval": 3,
    "townShopSellInterval": 4, "townCenterSellInterval": 24,
}
MATCH_RULES = {
    "actTimeoutS": 1.0,        # 每步基础时限（官方 actTimeout=1s）
    "overageBudgetS": 60.0,    # 每局超时总池（官方 remainingOverageTime=60s）：
                               # 每步耗时超出 1s 的部分从池里扣；池用完后仍超 1s 即判超时负
    "errorBudget": 30,         # 每局异常/结构非法上限 → 超了判 0 分负
    "drawMarginPct": 1.0,      # 现金差 ≤1% 判平
}

WORKER_OPS = {"NORTH", "SOUTH", "EAST", "WEST", "PASS", "PICKUP", "PLANT", "WATER",
              "HARVEST", "FERTILIZE", "BUILD_COOP", "BUILD_PASTURE", "DIG", "PLACE",
              "FEED", "COLLECT_FERTILIZER", "CARE", "DROP"}
MARKET_OPS = {"BUY_SEED", "BUY_PRODUCT", "BUY_ANIMAL", "SELL", "HIRE", "BUY_LAND"}


class Attr(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)
    def __setattr__(self, k, v):
        self[k] = v


def isolate_policy_rng(fn, seed):
    """Keep a policy's global random calls separate from the world and opponent."""
    private_state = random.Random(seed).getstate()

    def act(observation):
        nonlocal private_state
        world_state = random.getstate()
        random.setstate(private_state)
        try:
            return fn(observation)
        finally:
            private_state = random.getstate()
            random.setstate(world_state)

    if hasattr(fn, "set_timeout"):
        act.set_timeout = fn.set_timeout
    return act


def structify(v):
    if isinstance(v, dict):
        return Attr({k: structify(x) for k, x in v.items()})
    if isinstance(v, list):
        return [structify(x) for x in v]
    return copy.deepcopy(v)


def _pass_shape(n_hands):
    return {"farmer": ["PASS"], "hands": [["PASS"] for _ in range(n_hands)], "market": []}


ITEM_OPS = {"PLANT", "PICKUP", "PLACE"}
COUNT_OPS = {"BUY_SEED", "BUY_PRODUCT", "BUY_ANIMAL", "SELL"}


def _unit_args_ok(a):
    if a[0] not in ITEM_OPS:
        return True
    if len(a) >= 2 and not isinstance(a[1], str):
        return False
    if len(a) >= 3 and a[0] != "PLANT":
        try:
            int(a[2])
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def _market_args_ok(m):
    if m[0] not in COUNT_OPS:
        return True
    if len(m) >= 2 and not isinstance(m[1], str):
        return False
    if len(m) >= 3:
        try:
            int(m[2])
        except OverflowError:
            return False
        except (TypeError, ValueError):
            pass                 # 引擎按无效订单静默忽略
    return True


def _validate(action, n_hands):
    """返回 (可用动作 or None=需替换, 是否结构错误)。未知 op 和参数类型错误都算结构错误。"""
    try:
        if not isinstance(action, dict):
            return None, True
        farmer = action.get("farmer")
        if not (isinstance(farmer, list) and farmer and isinstance(farmer[0], str)
                and farmer[0] in WORKER_OPS and _unit_args_ok(farmer)):
            return None, True
        hands = action.get("hands")
        if not isinstance(hands, list) or len(hands) != n_hands:
            return None, True
        for h in hands:
            if not (isinstance(h, list) and h and isinstance(h[0], str) and h[0] in WORKER_OPS
                    and _unit_args_ok(h)):
                return None, True
        market = action.get("market")
        if not isinstance(market, list):
            return None, True
        for m in market:
            if not (isinstance(m, list) and m and isinstance(m[0], str) and m[0] in MARKET_OPS
                    and _market_args_ok(m)):
                return None, True
        act = {"farmer": list(farmer), "hands": [list(h) for h in hands],
               "market": [list(m) for m in market][:10]}
        return act, False
    except Exception:
        return None, True


class SeatStats:
    def __init__(self):
        self.errors = 0
        self.step_ms_max = 0.0
        self.total_s = 0.0
        self.calls = 0
        self.overage_used_s = 0.0
        self.timeout = False
        self.timeout_step = -1
        self.first_error = ""

    def as_dict(self):
        return {"errors": self.errors, "step_ms_max": round(self.step_ms_max, 1),
                "total_s": round(self.total_s, 2), "calls": self.calls,
                "overage_used_s": round(self.overage_used_s, 2),
                "overage_left_s": round(max(0.0, MATCH_RULES["overageBudgetS"] - self.overage_used_s), 2),
                "timeout": self.timeout, "timeout_step": self.timeout_step,
                "first_error": self.first_error}


def new_game(cfg, seed):
    random.seed(seed)
    farms = [_rules._new_farm(cfg["boardSize"], cfg["startingMoney"]) for _ in range(2)]
    market = _rules._new_market()
    town = _rules._new_town()
    state = []
    for i in range(2):
        state.append(SimpleNamespace(
            observation=Attr(step=0, day=0, hour=0, player=i, farms=farms,
                             market=market, town=town,
                             private=_rules._new_private(),
                             remainingOverageTime=float(MATCH_RULES["overageBudgetS"])),
            action={}, status="ACTIVE", reward=0))
    env = SimpleNamespace(configuration=Attr(dict(cfg)), done=False, info={"seed": seed})
    return state, env


def play_game(policies, seed, cfg=None, trace=None, recorder=None):
    """policies=[seat0_fn, seat1_fn]。返回对局结果 dict。

    recorder 为可调用对象 recorder(step, state, actions, dead)，在每步结算**之后**调用，
    处于计时区间之外，因此不会影响 actTimeout / overage 统计。
    """
    cfg = dict(DEFAULT_CONFIG, **(cfg or {}))
    state, env = new_game(cfg, seed)
    stats = [SeatStats(), SeatStats()]
    per_step = float(cfg.get("actTimeoutS", MATCH_RULES["actTimeoutS"]))
    error_budget = int(cfg.get("errorBudget", MATCH_RULES["errorBudget"]))
    overage_left = [float(cfg.get("overageBudgetS", MATCH_RULES["overageBudgetS"]))] * 2
    dead = [False, False]  # 超时/异常超限 → 该方判负
    index = 0
    limit = cfg["episodeSteps"] - 1
    while index < limit and not (dead[0] or dead[1]):
        for s in state:
            s.observation.step = index
        actions = []
        for i in range(2):
            s = state[i]
            n_hands = len(s.observation.farms[i].get("hands") or [])
            if dead[i]:
                actions.append({})
                continue
            obs = structify(s.observation)
            limit_setter = getattr(policies[i], "set_timeout", None)
            if limit_setter:
                limit_setter(per_step + overage_left[i])
            t0 = time.perf_counter()
            raised = False
            try:
                raw = policies[i](obs)
            except Exception as e:
                raised = True
                raw = None
                stats[i].errors += 1
                if not stats[i].first_error:
                    stats[i].first_error = "%s: %s" % (type(e).__name__, e)
            dt = time.perf_counter() - t0
            stats[i].calls += 1
            stats[i].total_s += dt
            stats[i].step_ms_max = max(stats[i].step_ms_max, dt * 1000)
            # 官方口径：每步给 per_step 秒；超出部分扣 60s 池；扣不出来就判超时
            if dt > per_step + overage_left[i]:
                stats[i].timeout = True
                stats[i].timeout_step = index
                dead[i] = True
                actions.append({})
                continue
            over = dt - per_step
            if over > 0:
                overage_left[i] = max(0.0, overage_left[i] - over)
                stats[i].overage_used_s += over
            s.observation.remainingOverageTime = overage_left[i]
            if stats[i].errors > error_budget:
                dead[i] = True
                actions.append({})
                continue
            act, bad = _validate(raw, n_hands)
            if bad:
                if not raised:
                    stats[i].errors += 1
                if not stats[i].first_error:
                    stats[i].first_error = "结构非法动作(已替换PASS)"
                act = _pass_shape(n_hands)
                if stats[i].errors > error_budget:
                    dead[i] = True
            actions.append(act)
        for i, s in enumerate(state):
            s.action = actions[i]
        _rules.interpreter(state, env)
        index += 1
        for s in state:
            s.observation.step = index
        if recorder is not None:
            recorder(index, state, actions, dead)
        if trace is not None and index % (cfg["turnsPerDay"] * 3) == 0:
            trace.append({"step": index, "day": state[0].observation.day,
                          "money": [round(float(f["money"]), 1) for f in state[0].observation.farms]})
    cash = [float(s.observation.farms[i]["money"]) for i, s in enumerate(state)]
    # 判罚优先：dead 方直接负
    if dead[0] != dead[1]:
        winner = 1 if dead[0] else 0
    else:
        diff = abs(cash[0] - cash[1])
        thr = MATCH_RULES["drawMarginPct"] / 100.0 * max(cash)
        winner = -1 if diff <= thr else (0 if cash[0] > cash[1] else 1)
    if any(dead):
        reason = "timeout" if any(s.timeout for s in stats) else "error_budget"
    else:
        reason = "cash"
    return {"seed": seed, "cash": cash, "winner": winner, "reason": reason, "forfeited": dead,
            "stats": [stats[0].as_dict(), stats[1].as_dict()],
            "final_day": int(state[0].observation.day)}


def run_match(agent_a, agent_b, seeds=None, cfg=None, trace_dir=None, replay_dir=None,
              replay_kwargs=None, replay_seats=None):
    """agent_x=(label,fn)。双方各坐一次先手/后手。

    replay_dir 给定时，每局额外落一份回放 JSON（见 engine/replay.py）。
    """
    if trace_dir:
        policy.check_output_dir(trace_dir, "results")
    if replay_dir:
        policy.check_output_dir(replay_dir, "replays")
    label_a, fn_a = agent_a
    label_b, fn_b = agent_b
    seeds = seeds if seeds is not None else [11, 23, 47, 83]
    games = []
    replay_files = []
    for seed in seeds:
        for swap in (False, True):
            pols = [fn_b, fn_a] if swap else [fn_a, fn_b]
            tr = [] if trace_dir else None
            rec = None
            if replay_dir:
                try:  # 脚本用法（engine 目录在 sys.path 上）
                    import replay as _replay
                except ImportError:  # 包用法（engine 作为包导入）
                    from . import replay as _replay
                rec = _replay.ReplayRecorder(cfg or DEFAULT_CONFIG,
                                             replay_seats or [{"seat": 0, "label": label_a},
                                                              {"seat": 1, "label": label_b}],
                                             seed, **(replay_kwargs or {}))
            res = play_game(pols, seed, cfg, trace=tr, recorder=rec)
            if rec is not None:
                os.makedirs(replay_dir, exist_ok=True)
                path = os.path.join(replay_dir, "replay_seed%d_%s.json"
                                    % (seed, "sw" if swap else "fw"))
                policy.check_output_dir(path, "replays")
                rec.save(path, res)
                replay_files.append(path)
            w = res["winner"]
            if w == -1:
                outcome = "draw"
            else:
                seat_a = 1 if swap else 0
                outcome = "A" if w == seat_a else "B"
            games.append({**res, "seat_a": 1 if swap else 0, "outcome": outcome,
                          "labels": [label_b if swap else label_a, label_a if swap else label_b]})
            if trace_dir:
                os.makedirs(trace_dir, exist_ok=True)
                trace_path = os.path.join(trace_dir, "trace_%d_%s.json" % (seed, "sw" if swap else "fw"))
                policy.check_output_dir(trace_path, "results")
                with open(trace_path,
                          "w", encoding="utf-8") as f:
                    json.dump(tr, f, ensure_ascii=False)
    tally = {"A": 0, "B": 0, "draw": 0}
    for g in games:
        tally[g["outcome"]] += 1
    cash_a = [g["cash"][g["seat_a"]] for g in games]
    cash_b = [g["cash"][1 - g["seat_a"]] for g in games]
    return {"mode": "match", "A": label_a, "B": label_b, "tally": tally,
            "avg_cash": [round(sum(cash_a) / len(cash_a), 1), round(sum(cash_b) / len(cash_b), 1)],
            "games": games, "replays": replay_files}


def save_result(result, subdir="results"):
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(ROOT, subdir, ts)
    policy.check_output_dir(out, "results")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "match.json")
    policy.check_output_dir(path, "results")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return path


def summarize_match(result):
    t = result["tally"]
    line = ("[%s vs %s] %d胜 %d平 %d负 | 平均现金 %.0f vs %.0f | %d 局"
            % (result["A"], result["B"], t["A"], t["draw"], t["B"],
               result["avg_cash"][0], result["avg_cash"][1], len(result["games"])))
    return line

"""对局回放录制与编码（k4u-arena-replay/1）。

设计要点
- 只记录"渲染需要的东西"，并对瓦片字段做**白名单**，避免把引擎内部键（如 pending_care_bonus）写进回放。
- 每帧自带 full / delta 标记：解码端顺序应用即可，录制端可在体积超标时中途切换成 delta。
- 录制只读 state，不做深拷贝；调用发生在引擎计时区间之外，不影响超时池统计。
- 帧内容与时间无关，因此同种子同双方的重跑产物逐字节一致（可做确定性断言）。
"""
import hashlib
import json
import os
import time

FORMAT = "k4u-arena-replay/1"

# 瓦片字符码：固定部分
C_EMPTY = "0"
C_LOCKED = "L"
C_WEED = "W"
C_COOP_EMPTY = "C"
C_PASTURE_EMPTY = "S"
C_COOP_ANIMAL = "c"
C_PASTURE_ANIMAL = "s"
CROP_CODE_CHARS = "123456789ABCDEF"
ANIMAL_CODE_CHARS = "abcdefghij"

# extra 数组布局（紧凑，位置固定）
# PLANT   : [1, crop_code, planted_day, watered(0/1), yield_units, unwatered_streak, fertilized_until_day]
# ANIMALS : [2, animal_code, placed_day, yield_units, fed(0/1), cared(0/1), fert_available(0/1)]
EX_PLANT = 1
EX_ANIMAL = 2


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _nonzero(d):
    """把 {k: v} 压成只保留正值，数值保持 int/float 原样。"""
    out = {}
    for k, v in (d or {}).items():
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if n:
            out[k] = int(n) if n == int(n) else round(n, 2)
    return out


def _pos(p):
    if not p:
        return None
    return [int(p[0]), int(p[1])]


class ReplayRecorder:
    """可调用对象：arena.play_game(..., recorder=rec) 每步回调一次。"""

    def __init__(self, cfg, seats, seed, every=1, delta=False, budget_bytes=400 * 1024,
                 engine_sha=None, labels=None):
        self.cfg = dict(cfg or {})
        self.seats = seats or []
        self.labels = labels or [s.get("label", "seat%d" % i) for i, s in enumerate(self.seats)]
        self.seed = seed
        self.every = max(1, int(every))
        self.delta = bool(delta)
        self.delta_forced = False
        self.budget_bytes = int(budget_bytes)
        self.engine_sha = engine_sha
        self.crop_codes = {}
        self.animal_codes = {}
        self.item_order = None
        self.frames = []
        self._last_codes = [None, None]
        self._last_extra = [None, None]
        self.steps_seen = 0
        self.dead = [False, False]
        self._prev = {}
        self.started = time.time()

    # ---- 编码辅助 ----
    def _crop_code(self, name):
        if name not in self.crop_codes:
            self.crop_codes[name] = CROP_CODE_CHARS[len(self.crop_codes) % len(CROP_CODE_CHARS)]
        return self.crop_codes[name]

    def _animal_code(self, name):
        if name not in self.animal_codes:
            self.animal_codes[name] = ANIMAL_CODE_CHARS[len(self.animal_codes) % len(ANIMAL_CODE_CHARS)]
        return self.animal_codes[name]

    def _cell(self, cell):
        """返回 (code, extra_or_None)。只读白名单字段。"""
        if cell is None:
            return C_EMPTY, None
        if isinstance(cell, str):
            return (C_LOCKED if cell == "LOCKED" else C_EMPTY), None
        kind = cell.get("kind")
        if kind == "PLANT":
            crop = cell.get("crop")
            code = self._crop_code(crop)
            extra = [EX_PLANT, code,
                     int(cell.get("planted_day") or 0),
                     1 if cell.get("watered_today") else 0,
                     int(cell.get("yield_units") or 0),
                     int(cell.get("consecutive_unwatered") or 0),
                     int(cell.get("fertilized_until_day") or 0)]
            return code, extra
        if kind == "WEED":
            return C_WEED, None
        if kind == "COOP":
            if cell.get("animal"):
                return C_COOP_ANIMAL, self._animal_extra(cell)
            return C_COOP_EMPTY, None
        if kind == "PASTURE":
            if cell.get("animal"):
                return C_PASTURE_ANIMAL, self._animal_extra(cell)
            return C_PASTURE_EMPTY, None
        return C_EMPTY, None

    def _animal_extra(self, cell):
        return [EX_ANIMAL, self._animal_code(cell.get("animal")),
                int(cell.get("placed_day") or 0),
                int(cell.get("yield_units") or 0),
                1 if cell.get("fed_today") else 0,
                1 if cell.get("cared_today") else 0,
                1 if cell.get("fertilizer_available") else 0]

    def _tiles(self, tiles):
        codes = []
        extra = {}
        for y, row in enumerate(tiles or []):
            for x, cell in enumerate(row or []):
                code, ex = self._cell(cell)
                codes.append(code)
                if ex:
                    extra[y * len(row) + x] = ex
        return codes, extra

    def _player(self, s):
        i = int(s.observation.player)
        farm = s.observation.farms[i]
        priv = s.observation.private
        codes, extra = self._tiles(farm.get("tiles"))
        return i, farm, priv, codes, extra

    def _frame(self, step, state, actions, dead):
        obs0 = state[0].observation
        market = obs0.market
        if self.item_order is None:
            self.item_order = list(market.get("prices", {}).keys())
        # 帧只写"变化了的东西"：town / 市场价 / 市场库存 / 判负标记 / 私库，
        # 未变化则由播放器沿用上一帧（显著压缩体积；首帧必然全写）。
        prices = [int(market.get("prices", {}).get(k) or 0) for k in self.item_order]
        inv = [int(market.get("inventory", {}).get(k) or 0) for k in self.item_order]
        town = list((obs0.town or {}).get("unlocked_shops") or [])
        players = []
        raw_rows = []
        for i, s in enumerate(state):
            idx, farm, priv, codes, extra = self._player(s)
            raw_rows.append((codes, extra))
            row = {
                "pos": _pos(farm.get("farmer")),
                "hands": [_pos(p) for p in (farm.get("hands") or [])],
                "money": round(float(farm.get("money") or 0), 1),
            }
            for key, val in (("shed", _nonzero(priv.get("shed"))),
                             ("seeds", _nonzero(priv.get("seeds"))),
                             ("inv", [_nonzero(x) for x in (priv.get("inventories") or [])])):
                pk = (i, key)
                if self._prev.get(pk) != val:
                    self._prev[pk] = val
                    row[key] = val
            players.append(row)
        dead_list = [bool(dead[0]), bool(dead[1])]
        frame = {
            "t": int(step),
            "p": players,
            "act": [self._act(a) for a in actions],
        }
        if self._prev.get("town") != town:
            self._prev["town"] = town
            frame["town"] = town
        if self._prev.get("prices") != prices:
            self._prev["prices"] = prices
            frame.setdefault("mkt", {})["prices"] = prices
        if self._prev.get("mktinv") != inv:
            self._prev["mktinv"] = inv
            frame.setdefault("mkt", {})["inv"] = inv
        if self._prev.get("dead") != dead_list:
            self._prev["dead"] = dead_list
            frame["dead"] = dead_list
        # delta 模式下首帧必须是全量（否则解码端没有基线）
        use_delta = self.delta and self._last_codes[0] is not None and self._last_codes[1] is not None
        if use_delta:
            patch = []
            xpatch = []
            for i in range(len(raw_rows)):
                codes, extra = raw_rows[i]
                prev_codes = self._last_codes[i]
                prev_extra = self._last_extra[i] or {}
                patch.append({k: v for k, v in enumerate(codes) if prev_codes[k] != v})
                px = {}
                for k, v in extra.items():
                    if prev_extra.get(k) != v:
                        px[k] = v
                for k in prev_extra:
                    if k not in extra:
                        px[k] = None
                xpatch.append(px)
            frame["patch"] = patch
            frame["xpatch"] = xpatch
        else:
            for i, row in enumerate(players):
                row["tiles"] = "".join(raw_rows[i][0])
                if raw_rows[i][1]:
                    row["extra"] = {str(k): v for k, v in raw_rows[i][1].items()}
        self._last_codes = [r[0] for r in raw_rows]
        self._last_extra = [r[1] for r in raw_rows]
        return frame, raw_rows

    def _act(self, act):
        """只保留非空字段，进一步压体积；播放器按缺失即空处理。"""
        if not isinstance(act, dict):
            return {}
        def norm(x):
            if isinstance(x, (list, tuple)):
                return [norm(y) for y in x]
            return x if isinstance(x, (str, int, float)) or x is None else str(x)
        out = {}
        for key in ("farmer", "hands", "market"):
            val = norm(act.get(key) or [])
            if val:
                out[key] = val
        return out

    # ---- 引擎钩子 ----
    def __call__(self, step, state, actions, dead):
        self.steps_seen += 1
        if step % self.every and step != 0:
            return
        frame, _ = self._frame(step, state, actions, dead)
        self.frames.append(frame)
        self.dead = [bool(dead[0]), bool(dead[1])]
        if not self.delta and self.budget_bytes and len(self.frames) >= 40:
            planned = max(1, int(self.cfg.get("episodeSteps") or 720) - 1)
            avg = self._size() / len(self.frames)
            if avg * planned > self.budget_bytes:
                self.delta = True
                self.delta_forced = True

    def _size(self):
        return len(json.dumps(self.frames, separators=(",", ":")))

    # ---- 输出 ----
    def build(self, result=None):
        cfg = {k: self.cfg.get(k) for k in
               ("episodeSteps", "boardSize", "turnsPerDay", "startingMoney",
                "actTimeoutS", "overageBudgetS", "errorBudget", "drawMarginPct") if k in self.cfg}
        meta = {
            "format": FORMAT,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "engine_sha256": self.engine_sha,
            "seed": self.seed,
            "cfg": cfg,
            "seats": self.seats,
            "labels": self.labels,
            "sample_every": self.every,
            "steps_planned": max(1, int(self.cfg.get("episodeSteps") or 720) - 1),
            "steps_done": int(self.frames[-1]["t"]) if self.frames else 0,
            "delta": self.delta,
            "delta_forced": self.delta_forced,
        }
        if result:
            meta["final_cash"] = [round(float(c), 1) for c in result.get("cash", [])]
            w = result.get("winner")
            meta["winner"] = {-1: "draw", 0: "A", 1: "B"}.get(w, None)
            meta["end_reason"] = result.get("reason")
            meta["stats"] = result.get("stats")
        legend = {
            "crops": {code: name for name, code in self.crop_codes.items()},      # code -> 名称
            "animals": {code: name for name, code in self.animal_codes.items()},  # code -> 名称
            "items": self.item_order or [],
            "tile_codes": {
                C_EMPTY: "空地", C_LOCKED: "未解锁", C_WEED: "杂草",
                C_COOP_EMPTY: "空鸡舍", C_PASTURE_EMPTY: "空牧场",
                C_COOP_ANIMAL: "鸡舍(有动物)", C_PASTURE_ANIMAL: "牧场(有动物)",
            },
            "extra_layout": {
                "plant": ["kind", "crop", "planted_day", "watered", "yield_units", "unwatered", "fert_until_day"],
                "animal": ["kind", "animal", "placed_day", "yield_units", "fed", "cared", "fert_available"],
            },
        }
        return {"format": FORMAT, "meta": meta, "legend": legend, "frames": self.frames}

    def save(self, path, result=None):
        data = self.build(result)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        return path, len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def decode_frames(replay):
    """把 full/delta 混合的帧还原成"每帧每方都有完整 tiles"的列表（供播放器/测试用）。"""
    turns = int(((replay.get("meta", {}).get("cfg") or {}).get("turnsPerDay")) or 24)
    tiles = [None, None]
    extra = [None, None]
    town = []
    prices = []
    mkt_inv = []
    dead = [False, False]
    rows = [{"shed": {}, "seeds": {}, "inv": []}, {"shed": {}, "seeds": {}, "inv": []}]
    out = []
    for frame in replay.get("frames", []):
        patch = frame.get("patch")
        xpatch = frame.get("xpatch")
        if "town" in frame:
            town = frame["town"]
        if "mkt" in frame:
            if "prices" in frame["mkt"]:
                prices = frame["mkt"]["prices"]
            if "inv" in frame["mkt"]:
                mkt_inv = frame["mkt"]["inv"]
        if "dead" in frame:
            dead = frame["dead"]
        cur = []
        for i in range(len(frame.get("p", []))):
            if patch is None:
                row = frame["p"][i]
                codes = list(row.get("tiles") or "")
                ex = {int(k): v for k, v in (row.get("extra") or {}).items()}
                tiles[i], extra[i] = codes, ex
            else:
                if patch[i] is not None:
                    codes = list(tiles[i] or [])
                    for k, v in patch[i].items():
                        codes[int(k)] = v
                    tiles[i] = codes
                if xpatch and xpatch[i]:
                    ex = dict(extra[i] or {})
                    for k, v in xpatch[i].items():
                        if v is None:
                            ex.pop(int(k), None)
                        else:
                            ex[int(k)] = v
                    extra[i] = ex
            src = frame["p"][i]
            for k in ("shed", "seeds", "inv"):
                if k in src:
                    rows[i][k] = src[k]
            cur.append({
                "pos": src.get("pos"),
                "hands": src.get("hands") or [],
                "money": src.get("money"),
                "tiles": "".join(tiles[i] or []),
                "extra": dict(extra[i] or {}),
                "shed": rows[i]["shed"],
                "seeds": rows[i]["seeds"],
                "inv": rows[i]["inv"],
            })
        t = int(frame["t"])
        out.append({
            "t": t, "day": t // turns, "hour": t % turns,
            "p": cur,
            "mkt": {"prices": prices, "inv": mkt_inv},
            "town": town, "act": frame.get("act") or [],
            "dead": dead,
        })
    return out


def frame_digest(replay):
    return sha256_bytes(json.dumps(replay.get("frames", []), sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))


def verify_against_engine(replay, cash):
    """校验回放的最后一步现金 == 裁判结算现金。"""
    frames = replay.get("frames") or []
    if not frames:
        return False, "回放没有帧"
    last = frames[-1]
    got = [round(float(p.get("money") or 0), 1) for p in last["p"]]
    want = [round(float(c), 1) for c in cash]
    if got != want:
        return False, "末帧现金 %s != 裁判结算 %s" % (got, want)
    return True, "末帧现金与裁判结算一致 %s" % (want,)

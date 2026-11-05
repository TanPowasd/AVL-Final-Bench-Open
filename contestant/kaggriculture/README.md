# 农场经营（Kaggriculture）

双人同图农场经营对抗赛。对局共 `720` 步（`30` 天 × `24` 小时/天），双方在各自独立的 `10 × 10` 农场棋盘上操作农夫与雇工，同时在**共享市场**与**共享城镇需求**中买卖种子、农产品、动物与肥料，终局按农场现金决定胜负。

---

## 1. 交付与运行限制

- **最终统一提交路径（必交）**：`../submissions/kaggriculture.py`（即工作区顶层 `submissions/kaggriculture.py`；本地测试通过后必须将最终文件以题目名保存至此目录，正式评分仅从顶层 `submissions/` 自动分发）。
- **题内开发自测路径（可选）**：`agents/submissions/my_agent.py`（`scripts/spar.py` 同时支持直接读取 `../submissions/kaggriculture.py` 或 `agents/submissions/my_agent.py`）。
- **语言与依赖**：单个 UTF-8 Python 3.10+ 源文件，只能导入下面白名单里的标准库。
- **入口接口**：定义模块级函数 `def act(observation) -> dict:`，或定义类 `class Agent:` 并实现 `def act(self, observation) -> dict:`（裁判自动实例化一次并在整局复用）。
- **运行方式**：裁判为每局启动一个独立子进程加载提交，模块顶层代码和 `Agent()` 限时 `60` 秒，加载失败或超时该局得 `0` 分。之后逐步调用 `act`，观测和动作经 JSON 传递，所以动作必须能序列化，元组会变成列表。`print` 的输出被丢弃。
- **允许导入的标准库白名单**：仅限 `math`、`random`、`itertools`、`collections`、`heapq`、`bisect`、`functools`、`statistics`、`json`。导入任何其他模块都会在加载阶段被拒绝；运行时移除 `open`、`eval`、`exec`、`compile`、`input`、`__import__`、`breakpoint`、`memoryview`、`globals`、`locals`、`vars`。
- **静态扫描（按字符串匹配，注释和字符串里出现也算命中，命中后每局 0 分）**：
  - 禁用模块的导入文本：`import os`、`import sys`、`import subprocess` 等 26 个模块名，`# import os` 这样的注释也会命中。
  - 反射：`.__class__`、`__subclasses__`、`__globals__`、`__bases__`、`__mro__`。
  - 带括号的调用（括号前可以有空格）：`open(`、`write(`、`remove(`、`rmdir(`、`exec(`、`eval(`、`compile(`、`interpreter(`。`list.remove()` 和 `set.remove()` 同样命中，请改用 `discard`、切片或 `del`。
  - 以这些词结尾的文本：`arena`、`rules`，例如 `my_rules` 也命中。
  - 出现即命中：`_new_farm`、`_do_hire`、`random.seed`、`getpass`、`keyring`、`environ`。需要独立随机源时请用 `random.Random(...)`。
  - 完整正则见 `engine/loader.py` 的 `BANNED_PATTERNS`。提交前运行一次 `scripts/spar.py`，它会报告命中项。
- **时间限制**：每步基础时限 `1.0` 秒；单步超出 `1.0` 秒的部分从每局 `60.0` 秒超时池（`observation["remainingOverageTime"]`）中扣除；超时池耗尽后任意一步超 `1.0` 秒立即判负，该局得 `0` 分。
- **卡死**：`act` 用时超过当前可用时间（`1.0` 秒加超时池余量）时，裁判杀掉子进程并判超时负。
- **异常与结构错误**：单步抛出异常或返回结构非法的动作时，该步按全员 `PASS` 处理并计 `1` 次错误；单局累计错误超过 `30` 次立即判负，该局得 `0` 分。`farmer`、`hands`、`market` 三个键都必填，`hands` 长度必须等于当前雇工数，未知的 `op` 也算结构错误。
- **非法子动作**：参数类型正确但当前无法执行的子动作（如越界移动、余额不足购买、对空地收割）静默忽略，不计错误。
- **参数类型错误不在静默忽略之列**：`["PICKUP", "WHEAT", "x"]`、`["PLANT", []]`、`["BUY_SEED", [], 1]`、`["SELL", "WHEAT", float("inf")]` 这类动作算结构错误，按上一条的“异常与结构错误”处理。
- **进程退出**：`exit()`、`quit()`、`raise SystemExit` 或崩溃后子进程不再恢复，之后每步记 `1` 次错误，直到错误超限判负。
- **工作区边界**：读写范围和规则优先级见工作区顶层 `README.md`。权威规则实现位于 `engine/rules.py`。

---

## 2. 棋盘、空间与初始状态

- **棋盘尺寸**：`boardSize = 10`，坐标 `(x, y)` 满足 `0 <= x < 10, 0 <= y < 10`。移动方向：`NORTH = (0, -1)`、`SOUTH = (0, +1)`、`EAST = (+1, 0)`、`WEST = (-1, 0)`。
- **四象限划分**：
  - `NW`：`x < 5, y < 5`（初始已解锁，全为空地 `None`）
  - `NE`：`x >= 5, y < 5`（初始全为 `"LOCKED"`）
  - `SW`：`x < 5, y >= 5`（初始全为 `"LOCKED"`）
  - `SE`：`x >= 5, y >= 5`（初始全为 `"LOCKED"`）
- **谷仓相邻格（Shed Access Tiles）**：棋盘中心 4 格 `(4, 4)`、`(5, 4)`、`(4, 5)`、`(5, 5)`。`DROP`、`PICKUP` 与向谷仓存入物品的 `PLACE` 仅当单位站在上述 4 格之一时生效。
- **双方初始状态（完全对称）**：
  - 现金 `money = 3000.0`；
  - 已解锁象限 `unlocked_quadrants = ["NW"]`；
  - 主农夫位于 `(4, 4)`，雇工列表 `hands = []`，当日已雇工次数 `hires_today = 0`；
  - 私有仓库 `shed` 容量上限 `shedCapacity = 100`（所有农产品与未放置动物共享该容量，初始全 `0`）；
  - 私有种子袋 `seeds`（5 种作物初始全 `0`，不占 `shed` 容量）；
  - 随身背包 `inventories = [{}]`（`inventories[0]` 对应主农夫，`inventories[k]` 对应 `hands[k - 1]`，无容量上限）。

---

## 3. 每步结算顺序（Step `0 .. 718`）

设当前全局步为 `step`（`day = step // 24`，`hour = step % 24`）。双方同时基于当前 `observation` 返回动作，引擎严格按以下顺序结算：

1. **单位行动（Unit Actions）**：
   - 先统计本方主农夫与所有雇工在本步提交的 `PLANT <CROP>` 总次数：若某作物本步计划种植总数大于 `private["seeds"][crop]`，本方本步针对该作物的**全部** `PLANT` 动作统一失效（变 `PASS`）。
   - 按 `farmer`、`hands[0]`、`hands[1]`、……的顺序依次执行单位动作。
2. **市场订单结算（Market Orders）**：
   - 每方每步最多取前 `10` 条市场订单（`maxMarketOrdersPerTurn = 10`）。
   - 按订单下标 `i = 0, 1, ..., 9` 逐步推进：
     - 若订单为 `HIRE` 或 `BUY_LAND`，立即执行，现金不足时静默忽略；
     - 若订单为 `SELL`、`BUY_PRODUCT`、`BUY_SEED`、`BUY_ANIMAL`（数量 `n >= 1`），双方在第 `i` 槽位的订单进入**逐件同步锁步循环（per-unit lockstep）**：每一轮先基于当前共享市场库存同时为双方报出当前单件价格，再依次为双方各成交 `1` 件并更新市场库存，直至双方第 `i` 条订单均完成或因缺货/缺钱/满仓无法继续，随后刷新公开市场价格，进入第 `i + 1` 槽位。单个槽位最多循环 `99999` 轮。
3. **城镇消耗（Town Consumption）**：
   - 若 `step % 4 == 0`：遍历 `town["unlocked_shops"]` 中每家已解锁商店；若该店需求列表仅有 `1` 种商品则市场库存减 `2`，否则该店每种需求商品的市场库存各减 `1`。
   - 若 `step % 24 == 0`：镇中心消耗除 `FERTILIZER` 外的其余 `8` 种商品市场库存各 `1` 件。
   - 刷新市场价格。
4. **过熟作物衰减（Plant Lifespan Decay）**：
   - 对每格带寿命的作物（`max_lifespan_step >= 0`：非持续作物从种下起就有寿命，持续作物产满后才有），当 `step >= max_lifespan_step` 且 `(step - max_lifespan_step) % 2 == 0` 时，`yield_units` 减 `1`；若减至 `<= 0`，该格变为 `{"kind": "WEED"}`。
5. **日终结算（仅在每日最后一步 `(step + 1) % 24 == 0` 触发）**：
   - **作物日结**：若当日浇过水（`watered_today == True`），`consecutive_unwatered` 置 `0`；否则加 `1`。随后 `watered_today` 重置为 `False`。若 `consecutive_unwatered >= 2`，作物枯萎变为 `{"kind": "WEED"}`。对未枯萎的持续作物（`TOMATO` / `STRAWBERRY`），记 `next_day = day + 1`、`days_since_first = next_day - planted_day - first_yield_day`：当 `days_since_first >= 0` 且 `days_since_first % interval == 0` 且累计产果次数 `days_since_first // interval + 1 <= max_yield` 时，若当日浇过水且 `fertilized_until_day >= day` 则 `yield_units = min(max_yield, yield_units + 2)`，否则 `min(max_yield, yield_units + 1)`；当累计产果次数恰好达到 `max_yield` 时，设其 `max_lifespan_step = (next_day + 1) * 24`。
   - **动物日结**：若当日喂过食（`fed_today == True`），`consecutive_unfed` 置 `0`；否则加 `1`。若 `consecutive_unfed >= 2`，动物消失，格子退回空建筑 `{"kind": "COOP"|"PASTURE"}`。若动物存活：当 `(next_day - placed_day - first_yield_day) >= 0` 且能被 `interval` 整除时，增加产量 `1 + (pending_care_bonus if fed_today else 0)`（上限 `max_held`），同时清零 `pending_care_bonus`；随后若当日 `cared_today and fed_today`，下一轮的 `pending_care_bonus` 加 `1`；最后置 `fertilizer_available = True`、`fed_today = False`、`cared_today = False`。
   - **杂草生长**：每个空地 `None` 格独立以概率 `weedSpawnChance = 0.005` 变为 `{"kind": "WEED"}`。双方在同一天使用相同的随机序列，每格固定抽一次，所以同一坐标的抽签结果相同，也不受对方棋盘影响。
   - **背包入库与人员重置**：将 `inventories`（主农夫及所有雇工）中的全部物品按顺序自动转入 `shed`（超出 `100` 容量的部分直接丢弃）；主农夫坐标重置回 `(4, 4)`；**所有当日雇工解散**（`hands = []`，`hires_today = 0`，`inventories = [{}]`）。
   - **新商店解锁**：若 `next_day % 3 == 0` 且已解锁商店总数 `< 8`，从 8 类商店中等概率可重复地随机解锁 1 家追加到 `town["unlocked_shops"]`，结果只由种子和天数决定。

> 注：引擎在 `step == 718` 执行完毕后判定对局结束（共调用 `act` `719` 次：`step = 0 .. 718`），终局得分为该时刻各自的 `farm["money"]`（未售出的库存、种子、建筑与动物不折算现金）。
>
> **最后一天没有日终结算**：`step = 719` 不执行，第 `29` 天的背包物品不会自动入库。要卖掉背包里的物品，必须站到谷仓相邻格执行 `DROP` 或 `PLACE`，再用 `SELL` 出售。单位行动先于市场结算，所以最晚可以在 `step 718` 同一步完成 `DROP` 和 `SELL`。

---

## 4. 作物、动物、市场、土地与雇工数值表

### 4.1 作物参数（`CROPS`）

| 作物 `crop` | 固定买种价 `seed` | 首收天数 `first_yield_day` | 最高产天数 `max_yield_day` | 持续产 `ongoing` | 产果间隔 `interval` | 产量上限 `max_yield` |
|---|---:|---:|---:|:---:|---:|---:|
| `WHEAT` | 10 | 2 | 4 | 否 | 0 | 6 |
| `CARROT` | 20 | 2 | 3 | 否 | 0 | 4 |
| `TOMATO` | 50 | 8 | 8 | 是 | 1 | 4 |
| `STRAWBERRY` | 100 | 10 | 10 | 是 | 2 | 4 |
| `MELON` | 80 | 10 | 12 | 否 | 0 | 6 |

- **刚种下时**：`planted_day = day`，`watered_today = False`，`consecutive_unwatered = 1`，`fertilized_until_day = -1`。**种下当天必须浇水，否则当日日终 `consecutive_unwatered` 变为 `2`，作物直接变 `WEED`。** 当天浇过水的作物，之后要连续两天不浇才枯死。
  - 非持续作物初始 `yield_units = 1`，`max_lifespan_step = (day + max_yield_day + 1) * 24`；在天龄 `age = day - planted_day` 满足 `(max_yield_day + 1) // 2 <= age <= max_yield_day` 的当天首次执行 `WATER` 时，若 `fertilized_until_day >= day` 则 `yield_units += 2`，否则 `+1`（封顶 `max_yield`）；`HARVEST` 需 `age >= first_yield_day` 且 `yield_units > 0`，收割后格子变回 `None`。
  - 持续作物初始 `yield_units = 0`，`max_lifespan_step = -1`；靠日终结算产出，`HARVEST` 后 `yield_units` 清零但植株保留。
- **施肥 `FERTILIZE`**：消耗单位背包中 `1` 个 `FERTILIZER`，令脚下作物的 `fertilized_until_day = max(fertilized_until_day, day + 2)`。

### 4.2 动物参数（`ANIMALS`）

| 动物 `animal` | 购买价 `cost` | 所需建筑 `structure` | 首产天数 `first_yield_day` | 产出间隔 `interval` | 存量上限 `max_held` | 产出商品 `product` |
|---|---:|:---:|---:|---:|---:|:---:|
| `GOOSE` | 300 | `COOP` | 4 | 1 | 4 | `EGG` |
| `COW` | 400 | `PASTURE` | 8 | 2 | 6 | `MILK` |
| `SHEEP` | 500 | `PASTURE` | 6 | 3 | 6 | `WOOL` |

- `BUILD_COOP` 与 `BUILD_PASTURE` 在空地 `None` 上免费建造（不扣现金）。
- 动物产品用 `HARVEST` 收取：单位站在动物格上执行，把 `yield_units` 全部放入背包并清零。
- `BUY_ANIMAL` 购得的动物先进入 `shed`（占 `1` 格容量），需由单位在谷仓相邻格 `PICKUP <ANIMAL> 1` 拿到背包，再走到对应的空建筑格执行 `PLACE <ANIMAL>` 安置。
- `FEED` 消耗单位背包中 `1` 个 `WHEAT`（每只每天限喂一次）；`CARE` 不消耗物品（每只每天限一次）；`COLLECT_FERTILIZER` 在 `fertilizer_available == True` 的动物格收取 `1` 个 `FERTILIZER` 到单位背包。

### 4.3 市场定价公式（`MARKET_PARAMS`）

共 9 种商品，初始市场库存均为 `I0 = 10000`，价格下限 `PRICE_FLOOR = 1`。给定当前市场库存 `I`：

- `round` 是 Python 的 `round`（四舍六入五成双）。市场库存没有下限，可以低于 `0`。
- 当 `I < I0` 时：`price = max(1, round(base + amp_below * shape(below_func, I0 - I, T)))`，其中 `amp_below = below_target * base / shape(below_func, T, T)`。
- 当 `I >= I0` 时：`price = max(1, round(base - amp_above * shape(above_func, I - I0, T)))`，其中 `amp_above = above_target * base / shape(above_func, T, T)`。
- 形状函数 `shape(func, x, T)`（`x >= 0`）：
  - `linear`: `x`
  - `sq`: `x^2`
  - `sqrt`: `sqrt(x)`
  - `log`: `ln(1 + x)`
  - `hinge`: `(x / T) + 8.0 * max(0, x / T - 1)^2`

| 商品 | `base` | `T` | `below_func` | `below_target` | `above_func` | `above_target` |
|---|---:|---:|:---:|---:|:---:|---:|
| `WHEAT` | 25 | 400 | `sqrt` | 0.8 | `log` | 0.2 |
| `CARROT` | 35 | 450 | `hinge` | 1.0 | `sqrt` | 0.7 |
| `TOMATO` | 60 | 200 | `hinge` | 0.4 | `sqrt` | 0.6 |
| `STRAWBERRY` | 120 | 100 | `sqrt` | 0.7 | `linear` | 1.6 |
| `MELON` | 250 | 300 | `log` | 0.2 | `sq` | 3.6 |
| `EGG` | 50 | 332 | `hinge` | 0.4 | `log` | 0.2 |
| `MILK` | 160 | 122 | `sqrt` | 0.6 | `linear` | 1.6 |
| `WOOL` | 200 | 105 | `log` | 0.2 | `sq` | 3.2 |
| `FERTILIZER` | 100 | 200 | `linear` | 0.4 | `linear` | 0.4 |

- `SELL <ITEM> <n>`：仅能出售 `shed` 中的 9 种商品；每成交 `1` 件按报价获得现金，若成交价 `> 1` 则市场库存 `+1`（成交价等于地板价 `1` 时市场库存不再增加）。
- `BUY_PRODUCT <ITEM> <n>`：仅允许购买 `WHEAT` 或 `FERTILIZER`，单件报价按 `market_price(item, I - 1)` 计算，存入 `shed`（受 `100` 容量限制），市场库存 `-1`。
- `BUY_SEED <CROP> <n>` 与 `BUY_ANIMAL <ANIMAL> <n>`：按固定表价成交，不影响市场库存。

### 4.4 城镇商店需求表（`SHOPS`）

- `BAKERY`: `["EGG", "WHEAT"]`
- `PIZZA_SHOP`: `["MILK", "TOMATO", "WHEAT"]`
- `BRUNCH_SPOT`: `["EGG", "WHEAT", "STRAWBERRY"]`
- `YARN_STORE`: `["WOOL"]`（单品店，每 4 步消耗 `2` 件）
- `ICE_CREAM_SHOP`: `["STRAWBERRY", "MILK", "WHEAT"]`
- `PET_CAFE`: `["CARROT"]`（单品店，每 4 步消耗 `2` 件）
- `SMOOTHIE_SHOP`: `["STRAWBERRY", "MILK"]`
- `FARMERS_MARKET`: `["WHEAT", "CARROT", "TOMATO", "STRAWBERRY"]`

### 4.5 雇工与买地

- **`HIRE`**：当日第 `k` 次雇工（`k = hires_today >= 0`）费用为斐波那契数 `fib(k)`：`fib(0)=1, fib(1)=1, fib(2)=2, fib(3)=3, fib(4)=5, fib(5)=8, ...`。新雇工出生在谷仓相邻 4 格 `[(4,4), (5,4), (4,5), (5,5)]` 中当前本方人员最少的第一格（当步市场阶段才出生，下一步起在 `hands` 中行动）。
- **`BUY_LAND`**：严格按 `NE`（`1000`）→ `SW`（`2000`）→ `SE`（`4000`）顺序解锁，将该象限所有 `"LOCKED"` 格变为 `None`。现金不足或已全部解锁时静默忽略。

---

## 5. 观测与动作接口格式

### 5.1 `observation` 字段（同时支持字典 `obs["day"]` 与属性 `obs.day` 访问）

- `step` (`0..718`), `day` (`0..29`), `hour` (`0..23`)
- `player` (`0` 或 `1`)
- `remainingOverageTime` (`float`，当前剩余超时池秒数)
- `farms`（长度为 2 的列表，双方公开状态）：
  - `money` (`float`)
  - `tiles` (`10 × 10` 二维数组，`tiles[y][x]` 取值为 `None`、`"LOCKED"`、`{"kind": "WEED"}`、`{"kind": "PLANT", "crop", "planted_day", "watered_today", "consecutive_unwatered", "yield_units", "max_lifespan_step", "fertilized_until_day"}`、或 `{"kind": "COOP"|"PASTURE", ...}`，若含动物则带 `"animal", "placed_day", "yield_units", "consecutive_unfed", "fed_today", "cared_today", "fertilizer_available", "pending_care_bonus"`)
  - `farmer` (`[x, y]`), `hands` (`[[x, y], ...]`), `unlocked_quadrants` (`list[str]`), `hires_today` (`int`)
- `market`: `{"inventory": {商品: int}, "prices": {商品: int}}`
- `town`: `{"unlocked_shops": [商店名, ...]}`
- `private`（仅本方可见）：`{"shed": {物品或动物: int}, "seeds": {作物: int}, "inventories": [dict, ...]}`

### 5.2 返回动作字典格式

```python
{
    "farmer": [op, *args],          # 主农夫动作
    "hands":  [[op, *args], ...],   # 长度必须严格等于当前 len(farms[player]["hands"])
    "market": [[op, *args], ...],   # 最多 10 条市场订单
}
```

- **单位动作 `op`**：
  - 移动 / 待机：`["NORTH"]`、`["SOUTH"]`、`["EAST"]`、`["WEST"]`、`["PASS"]`（可以走进 `LOCKED` 格。站在 `LOCKED` 格上不能耕作或建造，但 `DROP`、`PICKUP`、`PLACE` 不受影响，`(5, 4)` 等谷仓相邻格开局就是 `LOCKED`）
  - 耕作：`["PLANT", crop]`、`["WATER"]`、`["HARVEST"]`、`["FERTILIZE"]`、`["DIG"]`（清除脚下作物、杂草或无动物的空建筑）
  - 建筑与畜牧：`["BUILD_COOP"]`、`["BUILD_PASTURE"]`、`["FEED"]`、`["CARE"]`、`["COLLECT_FERTILIZER"]`
  - 存取与放置：`["DROP"]`（谷仓旁清空背包入 `shed`，谷仓放不下的部分**直接销毁**）、`["PICKUP", item, n]`（谷仓旁从 `shed` 取 `n` 件，默认 `1`）、`["PLACE", item, n]`（在空建筑格放 `1` 只对应动物，或在谷仓旁放 `n` 件物品入 `shed`，`n` 默认 `1`，放不下的部分留在背包）
- **市场订单 `op`**：
  - `["BUY_SEED", crop, n]`、`["BUY_PRODUCT", "WHEAT"|"FERTILIZER", n]`、`["BUY_ANIMAL", animal, n]`、`["SELL", item, n]`、`["HIRE"]`、`["BUY_LAND"]`
  - 市场订单的 `n` 必填，经 `int()` 转换（`"3"` 和 `2.9` 分别按 `3`、`2` 件成交），转换失败或 `n <= 0` 时忽略该订单，不计错误。`inf` 算结构错误。

---

## 6. 正式计分与本地自测

- **正式评分版本：`scoring_v2`，满分 100。** 固定对阵 3 个参考对手，每个种子交换座位，每份提交默认运行 `3 * 64 * 2 = 384` 局；N 个场景共 `6N` 局。
- 正常结束的单局得分：
  $$s = 50 + 50\tanh\left(\frac{money_T-money_R}{87403.52}\right).$$
  对全部对手、种子和座位等权平均。现金相同为 50 分；现金差不超过 1% 的平局判定仅保留为辅助胜负统计，不把连续得分强制改成 50。
- **技术判负优先**：本方因超时或异常预算耗尽被裁判判负，该局得 0 分；仅对手技术判负时本方得 100 分；双方均技术判负时各得 0 分。加载提交失败也记为该局 0 分，不能跳过失分对局。
- 正式比赛每局在新进程中重新加载双方策略。双方全局随机数流与环境随机数流隔离，不能通过多调用随机函数改变环境或对手的随机事件。
- 映射尺度 `87403.52` 来自独立完整基线对局的正绝对分差中位数除以 `atanh(0.6)`，在评分前冻结。Elo 联赛保留为辅助分析，不进入正式百分制。

- **每次随机、多局平均**：每次评测重新均匀抽取 N 个互不重复的场景种子，默认 N=64，可增加场景数。参考对手池、生成规则、完整局长及运行限制固定；同一批提交共享本次种子并交换座位/角色，对手、场景、座位分别等权。不会选择多次运行中的最好成绩，也不跨次缓存成绩。每个 seed 直接从操作系统安全随机源抽取（secrets/SystemRandom），去重并排除校准种子，不经固定主种子扩展；报告保存完整 seed 列表，供重放核查。
- **统计与版本**：成绩包含 `scoring_version = "scoring_v2"`、`score`（0–100）、`max_score = 100`、分项、原始指标、出错局数和 95% bootstrap 区间。区间按完整种子块重采样，保留同种子下座位、对手与赛制之间的相关性；只有一个种子时不报告区间。
- **可比性**：报告包含冻结配置、引擎、对手及提交源码指纹。旧版成绩和练习成绩不进入新版正式汇总；不同配置指纹的成绩不直接混用。这里的百分制表示本题固定评测下的表现，不是最优率或胜率。
- **本地自测**：`scripts/spar.py` 同样默认随机抽取场景，使用 `--seed-count N` 指定数量，使用 `--seeds 123 456` 重放。公式与正式赛一致，但对阵公开基线，始终标记 `official = false`。正式入口中显式指定 `--seeds` 的重放或缩短局长的运行也标为非正式。

- **公开基线**（`agents/baselines/`）：`passive`（`passive.py`）、`random`（`random_legal.py`）、`greedy`（`greedy_farm.py`），也可与自身镜像对局。不指定对手时 `spar.py` 对阵 `greedy`。

```bash
# 列出公开基线
python scripts/spar.py --list

# 与指定公开基线或全部基线交换座位对局
python scripts/spar.py ../submissions/kaggriculture.py
python scripts/spar.py agents/submissions/my_agent.py greedy --seed-count 2
python scripts/spar.py agents/submissions/my_agent.py --all --seed-count 2

# 单场指定种子对局或镜像自搏
python scripts/run_match.py --a agents/submissions/my_agent.py --b agents/submissions/my_agent.py --seed-count 2

# 录制对局回放（输出至 replays/，可用 viz/player.html 打开查看）
python scripts/record_replay.py --a agents/submissions/my_agent.py --b greedy --seed 11
```

# rift_era 回放文件格式（format 1，原型 v1.3.1）

回放由 `replay/record.py` 的 `Recorder` 生成：挂在一个 `sim.Game` 实例上，只读观测，不改对局（录制局与不录制局各国 TP、现金、人口、净资产、汇率、全局统计逐位相同，`replay/verify_nochange.py` 校验）。回放页（viewer）直接导入这些文件。

## 文件

| 来源 | 文件名 | 说明 |
|---|---|---|
| `score.py` 选手局 | `replay_seed<seed>_seat<k>.json.gz` | 每个 seed 一份；`k` 为选手席位（= seed 序号 mod 8） |
| `score.py --shadow-replay` | `replay_seed<seed>_shadow.json.gz` | 同 seed 全 Super 影子局（可选） |
| `score.py` | `index.json` | 本次评测全部 seed 汇总与总分（不压缩） |
| `seats.py --replay-dir` | `replay_seed<seed>_mix.json.gz` / `replay_seed<seed>_<类名>x8.json.gz` | 8 席同局 |
| `calib.py --replay-dir` | `replay_seed<seed>_seat<i>_<模块>_<类名>.json.gz` | 单席，其余白板 |
| `record.py` 命令行 | 任意路径；默认 `replay/replay_seed<seed>.json` | 以 `.gz` 结尾则压缩 |

- 内容是 UTF-8 的紧凑 JSON（无空白，`ensure_ascii=False`）。`.json.gz` 为 gzip（压缩级 9，`mtime=0`，同一对局同一字节）；读取端按扩展名判断，`.json` 为未压缩。浏览器端可用 `DecompressionStream('gzip')` 解压。
- 大小：2,880 周期、8 国、每 10 周期一帧（289 帧）、事件上限 400 时，未压缩约 0.58 MB/局，gzip 后约 0.12–0.13 MB/局（2026-11-03 实测 126–129 KB；64 seed 选手局约 8 MB）。帧间隔加倍约减半。
- 数值一律保留 3 位有效数字（`r3`），能整数化的写整数；非有限值写 `null`。

## 顶层

```json
{"meta": {...}, "cycle": [0, 10, ..., 2880], "world": {...}, "nations": [{...} x 8], "events": [[cycle, nid, type, text], ...]}
```

- `cycle[f]`：第 f 帧的工业周期号。第 0 帧是开局状态；之后每 `frame_every` 周期一帧；周期数不能整除时最后一周期补一帧。所有时间序列与 `cycle` 等长、下标对齐。
- **存量字段**取该帧时刻的值；**流量字段**是本帧与上一帧之间各周期的均值（标注“均值”）或合计（标注“合计”）。第 0 帧流量为 0。

## meta

| 键 | 含义 |
|---|---|
| `game`, `rules`, `format` | `"rift_era"`、规则版本 `"v1.3.1"`、本格式版本 `1` |
| `repo_head` | 录制时仓库 HEAD（短哈希；工作区可能有未提交改动） |
| `seed`, `cycles`, `frame_every`, `n_frames` | 种子、总周期、帧间隔、帧数 |
| `seating` | 每席 bot 名（`Super`/`Advanced`/`Medium`/`Blank` 或选手类名） |
| `elapsed_s` | 录制局墙钟秒数 |
| `nations[i]` | `id`、`seat`、`name`（显示名，如 `N3·超级`、`N0·选手 Advanced`）、`bot`（档位小写或类名小写）、`bot_class`（`模块.类`）、`strategic`（本国拥有的战略矿）、`hydro_pot`（水电潜力上限，电/周期） |
| `labor_cats`, `sources` | `labor` 与 `gen` 子表的键顺序 |
| `event_types`, `events_total`, `events_dropped` | 出现过的事件类型、裁剪前事件数、被裁掉的数目 |
| `final_stats` | 局末全局统计（`g.stats`：拍卖成交额 RU、强平、违约、汇率脱钩、黑网、AI 事故、国家间货物 RU、推理服务 RU、授权次数） |
| `label` | 仅评测回放：`"eval"`（seed 由 OS 安全随机源抽取）或 `"replay"`（命令行指定 seed 的重放测试，不得混入正式记录） |
| `score` | 仅评测回放，见下 |

### meta.score（score.py 写入）

| 键 | 含义 |
|---|---|
| `player_seat` | 选手席位 k |
| `player_bot` | 选手 bot 规范名（`模块:类` 或 `路径.py:类`） |
| `tp_player` | 选手局中选手席位的局末 TP（`sim.tp`：已知科技理论点 + AI 里程碑等价点） |
| `tp_shadow` | 同 seed 全 Super 影子局同席位的局末 TP |
| `tp_ratio` | `tp_player / tp_shadow` |
| `s` | 原型临时单局分 `clamp(50 · tp_ratio, 0, 100)`（与同席 Super 持平 = 50） |
| `s_log2` | log2 倍数提案单局分 `clamp(50 + 50 · log2(tp_ratio), 0, 100)` |
| `label` | 同 `meta.label` |
| `role` | 仅影子回放：`"shadow"`（其 `tp_player = tp_shadow`，`s = s_log2 = 50`） |

TP 与这两个分数都是原型临时口径，不是 07 号的五支柱正式评分。

## world（每帧一个值）

| 键 | 单位 / 含义 |
|---|---|
| `rate` | 各国政策利率按 GDP×汇率加权平均，年化 %（1 年 = 100 周期） |
| `infl` | 各国通胀 `pi` 加权平均 × 10⁴（bp，sim 内 `pi` 的 EMA 口径） |
| `goods_trade` | 本帧区间国家间货物成交额合计，RU |
| `inference_trade` | 本帧区间推理服务成交额合计，RU |
| `inference_rev` | 本帧区间各国推理服务收入合计，RU |
| `license_trade` | 本帧区间权重 / 数据授权成交额合计，RU |
| `licenses` | 本帧区间授权次数 |
| `npc_imports`, `npc_exports` | 本帧区间各国对世界市场（NPC）进口 / 出口额合计，RU |
| `bond_out` | 帧时刻全部未偿国债面值 × 发行国汇率，RU |
| `blackouts_active` | 帧时刻处于黑网的国家数 |

## nations[i]（每国；每键一个与 cycle 等长的数组）

人口与生产：`pop`（人口）、`housing`（住宅区座数）、`housing_cap`（住房容量，人）、`slot_cap`（设施槽位上限）、`facilities`（设施数）、`labor_avail`（可用劳动力）、`labor_need`（已开工产线与采掘场的用工需求）、`food`（食品满足率 0–1）、`support`（支持度 0–1）、`pollute`（污染指数）。

科技与 AI：`tp`（TP）、`tier`（最高阶 0–6）、`techs`（已知科技数）、`rp`（研究点，均值/周期）、`rp_cum`（累计研究点）、`cap`（AI 能力 Cap）、`align`（对齐）、`ai_ms`（已达 AI 里程碑数）、`compute`（算力，均值/周期）、`inf_rev`（推理服务收入，RU，均值/周期）、`inf_rev_cum`（累计推理收入，RU）、`incidents`（累计 AI 失控事件）。

电力（单位：电/周期，均值）：`demand`（负荷）、`served`（已供电）、`unmet`（切负荷）、`power_import` / `power_export`（跨国购 / 售电）；`blackout`（本帧区间内是否发生过黑网，0/1）、`store_e`（帧时刻储能电量）、`elec_price`（电价，本币/电）。

金融：`cash`（现金，本币）、`net_worth`（净资产，RU）、`e`（汇率，RU/本币）、`rate`（政策利率，年化 %）、`infl`（`pi` × 10⁴，bp）、`cpi`（开局 = 1）、`debt`（未偿国债面值，本币）、`reserves`（外汇储备，RU）、`defaults`（累计违约次数）。

贸易：`imports` / `exports`（本帧区间对世界市场买 / 卖额合计，RU）、`lic_in_cum` / `lic_out_cum`（累计授权付出 / 收入，RU）、`goods_in_cum` / `goods_out_cum`（累计国家间货物买入 / 卖出，RU）。

子表：
- `labor[cat]`：帧时刻用工需求结构占比（0–1，各类和为 1）；`cat` ∈ `food, extraction, power, industry, labs, other`（`other` 含储能、数据中心、数据整理与住宅区维护）。
- `gen[src]`：本帧区间各电源发电量，电/周期均值；`src` ∈ `coal, gas, oil, hydro, wind, solar, nuclear, fast_reactor, fusion, hydrogen, storage`（风光混合 P072 平分到 wind/solar；`storage` 为储能放电估计 = 已供电 −（本国发电 − 外售）− 外购）。

## events

`[cycle, nid, type, text]`，按（周期, 国家）排序；`text` 为中文说明。类型与优先级（数字小 = 重要；0 级全部保留，其余在 400 条上限内先保每国每类首条、再按金额 / 规模）：

| 优先级 | 类型 |
|---|---|
| 0 | `tier`（达到新阶）、`ai_milestone`、`default`（国债违约）、`blackout_start`、`blackout_end` |
| 1 | `ipo`、`export_control`、`ore_depleted`（战略矿耗尽）、`bond`（发债） |
| 2 | `license`（买入授权） |
| 3 | `housing`（住宅区每 10 座）、`pop`（人口突破 500 的整倍数） |
| 4 | `ai_incident` |

## index.json（score.py）

`kind = "rift_era_score_index"`、`format`、`label`（eval / replay）、`official = false`、`status`（`OK` / `INVALID_EVALUATION`）与 `invalid_reason`、`created`、`rules`、`repo_head`、`engine_fingerprint`（sim/ref5/tiers/bots 源码 SHA-1 前 12 位，也是影子缓存键）、`player_bot`、`reference`、`seat_rule`、`score_def`、`seeds`、`seats`、`cycles`、`frame_every`、`replays`、`shadow_replays`、`jobs`、`elapsed_s`；

`score`：`S`（s 的 seed 均值）、`S_log2`、`ratio_mean`、`ratio_geomean`、`n`、`n_seeds`；

`games[]`：每 seed 一行 `seed, seat, tp_player, tp_shadow, tp_ratio, s, s_log2, tp_peers_mean`（选手局其余 7 席 TP 均值）、`tp_shadow_all` / `tp_player_game_all`（两局全部 8 席 TP）、`player_errors` / `player_first_error`（选手 act 异常次数与首条 traceback）、`elapsed_player_s` / `elapsed_shadow_s`、`shadow_cached`、`replay` / `replay_bytes` / `replay_raw_bytes`，有影子回放时另有 `shadow_replay*`。viewer 可先读 index.json 列出各 seed，再按 `replay` 文件名逐个导入。

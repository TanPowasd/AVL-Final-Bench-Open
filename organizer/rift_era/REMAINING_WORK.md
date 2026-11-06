# 裂陆纪元待实现清单（2026-11-05 审查）

本文件是接手实现时的工作清单，按优先级排列。背景和已接入内容见 [HANDOFF.md](HANDOFF.md)，完整对局验收见 [FULL_MATCH_TEST_TODO.md](FULL_MATCH_TEST_TODO.md)。

审查基线：提交 `06afab4`，引擎指纹 `28011425…a016`，规则 `rift_era_rules_r6`。

## 结论

- **引擎：** 已完成的部分质量可靠，但还不能跑完整对局。现在只有开局经济入口，61/75 条规则命令已注册，五支柱采集和批次评分都没有装配。
- **交接文档：** 主要说法经核对属实，可以作为入口。但遗漏了本文件 P0-4、P1 的部分条目，也有几处过期，见最后一节。

## 已核对通过

| 项目 | 结果 |
|---|---|
| 回归证据 | `validation/handoff_full_20261105.json` 记录 536 项 0 失败，其引擎指纹与当前源码一致 |
| 确定性 | seed 1000 跑 150 Tick，`PYTHONHASHSEED=0` 和 `12345` 的最终状态哈希相同 |
| 开局自举 | seed 1000 通过核心检查 |
| 新候选地图 | `world_atlas` 的 seed 1000 无玩家命令跑 150 Tick，全局审计通过；这只是组件探测，不是地图验收 |
| 命令字段 | 已注册命令与 06 表做了双向比对，字段冲突只有下面的 `corridor_policy` |
| 主线接线 | 交接中已声明接入的系统都有阶段调用；`OperatingWorld` 的系统都进入快照或属于缓存 |
| 活跃挂单上限 | 现货、债券、证券、AI 服务都执行了 256 单上限 |

## P0：完整对局的前置条件

### P0-1 完整对局入口

`opening_economy` 限制最多 150 Tick，且没有正式运行命令。需要交付：

- 28,800 Tick 的完整装配和命令行入口，继续保留发布门禁。
- 真实的终局处理和正式 finalizer。`driver.py` 已要求 finalizer 认证 `full_rules_accepted`，目前没有实现。
- 探测结果：临时脚本绕过限制后，旧地图 seed 1000 无玩家命令跑到 600 Tick，全局审计通过，每 50 Tick 耗时保持 49–65 秒。这只说明空局前 2% 没崩，不能只删除 150 Tick 限制就算完成。

### P0-2 五支柱采集与评分

`scoring.py` 的 `industrial_pillar`、`control_pillar`、`technology_pillar`、`international_pillar`、`relative_ratio`、`score_interval` 都没有生产调用。

- 每周期统一采集国力，保留 2,880 周期轨迹。
- 接入已有的只读接口：`asset_book`、`debt_book`、`long_book`、`external_equity_book`、`short_liability_book`、`creditor_book`、`scoring_inputs`、`population_scoring_rows`。保证金已在现金账内，不要重复计入。
- 完成 `active_scale.py` 的待定指标后再接入主线。它目前未被任何模块导入。
- 实现 NFA、可持续性、Loss_action 的唯一归属，以及 `fx_revaluation_invariance` 要求的汇率变化不重估。

### ~~P0-3 正式地图接线~~（已做，2026-11-05）

- 主线入口改用 `world_atlas`：`world`、`bootstrap`、`strategy-opening` 默认 `--map atlas`，旧地图保留为 `--map legacy`。
- 对局用的地图经 `bootstrap.accepted_atlas_world(seed)` 产生：每个候选都真实跑 150 Tick 开局（`atlas_bootstrap`），失败就换下一个 attempt，不补资源；256 次都失败报 `INFRA_FAULT_WORLDGEN`。seed 1000 的 attempt 0 通过（约 116 秒）。
- 回放清单记录 `map_version`、`map_attempt`，`replay-strategy-opening` 按它们重建同一张图。
- 572 条 atlas 资源全部注册：72 个起始矿照旧，另外 500 个远端矿点进入 `resource_sites`/`deposits`/`resource_sources`，并在 `nature` 账上存入真实储量（水共用 `basin:<hub>`）。它们没有采掘线，要用 `build_mine` 建矿后才开采。
- 起始矿储量读 atlas 的 `reserve_mU`（数值与旧开局表相同）；旧地图没有 atlas，仍读开局表。
- `Hydrology` 在有 `world.river_downstream` 时直接用 atlas 的河流 DAG，按高程从高到低处理；旧地图仍用原来的推导。
- 测试 `tests/test_atlas_wiring.py`（4 项）：资源注册与储量、河流 DAG、远端矿建成并实际消耗储量、重抽门禁。旧地图逐 Tick 哈希不变。
- 已知限制：远端矿只有己方控制的枢纽能建；建材要在该枢纽有库存，所以首都以外的矿需要先运料。

### P0-4 引擎性能（第一轮已做，2026-11-05）

**单局墙钟上限：6 小时**（28,800 Tick，即无玩家命令时 ≤ 0.75 s/Tick，单核）。

依据：正式批次 64 seed 加同数量影子局共 128 局。按 6 小时/局，64 核机器并行两轮约 12 小时，一个通宵能跑完一批。上限比这再松，批次就要跨天，无法当天出分复核。

第一轮结果（seed 1000，`opening_economy` 无命令，aarch64 沙箱单核，同机前后各跑 25 Tick）：

| | 秒/Tick | 一局估算 |
|---|---|---|
| 优化前（c4586c7） | 1.35 | 10.8 h |
| 优化后 | 0.71 | 5.7 h |

60 Tick 内逐 Tick 状态哈希与优化前完全一致（每 Tick 后取 `fingerprint(state.snapshot())` 逐行比对）。每 Tick 耗时稳定，没有随 Tick 增长，整周期结算 Tick 约慢 25%。

改了什么（都不改变结果，只减少重复计算）：

- `materials.commit`：不再每次对全部材料池做 `intersection_update`，只处理本次计划动过的账户。依据是每次提交后都有 `_virtual ⊆ _pools`。
- `determinism.primitive` / `digest`：常见精确类型走快速路径；`digest` 的键只含 `str`/`int`，直接编码。字节输出不变。
- `split_payment`：没有余数要分时直接返回；有余数时只给余数为正的键算哈希。余数和等于待分数且每个都小于 1，所以零余数的键不可能被选中。
- `split_origins`：没有零头时不再排序算哈希；`delivery_key` 每笔只算一次。
- `_input_payment`：本 Tick 材料事件按 (owner, good, bucket) 建增量索引，替代每次全表扫描（原来是 Tick 内平方级）。
- `warehouse_load_at(hub)`：只算一个枢纽的仓储占用。
- 电力预留里的 `unpaid_fraction` 改为循环前一次性按付款方汇总。

剩余耗时主要在标准库 `Fraction` 运算本身（约占一半），引擎自身的扫描已不突出。再往下需要改数值表示，会碰到结果口径，不在本轮做。

尚未验证：

- 有玩家命令时的单 Tick 耗时（需 P0-1 的对局入口和参考策略后测）。
- 正式地图接线（P0-3）后实体规模变大时的耗时。
- 上限 6 小时是针对本沙箱单核测得的。官方机器若更慢，要按同一脚本重测。

## P1：规则缺失

### 缺失的 14 条命令

06 中定义、引擎未注册。调用时返回 `ERR_SCHEMA`，不是空操作。

| 命令 | 依赖或说明 | 交接是否列出 |
|---|---|---|
| ~~`logistics/forward_contract`~~ | 已做（2026-11-05），见 `forward_v1`、`tests/test_forward_contract.py` | 是 |
| ~~`finance/secondary_offering`~~ | 已做（2026-11-05），见 `secondary_offering_v1`、`tests/test_secondary_offering.py` | 是 |
| ~~`finance/fx_short`、`finance/fx_cover`~~ | 已做（2026-11-05），见 `fx_short_v1`、`tests/test_fx_short.py` | 是 |
| ~~`diplomacy/terminate_contract`~~ | 已做（2026-11-05）：类型 1/3/5/7/8/9 可终止，防务 −.25、其余 −.15，免责见 `contract_termination_v1`；类型 2/4/6 按各自规则结束，返回 `ERR_NO_PERMISSION` | 是 |
| ~~`military/hypersonic_strike`~~ | 已做（2026-11-06），见 `hypersonic_v1`、`tests/test_hypersonic.py` | 是 |
| ~~`military/blockade`~~（已做 2026-11-05，见 `blockade_v1`）、~~`military/interdict`~~（已做 2026-11-05） | `blockade_fraction` 已接入走廊 `blocked`，并进入 07 的 Blockade_r 与行动损耗 | 是 |
| ~~`intelligence/steal_model`~~ | 已做（2026-11-06），见 `cyber_v1` | 是 |
| ~~`governance/climate_intervention`~~ | 已做（2026-11-05），见 `climate_v1`、`tests/test_climate.py` | 是 |
| ~~`diplomacy/grant_climate_consent`~~ | 已做（2026-11-05），见 `climate_v1` | **否** |
| ~~`intelligence/cyber_op`~~ | 已做（2026-11-06）：S03 渗透/窃密、S08 瘫痪，含 E82 系统冻结、E85 归因信用、C33 暂停竞价，见 `cyber_v1`、`tests/test_cyber.py` | 仅写了 S03 |
| ~~`governance/conditional_plan`~~ | 已做（2026-11-05）：C10 + 枢纽 ai_core，最多 32 条，每窗口首 P1、玩家命令之前判定，冷却 ≥5 tick，可 cancel_by_id；口径见 `conditional_plan_v1` | **否** |
| ~~`research/set_backup_hub`~~ | 已做（2026-11-05）：新公共系统 `research_backup`（R08，计算+通信设备各 1）；实验室损毁/失控时按 04 结算（无备份留 30% 理论、验证清零；有备份留 90%/75%），见 `research_continuity.py`、`tests/test_research_backup.py` | **否** |

### 与冻结规则不一致（交接未提及）

1. ~~**`finance/stock_order` 没有检查 F03。**~~ 已修（2026-11-05）：融资买入和融券卖空未解锁 F03 时返回 `ERR_TECH_LOCKED`。 06 要求融资和融券先解锁 F03，但整个引擎里没有 `F03` 判断，开局即可融资融券。应返回 `ERR_TECH_LOCKED`。
2. ~~**`logistics/corridor_policy` 接口不符。**~~ 已改（2026-11-05），口径见 `IMPLEMENTATION_CONTRACT.md` 的 `corridor_policy_tiers_v1`；预锁费用和在途掉头仍未做。
   - 规则：`access_tier: 0..3` 和 `toll_rate_bp: 0..2000`，费用按实际载货批次的基数计算。
   - 引擎：`allowed: bool` 和固定的 `toll_ru_mU`，见 `transport_operations.py:696`。
   - 费率还写在走廊上，改一国费率会影响所有国家。`blockade_food_substitution` 依赖 `access_tier`。
3. ~~**本国地区的支持度事件几乎没接。**~~ 已接（2026-11-05），口径见 `IMPLEMENTATION_CONTRACT.md` 的 `support_events_v1`；C05 对医疗系数的 B1 修正仍未接。 `local_economy.py:44` 把 `occupied` 写死为 `False`，`events_mU` 只有经济学说切换一项。因此下列规则未生效：敌占 −12、C04 黑网 −20、E26 B1 欠供、E71 bombard −300。

### 未找到实现的耦合

以下是按公式特征检索代码的结果，接手时先核对一次。

| 编号 | 内容 |
|---|---|
| ~~C06、C07~~ | 已接（2026-11-06），见 `import_parity_v1` |
| ~~C15~~ | 已接（2026-11-06）：停运折减与高压线路线损两项 |
| ~~C16~~ | 复核（2026-11-06）：`StockMarket._npc_policy` 已实现阈值与动量池两项，原清单误报 |
| ~~C18~~ | 已接（2026-11-06），见 `couplings_v1` |
| ~~C19~~ | 已接（2026-11-06），见 `couplings_v1` |
| ~~C24~~ | 已接（2026-11-06），见 `couplings_v1` |
| ~~C25~~ | 已接（2026-11-06），见 `couplings_v1` |
| C28 | 信用约束跨国电力容量和新合同。**阻塞**：引擎没有跨境电力线路和电力合同子项（03 类型 1 的 corridor 电力子项未实现），无处可乘 |
| ~~C33~~ | 已接（2026-11-06，cyber_v1） |

C03、C05、C09、C13、C14、C22、C23、C29、C31、C32 已确认有实现。

### 有定义但没有效果的科技

代码和配方数据都没有引用：

- ~~`A07` 精确农业~~（2026-11-06，`tech_effects_v1`）
- `E12` 洲际能源互联（2026-11-06 接了海底线路与 8% 线损封顶；跨国电力合同结算**阻塞**，同 C28）
- ~~`F04` 主权基金~~（2026-11-06，`equity_stake_v1`；战时为自动冻结）
- ~~`G07` 协同作战~~（2026-11-06 多兵种 ×1.2；追踪精度现已按国家共享，比规则宽松，待作者决定）
- ~~`G12` 全域联合指挥~~（2026-11-06，×1.1 与首都失联冻结 10 tick；跨战区接力未做）
- ~~`I12` 分子级制造~~（2026-11-06 投入 −30%、无次品、电力不足报废在制品）
- `L12` 巨构工程
- ~~`V07` 生态修复~~（2026-11-06，biomass 部分；water 无再生率状态）
- ~~`V08` 区域气候干预~~（2026-11-05 已接，见 `climate_v1`）

`F03` 同样未引用，见上文。其他 43 个科技没有代码引用，但有配方或军事数据门控，其效果未逐项核对。

### 已写好函数但生产路径未调用

| 函数 | 待接入的规则 |
|---|---|
| `Authority.military_operating_factor` | 敌占区军事运行系数 0.6 |
| `FacilityOwnership.prepare_cost_payment` | JV 经营成本按份额分摊 |
| `CityStateEconomy.record_coercion` | 胁迫城邦的关系后果 |
| `Consumption.synthetic_share`、`eta` | 评分采集 |

### 交接已列出的其余项

- 破产重整和强制拍卖：引擎里没有相关代码。
- 投降和俘虏：引擎里没有相关代码。
- 完整野战补给和航空航程。
- 占领后的居民、工资和税币归属。
- 设备维修、残值和抵押的经济所有权。
- 客运跨模态换乘，Cargo 与客运同档统一容量仲裁，水渠 40% 上限。
- 电力合同的专用负荷和交付。
- 城邦转口的封堵补偿。
- 融资融券的强平连锁、费用并入本金和跨托管卖出，见 [HANDOFF_FINANCE_20261105.md](HANDOFF_FINANCE_20261105.md)。

## P2：比赛交付

- **参考策略：** 没有能走正式协议的参考池。`designs/rift_era/prototype/` 的 bot 基于原型模拟器，不能直接用。
- **批次评分：** `evaluation.py` 的 `Batch.write_new`、`shadow_key`、`ResourceBudget.concurrency` 没有运行器调用。需要实现 64 seed、影子基准和 `INVALID_EVALUATION`。
- **题库接入：** 按 [INTEGRATION.md](INTEGRATION.md) 交付目录、`score.py` 和导出脚本，最后的注册由仓库作者在私有环境完成。
- **计分校准：** V 梯度目标已定：
  - 混坐 64 seed 的平均 V 满足 `Super > Advanced > Medium`，相邻两档的 95% 置信区间不重叠。
  - 具体差距暂不定，等正式参考策略跑出数据后再定。
  - 现状：原型混坐 seed 1000 时 V 倒挂（Super 78.8 < Advanced 139.2），这只是单个 seed 的原型数据。
  - 先处理现金在 NFA 中不封顶、库存不计值这两个主因。两项都要等 P0-2 的真实 NFA 采集接上后才能验证。
  - 如需改评分规则，发新规则版本，不要改 `rift_era_rules_r6` 的文件。
- **快照缺口：** `EquityFinance.sequence` 影响强平单 ID，但不在 `snapshot()` 里，应补上。`Macroeconomy`、`Payments`、`ForeignExchange`、`DebtFunding`、`Relations`、`Battles` 等没有 `audit()`，建议补。
- **宿主：** 512 MiB 管道容量和 cgroup 委派的问题仍未解决，见 `IMPLEMENTATION_CONTRACT.md` 的策略 IPC 一节。

## 15 个动态场景的就绪情况

这是静态判断，场景都没有执行过。引擎侧没有场景运行器，`round6_scenarios.json` 只被文档引用。

| 状态 | 场景 |
|---|---|
| 机制已有，可以先写场景测试 | `patrol_loop`、`repair_single_count`、`healthy_relocation`、`cpu_budget`、`zero_material_hostility`、`damage_power_and_repair`、`relations_and_neutrality`、`barrage_no_gains`（缺 bombard −300） |
| 需要评分采集（P0-2） | `fx_revaluation_invariance`、`transit_formula`、`world_pool_pressure` |
| 需要缺失命令或修正接口 | `cyber_credit_floor`（`cyber_op`）、`blockade_food_substitution`（`corridor_policy`）、`energy_substitution`（`corridor_policy`） |
| 需要批次运行器 | `batch_consistency` |

## 文档待修

- `README.md`：多处仍是 0.4.0 时的说法，例如“尚未接入”和“下一项实质性交付”。`ENGINE_VERSION` 也还是 0.4.0，冻结新版本时要一起升。
- `validation/command_surface_20261104T180634Z.json`：记录 42 条已注册，当前是 67 条，需要重新生成或标为历史记录。
- `HANDOFF.md`：第 3、4 项应补上表中标“否”的命令；第 8 项应补上性能。

## 建议顺序

1. 先修两处规则不一致（F03、`corridor_policy`）和支持度事件，改动小，能解除场景阻塞。
2. 同时做 P0-3 和 P0-4。地图接线后实体规模才接近真实，这时做性能优化才有意义。
3. 完成 P0-1 和 P0-2，让引擎能跑完一局并出分。
4. 补缺失命令和耦合，每项都写场景测试。
5. 做参考策略和批次运行器，再把 `FULL_MATCH_TEST_TODO.md` 交给测试方。

### 空局人口下降（2026-11-06 排查）

atlas seed 1000 空局前 43 周期：8 国首都 Hub 每周期净增约 +0.05%（食物满足 1.0）；50 个人口 Hub 中 42 个是城邦，食物满足度一直为 0、支持度 0，每周期净减约 0.4%（死亡率 .001 + 饥荒 .003），总人口因此下降。原因是城邦只有两座矿、没有农场，食物只能从市场买，空局里没有任何席位挂食物卖单。按 03 城邦规则这属于预期行为，但完整一局 2,880 周期下，无人交易的城邦会饿到接近零。是否给城邦一个兜底食物来源（例如 world 卖方或初始储备），需要作者决定。诊断脚本在开发目录 `dev/diag/popdiag.py`。

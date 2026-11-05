# 裂陆纪元：独立设计与完整规则规范包

状态：**规则已冻结，版本 `rift_era_rules_r6`，以 Round 6 为准（2026-11-04 用户确认）。** Round 6 响应 Opus 5.5 终审提出的 OR-001～017，Gemini 复审全部关闭并审核通过。原型为不同经济近似模型，不是该规则的实现；完整对局引擎尚未完成（开发目录 `organizer/rift_era`）。

这是一道面向策略程序的大世界即时策略题。参赛程序控制一个近未来主要国家，在 `512×512` 多大陆跨洋世界中，对同时运行的产业、采掘、电网、多模态物流、科研、军事、情报、外交和国内治理系统作出长达 `28,800 Tick`（`2,880` 个工业周期）的全局耦合决策。世界其余 7 个主要国家与 `36..48` 个中立城邦由固定参考策略池与确定性效用规则驱动，不需要自然语言裁判。

本目录可以整体复制给任何开发者、设计者或后续实现引擎的 AI，无需原始聊天记录。它不包含其他题目的设计或对比。

## 已确定的用户要求

- 大世界（已按指示做大至 `512×512` 多大陆跨洋尺度）、即时策略、丰富的机制与可能性，重点考验模型的全局思维。
- 目标难度超过现有「前沿实验室」；这是设计目标，尚待引擎实现后实测验证。
- 多个机制深度相互作用，具备完整、可选择的 123 个研究节点 + 7 个 AI 能力里程碑（12 主干、6 阶，含互斥制度组）科技树和 119 配方工业系统。
- 成绩连续映射到 0–100 分，不只记录胜负；严格满足防焦土性质：2026-11-03 13:14 UTC用户确认任何损人行动必须严格扣分，包括行动方无其他代价；作战/敌对耗材按目录值记自身损耗，不给消纳/回执分，零耗材分支也须正扣项。独立领土、市场等实益大于损耗时正常竞争仍可净得分（精确定义见07）。
- 默认每次从操作系统安全随机源抽取 64 个互不重复 seed，多局平均；同批提交共享评测条件；保留种子和版本以重放。
- 允许检测可用 CPU 并行评分，但必须给操作系统预留 CPU 和内存余量，并对策略子进程实施硬边界隔离。
- 设计与规则规范在本目录独立归档；正式引擎在 `organizer/rift_era` 另行开发。
- v1.3（2026-11-03 用户）：完整的金融股市与外汇机制；科技树添加 AI，AI 科技是特殊机制；人口不设上限；完整的电力与 AI 机制。
- 2026-11-03 11:35 用户：“我希望所有机制相互耦合，做到一体性”（见 [10_system_coupling.md](10_system_coupling.md)、[0013](decisions/0013_system_coupling.md)，已采纳并合入 v1.4 正式规范）。

本轮执行补充：[11_execution_contract.md](11_execution_contract.md)；新增数值表：[runtime_rules.json](data/runtime_rules.json)、[military.json](data/military.json)。审核与逐条修订记录：[LOG.md](review/LOG.md)。

## 阅读顺序

| 文件 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | 协作边界、事实源约束与设计诚实性要求 |
| [00_design_principles.md](00_design_principles.md) | 设计理念、预期全局思考难点与反退化原则 |
| [01_world_and_realtime.md](01_world_and_realtime.md) | `512×512` 双层空间、开局自举配平、六维控制权矩阵、三级时钟与 `P1–P8` 守恒流水线 |
| [02_economy_logistics_governance.md](02_economy_logistics_governance.md) | 三部门守恒宏观经济、破产重整、v1.3 无上限人口、货币外汇央行、证券与债券、危机传导、电力市场、7 类物理走廊、12 种资源采掘衰竭与三级可预测国内治理 |
| [03_warfare_diplomacy_intelligence.md](03_warfare_diplomacy_intelligence.md) | 11类作战单元与双槽位改装、同时射击与侦察耦合、迷雾与雷达暴露、轨道寿命、9 类合同与城邦效用 |
| [04_technology_tree.md](04_technology_tree.md) | 12 条主干、6 阶、123 个研究节点与 7 个 AI 能力里程碑、互斥制度组、研发容灾保密制度、系统科技定量表与 AI 科技线 |
| [05_industry_system.md](05_industry_system.md) | 108 种物品/流量、119 个工业配方、S1–S5 设施、设施维护欠账、v1.3 完整电力系统（机组、天气、电网、储能、调度、切负荷）与 AI 算力设施 |
| [06_agent_interface.md](06_agent_interface.md) | 零信任物理沙箱、OS 硬内存/进程上限、RLE 增量自愈协议、高层持续计划（v1.3 新增金融与 AI 命令）与标准回执码 |
| [07_scoring_and_resources.md](07_scoring_and_resources.md) | 静态影子权重、五支柱原始国力 $V$、四纪元可持续积分、影子反事实座位公平与双门限并发调度 |
| [08_open_questions.md](08_open_questions.md) | `Q01–Q46` 全量状态追踪与四级分层验证状态（`SPEC_DEFINED / CATALOG_CHECKED / PROTOTYPE_TESTED / FULL_ENGINE_PENDING`） |
| [09_collaboration_plan.md](09_collaboration_plan.md) | 模块分工接口与后续原型交接指南 |
| [10_system_coupling.md](10_system_coupling.md) | 17 机制耦合矩阵、主要增强/调节回路、33 条新增耦合公式与有界性、`P7` 结算顺序、原型实现清单与张力（0013 已采纳） |

## 专题提案与架构决策记录（`decisions/` & `proposals/`）

- **架构决策记录**：
  - [decisions/0001_scope.md](decisions/0001_scope.md)：初始用户要求与 `v0.1` 范围边界
  - [decisions/0002_topology_timing_and_tick_order.md](decisions/0002_topology_timing_and_tick_order.md)：`512×512` 超大陆双层拓扑、`9,600 Tick` 长时钟与 `P1–P8` 守恒结算
  - [decisions/0003_national_power_and_seat_fairness.md](decisions/0003_national_power_and_seat_fairness.md)：抗刷分五支柱国力 $V$、四纪元可持续折现与同种子影子反事实座位公平
  - [decisions/0004_world_init_extraction_and_macroeconomy.md](decisions/0004_world_init_extraction_and_macroeconomy.md)：`T=0` 正余量启动包、12 种资源采掘衰竭、7 类走廊建设与守恒货币破产规则
  - [decisions/0005_tech_modifiers_factory_and_grid_physics.md](decisions/0005_tech_modifiers_factory_and_grid_physics.md)：全 64 科技定量语义闭合、研发备份保密、工厂质检维护与电网储能核冷物理
  - [decisions/0006_combat_intelligence_and_orbital_rules.md](decisions/0006_combat_intelligence_and_orbital_rules.md)：9 类作战编制双槽位、同时射击与侦察耦合、迷雾通信延迟与 LEO 180 周期寿命
  - [decisions/0007_control_governance_and_diplomacy_rules.md](decisions/0007_control_governance_and_diplomacy_rules.md)：六维控制权状态转移矩阵、三级透明治理预警、不可抗力合同与城邦 EMA 效用
  - [decisions/0009_tech_expansion.md](decisions/0009_tech_expansion.md)：科技树扩增至 120 节点、6 阶、11 主干与互斥制度组（v1.1）
  - [decisions/0010_longer_game_population_and_package_submission.md](decisions/0010_longer_game_population_and_package_submission.md)：局长 ×3、逻辑斯蒂人口、实验室 ×1.5、压缩包提交。
  - [decisions/0011_opponent_pool_and_scoring_set.md](decisions/0011_opponent_pool_and_scoring_set.md)：对手池与评分集分离。
  - [decisions/0012_v13_population_finance_forex_ai.md](decisions/0012_v13_population_finance_forex_ai.md)：v1.3 人口无上限、货币外汇、证券债券、AI 特殊科技线与完整 AI/电力系统（已采纳，细则以 `00–07, 10` 与 `data/*.json` 为准）。
  - [decisions/0013_system_coupling.md](decisions/0013_system_coupling.md)：系统耦合，全部机制接成一体（已采纳，细则以 `10_system_coupling.md` 与 `data/coupling.json` 为准）。
  - [decisions/0008_sandbox_isolation_and_ipc_quotas.md](decisions/0008_sandbox_isolation_and_ipc_quotas.md)：零信任硬沙箱配额、双门限余量调度、故障二分矩阵与可扩展持续计划协议
- **历史推导提案（非权威归档）**：见 [proposals/](proposals/README.md) 下的专题方案（`AF`、`E`、`BA`、`B`、`C`、`D`、`F`、`G`、`H`）。当提案与 `data/*.json` 或 `00–07, 10` 正式规范冲突时，一律以 `data/*.json` 与正式规范章节为准。
- **可视化源文件**：[完整科技依赖图](diagrams/technology_tree.mmd)、[产业与世界系统关系图](diagrams/industry_overview.mmd)。

## 本地结构与一致性检查

只需要 Python 3 标准库，在本目录运行：

```bash
python3 tools/catalog.py check
python3 tools/catalog.py render
python3 tools/coupling.py check    # 耦合矩阵（data/coupling.json → 10_system_coupling.md）
```

`check` 校验全部 130 项科技 ID（含 7 个 AI 能力里程碑的阈值单调性）、互斥制度组、108 种物品、119 项配方、DAG 无环性、理论可达性、自动生成段落一致性与全目录 Markdown 相对链接完整性。

终审意见与处理：[Opus报告](review/FINAL_REVIEW_opus55_2026-11-03.md)、[Round6日志](review/LOG.md)；场景契约：[round6_scenarios.json](data/round6_scenarios.json)。OR复核安排由用户决定。

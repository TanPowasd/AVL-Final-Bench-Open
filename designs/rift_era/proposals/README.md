# 设计提案索引（Tier 3 历史推导归档）

> **权威性声明（AR-001）**：本目录下的所有提案文件（`proposals/*.md`）均为设计演化过程中的**历史推导归档（Tier 3）**，不作为冻结规范的直接依据。当本目录任何文件中的草案文字、旧版公式或候选参数与机器可读目录（`data/*.json`，Tier 1）或正式规范章节（`00–07, 10`，Tier 2）存在差异时，**一律以 `data/*.json` 与正式规范章节为准**。

不同协作角色使用不同文件名，避免覆盖。可复制 [TEMPLATE.md](TEMPLATE.md)，按任务字母和主题命名。提案应说明相对既有草案的变更，区分规则选择、分析推断与待原型实测事项。

## 已完成并合入 `v1.0` 设计规范的专题提案

| 提案文件 | 覆盖任务与问题 ID | 对应决策记录 | 核心内容摘要 |
|---|---|---|---|
| [AF-world-topology-timing-and-tick-conservation.md](AF-world-topology-timing-and-tick-conservation.md) | 任务 A+F（`Q03, Q06`） | [0002](../decisions/0002_topology_timing_and_tick_order.md) | 双层世界拓扑（按指示扩至 `512×512` + `~480` 分区）、三级长时钟（`9,600 tick / 960 周期`）、毫单位账本与 `P1–P8` 无偏守恒流水线 |
| [E-national-power-scoring-and-seat-fairness.md](E-national-power-scoring-and-seat-fairness.md) | 任务 E（`Q01, Q02`） | [0003](../decisions/0003_national_power_and_seat_fairness.md) | 静态目录影子价值、五支柱抗刷分国力 $V$、四纪元可持续积分与 64-seed 同种子全参考影子局反事实座位公平 |
| [BA-world-init-extraction-and-macroeconomy.md](BA-world-init-extraction-and-macroeconomy.md) | 任务 B+A（`Q04, Q05, Q08`） | [0004](../decisions/0004_world_init_extraction_and_macroeconomy.md) | 8 大国 `T=0` 正余量启动包、12 种资源 `G1/G2` 采掘衰竭、7 类物理走廊与 `L08` 模块化建造、三部门守恒货币与主权破产重整 |
| [B-tech-modifiers-factory-ops-and-power-grid.md](B-tech-modifiers-factory-ops-and-power-grid.md) | 任务 B（`Q09, Q10, Q11, Q17`） | [0005](../decisions/0005_tech_modifiers_factory_and_grid_physics.md) | 18 项非配方系统科技完整定量语义（64/64 全闭合）、研发并行保密与 `R08` 备份、工厂质检维护欠账及电网储能核冷停堆物理 |
| [C-combat-formations-intelligence-and-orbital.md](C-combat-formations-intelligence-and-orbital.md) | 任务 C（`Q12, Q16, Q18`） | [0006](../decisions/0006_combat_intelligence_and_orbital_rules.md) | 9 类作战单元属性、双槽位改装、同时射击与侦察—制导耦合、三种基建姿态、迷雾区间与雷达电磁暴露、LEO `180` 周期寿命循环 |
| [D-control-transitions-governance-and-diplomacy.md](D-control-transitions-governance-and-diplomacy.md) | 任务 D（`Q13, Q14, Q15`） | [0007](../decisions/0007_control_governance_and_diplomacy_rules.md) | 六维控制权状态转移矩阵、零随机数三级国内治理预警与恢复方程、9 类合同与不可抗力豁免、`36..48` 城邦 20 周期 EMA 效用函数 |
| [F-sandbox-isolation-budgets-and-scalable-ipc.md](F-sandbox-isolation-budgets-and-scalable-ipc.md) | 任务 F（`Q07, Q22`） | [0008](../decisions/0008_sandbox_isolation_and_ipc_quotas.md) | 零信任目录禁网隔离、`1 GiB / 1 进程 / 4 线程` OS 硬上限、双门限余量调度、故障二分矩阵、RLE 增量自愈与高层持续计划协议 |
| [G-tech-expansion-tiers5-6-and-new-branches.md](G-tech-expansion-tiers5-6-and-new-branches.md) | 科技扩增（`Q23, Q24, Q25`） | [0009](../decisions/0009_tech_expansion.md) | 5–6 阶与 S/V/F 三条新主干（120 节点）、S5 设施与增材线、互斥制度组、轨道碎片/气候/网络/金融外部性与全球累积排放候选机制 |

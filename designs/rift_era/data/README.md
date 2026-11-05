# 目录数据说明

这些JSON是设计目录的数据源，不是可以直接运行的游戏配置。

`technology.json`：branch 是主干，tier 是研究成本档位，requires_all 为同时满足的前置，非空 requires_any 表示其中至少满足一个；unlocks 描述解锁能力，其完整数值语义需在对应规则中补齐。v1.3 新增 `capability_milestones` 数组：与 technologies 共用 ID 命名空间，`unlock` 恒为 `capability`，无 tier，以 `cap_threshold`（模型能力 Cap 阈值）代替理论点，`milestone` 为 `algorithm / deployment / frontier`；不消耗理论点与验证包，链式前置的阈值必须递增。

`industry.json`：goods 定义物资与流量；sources 定义潜在来源而非免费产量；recipes 包含科技前置、厂类、设施档位、每周期投入/产出、电力与劳动。installed 为一次性装机，不可按每周期扣除。facility_profiles 定义厂房与维护；line_modes 定义制造线的互斥架构。

空科技前置表示 BASE。所有投入和产出均为抽象游戏单位。精确的采掘、价格、人口、战斗和完整运行预算不在这两个文件中。

修改后从设计目录执行 `python3 tools/catalog.py render`，再执行 `python3 tools/catalog.py check`。请保留已有 ID，不通过重新排序更改 ID 的含义。

`coupling.json`（0013）：系统耦合清单。`mechanisms` 为 17 个机制（`layer`：`econ` 经济层、`mil` 军政层、`sink` 计分汇点）；`existing` 为既有耦合（`from`、`to` 列表、内容与文档出处）；`new` 为新增耦合 `C01–C33`（公式、有界性 `cap`、滞后口径 `lag`、`prototype` 表示纯经济原型应否实现）。修改后执行 `python3 tools/coupling.py render` 与 `check`。

`scoring.json`（0014）：第 07 章计分系统的结构化参数与静态目录影子价值事实源。包含全部 108 种物品/流量的静态影子权重 `w_cat`（含联产物理当量分摊与回收物固定点推导规则）、五大支柱公式参数、全部 130 个科技/里程碑节点的落地规模口径 `active_scale_rules` 与阈值 `theta`、四纪元与有符号终局可持续调整规则，以及有符号国力正变换 $\tilde{V}(V)$ 与防焦土反事实相对比 $R_k$、测试向量。catalog检查现有引用/枚举/向量，并不验证计算端完全实现。


`runtime_rules.json`（Round4）：启动包、整机集合、采掘、环境、预算/动态实体限额与执行策略参数；计算语义在01–07及11。`military.json`：十一类编制、部署成本/crew/火力/射程/消耗，编制ID可不同于库存good，recipe_id非null时必须真实产出对应装机整机。两表须额外跨表校验，现有catalog不自动读取它们。

JSON中的formula字符串是规范说明，不允许eval；状态机须按文档实现。冻结规则版本rift_era_rules_r6与prototype_v13_5pillar_approx不可混标签。

`review_cases.json`是Round4独立算术/跨表检查结果与期望值的可审阅记录，不是游戏数值配置，也不是正式引擎测试通过报告。


Round6：scoring.json包含action_loss正扣项、ACTION_LOSS用途排除和历史RU金融账；runtime_rules新增外交状态、封锁及资产修理参数。round6_scenarios.json为OR定点场景契约，scope明确区分算术与formal_engine_acceptance_pending；不将期望值伪装成已执行结果。review_cases.json中的旧轮结果保留所属版本。

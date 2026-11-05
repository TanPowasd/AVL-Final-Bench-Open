# 裂陆纪元引擎开发

**2026-11-05 已按用户要求收尾并停止扩展。最新状态、验证与剩余工作请先读 [HANDOFF.md](HANDOFF.md)。下面保留历史模块说明；历史边界不代表本轮最新集成情况。正式发布门禁仍关闭。**

当前版本：`rift_era_engine_0.4.0`。已包含规则组件、供应链集成场景、真实账本约束下的八国开局经济、实物现货交易及命令换汇、跨锁仓/生产/运输的原产物资账和实际消费统计，以及 Linux 沙箱启动器。**完整 28,800 Tick 对局、全部命令和正式评测尚未完成**。`--official` 明确失败，退出码为 2。未接入题库统一评分与公开导出。持续交付目标与验收进度见 [IMPLEMENTATION.md](IMPLEMENTATION.md)。

规则基线为 [`rift_era_rules_r6`](../../designs/rift_era/README.md)（已冻结）。六份机器数据从 `designs/rift_era/data/` 只读加载并参与指纹；不复制原型代码，不执行 JSON 中的公式字符串。仅依赖 Python 3.11+ 标准库。

工作区存在尚未冻结的新实现：施工/维修/装机、科研验证、维护付款池、能源政策、治理培训、储能、水文、多枢纽民生、央行/宏观结算、守恒成本精度、策略进程 IPC、八席位窗口/回放调度，以及公路实体运输和 G1/G2 施工、陆军编队/交战/行军、补员维修及交易禁运。最新进展以 [IMPLEMENTATION.md](IMPLEMENTATION.md) 为准；新增接口和候选参数见 [IMPLEMENTATION_CONTRACT.md](IMPLEMENTATION_CONTRACT.md)。下方 0.4.0 模块边界是已入库检查点的说明，不能替代新版本的完整验收。

## 运行与验证

在仓库根目录执行：

```bash
python3 -m organizer.rift_era check
python3 -m unittest discover -s organizer/rift_era/tests -v
python3 -m organizer.rift_era world --seed 1000
python3 -m organizer.rift_era bootstrap --seed 1000
python3 -m organizer.rift_era scenario --seed 1 --ticks 100
python3 -m organizer.rift_era scenario --seed 1 --ticks 100 --decision-ticks 1
```

`world` 构造 512×512 的空间候选及八大国开局模板，输出规模与状态指纹；`bootstrap_accepted=false`，没有把几何检查冒充 150 Tick 动态自举。`scenario` 执行两个枢纽之间的生产、运输、卸货和返程，是开发用集成场景；P3 电力/劳动额度是明确的外生测试条件，不计算正式国力。

`bootstrap` 实际执行 150 Tick 八国开局经济：P1 冻结民生、工资、工业原料及电力预算；真实有限清算池换汇；P3 消耗煤/水并付电费；P6 采掘与制造共同竞争仓容；P7 实付工资、居民采购、税收、宏观与人口/污染更新。它检查资金/物资守恒、粮食缓冲、工资欠款及自然供水，报告 `passed_core_checks`。该 profile 尚未装配完整金融合约、城邦经济、军事/外交和完整对局业务，因此 `full_rules_accepted` 固定为 false，也不允许借此 profile 跑超过 150 Tick。`--audit-every-phase` 可开启八阶段状态指纹与逐阶段审计。

开发 API `opening_economy(..., commerce=True)` 在相同经济系统内接收 `Kernel.step({nation: orders})`，已执行 `logistics/market_bid`、`market_ask`、`trade/export_offer`、`buy_from`、`cancel/cancel_by_id` 和 `finance/fx`。命令永久水位、字段校验、货款预锁、同 Hub 原地过户、P5 竞价、退款和 P8 本国成交回执均进入真实状态；默认 `commerce=False` 的自举仍拒绝任何玩家命令。此 API 是开发集成入口，不能绕过正式发布门禁。

工作区新增命令处理器覆盖已实现的施工/科研/能源/治理以及 `finance/central_bank`，统一注册于 [`engine/commands.py`](engine/commands.py)，未注册操作仍明确拒绝。央行政策下周期生效，替换使用 `replaces_id`；实际发行、干预和资本管制费进入现金账，观测提供本国宏观状态。

开发物流已支持本国公路上的 `route_shipment`（`manual` / `P036`）及公路 `build_corridor`（G1/G2），具备真实车队、尾批装载、返程、有限仓容和露天损耗。其余运输模式、跨国 ACL/收费和乘客迁移尚未接通；精确边界见 [实现口径](IMPLEMENTATION_CONTRACT.md)。

地区市场标识使用公开 Hub ID；仅库存品可交易，整机按 1000 mU 量子。买方须实际持有交易币种。`fx.amount_mU` 表示交出币种的含手续费预算，有限池统一分配后返还未用金额；跨币到账在该 P1 批结算时完成，不用另一条尚未结算的换汇请求为当前命令预锁出资。双边买单只引用上一窗口已有的公开报价，多国竞争同一报价按数量比例分配，未成交预算即时返还。同拍卖方撤单使该报价不成交；不会按国家遍历顺序抢先买走。出售和过户不产生末端消费回执。

回放必须使用一个尚不存在的路径：

```bash
python3 -m organizer.rift_era scenario --ticks 100 --replay /tmp/rift-new-run.jsonl
python3 -m organizer.rift_era verify-replay /tmp/rift-new-run.jsonl
python3 -m organizer.rift_era replay /tmp/rift-new-run.jsonl
```

`verify-replay` 验证哈希链、完整性和终止记录；`replay` 重新执行供应链场景，逐 Tick、逐阶段比较状态哈希。规则、源码、运行时不匹配会拒绝重执行。回放以排他创建方式写入，不覆盖已有记录。

## 策略进程开发入口

[`sdk.py`](sdk.py) 是可复制到提交 zip 的纯标准库客户端，提供分页校验、`ready()`、单调命令 ID 和带窗口号的 `orders()`。真实进程调度模块为 [`engine/ipc.py`](engine/ipc.py) 和 [`engine/driver.py`](engine/driver.py)。例如在满足硬隔离与完整输入缓存要求的宿主上：

```bash
python3 -m organizer.rift_era strategy-opening --seed 1000 --ticks 150 \
  --submissions p0.zip p1.zip p2.zip p3.zip p4.zip p5.zip p6.zip p7.zip \
  --cgroup /path/to/delegated-cgroup --replay /tmp/rift-strategy-new.jsonl
python3 -m organizer.rift_era replay-strategy-opening /tmp/rift-strategy-new.jsonl
```

这仍是开发开局装配，报告不含正式分数，也不接受超过 150 Tick。所有席位通过同一沙箱入口运行；不提供普通子进程降级选项。当前宿主尚不满足 cpu/pids 委派和默认 512 MiB 管道容量申请，命令会报基础设施故障。输入容量的推导、计时边界及内存验收要求见 [实现口径](IMPLEMENTATION_CONTRACT.md#策略-ipc-与调度)。已有八席位可信 fixture 的真实管道/回放测试，但它不是目标宿主沙箱验收。

## 模块边界

| 模块 | 已实现的职责 | 当前边界 |
|---|---|---|
| `catalog.py` | 不可变目录、引用/DAG、整机与军品跨表检查、数据指纹 | 科技文本效果仍需要各系统显式实现 |
| `numeric.py`、`determinism.py` | mU/nmU、精确有理数、两种分配舍入、独立哈希随机流 | 超越函数须记录运行时；不能保证跨所有平台逐位相同 |
| `ledger.py` | 原子库存转移、显式源汇、多币现金、历史 RU 成本、两腿 FX、费用残差、只读守恒审计；跨库存/现金 DVP、无副作用预检及过期事务拒绝 | 业务层仍须检查科技、权限、发行上限、抵押和清算优先级；账本只由单线程 Kernel 修改 |
| `materials.py`、`consumption.py` | 原产/消环路径/合成食品属性随实物原子转移；真实输入信用和在制品对账；使用时信用冻结、20 周期实耗和末端回执、永久逐国逐品 SinkDebt | 已接开局生产/军粮/居民、商品交易及运输组件；建设投产限额、赠与援助、跨境服务和完整五支柱指标仍待接通 |
| `clock.py`、`kernel.py` | 三类独立时钟、P1–P8、系统依赖排序、阶段审计和错误隔离 | 完整国家模拟的系统装配尚未完成 |
| `world.py`、`initialization.py` | 候选地图、连通领土、枢纽/路线、27 S1/9 G1 的大国开局模板、人口/军职/现金分账 | 完整流域、岛链和城邦实体经济尚待接通 |
| `economy_state.py`、`economy.py`、`payments.py`、`bootstrap.py` | 八国 150 Tick 真实经济；预算预留、有限 FX、物理资源池、工资/消费/税收、冷启动与民生断供 | 仅固定开局计划；全局长、全命令与全部 P7 宏观耦合尚未完成 |
| `production.py` | 同阶段共享池、仓容二次分配、纳单位预扣信用、整机释放、产出缓冲、技能劳动分配 | 调用方必须先提供真实 P3 电量和当拍劳动上限；不自动套用全部科技/模式 |
| `power.py` | 机组状态、核冷却、黑启动迟滞、储能方向、整数路径配电与费用凭证；孤立电网批量算法已与逐 mU 参考算法随机对照 | 跨枢纽网络仍使用参考调度器，全规模性能未验收 |
| `logistics.py` | 独占运具、真实航程、全程预付油、卸货等待、空车返程、断边回退、缺油搁浅 | 尚需模态/ACL/拥塞准入层、部分装载、途中腐坏和凭证联动 |
| `assets.py` | 剩余成本向量、战损、修复资本化、整机修复量子拒绝 | 修复资源托管与施工指令装配未完成 |
| `market.py` | 集合竞价、同价比例分配、统一有限池、债限和偿付分配 | 商品托管/DVP 已由 spot 装配；完整证券/远期/违约状态机仍待实现 |
| `spot.py`、`commerce_commands.py` | 商品挂单托管、双边出口托管、全额现金预锁、实际 DVP、撤单/到期、有限池换汇命令、只读本国成交视图 | 商品链路与原产明细已接八阶段经济；证券/远期、世界池实物采购、出口禁运和完整宏观统计仍待装配 |
| `diplomacy.py`、`combat.py` | 双边关系状态、同时宣战、停战到期、四域火力、同时伤亡、行动损耗、封锁比例 | 占领、合同、城邦效用、军队移动/后勤、附伤、情报和轨道的完整装配未完成 |
| `research.py`、`ai.py` | 项目绑定、跨 Hub 递减、验证、局地算力预算、批次寿命、训练、数据去重、里程碑 | 科研/AI 实际资源支付、设施装机、事故与服务交易未全部接通 |
| `population.py`、`environment.py` | 分技能人口、住房、生育/死亡、治理预警、迁居到达、有限矿、河网守恒、污染 | 民生采购、工资税收、社会保障和全部 C01–C33 耦合尚未装配 |
| `provenance.py`、`scoring.py` | 原产标签、全路径消环、末端回执、行动损耗、五支柱函数、四纪元、精确比值、分数区间 | 实物消费/回执已采集；其余国力指标、正式完整轨迹仍待装配，当前没有正式对局成绩 |
| `protocol.py`、`observation.py` | 有界 JSONL、窗口校验、命令水位/替换、分块快照、观测者别名、冻结轨迹 | 尚需完整命令 Schema、真实传感器与进程 IPC 驱动；轨迹老化待接通 |
| `archive.py`、`evaluation.py`、`replay.py` | 安全解包、批次 manifest、资源准入、思考预算、只增回放 | 完整对手运行器与正式评分装配待实现 |
| `sandbox.py`、`sandbox_entry.py`、`seccomp_filter.py` | bwrap 隔离、真实 cgroup 文件系统校验、1 GiB/1 CPU/5任务配置、执行用户代码前安装 seccomp、64 MiB 工作区、只读运行时 | 当前宿主未委派 cpu/pids；完整组合隔离、OOM 和并发压力验收尚未通过 |

## 扩展约束

1. 业务拒绝使用 `RuleViolation`，在 P1 逐条捕获；异常、守恒失效和宿主故障使用 `InfrastructureFault`，不能计作选手失败。失败的 Kernel 禁止继续运行。
2. 系统必须显式声明所属阶段及依赖。P7 的完整子阶段应按设计第 10 章装配，不能用函数注册顺序暗示依赖。
3. 现金只从真实账户支付，不能以产值充当收入。锁仓、到货、生产和末端使用必须是不同事件；不能直接修改余额绕过账本。
4. 同一共享池先统一收集需求，再分配、提交。实体遍历顺序不得改变结果。输入先预扣，产出进入 `next_available`，P7 后才能供下一 Tick 使用。
5. 库存使用 mU，产线信用/在制品使用 nmU；输入信用已经离开公共库存。`Line.audit()` 验证领料 = 实耗 + 信用；换线报废由调用方记损耗。
6. 真值、合法观测、序列化输出分层。传感器向 `Observer` 提供过滤后的 `Sighting`，不将整个 World 传入选手代码。未知/不可见目标的错误码还需在命令层统一。
7. 评分函数只读。损耗和正项用 `Fraction` 保存；内部比较用精确 `relative_ratio`，展示使用向外舍入的分数区间，不能用展示值排序或判断微小损耗。
8. 新机制同时交付：明确资源来源/去向、所属阶段、可观察字段、反例测试。没有实现的操作不得以成功回执或空函数代替。

## 实现解释与候选分布

**电网的累计路径舍入**：路径输电采用累计接收量反推累计发送量，再扣相邻累计值的差。若每发送 1mU 都独立执行 `ceil(1/0.9)`，100mU 只能送达 50mU，与 90% 线路效率示例冲突。当前实现使 100mU 送达 90mU，且每笔库存与费用守恒。对应测试 `test_grid_receipt_cash_and_energy_balance`。此口径已写入规则 05 章第 4 节。

**地图分布 `rift_spatial_stratified_v1`**：24×20 个战略分区，20 个陆地列、4 个海洋列，按独立哈希流分成 2–4 块大陆；每国 20 个相连初始分区、5 个核心 Hub。其余枢纽、城邦和路线从显式种子流生成。该版本用于验证尺度、编码与初始化接口；没有宣称其地理形态、资源分布或座位公平已达到正式比赛标准。地图版本独立于规则与引擎版本。

**开局资源参数 `rift_opening_endowments_v1`**：初始矿藏储量、局地自然来水、降水到农业产出的映射、默认开局税率单独记录在 [`data/opening_endowments_v1.json`](data/opening_endowments_v1.json)，参与源码/状态指纹。它们是实现候选参数，不冒称设计目录已经冻结的数值；不得根据某次自举的短缺临时增发资源。自然资源使用独立 `nature/deposit` 实物池，通过同一生产预扣信用机制转换为玩家库存；未开采水流显式流出，不能累成免费水库。

**原产分配精度**：来源相同且消环路径相同的实物合并存储，不按生产 Tick 永久新增批次 ID。混合池拆分采用 1 nmU 的比例向下取整与哈希尾差，转出份额从原池同额扣减，保持每个来源守恒；实际燃油消耗不足 nmU 的有理余量保留在实体信用中，不作为比例混合时新生成的小数。这样避免循环转仓让分母指数膨胀。此实现解释随引擎版本冻结，尚不代表全部数值/全规模发布门槛已通过。

沙箱预检使用 `python3 -m organizer.rift_era sandbox-check --cgroup <已委派的cgroup目录>`。启动器不会自动修改宿主的上级控制器，也不会在硬限制缺失时退化为普通子进程。仅有伪造的同名配置文件不能通过预检，目录必须位于真实 cgroup v2 文件系统。post-exec seccomp 已用独立可信测试进程验证 fork/socket/exec 拒绝、线程正常运行；这不替代 cgroup 与命名空间组合验收。

**数值实现**：物资/现金用 Python 任意精度整数，金额成本与评分用 `Fraction`。研究/AI 的幂对数及福祉平方根目前采用 50 位 Decimal；正式规范的冻结 binary64 语义与这些计算的逐阶段舍入还需统一验收，不能宣称已逐位复现正式规则。环境随机噪声使用冻结运行时的 `math`/Box–Muller。

## 验收状态与下一交付

按用户分工，完整对局与正式批次测试交由另一 AI 执行，交接清单见 [FULL_MATCH_TEST_TODO.md](FULL_MATCH_TEST_TODO.md)。实现方继续负责代码和单元/组件回归；完整对局验收保持待验证，不能据委派移除发布门禁。

现有自动测试覆盖组件反例、顺序不变性、拒绝原子性、只读审计、动态场景片段及供应链逐阶段重放。`data/round6_scenarios.json` 中 15 个动态场景仍标记为正式引擎待执行；本项目没有将组件测试偷换为这些场景的端到端通过记录。

下一项实质性交付是完整对局装配：全部命令、长局维护、科研验证、全 P7 宏观结算，以及物流/军政/金融/AI 的真实事件衔接。随后验收目标宿主沙箱、参考策略、资源峰值与 64 seed 评测。完整发布门槛由 [`engine/release.py`](engine/release.py) 显式列出；本次没有删除未通过的门槛。

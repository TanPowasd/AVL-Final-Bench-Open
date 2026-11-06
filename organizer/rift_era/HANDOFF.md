# 裂陆纪元引擎交接（2026-11-05）

用户最新指令是“完成手上的就停止，写下交接文档，然后全推送 GitHub”。本轮已停止扩展；所有并行子代理已收尾。本次是开发检查点交接，**不是正式引擎交付**。`release.py` 正式门禁保持关闭，`opening_economy` 仍明确限制最多 150 Tick。不要只放大时长、清空门禁或把测试 fixture 的外生资源当正式实现。

权威规则为冻结的 `rift_era_rules_r6`。未修改设计 JSON；新增接口、候选生成分布与用户确认的口径见 [IMPLEMENTATION_CONTRACT.md](IMPLEMENTATION_CONTRACT.md)。用户授权未定义细节按真实运作、守恒、防刷分补充并版本化。完整对局/64 seed/资源与宿主验收由用户另行安排的 AI 负责，任务单为 [FULL_MATCH_TEST_TODO.md](FULL_MATCH_TEST_TODO.md)。**缺失业务实现仍由实现方补齐，不能转写成测试 TODO 就算完成。**

## 从哪里接手

先阅读本文件和 [REMAINING_WORK.md](REMAINING_WORK.md)（2026-11-05 审查后的待实现清单，含本文件未列出的缺口），再读三个分支交接：

- [金融与军政合同交接](HANDOFF_FINANCE_20261105.md)：证券融资融券的已测边界、债本息、借券/还券、强平及未完限制。
- [城邦与债券交接](HANDOFF_CITY_BONDS_20261105.md)：城邦实体经济、关系/援助/转口、占领/吞并、真实债券历史 RU 成本。
- [世界与物流交接](HANDOFF_WORLD_LOGISTICS_20261105.md)：多模态货运、迁居、联运、公用网络、新地图候选及尚未接线部分。

[IMPLEMENTATION.md](IMPLEMENTATION.md) 保留历史检查点及当时的源码指纹。其中很多“尚未接入”是旧阶段记录；判断当前状态应以本交接、真实命令注册和代码/最新验证报告为准。历史全量报告不证明当前源码通过全量回归。

## 本轮已接入主线的内容

主线 `economy_state.OperatingWorld` 初始化，`commands.EngineCommands` 分发，`economy.opening_economy` 负责 P1–P8。新增系统均参与快照，实物虚拟库存参与统一审计。

- 城邦 P1 实际预算/挂单、P3 同一全局配电与真实电费/煤水/工资、P5 真实现货交割、P6 有限采矿与维护、P7 居民实耗/人口/亲和度、P8 合法整数观测。独立 NPC 不会进入八个玩家席位。占领不自动取走公司资产；吞并保留外部股权。
- 公路/铁路/河海空货运、独占实体载具、实际燃油与空返、逐 Hub 联运锁仓、仓满等待、通行合同/真实收费、实际供电的电铁。迁居按实际住房/技能/载具分批，在途人口不在两地重复劳动或生育。
- `UtilityNetwork.dispatch_edges` 已交给真实配电器；P1 锁真实水和泵预算，P3 实际电力驱动输水。升级保留原结构 book 与物权。公用网络仍有细节缺口，见分支交接。
- 海空域移动和实际基地条件、驻军占领、和平/割地/DMZ、通行/基地、防务实物援助、原时间情报共享和旧区射击、联合禁运。type1/2/4 增加实际双库互换/现金腿、真实贷款抵押、工业与研究 JV。
- `FacilityOwnership` 已注册；JV 施工资本和生产产出按实际份额拆经济 title，总实物数量不变。**JV 所有经营成本共同分账尚未贯通**，不能凭产出份额测试宣称完整产权链路完成。
- 融资融券接入 `StockMarket` 的 P1 共享池请求、成交角色、锁仓、取消、mark、可见状态和快照。根线收尾已补：`DebtFunding` provider；P7 `finance.accrue → fund_hard_debt → finance.settle`；公司分红声明后独立 `dividend_compensation`。本息只支付真实账户资金，借券只来自 NPC 实有可用股票；不足留明确负债。
- 主权发行与二级债券分别维护债务/资产历史 RU basis；使用真实现金事务 movement，不用屏幕汇率重估已有金融成本。

此前已接的账本/原产/末端回执、施工科研、宏观治理、AI 训练/部署/服务/许可、轨道观测/ASAT、策略 IPC/回放继续保留。它们的正式全局耦合、所有权与长局完整性仍需继续审查。

## 必须继续完成的实现

下面按重要性排列，**不是已完成能力清单**：

1. **完整五支柱采集和正式比赛装配**：`scoring.py` 有纯公式，但尚无统一实际 P7 国力采集器/完整 2,880 周期轨迹/正式 finalizer。`active_scale.py` 已在 full_match 接入（2026-11-06），三个无回执的 meter 仍为 0。还需处理全部 tech ActiveScale/LicensedScale、控制连通与穿透份额、损毁/敌对扣项唯一归属、真实 NFA、城邦人口/援助/伙伴、可持续性。金融可用接口包括 `stock_market.long_book/external_equity_book`、`stock_market.finance.debt_book/short_liability_book`、`sovereign_finance.debt_book`、`bond_market.asset_book`、`contracts.loans.debt_book/creditor_book`、`payables.debt_book`；保证金已经在现金 book，不可再列第二份资产。
2. **正式地图真正接线**：新 `world_atlas` 候选及性质检查已实现，正式入口仍用旧候选；`InitialState.resource_atlas` 中远端矿点尚未全部注册为 `OperatingWorld.resource_sites/resource_sources/deposits`。动态 Hydrology 仍需读取新河 DAG；起始 reserve 与新候选 atlas 需统一。只能通过实际 150 Tick 自举拒绝采样，不能拿模拟的 BootstrapResult 代替运行。
3. **完整命令与耦合**：远期合约、secondary_offering/受限 Capex 支付与原路退款、FX 做空/平仓、破产重整/强制拍卖、其余复杂合同归责、通用合同终止仍需核对和完成。九类合同已有部分业务不等于全部字段/违约语义已完成。
4. **剩余军政/情报/环境**：高超声速打击、封锁破交、投降/俘虏、完整野战补给、航空航程、S03/S08/C12/模型窃取、气候干预等仍有缺失或未完整集成。停止时没有开始新的 strike/blockade/prisoners 分支；不要假定存在相应模块。
5. **产权与跨系统边界**：JV 股东经营成本、设备维修/残值与抵押经济所有权、占领后的普通国家居民/工资/税币归属，必须由实际资金和合法实物交割驱动。城邦吞并后的国家管线有组件验证，不代表多国长期占领链路已全部完成。储能电量原产与最终流量回执仍需统一；已确认充电负 VA/民用终耗放电正 VA，不能循环刷分。
6. **物流完整性**：客运跨模态换乘、Cargo/客运同档统一容量仲裁、部分后续边拥塞、公用水渠自然来水 40% 上限与模式控制、合同电力专用负荷/交付尚未齐备。
7. **正式评测与参考策略**：观察者合法参考池、完整驱动/64 seed 影子缓存和批次统一失效处理、正式分数/资源档案、题库统一评分/导出仍需装配。不能让 `--official` 旁路发布门禁。
8. **实测验收**：完整对局、15 个 Round6 动态场景、64 seed、公平/性能/16MiB 快照、目标宿主沙箱组合测试均未宣称通过。宿主 CPU/PID 委派与完整输入缓存曾不足，见历史 probe；不得改共享上级 cgroup 或悄悄软降级。

## 验证与操作

常用命令（仓库根目录）：

```bash
python3 -m organizer.rift_era check
python3 -m unittest discover -s organizer/rift_era/tests -v
python3 -m compileall -q organizer/rift_era/engine organizer/rift_era/tests
```

本轮子模块定向结果：城邦 23 项；证券/融资 23 项；债券/现金/现货 38 项；地图/公用网络/迁居/运输最后组合 21 项；联运 50 Tick 真实阶段场景通过。子集有重叠，**不可相加当全量数量**。根线最后的融资/企业税分红/统一偿债组合 32 项通过；商品/融资/策略驱动冻结定向25项通过。最终冻结版全量组件回归536项全部通过，不等于完整对局验收。

并行开发期全量运行实际执行523项，2失败/1错误：两条商品测试仍假定市场只有玩家订单，已改为锁定目标venue和玩家订单核对；证券部分成交错误已在最新证券定向组合中通过。源码和测试在运行期间变化，这不是当前版本的冻结全量结论。随后最终冻结版实际执行536项，0失败/0错误/0跳过，耗时1212.685秒；源码和测试指纹前后一致。详细记录见 `validation/handoff_full_20261105.json`，旧523项结果仅作历史问题记录。

最终定向复核、源码/测试指纹与失败明细由 `validation/handoff_*.json` 及关联日志记录。并行编辑期间的旧全量运行不具备冻结指纹证据；driver 因源码变化拒绝是门禁实际生效，必须在停止编辑后的版本重跑。按真实结果保留失败与待办，不降低断言以求绿色。

本轮按用户要求在现有非 main 的 `rift-era-engine` 分支提交和推送；用户随后明确要求修复失败/错误并合并到 `main`，因此最终通过 GitHub PR 合并保留开发状态的检查点，而非直接在 main 上开发。没有重新导出 contestant、没有将 organizer 隐藏状态放入选手包、没有将开发状态标记为正式可交付。下一位 AI 应从本文件列出的真实实现缺口继续，勿将历史进度重新当作当前结论。

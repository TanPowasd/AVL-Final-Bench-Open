# 2026-11-05 handoff: city economy and bond historical basis

本轮已停止新增范围；没有提交或推送。下面记录的是共享工作区当前实现与证据，不能表述为正式完整对局通过。

城邦实现位于 `engine/city_state_economy.py`、`city_power.py`、`city_relations.py`，配置为 `data/city_states_v1.json`。城邦不进入八席位 `known`。每座城由实际地形候选矿种、有限自然储量、居民人口、住房、矿场和 S1/P070 本地电站组成；P1/P3/P6/P7 通过真实工资、燃料、供电、市场、维护、仓容和居民消费运行。缺钱或缺燃料/电力时不出矿，不发欠款收入。

城邦关系通过 `CityRelations` 接入：真实RU港务租约和物理边容量、实物援助托管到末端使用、受管制原产10%/周期转口（跨买方共享同一额度，保存实际原产）、protectorate复用合同防务义务。`commerce_commands.py` 的通用P5跳过 `city_export:*` 专用venue，城邦 `after_markets` 在同一本现货账上清算专用买卖并写入市场/宏观收入。可见观测使用 `affinity_mU`、`city_aid_mean20_mU` 整数；内部评分接口 `scoring_inputs`/`population_scoring_rows` 保留Fraction。

占领读取 `Authority` 的真实 controller/support；敌占经营能力折减到0.6，居民仍留原地，人口评分只给实际 controller。JV的直接股份和矿/电Activity通过 `apply_industry_activity` 写进现有 `IndustryActivity` rows，采掘本体不制造正工业VA，实际电、工资、维护成本按股权和汇率写入。吞并在P7检查240周期、Support≥650后只发生一次，真实转移城邦现金/库存、终止自治订单/战争关系，把同一个Population、住房、矿、电站、维护和水文实体注册到正常国家管线；外部直接股份保留，不能复制库存或货币。

债券历史basis实现位于 `sovereign_finance.py`、`bond_market.py`，并在 `spot.py` 增加 `_after_settlement(result,cash_plan,participating)` 事务后钩子。主权债务 `SovereignBond.proceeds_ru_mU` 只由真实 primary 现金历史RU movement建立，不能因屏幕FX变化重估；发行人债务basis与持有人二级资产basis分离。`FinancialExchange` 在DVP commit成功后读取真实 `CashTransferPlan.movements`（多卖方、退款和舍入尾差均来自同一实际cash plan），更新 `buyer_books`；snapshot包含该持久状态。

债券持仓basis在 `BondMarket.position_books[(holder,bond_id)]`：primary世界持仓不作为外部投资者资产；二级买方按真实付款movement取得历史RU成本；按实际 principal partial payment 比例减少basis；未付利息资本化为持有人face并按历史RU汇率成本比例分配；发行人回购取消face和对应持仓basis；成熟全额偿还清空basis；部分default保留face/债务和资本化利息。`asset_book(owner)`、`SovereignFinance.debt_book(owner)`向评分提供债务/持仓历史basis，均不更改现金余额或屏幕汇率。

验证证据：

- 城邦专项 23 项通过（含真实kernel 10 tick、P070燃料/工资/维护、实际居民消费、援助/转口、占领/估值、239/240吞并、吞并后正常kernel、JSON观测）。
- 城邦 + transport/transit 46 项通过（86.1秒）。
- 债券/现金/spot 专项合计 38 项通过：`test_bond_basis.py` 5 项有意义basis/守恒反例，加现有主权、债券市场、spot、cash basis 回归。覆盖 primary借款不产生NFA、屏幕FX不重估、二级真实DVP历史成本、多卖方舍入、到期偿还、部分default和资本化利息、回购注销。
- 本轮py_compile、`git diff --check`通过；总集成线仍须在最终合并源码上验证。当前未执行完整对局、64 seed批次或正式发布门禁。

未完边界：完整评分器尚未调用 `scoring_inputs`/`population_scoring_rows`/`asset_book`；城邦10%转口封堵补偿、完整公司估值历史接线和所有受控租约命令仍需总集成；债券fixture helper 可手造 `SovereignBond`，因此正式路径应优先依赖真实primary/secondary book，fixture不应被当作发行成本证明。

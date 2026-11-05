# 城邦经济组件（开发候选）

实现版本 `rift_city_state_economy_v2`，生成选择记录在 `data/city_states_v1.json`。不修改冻结的 `rift_era_rules_r6`。没有完整对局验收结论。

`CityStateEconomy(state)` 在 `OperatingWorld` 已建 `asset_lifecycle`、`authority`、现金/材料/市场账本之后构造。城邦保持独立NPC人口池，不进入八个玩家席位。构造时声明自然矿藏、初始资本与一次性煤/水/维护缓冲；模拟中不能再用构造函数补库存。

总集成接口与顺序：

1. P1 `begin_tick(ctx)` 在材料时钟、订单到期后执行。冻结本tick工资/电费、维护、周期首居民真实买单、矿产真实卖单、地方政府燃料/维护采购。
2. P3 将 `power_loads()` 加入同一全局dispatch，将 `power.supplies(ctx)` 加入supplies；payees合并 `power.payees()`。dispatch后 `record_power(ctx,result,payees)` 实付城市电费，再 `power.complete(ctx,result)` 消耗实际燃料并发工资。局部电源为真实BASE P070，具有S1资本、煤/水耗、Plant爬坡/停机与周期维护。不得绕过共享dispatch自己再运行第二次调度。
3. P5 全部现货市场完成后 `after_markets(ctx)`；在P7缓冲合并前冻结居民实际买单成交量。
4. P6 `produce(ctx)` 按真实电力/劳动/有限储量/本地仓容扣自然资源并出矿到next_available，结算独立Maintenance。
5. P7 在现有经济提交库存缓冲后、统一consumption_receipts前 `settle(ctx)`。消费居民已购本地货物、实际援助，返还未用工资/电费，周期末有限50RU补助、人口与逐国EMA。
6. P8 `publish()` 冻结 `visible(owner)`。查询不推进经济。全状态接snapshot/audit。
7. 注册 `CityStateCommands(state)`；`diplomacy/city_state_treaty` 原r6字段登记待四周期评估的实际托管提案。

独立跨系统接口：

- `activity_rows()` 提供矿/发电资产、实际mU产出/投入/工资及直接股权。需总集成接IndustryActivity，只读接口不认分。
- `relations.transit_right(nation,edge_id,cycle)` 返回具有实体运力上限的租约授权；另一外国端点仍需独立许可。`entry_fee`/`pay_entry` 使用实际RU储备向城邦金库付0.1RU/载重，不给免费运力。
- `relations.grant_aid(nation,city,good,qty,tick=...)` 要求实际物资已在城市Hub；独立援助库存只有实际居民不可逆消费才产生 `aid_mean20`，排除普通贸易回执。
- `relations.reexport_limit` 为只读；`reexport` 从城市实际受管制原产库存卖出至买方同Hub的next_available，真实RU支付，10%周期总额度在首次实销冻结。保留实际原产，拆单不能重置额度，整机仍整台。

验证：16项专项；与现货/世界/维护组件合计40项。覆盖确定性、有限储量、真实电费/燃料/工资、缺钱/缺燃料/缺电停机、维护耗料及欠账、本地采购、原产末端使用、逐国亲和度、JV20%交割与到期保留、援助先物理到达后末端认领、转口配额与原产保留、阶段重入拒绝。都是组件证据。

仍待总集成或补完：terrain→mineral为明确地质候选表（当前SpatialWorld没有矿藏atlas字段），仍需完整地图分布验收；通行租约需CargoOperations/Contracts调用；保护国防务违约尚未接；IndustryActivity需读取独立行；实物援助与转口的选手命令需总分发层公开相应版本接口；城邦转口封堵补偿未实现。新初始煤电及缓冲属于v2显式候选选择，尚无完整地图/长局平衡证据。

2026-11-05续作：通用P5排除city_export专用venue，CityStateEconomy.after_markets调用同一本SpotExchange内的reexport清算，固定10%真实卖方锁仓、实际买单100%RU托管、同价比例，产生正常market_events/实收与宏观现金腿。公开city_state_aid与city_reexport命令均纯预检后提交。visible公开affinity_mU和city_aid_mean20_mU整数，内部评分继续有理数。

占领读Authority的真实controller/support；敌占产业采用既定0.6行政产能上限，支持度按占领状态递推。population_scoring_rows只给当前控制国本地居民的占领合法性分，不把城邦人口静态算给原主权国。apply_industry_activity在city.settle调用，把真实city电力/矿业投入、工资、维护和直接股权写入现有Activity；采掘不认正工业VA。

吞并P7检查连续240周期与Support>=650，真实转移剩余RU/仓库物资、结束战争/提案/自治订单；同一个Population对象和住房、矿、电站注册到正常国家人口/产业/水文/维护管线，旧NPC停止。已持有的外部JV权益不被没收，城邦剩余份额一次性移交；没有新增现金或复制资产。

本次23项城邦测试通过（33.1秒），含真实kernel10tick与json.dumps观测、占领控制归属、JV估值份额、239周期拒绝/240周期吞并、吞并后再执行正常kernel；连同transport_operations/transit_contracts为46项通过（86.1秒）。仍为组件/局部集成证据，不代替完整对局。

# 世界、物流与公用网络交接（2026-11-05）

本文件记录本分支实际交付与测试范围。用户要求当前任务收尾后停止；尚未开始 `strike_operations.py`、`blockade_operations.py`、`prisoners.py`。本分支没有提交、推送，也没有修改冻结的 r6 JSON 或导出 contestant。

## 文件和已实现接口

- `engine/logistics.py`：真实载具/专属油箱/返程状态机，Corridor 模态、ACL、收费，卸货可指定同一物权/物品/真实目的 Hub 的锁仓；辅助随行物资逐边记录实际 Leg。
- `engine/transport_operations.py`：实体公路/铁路/河海空货运；真实燃料、载重、逐 tick 准入、起点比例尾批、仓储与 WAIT_UNLOAD、空返；海空线路共享 Hub 吞吐；实际通行费、城邦租约与签署通行合同容量。`route_shipment` 的 `propulsion=electric` 仅铁路，`power_demands()` 预约真实电力，`advance_electric(ctx)` 按 P3 实得电推进，无电停止。
- `engine/corridor_commands.py`：实体公路、铁路、河道渠化、港空设施施工；后增公用设施模式委托 `UtilityConstruction`。河运施工必须匹配地图中的 river 几何。港空两端材料实际施工后才提供容量。
- `engine/intermodal_operations.py`：多边父计划在各实际 Hub 接力。每边使用本地真实载具和油，原载具完成自身空返；货物卸入仓容计量的专属 `locked_relay`，下段按实际货量和载具拆批。满仓留车、缺车/油/断边留本地锁仓。`prepare/reserve/advance/cancel/snapshot/visible/audit` 已接主线。`auto/intermodal` 允许混合模式，显式 P036 等禁止跨模式。多边同模态目前也采用逐 Hub 接力。
- `engine/passenger_operations.py`：P1 pending 计划统一按原籍技能、目的住房和路径容量配给；大计划分批且未发人口仍在原籍。道路缺车可用真实人力基荷并预约劳动；同模态铁路/河海空采用相应实体载具。随行粮水计载重、逐 tick 实耗，储蓄保留币种；人口抵达才进入目的技能池，住房预约到达/死亡时释放，载具物理空返。命令取消只取消未发批次。被动迁移按支持度目的权重排计划。
- `engine/utility_network.py`：`UtilityNetwork(state)`；`dispatch_edges(ctx)` 返回 `power.PowerEdge`，并入真实 dispatch，不把 electricity 入库存。E02 单回 60/周期、双回 120，E09 240；E07/E09 按长度改变损耗，35%封顶；容量受真实资产可用率限制。线路收入复用 PowerReceipt toll 分款。`reserve(ctx)` 从真实本地已采水库存锁水，`power_demands()` 注册逆高差泵，`advance(ctx)` 按实际供电、源水和目标仓容输水并记渗漏蒸发。`receipts/snapshot/visible/audit` 提供接口。升级保留旧结构真实 book/物权，各组件共同限制可用率。
- `engine/world_atlas.py` 与 `data/world_atlas_v1.json`：新候选版本 `rift_world_atlas_v1`。旧 `world.generate_candidate` 保持旧测试接口；新入口 `world_atlas.generate_candidate(seed, attempt)` 生成候选，`world_atlas.generate_world(seed, bootstrap)` 只接受实际返回 `BootstrapResult(accepted=True,ticks_executed=150,invariant_failures=())` 的候选，最多 256 次重抽。不自行声明跑过自举。
- `engine/world.py`：SpatialWorld 增加正式 atlas 字段，public_map 白名单只含公开地理、河网、海岸；manifest 才包含隐藏资源、seat permutation 与版本。
- `engine/initialization.py`：`InitialState.resource_atlas` 保存隐藏 ResourceSite 记录，起始 mine grade 从对应记录读取。

正式 atlas 候选有 380 陆区/100 海区、250 Hub、8 国各 20 相连领土/5 连通核心、36..48 城邦、独立 seat permutation、蜿蜒海峡/湾岸/海岛、严格降海拔邻接河网 DAG、各大陆 Hub 的连通道路生成树与近邻边、海运水面合法路径、river 非运营预留路线。seed0/1/2 的路线数实测为 1328/1329/1342；seed0 陆地连通分量为 112/132/133 加三个单区海岛。

隐藏 `ResourceSite` 字段为 `id, hub, region, good, reserve_mU, grade_mU, renewable, starting_mine`。起始资源 ID 与现有 mine 相同；远端 ID 为 `deposit:<hub>:<good>`。其储量/品位按固定 profile 和种子生成，地形约束矿种；不会在失败后补库存。

## 当前明确未完成或有边界

1. 新 atlas 仍未替换 driver/正式 bootstrap 的旧候选入口。`OperatingWorld` 当前仍只注册 72 个模板起始 deposit；572 条 `resource_atlas` 已保存，但远端站点还没有全部变成可开采 `resource_sites/resource_sources/Deposit`。起始 reserve 也尚未全面改读 atlas。
2. `Hydrology` 当前仍自行生成 drainage atlas，没有读取正式 `world.river_downstream`；正式河网几何和动态水系还需统一。已构造候选 10 tick 运行不代表这一耦合已经完成。
3. L1 每格地域/地形已提供；海拔/风光/肥力以 region 公开表提供，没有独立每格微地貌变化。座位为先地理后独立哈希置换，有基础七源模板一致性；未进行 64 seed 路线公平性或完整平衡验收。
4. 当前水渠从已实际采水入仓的库存输送，保证不会凭空抽取河流，但没有直接纳入同 tick 流域自然入流 40% 跨流域调水预约。水渠按建造方向 a→b 自动运行，无专门开启/关闭命令。
5. Utility PowerEdge 已实现普通跨 Hub 电力路径；type1 当前 `periodic_contracts.py` 还是实物双库 barter，电力合同专属 load/托管交付没有在此分支实现，不能把普通跨 Hub 供电称作 type1 电力合同完成。
6. 客运混合模态换乘尚未实现，同一客运路径必须同模态。货运多边接力已实现。Cargo 和客运在 P2 仍有先后次序；已有车载后续边准入仍按整辆车等待/哈希仲裁，不能声称所有来源全局同档比例一体化已完成。
7. 没有完整对局、64 seed、正式发布或吞吐验收。`release.py` 的正式 gate 不应因本分支组件测试被清空。

## 测试证据

- 收尾最后运行：`python3 -m unittest organizer.rift_era.tests.test_world_atlas organizer.rift_era.tests.test_world organizer.rift_era.tests.test_utility_network organizer.rift_era.tests.test_passenger_operations organizer.rift_era.tests.test_multimodal_transport`：21 项通过，16.945 秒。
- 联运专项 4 项：纯 prepare、真实 road→rail 分段传货/空返、缺下游车与断边留实际 Hub、仓满保留车载，均通过。新增第 5 项真实 phase 50 tick road→road 父计划测试通过，47.879 秒，含最终 6000 到货、各车空返、父计划清理、state.audit。
- 此前四模块合并：运输 12 + 多模态 4 + 客运 7 + 联运 4 = 27 项通过，36.249 秒；该合并发生在后续 utility/world atlas 新增之前。
- utility 3 项含真实 PowerEdge dispatch/损耗与分款、缺电/半供电泵水与守恒、实际 210 tick 建造与双回差额升级，最近独立通过 1.947 秒。曾发现升级测试缺铜导致真实停工，补显式测试禀赋后完成；没有修改引擎偷偷补料。
- atlas seed0/1 各运行实际 `initialize + opening_economy(debug=True)` 10 tick 并 `state.audit()` 通过，记录 72 起始 deposit/572 atlas 记录；这是组件开局检查，未跑 150 tick 自举。
- world_atlas/world 旧新测试合计 7 项通过；性质覆盖 seed0/1/2 地图规模、国家核心连通、河 DAG、海路线合法性、公私边界、seed17 确定性/矿点 grade 绑定，以及 149 tick 假报告拒绝、150 tick 报告接受的闸门行为。闸门测试不是实际 150 tick 仿真证据。
- 最近 world_atlas/world/initialization 与 utility/corridor 的 py_compile、相关路径 git diff --check 均通过。

主入口由主代理统一修改：state 初始化、命令分发/cancel、P1 reserve、P2 cargo/relay/passengers、P3 electric cargo/water canals、P7 migration、观察/快照/audit。交接后先整体检查共享工作树和导入，再决定提交范围；不要把其他 AI 的未完改动无意合并成“全完成”。

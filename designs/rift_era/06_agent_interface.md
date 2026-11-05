# 策略程序接口与沙箱隔离规范

状态：**`rift_era_rules_r6`（已冻结；实现与沙箱验收待完成）**。历史推导见 [proposals/F-sandbox-isolation-budgets-and-scalable-ipc.md](proposals/F-sandbox-isolation-budgets-and-scalable-ipc.md) 与 [decisions/0008_sandbox_isolation_and_ipc_quotas.md](decisions/0008_sandbox_isolation_and_ipc_quotas.md)（若与本章冲突以本章为准）。

---

## 交付物、解包安全配额与零信任内核沙箱（`AR-031` 闭式规范）

### 1. 压缩包交付配额与预解包安全校验

1. **交付形式与硬配额（全面闭合原 Q26）**：
   - 参赛方提交**一个 `.zip` 压缩包**，包内可含多个文件与子目录，可附带源码、数据表、预计算模型权重或查找表等任意静态文件，包根目录必须包含入口文件 `main.py`，由评测机以 `python3 main.py` 启动。
   - **压缩包与解包硬上限**：
     - `.zip` 压缩包文件体积上限：**`<= 512 MiB`**；
     - 解包后全部文件总大小上限：**`<= 2,048 MiB`（`2 GiB`，不限制单个文件大小，例如单个 `1.8 GiB` 的预计算权重文件完全合法）**；
     - 包内文件与目录总条目数上限：**`<= 4,096` 个条目**；
     - 单条目最大压缩比上限：**`<= 100 : 1`**。
   - **流式预检与防 Zip 炸弹**：评测机在解包落盘前先扫描中央目录（`ZipInfo`），并在流式解压时维护真实累计写入字节计数器；一旦发现绝对路径、`..` 路径穿越、符号链接（`symlink`）、硬链接、FIFO/设备节点、非 UTF-8 路径、大小写不敏感重名冲突、单条目压缩比 `> 100:1` 或累计解压字节 `> 2 GiB`，立即中止解包并判定提交包格式非法（`ERR_ARCHIVE_SECURITY`）。
   - **独立装载时限**：解包与只读目录挂载在启动策略子进程**之前**完成，享有独立的 **`10.0 s` 预装载墙钟时限**，**完全不计入**对局 `init` 阶段的 `15.0 s` 思考时限。

### 2. Linux 内核级零信任沙箱隔离矩阵（`--official` 强制闭锁）

1. **命名空间与文件系统物理隔离**：
   - 正式评测运行于 Linux 宿主机，每个参赛策略在独立低权限用户与内核命名空间中运行：`unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID | CLONE_NEWIPC | CLONE_NEWUTS)`（通过 `bwrap` / OCI 沙箱容器封装）。
   - 解包后的选手目录与 Python 标准库以**只读绑定挂载（`MS_RDONLY | MS_NOSUID | MS_NODEV`）**映射至子进程根文件系统；工作目录 `/work` 与 `/tmp` 挂载为单局专属内存文件系统（`tmpfs`，容量上限 **`64 MiB`**，计入子进程总内存配额）；剥离全部宿主环境变量，网络命名空间完全空白（不含外部网卡，且禁止绑定 `lo` 回环套接字），物理隔绝裁判内存、随机 `seed`、对手源码及其他并发局目录。
2. **操作系统级硬资源配额（`cgroup v2` + `rlimit`）**：
   - **内存硬上限**：`memory.max = 1073741824`（**`1,024 MiB = 1 GiB`**），`memory.swap.max = 0`（严禁交换分区抖动；超限由内核直接触发 cgroup OOM 终止该子进程，不波及宿主机与裁判进程）；
   - **CPU 带宽配额**：`cpu.max = "100000 100000"`（单策略子进程严格绑定 **`1.0` 个物理核当量**，防止多线程抢占宿主机或其他座位 CPU）；
   - **进程与线程硬上限**：`pids.max = 5`（1 个主线程 + 至多 4 个工作线程，即子进程控制组内**总任务/线程数 `<= 5`**，与 `07_scoring_and_resources.md` 完全一致）。
3. **系统调用白名单过滤（经典 `seccomp-bpf` 可执行内核策略：对 `clone3` 返回 `ENOSYS` 回退 `clone` 标量寄存器检查，`AR-031`）**：
   - 仅允许调用 Python 标准库（包内自带的纯 Python 模块可正常 `import`），严禁调用外部语言模型、网络接口或第三方服务。
   - 针对标准库 `ctypes` 可能直接发起底层系统调用的风险，可信启动器完成唯一一次exec之后、执行用户main.py之前安装经典内核 **`seccomp-bpf` 过滤器**（仅检查 `struct seccomp_data` 中的系统调用号与标量寄存器参数 `args[0..5]`，不解引用用户态指针）：
     - **对 `clone3` 无条件返回 `ENOSYS`**：由于 `clone3` 的参数位于用户态结构体指针中，传统 `seccomp-bpf` 不读取指针内存，故直接对 `clone3` 返回 `ENOSYS`（触发 `glibc` `pthread_create` 标准回退至传统 `clone` 系统调用）；
     - **对 `clone` 直接检查标量标志寄存器 `args[0]`**：仅当 `(flags & CLONE_THREAD) != 0` 且未携带任何命名空间创建标志 `CLONE_NEW*` 时放行（且总线程数受 `cgroup v2 pids.max = 5` 硬上限约束），其余 `clone`、`fork`、`vfork` 一律拦截返回 `EPERM`；
     - **硬性拦截危险系统调用**：拒绝 `socket`、`socketpair`、`connect`、`bind`、`listen`、`accept`、`accept4`、`sendto`、`recvfrom`、`sendmsg`、`recvmsg`、二次 `execve`/`execveat`、`ptrace`、`process_vm_readv`、`process_vm_writev`、`mount`、`umount2`、`pivot_root`、`chmod`、`fchmodat`、`chown`、`perf_event_open`，并对 `kill`限制本进程；`tgkill`检查tgid为本进程，`tkill`默认拒绝（线程信号使用tgkill）；具体架构白名单须由引擎安全测试验证，不能笼统假定三种调用参数相同（否则返回 `EPERM`）。
4. **`--official` 正式评测强制闭锁（`Fail-Closed`）**：
   - 评测器在 `--official` 模式下启动前强制自检 `user/net/pid/mount namespaces`、`cgroup v2` 与 `seccomp-bpf` 可用性；若任一内核隔离原语缺失或权限不足，评测器**立即报错 `INFRA_FAULT` 并拒绝启动对局**，严禁退化为软限制运行。

---

## 对局回放（裁判侧，策略不可见）

- 正式评测每局由裁判侧录制器只读记录回放（世界与各国状态每若干周期一帧、关键事件），格式见 [prototype/replay/REPLAY_FORMAT.md](prototype/replay/REPLAY_FORMAT.md)，归档要求见 [07 对局回放归档](07_scoring_and_resources.md)。
- 回放含世界 `seed` 与全部国家的隐藏真值，对局进行中与同批评测结束前不得进入任何策略子进程的观测、工作目录或 `stderr`；录制不得改变对局结果。
- 策略自身的 `stderr` 诊断日志环形缓冲上限 **`<= 4 MiB`**（严禁向对手泄露），不写入公开回放；参赛方故障（异常、超时、托管）的周期与明细写入评测报告与 `index.json`。

---

## 时序窗口、帧同步序号与思考时间预算（`AR-030` 闭式规范）

对局总长为 **`28,800 Tick` = `5,760` 个决策窗口（每 `5 tick` 交互一次，`w ∈ {0..5759}`）= `2,880` 个工业周期（`c ∈ {0..2879}`）**：

1. **时限预算与单窗口硬上限**：
   - **初始化握手 `init`**：墙钟时限 **`15.0 s`**（适配 `512×512` 大地图解码与全局路网预计算），成功就绪需返回一行 `{"type": "ready"}`。
   - **单决策窗口 `obs -> orders`**：基础墙钟时限 **`0.5 s`**；另配整局共享、不回补的独立超时缓冲池 **`time_pool = 450.0 s`**（单窗口超出 `0.5 s` 的部分从池中精确扣除）。
   - **单窗口最大墙钟硬上限（`max_window_wall_s`）**：任何单个决策窗口的最大允许思考墙钟时间为 **`30.0 s`**（即实际窗口时限为 $\min(0.5 + \text{time\_pool},\, 30.0)\text{ s}$），防止策略在单窗口一次性耗尽全部 `450.0 s` 缓冲池导致对局挂起。
   - **单局累计总思考预算上限**：`3,600.0 s`（`60` 分钟）。
2. **窗口帧序号对齐、有界行缓冲与半行/脏行清理机制（彻底消除跨窗口错位与缓冲区无限增长，`AR-030`）**：
   - 裁判下发的每一帧观测均携带显式窗口序号：`"type": "obs", "window": w, "tick": 5*w, "cycle": w // 2`。
   - 策略输出的每一行命令 JSON **必须携带匹配的 `"window": w` 字段**：`{"type": "orders", "window": w, "orders": [...]}`。
   - **非阻塞定长环形缓冲与超长半行截断（`max_line_bytes = 256 KiB`）**：
     - 裁判进程通过 `epoll` 非阻塞读取策略 `stdout`，每个席位维护固定容量 **`512 KiB`** 的字节接收缓冲（单行合法上限 `max_line_bytes = 262,144` 字节 = `256 KiB`）；
     - **超长未换行字节截断**：若当前累积未遇到 `\n` 的半行字节数超过 `262,144` 字节，裁判**立即清空当前缓冲并置位 `discard_until_newline = True`**（后续读入的字节直接丢弃直至遇到下一个 `\n` 为止，绝不因为缺失换行符而无限扩张裁判内存），当窗记 `ERR_LINE_TOO_LONG` 并按空命令 `{}` 处理；
     - **跨超时半行清理**：若窗口 $w_0$ 超时截止时，接收缓冲内仍有未遇到 `\n` 的残留半行字节（`len(buf) > 0`），裁判立即清空该残留半行并置位 `discard_until_newline = True`，使得该超时帧后续吐出的半行尾部（直至下一个 `\n`）被自动丢弃，绝不污染下一窗口 $w > w_0$ 的首行 JSON 解析！
   - **超时迟到行有界清理预算**：在窗口 $w > w_0$ 读取该策略 `stdout` 时：
     - 若读到的完整 JSON 行满足 `"window" < w`（上一超时窗口迟到吐出的旧输出），引擎丢弃该迟到行，记告警 `ERR_STALE_WINDOW`，并继续读取下一行；但单窗口内最多清理 **`16` 条迟到行或累计 `1 MiB` 字节**（超出清理预算时直接排空管道当前可读字节，记 `ERR_PIPE_FLOOD` 并按 `{}` 处理当前窗口）；
     - 若读到 `"window" > w`，拒绝该行并记 `ERR_FUTURE_WINDOW`。
     由此在严格有界的 CPU 与内存开销下保证单次窗口超时**绝不会导致后续整局命令发生 off-by-one 窗口错位**！
3. **超时与崩溃托管**：
   - 参赛进程单窗口超时或输出非法 JSON 按空命令 `{}`（继续执行本地已生效持续计划）处理；累计超时未响应达 `50` 次、子进程崩溃/OOM 或耗尽 `time_pool` 后，剩余窗口自动以空命令托管跑完本局并按实际国力计分。

---

## 紧凑观测编码、动态实体上限与增量自愈同步（`AR-023`、`AR-031`）

下行逻辑消息分为init、obs和end；init及全量snapshot按11§4分块下发，每块是一条JSON记录。单块上限256KiB、逻辑快照整体上限16MiB；不能因管道缓冲区小于快照就丢字段或截断。完整接收后才启动本窗口策略计时，超内部上限报INFRA_FAULT。

1. **动态实体上限**：数值唯一来源为 [runtime_rules.json](data/runtime_rules.json) 的limits，下表为对应口径；达到上限后拒绝新建，返回ERR_ENTITY_CAP_EXCEEDED，不删除既有实体，不限制自然人口。

| 对象 | limits字段 | 上限 | 作用域 |
|---|---|---:|---|
| 设施（含在建，S1–S5及P110） | `facilities_per_nation` | 512 | 国 |
| 采掘场（G1/G2） | `mines_per_nation` | 128 | 国 |
| 走廊（所有国家/城邦合计） | `corridors_global` | 4096 | 全世界 |
| AI模型 | `models_per_nation` | 16 | 国 |
| 编队 | `formations_per_nation` | 64 | 国 |
| 在途Shipment（含返程/滞留/迁移） | `shipments_per_nation` | 512 | 国 |
| 活跃挂单 | `active_orders_per_nation` | 256 | 国 |
| 生效合同 | `contracts_per_nation` | 128 | 国 |
| 持续计划 | `plans_per_nation` | 64 | 国 |
| 可见及历史轨迹 | `visible_tracks_per_observer` | 8192 | 观测者 |

设施最多两产线，因此产线总数最多1024条/国；走廊只使用全局4096段上限，不再另设512段/国。调蓄水库按公共设施槽计入Facility，不再与矿场重复计Mine。模型满额须先prune，不能删除正在部署或在训基座。命令去重只保存永久单调水位last_command_seq；最近256条回执可作有界诊断缓存，淘汰回执不降低水位、不恢复旧ID的执行资格。

上述数量限制不构成“引擎恒≤64MiB”证明。序列化按256KiB分块、16MiB总额处理，实际引擎内存峰值及压力测试仍待实现阶段验收；不另承诺1.5MiB或2MiB的快照硬上限。

2. **三类下行消息内容**：
   - **`init`（常规体积约 `400..600 KiB`）**：包含规则版本号、采用调色板+游程编码（RLE）的 L1 `512×512` 静态地理栅格、L2 `420..540` 分区与走廊邻接图、本国身份、完整科技与工业目录、静态目录影子价值表 $w_g^{\text{cat}}$（来自 [`data/scoring.json`](data/scoring.json)）、开局资产与时限配额说明；严禁包含世界 `seed` 与未勘探区隐藏真值。
   - **`obs`（每 `5 tick` 一次，常规增量体积约 `20..40 KiB`）**：推送本窗口发生变化的本国账目、分枢纽分锁仓库存（`available / locked_order / locked_escrow / locked_build / next_available / in_transit`）、产线与维护欠账、科研进度、物流任务、人口与治理三级预警码、可见外部目标（附冻结快照 `(source, observed_tick, last_region, quality_q, est_lo, est_hi)`）、上一窗口末集合竞价出清价、外交提案及命令回执。
   - **实体版本、销毁/脱锁分离与自愈快照（`AR-023`）**：
     - 本国实体使用内部ID；外国实体只能发观测者独立接触别名和可见快照版本，见11§4，不能暴露创建国内部seq；
     - **`deleted_ids`**：仅列出本国已销毁/退役实体，以及在**本窗口正处于本国有效视野内（$q > 0$）被目视确认摧毁**的外国实体 ID；
     - **`lost_contact_ids`**：列出上一窗口可见、本窗口脱离传感器覆盖（$q = 0$）进入迷雾的外国实体 ID（迷雾中死亡的外国实体绝不进入 `deleted_ids`）；
     - **`expired_track_ids`**：列出老化后置信度跌破 $q(t) < 0.05$ 被归档清理的历史轨迹 ID；
     - 每24个窗口（120tick）下发一次可见权限内全量对齐快照；按上述256KiB/16MiB协议分块，不使用旧版1.5MiB/2MiB限额。

---

## 高层持续计划（Standing Plans）与十一类命令体系（`AR-030` 闭式规范）

### 1. 十一类命令总表与 `BASE` vs `C10` 持续计划边界

全系统命令统一划分为 **11 个标准类别（`kind`）**，单窗口最多提交 **`256` 条命令（行长 `<= 256 KiB`）**：

| 类别 (`kind`) | 包含的操作码 (`op`) | 核心语义 |
|---|---|---|
| `research` | `set_focus`, `validate_project`, `set_backup_hub` | 设定在研科技焦点、主研枢纽、保密档位（`open/standard/classified`）、启动工程验证及 `R08` 异地备份枢纽 |
| `industry` | `build_facility`, `upgrade_facility`, `demolish_facility`, `set_line`, `warehouse_expand`, `remediate_pollution` | 建造/升级/拆解 `S1–S5` 设施、`G1–G2` 采掘场、`P110` 住宅区与 `L12` 桥隧/人工岛；配置产线配方、模式（`general/specialized/automated/flexible/additive/high_temp/gene_edited`）、本地目标库存 `target_stock_mU` 与附属模块（`sensor/carbon_capture_unit/ai_core/quantum_module`）；扩建枢纽公共仓容；投放 `V02` 修复剂 |
| `logistics` | `market_bid`, `market_ask`, `forward_contract`, `route_shipment`, `corridor_policy`, `storage_mode`, `plant_maintenance` | 区域商品集合竞价限价买/卖单；`F03` 远期交割合约（交割期 $\ge 10$ 周期，双边锁定 `20%` 保证金）；创建点对点/循环运输任务；设置走廊通行权档位与过境费率；配置 `E04/抽蓄` 储能模式（`auto` 或手动曲线）；调整电站计划检修窗口（`-20..+20` 周期） |
| `military` | `deploy_formation`, `refit_formation`, `formation_task`, `hypersonic_strike`, `asat_strike` | 组建纯域编队（`Land/Sea-Surface/Sea-Sub/Air`）；在枢纽执行双槽位改装（槽位 A/B）；下达机动/驻防/进攻任务、交战姿态（`preserve/assault/bombard`）、补给源绑定与撤退阈值 `retreat_loss_mU`；发射 `N10` 高超声速弹或 `N12` 反卫星导弹 |
| `governance` | `fiscal_policy`, `set_regime`, `academy_train`, `mobilize`, `autonomy`, `power_priority`, `power_price_cap`, `climate_intervention`, `conditional_plan` | 设定综合税率与预算；切换互斥制度组（`R10/R12` 科学制度、`F07/F08` 经济体制）；下达 `R02` 学院培训计划（普工→技工→工程师）；紧急动员与地方自治留成 `Autonomy`；重排 `B1–B4` 负荷优先级与设定电价上限；执行 `V08` 区域气候干预；配置 `C10` 跨系统条件触发计划 |
| `diplomacy` | `propose_contract`, `accept_contract`, `reject_contract`, `terminate_contract`, `declare_war`, `city_state_treaty` | 九类有限谓词合同提案、签署、拒绝与可撤销条款终止；正式宣战（`declare_war`）；与中立城邦签署合资/港务/保护国协议 |
| `intelligence` | `radar_toggle`, `orbital_launch`, `intel_op`, `cyber_op`, `steal_model` | 雷达/电战开关与轨道发射；S02仅被动监听。intel_op执行S06假目标或C12密码分析；cyber_op执行S03/S08；steal_model仅按S03窃取，不附加S06或S07权限。 |
| `finance` | `stock_order`, `ipo`, `secondary_offering`, `dividend`, `bond_issue`, `bond_order`, `fx`, `central_bank`, `export_control` | 证券集合竞价买/卖/融券/平仓单；国家公司一级市场 `IPO` 与 `C30` 二级增发；设定分红率；发行主权债券与二级交易；外汇即期兑换；央行利率/印钞/干预/钉住/资本管制；高端芯片与加速器出口管制 |
| `ai` | `train`, `deploy`, `model`, `prune_model`, `service_offer`, `service_bid`, `license_offer`, `license_accept` | 启动大规模训练运行；分配 8 类算力预算；指定当前激活部署模型或清理旧模型；挂出/买入 AI 推理服务；挂出/接受模型权重或精标数据授权 |
| `trade` | `export_offer`, `buy_from` | 国家间双边挂牌出口报价（锁入 `locked_escrow`）与定向接受买单 |
| `cancel` | `cancel_by_id` | 按 `target_id` 终止本国有权取消的未成交挂单、在途训练、施工或持续计划 |

- **`BASE` 持续计划与 `C10` 条件触发计划的严格边界**：
  - **`BASE` 基础持续计划**：所有国家开局默认支持单产线连续排产与本地目标库存停机阈值（`set_line.target_stock_mU`）、循环班列按固定周期发车（`route_shipment.repeat_cycles`）、央行利率/干预常设指令以及编队撤退损耗阈值（`retreat_loss_mU`）。
  - **`C10` 智能调度系统（需枢纽装机 `ai_core×1`）**：解锁 `governance / conditional_plan`，允许设置至多 `32` 条跨系统条件触发规则 `{"trigger": {"metric": "market_price|grid_price|hub_stock", "comparator": "<=|>= ", "threshold_mU": ...}, "action": <Order>}`，当枢纽在线且未受S03/S08冻结时，每个决策窗口首tick的P1按上个窗口已可见快照判定一次；每计划每窗口最多一次，冷却至少5tick。

### 2. 命令参数域、Schema 与 `P1` 预锁校验表（`AR-009`、`AR-024`、`AR-030`）

下表完整定义各类命令操作码（新增施工/部署操作见下方补充表）的必填字段、参数类型/取值域及 `P1` 预锁校验规则（任何类型不符或超出定义域的字段在 `P1` 入口返回 `ERR_SCHEMA`）：

| 类别 / `op` | 必填与可选字段（含类型与取值域） | 语义与 `P1` 预锁校验规则 |
|---|---|---|
| `research` / `set_focus` | `line_id: str`, `tech_id: str\|null`, `sec_mode: "open"\|"standard"\|"classified"` | 绑定实验室产线在研项目（`null` 为待机）；校验 `requires_all/any`（未满足返 `ERR_TECH_LOCKED`） |
| `research` / `validate_project` | `line_id: str`, `tech_id: str` | 启动已完成理论阶段的科技工程验证；`6` 阶须在装有 `R09` 的 `S5` 枢纽进行（否则返 `ERR_SITE_INCOMPATIBLE`） |
| `research` / `set_backup_hub` | `primary_hub: str`, `backup_hub: str` | 需 `R08` 且备份枢纽装有 `computing_equipment×1 + communication_equipment×1` |
| `industry` / `build_facility` | `hub_id: str`, `facility_type: str`（industry.json厂类或public）, `profile_id: "S1".."S5"\|"G1"\|"G2"\|"P110"\|"L06_canal"\|"L06_tunnel"\|"L06_dam"\|"L12_bridge"\|"L12_island"`, `good?: str`, `use_modular_mU?: int (0..500)` | 校验科技前置、分区地形/槽位上限与 `512` 座设施硬上限；在 `P1` 预锁首期施工建材入 `locked_build` |
| `industry` / `upgrade_facility` | `facility_id: str`, `target_profile: "S2".."S5"\|"G2"` | 校验所有权、科技前置与升级建材库存，预锁建材后转入改造工期 |
| `industry` / `demolish_facility` | `facility_id: str` | 校验所有权且本地无本tick战斗；拆解耗时 `2` 周期，返还直接消耗钢/铜的 `50%` 并记入 `Loss_demolish` |
| `industry` / `set_line` | `line_id: str`, `recipe: str\|null`, `mode: "general"\|"specialized"\|"automated"\|"flexible"\|"additive"`, `high_temp?: bool`, `gene_edited?: bool`, `target_stock_mU?: int (>=0)`, `addons?: list["sensor"\|"carbon_capture_unit"\|"ai_core"\|"quantum_module"]` | 校验配方/模式科技前置与设施等级上限；首次装机从 `available` 一次性扣除 `installed` 及模式/附属模块装机件 |
| `industry` / `warehouse_expand` | `hub_id: str`, `modules: int (1..10)` | 每模块消耗 `steel×4 + cement×6`，完工后该枢纽公共仓容 `+500,000 mU`（`+500` 单位） |
| `industry` / `remediate_pollution` | `region_id: str`, `amount_mU: int (>=0)` | 需 `V02`；从所属枢纽 `available` 预锁并投放 `remediation_agent`（足额 `1000 mU` 触发额外 `+2.0%/周期` 衰减与污染惩罚减半） |
| `logistics` / `market_bid` | `region_market: str`, `good: str`, `qty_mU: int (>0)`, `limit_price_mU: int (>0)`, `expire_cycles: int (1..20)` | 区域商品集合竞价买单；在 `P1` 按 `qty_mU * limit_price_mU / 1000` 全额预锁买方现金入 `locked_order` |
| `logistics` / `market_ask` | `hub_id: str`, `good: str`, `qty_mU: int (>0)`, `limit_price_mU: int (>0)`, `expire_cycles: int (1..20)` | 区域商品集合竞价卖单；在 `P1` 从 `available` 全额预锁商品入 `locked_order` |
| `logistics` / `forward_contract` | `counterparty: str`, `good: str`, `qty_mU: int (>0)`, `strike_price_ru_mU: int (>0)`, `delivery_cycle: int (c+10\|c+20\|c+50)`, `delivery_hub: str` | 需双方解锁 `F03`；签署时双边各预锁合同名义价值 `20%` 的初始保证金入 `ForwardMargin[p]` |
| `logistics` / `route_shipment` | `origin_hub: str`, `dest_hub: str`, `corridor_path: list[str]`, `good: str`, `qty_mU: int (>0)`, `carrier_recipe: "manual"\|"P036"\|"P037"\|"P052"\|"P053"\|"P059"\|"P060"`, `repeat_cycles?: int (0..100)` | 校验路径连通性、走廊通行权、整数台运载工具及全程燃料/电力，在 `P1` 预锁货物与全程燃料后发车 |
| `logistics` / `corridor_policy` | `corridor_id: str`, `target_nation: str`, `access_tier: 0..3`, `toll_rate_bp: int (0..2000)` | 设定本国控制走廊对他国的通行权档位（`0` 禁行、`1` 商贸、`2` 军需、`3` 部队过境）与过境费率 |
| `logistics` / `storage_mode` | `facility_id: str`, `mode: "auto"\|"charge"\|"discharge"\|"idle"`, `power_mU?: int (>=0)` | 配置 `E04` 电化学储能或抽水蓄能运行模式（`auto` 引用截至上一周期末近 10 周期中位电价） |
| `logistics` / `plant_maintenance` | `facility_id: str`, `shift_cycles: int (-20..20)` | 调整电站计划检修窗口偏移量（推迟超 `20` 周期后强迫停运率 `×3`） |
| `military` / `deploy_formation` | `hub_id: str`, `domain: "Land"\|"Air"\|"Sea-Surface"\|"Sea-Sub"`, `units: dict[str,int]`, `crew_mU?: int (>0)` | domain枚举只取military.json的domain_channel键；unit_id只取units键，整数台且全属指定域。crew可选断言值须等于单元定额和；混域拒绝ERR_MIXED_DOMAIN_FORMATION，未知域/单元拒绝ERR_SCHEMA；实际扣整机和兵员。 |
| `military` / `refit_formation` | `formation_id: str`, `slot_a: str\|null`, `slot_b: str\|null` | 编队须驻停友方枢纽；从枢纽库存扣除改装模块并停机改装 `2` 周期 |
| `military` / `formation_task` | `formation_id: str`, `task: "move"\|"garrison"\|"attack"\|"escort"\|"patrol"`, `target_region: str`, `posture: "preserve"\|"assault"\|"bombard"`, `supply_hub: str`, `retreat_loss_mU: int (100..900)` | 下达编队机动/作战指令、交战姿态、补给源枢纽与自动撤退战损阈值 |
| `military` / `hypersonic_strike` | `origin_hub: str`, `target_id: str`, `missile_qty: int (1..20)` | 需 `N10`；从发射枢纽扣除 `hypersonic_missile × missile_qty`（`P098`），`1 tick` 跨区打击高价值目标 |
| `military` / `asat_strike` | `origin_hub: str`, `target_sat_id: str` | 需 `N12`；消耗 `hypersonic_missile×1 + guidance_module×1` 拦截敌方在轨节点（产生轨道碎片惩罚） |
| `governance` / `fiscal_policy` | `tax_rate_bp: int (500..5000)`, `welfare_budget_mU: int (>=0)`, `maint_budget_mU: int (>=0)` | 设定名义税率tau_tax（5%..50%）；F01与封顶后的有效税率按02计算、`F05` 社会保障实付预算与公共设施维护预算 |
| `governance` / `set_regime` | `group: "science_doctrine"\|"economic_doctrine"`, `choice_tech_id: str` | 切换互斥制度或路线组（需已解锁目标科技；`science_doctrine` 过渡 `40` 周期，`economic_doctrine` 过渡 `60` 周期） |
| `governance` / `academy_train` | `hub_id: str`, `track: "general_to_skilled"\|"skilled_to_engineer"`, `trainees_mU: int (0..5000)` | 需 `R02` 学院设施；培训人员实际退出岗位并锁入training人口池，不能同时生产/迁移/征兵；单座学院并发培训上限 `5.0` 人口单位（`5000 mU`），耗时 `6` 或 `12` 周期 |
| `governance` / `mobilize` | `enabled: bool` | 全国紧急劳动动员，2周期爬坡、至少4周期；军职征入由deploy_formation固定crew处理，不能重复征召 |
| `governance` / `autonomy` | `region_id: str`, `autonomy_mU: int (0..500)` | 设定分区地方财政留成比例 `Autonomy_r ∈ [0, 50%]`（提升地方支持度恢复，同比例减少上缴国库税基） |
| `governance` / `power_priority` | `priority_order: list["B1"\|"B2"\|"B3"\|"B4"]` | 必须为 `["B1","B2","B3","B4"]` 的 4 元全排列置换（否则返 `ERR_SCHEMA`）；设定本国第 `1..4` 电力调度秩次 |
| `governance` / `power_price_cap` | `cap_price_mU: int (5000..50000)` | 设定本国用户侧电价上限 $\bar{\lambda}_{\text{cap},p} \in [5.0, 50.0]$（本币/电） |
| `governance` / `climate_intervention` | `target_region: str`, `mode: "enhance"\|"suppress"` | 需 `V08`；持续 `60` 周期（冷却 `120` 周期），每周期扣 `remediation_agent×2 + 10.0 B3电` |
| `governance` / `conditional_plan` | `hub_id: str`, `metric: "market_price"\|"grid_price"\|"hub_stock"`, `ref_id: str`, `comparator: "<="\|">="`, `threshold_mU: int`, `action: dict`, `cooldown_ticks: int (>=5)` | 需 `C10` 且枢纽装有 `ai_core×1`；单窗口最多触发 `1` 次，冷却至少 `5 tick`（`1` 窗口） |
| `diplomacy` / `propose_contract` | `counterparty: str`, `clause_type: 1..9`, `params: object（仅03固定合同schema，不接受任意字段）`, `duration_cycles: int (10..720)` | 提交九类有限谓词结构化合同提案（含商品互换、通行权、主权贷款、研发合资、停火、防务等） |
| `diplomacy` / `accept_contract` | `proposal_id: str` | 接受对方发出的未过期合同提案，在 `P1` 锁定首期交割义务后正式生效 |
| `diplomacy` / `reject_contract` | `proposal_id: str` | 拒绝未生效提案（无信用惩罚） |
| `diplomacy` / `terminate_contract` | `contract_id: str` | 终止既有合同；若未满足合同约定的免责终止谓词，按单方撕约扣减 `Credit_p` |
| `diplomacy` / `declare_war` | `target_nation: str` | 按03§0统一P1关系状态机向国家或独立城邦宣战；同拍双方声明均为aggressor，盟友不自动参战；违约与C26/C27同该节 |
| `diplomacy` / `city_state_treaty` | `city_state_id: str`, `treaty_kind: "resource_jv"\|"port_lease"\|"protectorate"`, `aid_ru_mU: int (>=0)` | 与中立城邦签署资源合资、港务租借或保护国协议（须满足对应亲和度阈值 `0.35 / 0.50 / 0.65`） |
| `intelligence` / `radar_toggle` | `entity_id: str`, `active: bool`, `ew_mode?: "passive"\|"jamming"` | 切换雷达或 `G09` 电子战模块主动开机/静默状态（主动开机提升侦测但暴露自身辐射源） |
| `intelligence` / `orbital_launch` | `hub_id: str`, `payload: "satellite"\|"orbital_platform"`, `orbit_plane: 0..2`, `slot: 0..7`, `berth: 0..1`, `use_reusable: bool` | 从 `S3/S5` 发射场扣除载荷整机与燃料（或 `P096 reusable_launcher`）发射入轨；基础仅berth=0，berth=1须全球已扩容且本国N12，同位争抢按03哈希，未获位不扣载荷 |
| `intelligence` / `intel_op` | `target_region: str`, `op_kind: "S06_decoy"\|"C12_cryptanalysis"`, `reported_strength_mU?: int (0..10000000)`, `reported_type?: str`（military.json unit_id） | 仅S06假目标（cyber_suite .1）或C12密码分析，条件和成本见03/11；S02是被动监听。其他op_kind返回ERR_SCHEMA。 |
| `intelligence` / `cyber_op` | `target_nation: str`, `target_hub: str`, `op_kind: "S03_infiltrate"\|"S03_steal_tech"\|"S08_paralyze"` | 按03执行S03渗透/窃密或S08瘫痪；S07只有被动防御，不提供攻击命令。其他op_kind返回ERR_SCHEMA。 |
| `intelligence` / `steal_model` | `target_nation: str`, `target_hub: str` | 需 `S03`；对敌方数据中心发起模型权重窃取，成功获最高模型 $\gamma = 0.70$ 能力副本 |
| `finance` / `stock_order` | `company: str`（`<国>-<行业>`）、`side: "buy"\|"sell"\|"short"\|"cover"`、`qty: int (>0)`（股）、`limit_price_mU: int (>0)`、`margin: bool`、`expire_cycles: int (1..20)` | 进入下一次证券集合竞价：`margin=false` 的现货买单（`buy`）按 **`100%` 挂单本金（`qty × limit_price_mU` + 预估手续费）** 全额预锁买方现金（现货卖单 `sell` 100% 预锁自由持仓股份）；仅当 `margin=true`（融资买入）或 `side="short"`（融券卖空，均需本国已解锁 `F03`）时，按 **`50%` 初始保证金**预锁现金，资金或股份不足返回 `ERR_INSUFFICIENT_CASH` 或 `ERR_INSUFFICIENT_STOCK` |
| `finance` / `ipo` | `company: str`、`qty: int`（累计 $\le 490,000$ 股）、`min_price_mU: int (>0)` | 需 `F01`；仅限本国公司，从国库所持 `1,000,000` 原始股中向市场公开发售，募资入国库 |
| `finance` / `secondary_offering` | `company: str`、`qty: int`（每 100 周期 $\le 100,000$ 股，外部总持股累计 $\le 490,000$ 股）、`min_price_mU: int (>0)`、`target_facilities: list[str]` | `C30`；需 `F01` 且该公司已 IPO；总股本保持 `1,000,000` 股不变，从国有股释出，募资锁入 `CapexEscrow[p, k]` 专户，仅可用于所列本行业设施建设 |
| `finance` / `dividend` | `company: str`、`payout_mU: int`（`0..1000`，代表 `0..100%`） | 仅控股国（国有股 $\ge 51\%$）；仅对当期实现现金流正利润 $\Pi_{k,\text{cash}} > 0$ 分红 |
| `finance` / `bond_issue` | `term: 20\|50\|100`、`face_mU: int (>0)`、`min_price_mU: int (>0)` | 需 `F02`；受 `40 × max(mean_tax_20, 25)` 主权债上限约束，超限返回 `ERR_NO_PERMISSION` |
| `finance` / `bond_order` | `bond_id: str`、`side: "buy"\|"sell"`、`qty_mU: int (>0)`、`limit_price_mU: int (>0)` | 二级市场交易，预锁 100% 资金或债券面值份额 |
| `finance` / `fx` | `side: "buy_ru"\|"sell_ru"`、`amount_mU: int (>0)`、`currency: str` | 即时按当周期汇率与 `WorldClearPool` 结算（扣除手续费与资本管制费，受池余额上限约束） |
| `finance` / `central_bank` | `rate_bp: int`（`0..2000`，储备国下限 `-100`）、`issue_mU: int`（0≤数值≤2%全球本币存量M_p）、`intervene_ru_mU: int`、`peg: int (>0, RU每本币的mU)\|null`、`capital_control_bp: int`（`0` 或 `100..500`） | 需 `F02`；持续计划，直至被 `replaces_id` 更新覆盖 |
| `finance` / `export_control` | `goods: list[str]`（加速器、高端芯片 ID 列表）、`targets: list[str]` | 即时生效；若导致对守约方的既有合同断供，按主观违约扣减信用 |
| `ai` / `train` | `hub_id: str`, `alloc_compute_mU: int (>0)`、`cycles: int`（$\ge 5$）、`base_model: str\|null`、`max_price_mU: int (>0)` | 需 `AI02`；纳入算力总预算，中途 `cancel` 将 `50%` $C_{\text{run}}$ 存为检查点模型 |
| `ai` / `deploy` | `hub_id: str`, `research: int`、`production: int`（附 `lines: list[str]`，须全在hub_id）、`finance: int`、`cyber: int`、`market: int`、`align: int`、`grid: int`（各为非负算力 `mU`） | 与本Hub的train.alloc_compute_mU合计≤Cbar[hub_id]（超上限返回ERR_CAPACITY_EXCEEDED；全国总量不足不是唯一校验）；无对应里程碑的项须为 `0`，否则返回 `ERR_TECH_LOCKED` |
| `ai` / `model` | `active_model: str`（`model_id`） | 指定当前用于推理/运营部署的模型版本 |
| `ai` / `prune_model` | `model_id: str` | 删除指定非激活历史模型版本以释放 `16` 个模型槽位上限 |
| `ai` / `service_offer` | `qty_mU: int (>0)`、`price_ru_mU: int (>0)` | 需 `AI04`；每单位占用 `1000 mU` `market` 部署算力 |
| `ai` / `service_bid` | `qty_mU: int (>0)`、`max_price_ru_mU: int (>0)`、`use: "research"\|"production"\|"finance"\|"cyber"` | 买入他国推理服务（需买方自身已解锁目标 `use` 对应里程碑） |
| `ai` / `license_offer` | `kind: "weights"\|"data"`、`price_ru_mU: int (>=0)`、`target: str\|null` | 持续挂牌；数据授权仅限本国人工精标数据 $D_{\text{cur}}^{\text{human}}$ |
| `ai` / `license_accept` | `seller: str`、`kind: "weights"\|"data"`、`max_price_ru_mU: int (>0)` | 需 `AI01`；按来源国去重高水位计入 $D_{\text{lic\_from}}$ |
| `trade` / `export_offer` | `hub_id: str`, `good: str`、`qty_mU: int (>=0)`、`price_ru_mU: int (>0)` | 国家间双边卖单；立即从 `available` 锁入 `locked_escrow` |
| `trade` / `buy_from` | `offer_id: str`, `seller: str`、`good: str`、`qty_mU: int (>0)`、`max_price_ru_mU: int (>0)` | 接受双边卖单，按RU付款并在卖单Hub原地过户，买方须另安排Shipment |
| `cancel` / `cancel_by_id` | `target_id: str` | 按 `target_id` 终止本国有权取消的未成交挂单、在途训练、施工或持续计划（不存在或已完结返 `ERR_UNKNOWN_TARGET_ID`） |

```json
{"type":"orders","window":14,"orders":[{"id":"101","kind":"industry","op":"set_line","line_id":"own-line-8","recipe":"P015","mode":"general","target_stock_mU":20000}]}
```

### Round 4 新增及补齐操作

| kind/op | 字段 | 执行 |
|---|---|---|
| logistics/build_corridor | origin_hub,dest_hub: str; mode: road/rail/river/sea/air/grid/water; level:1或2; path_cells: list[int] | 两端权限、02科技/材料/工期校验，按路径格长扣料；不是仅设置既有边通行权 |
| industry/build_mine | hub_id,deposit_id,good: str; level:G1/G2 | 必须本地真实矿脉；runtime_rules定额及02建设维护 |
| industry/install_system | hub_id:str; system_id:str（见下表） | 本地公共槽装机，按固定成本扣库存，2周期完工，未满足前置拒绝 |
| industry/plant_toggle | facility_id:str; online:bool | 变更电站运行开关，05状态机处理 |
| industry/refill_accelerators | facility_id:str; qty:int(1..50) | 同代际空位装整台，保留各批日期，不重置原芯片寿命 |
| military/disband | formation_id:str | 仅友好Hub，幸存crew归原技能池、完好装备保留HP回库，不能刷新健康 |

system_id及实物：reservoir（A01，cement20+steel5，200蓄水）；academy（R02，lab_supplies2）；radio_backbone（C02，communication_equipment1，连接相邻有公路的同类Hub）；smart_grid（E07，control_module1）；fusion_sensor（C08，computing_equipment1）；environment（V01，sensor1）；water_recycle（V04，basic_parts2+control_module1，2电/周期）；climate_model（V06，computing_equipment1，2电/周期）；counterintel（S04，communication_equipment1+crypto_module1，1电/周期）；resilience（S07，control_module1+crypto_module1，1电/周期）；crypto（S01，crypto_module1）；post_quantum（S05，chip2升级已有crypto）；listening（S02，communication_equipment2）；automated_terminal（L07，automation_module2+communication_equipment1）；launch_site（N06，S3/S5公共槽，communication_equipment1+computing_equipment1）；big_science（R09，S5公共槽，computing_equipment2，持续40电）；closed_ecology（A12，已有water_recycle并具P030/P090，control_module2，4电）。未另列电耗的通信/监测装机均.1电/周期B0；水库无电但操作者1劳动/周期。装机占已有S1公共槽；S5特例按要求，不能把一条生产线和公共槽重用。

所有command公共seq、金额、数组、嵌套、分页、动态实体上限以11和runtime_rules为准。旧版示例任意字符串id不再可执行。此表是设计schema，完整验证器和超限协议测试留到正式引擎验收。


---

## 逐条原子性、幂等替换、乐观并发版本与标准回执码（`AR-030`）

1. **逐条原子、幂等去重与同批多次 `replaces_id` 顺序替换规则**：
   - 同批命令按本国提交顺序 `order_index = 0 .. K-1`（同优先级下严格保序）逐条校验并锁定预算；单条命令被拒绝不连坐同批其余合法命令。
   - **新建幂等保护**：每国命令id是从1开始的单调整数seq（可序列化为十进制字符串）。同帧按数组顺序处理，每个可解析seq须大于last_command_seq；否则返回ERR_DUPLICATE_ID。新seq进入处理时即推进水位，之后即使业务校验REJECTED也不能用该seq重试。最近回执缓存与去重资格无关；永久水位不淘汰。
   - **持续计划更新（`replaces_id`）与同批链式/冲突替换**：修改已生效的持续计划时，客户端须生成高于最高已处理seq的新 `id` 并传入 `"replaces_id": "<old_id>"`。仅当新命令通过全部权限与资源校验被 `ACCEPTED` 时，原持续计划 `<old_id>` 被原子替换终止；若新命令校验失败被 `REJECTED`，原持续计划 `<old_id>` 保持原状继续运行。若同一批次内有两条命令 $i < j$ 均指向同一个 `"replaces_id": "101"`，且命令 $i$（`id = "102"`）已被 `ACCEPTED`（此时 `"101"` 已注销），则随后的命令 $j$ 因目标 `"101"` 已不存在而返回 `ERR_UNKNOWN_TARGET_ID`（若需在同批内连续更新，命令 $j$ 须指定 `"replaces_id": "102"`）。
2. **乐观并发版本校验（`expected_version`）**：
   - 针对设施、产线、编队等可能因战损或故障在同一窗口发生状态变化的动态实体，命令可选携带 `"expected_version": v`（整数）。若引擎在 `P1` 校验该命令时实体的当前 `entity_version != v`，则拒绝该命令并返回 `ERR_STALE_VERSION`。
3. **回执状态与标准错误码**：
   - 命令生命周期状态：`ACCEPTED / WAITING_RESOURCE / EXECUTING / COMPLETED / CANCELLED / REJECTED`。
   - 标准错误码：`ERR_SCHEMA`、`ERR_LINE_TOO_LONG`、`ERR_PIPE_FLOOD`、`ERR_STALE_WINDOW`、`ERR_FUTURE_WINDOW`、`ERR_DUPLICATE_ID`、`ERR_UNKNOWN_TARGET_ID`、`ERR_STALE_VERSION`、`ERR_NO_PERMISSION`、`ERR_NO_VISIBLE_TARGET`、`ERR_NOT_AT_WAR`、`ERR_REPAIR_QUANTUM`、`ERR_TECH_LOCKED`、`ERR_CAPACITY_EXCEEDED`、`ERR_INSUFFICIENT_CASH`、`ERR_INSUFFICIENT_STOCK`、`ERR_ASSET_ALREADY_PLEDGED`、`ERR_MIXED_DOMAIN_FORMATION`、`ERR_SITE_INCOMPATIBLE`、`ERR_CORRIDOR_BLOCKED`、`ERR_ENTITY_CAP_EXCEEDED`、`ERR_WINDOW_ORDER_CAP`。
4. **基础设施故障隔离**：
   - 裁判引擎异常、内核沙箱自检失败、参考策略启动失败或宿主机资源枯竭一律归入 `INFRA_FAULT` 并使整批正式评测作废重跑，严禁伪装成玩家命令超时或计零分。

## 原型与安全边界

本轮没有实现沙箱或协议解释器。16MiB分页快照、固定接收环、进程/内存硬限额的设计必须在正式引擎做压力与对抗测试；不能用原型进程内执行证明这些约束已经落地。具体可信启动顺序见11§4。


额外金融与补员命令：`finance/fx_short {currency, amount_mU>0}`、`finance/fx_cover {currency, amount_mU>0}`，需F03，按02真实贷款/抵押/还款；`military/reinforce {formation_id, unit_index, crew_mU>0}`仅友好Hub、上限为该单位标准crew缺额，按同技能实际从人口池划入，训练2周期内不增战力。`set_line`的厂类只能与建成facility_type一致，首次选择public槽转生产需完整改造成本，不能免费反复改用途。


## Round 6 动作与参数补充（OR-003～005/009/017）

| kind/op | 必填/可选字段 | 语义与校验 |
|---|---|---|
| military/blockade | formation_id,edge_id:str; target_nations:list[str]（1..8） | 03§7合法位置/域/补给、必须WAR目标，每编队一边；生成持续计划，实际每tick按07付损耗 |
| military/interdict | formation_id,shipment_track_id:str | 只接受当前可见轨迹，运输者及货主均须WAR；共享射击预算，不打中立第三方，03§7结损 |
| industry/repair_asset | asset_id:str; target_health_mU:int(1..1000); work_hub_id:str | 自有/获授权本地设施、线、边或仓；目标>现状，按05预锁材料/工/电及工期；不得超book或绕过整机精度 |
| governance/relocate_population | origin_hub,dest_hub:str; skills_mU:object{general_worker,skilled_worker,engineer:nonnegative_int}; path:list[str]; allow_unhoused?:bool=false | 复用Passenger Shipment、各技能不超过可用人口；10本币/人服务费、5周期口粮预装、住房或明确无房选择，见11 |
| diplomacy/grant_climate_consent | target_nation:str; region_ids:list[str]; duration_cycles:int(1..720) | 仅授权本国控制区域，期限内撤销从下一周期起生效；V08区域/下风向均需覆盖才算合资格非敌对干预，不许替第三国签字 |

新增install_system枚举：cryptanalysis_defense（需C12，quantum_module1，B4持续2电/周期）、deception_defense（需S06，cyber_suite1+computing_equipment1，B4持续2电）、system_defense（需S08，cyber_suite2+ew_module1，B4持续4电）。均占本国公共槽并按原2周期工期安装，在线本国测试能力按07计量；不因发出敌对动作新得落地分。

corridor_policy的toll_rate_bp计费基数为每次实际新入边载货批次`Σ_g(q_mU[g]/1000)*w_cat[g]*10 RU`，费用=基数*toll_rate_bp/10000；不是任意市场成交价。空返车费0，人员按food目录值×人数作为基数。发车前预锁费用，真实入边一次性转边经营方，不按每tick或经过边两端重复收；失败未入边解锁。电网沿用05固定.01RU/送端电的规则，不同时套此货运费。对外收紧ACL/无约定拒入的敌对执行事件按07扣项，不能靠免费关边伤敌。

conditional_plan统一每窗口首P1判定一次，冷却tick从上次实际执行起计，冻结时不执行也不补追历史触发。新增type6参数mode及外交角色均以03§0为准；mode=cession须双方明确列出出让国与其主权region_ids。

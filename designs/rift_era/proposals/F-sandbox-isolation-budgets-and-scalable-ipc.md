# 策略子进程硬隔离、宿主机余量调度、故障归责矩阵与大世界可扩展协议提案

- 作者/任务：主设计与整合（任务 F「接口、隔离与计算预算」）
- 状态：已采纳并合入设计规范（见 [../decisions/0008_sandbox_isolation_and_ipc_quotas.md](../decisions/0008_sandbox_isolation_and_ipc_quotas.md)）
- 对应未决问题 ID：`Q07`、`Q22`
- 影响文件与稳定 ID：
  - 更新：[../06_agent_interface.md](../06_agent_interface.md)、[../07_scoring_and_resources.md](../07_scoring_and_resources.md)、[../08_open_questions.md](../08_open_questions.md)
  - 新增决策记录：[../decisions/0008_sandbox_isolation_and_ipc_quotas.md](../decisions/0008_sandbox_isolation_and_ipc_quotas.md)
  - 科技与工业目录稳定 ID：不涉及目录 ID 改动。

---

## 要解决的问题

1. **大世界评测中的隐藏状态防泄露与异常进程资源耗尽风险（`Q07`）**：仅靠调度器估算并发数无法阻止单个恶意或存在内存泄漏的参赛进程吃光宿主机物理内存、派生子进程炸弹（Fork Bomb）或偷读其他局日志与种子。同时必须严格界定玩家代码缺陷与裁判基础设施故障的计分边界。
2. **`512×512` 超大陆与 `1,920` 窗口下的跨进程管理复杂度（`Q22`）**：若没有紧凑编码、增量同步、自愈快照、高层持续计划与清晰的错误回执码，策略在管理数十座城市与上百条走廊时会被协议体积与状态失步拖垮。

---

## 建议的规则或公式

### 1. 物理进程隔离、OS 级硬资源配额与并发准入调度（`Q07`）

1. **零信任进程与文件系统边界**：
   - 裁判引擎与内置 7 个参考策略运行于受保护的主控进程地址空间；待测参赛策略通过受限低权限子进程启动，仅以匿名管道（`stdin / stdout`）与裁判通信。
   - **隐藏状态物理不可见**：子进程工作目录设为单局专属空白临时目录（只读挂载参赛压缩包解包目录与 Python 标准库；v1.2 起提交形式改为压缩包，见 decisions/0010），剥离全部宿主环境变量，禁用全部网络套接字（含本地回环端口探测），物理隔绝裁判内存、随机种子 `seed`、对手策略源码及其他并发对局目录。
2. **跨平台操作系统级硬限制（Hard Sandbox Quotas）**：
   - Linux 采用 `cgroup v2` + `rlimit` + 命名空间隔离；Windows 采用 `Job Object`（绑定 `JOB_OBJECT_LIMIT_JOB_MEMORY`、`JOB_OBJECT_LIMIT_ACTIVE_PROCESS`、`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）与受限令牌：
     - **子进程内存硬上限**：`1,024 MiB`（`1 GiB` 提交内存峰值，超限由内核直接触发 OOM 终止，绝不波及宿主机与其他局）；
     - **进程与线程上限**：活跃进程数 `= 1`（严禁 `fork / subprocess` 派生子进程），内部线程数上限 `<= 4`（防止选手用多线程抢占预留给操作系统的 CPU 核心）；
     - **I/O 配额**：单窗口 `stdout` 行上限 `<= 256 KiB`；整局 `stderr` 诊断日志环形缓冲上限 `<= 4 MiB`（仅赛后供该选手本人排障或归档，严禁跨选手暴露）。
3. **宿主机 CPU 与内存双门限自适应并发调度**：
   - 检测进程实际可用逻辑核心数 $C$（取硬件线程数、进程亲和性掩码与容器 CPU 配额三者最小值），强制预留系统余量：
     $$C_{\text{reserve}} = \min\!\big(C - 1,\; \max(1, \lceil C / 4 \rceil)\big), \qquad C_{\text{worker}} = \max(1,\; C - C_{\text{reserve}})$$
     单核机器（$C = 1$）自动降级为低优先级单进程串行执行；所有评测工作进程调低调度优先级（`BELOW_NORMAL_PRIORITY_CLASS` / `nice +10`）。
   - 预留操作系统安全内存 $M_{\text{reserve}} = \max(4\text{ GiB},\; 0.20\, M_{\text{total}})$，设原型实测标定的单局总内存包络（引擎 + 参赛子进程）为 $M_{\text{match\_est}}$，则允许同时在途的最大对局数为：
     $$K_{\max}(t) = \max\!\left(1,\; \min\!\left(C_{\text{worker}},\; \left\lfloor \frac{M_{\text{avail}}(t) - M_{\text{reserve}}}{M_{\text{match\_est}}} \right\rfloor \right)\right)$$
     当系统瞬时可用内存低于阈值时，调度器暂停派生新局直至在途局释放内存，绝不一次性排入全部 64 局耗尽交换分区。

---

### 2. 故障责任严格二分矩阵（`Q07`）

| 异常类别 | 具体触发情形 | 引擎处置动作 | 对正式评测成绩的影响 |
|---|---|---|---|
| **参赛方策略故障** (`CONTESTANT_FAULT`) | 子进程非零退出/未捕获异常、触发 `1 GiB` 内存硬上限被内核终止、单步及缓冲池超时耗尽、累计 `50` 次空响应、输出非法 JSON | 裁判捕获管道断开或超时，将该国剩余决策窗口无缝切换为 **空命令 `{}` 本地持续计划托管** 直至第 `9,600` tick 正常终局 | **本局有效**，按该国实际取得的最终国力 $V_{\text{player}}$ 正常计入 64-seed 均值，报告如实记录故障类型与发生 tick |
| **单条业务命令拒绝** (`ORDER_REJECTED`) | 资金/库存不足、前置科技未解锁、目标走廊不可达、`entity_version` 过期、单窗口超 `256` 条配额 | 仅拒绝该条命令并在下一窗口 `obs.receipts` 返回结构化错误码，同批其余合法命令照常执行 | 属于正常局内交互，不视为进程故障 |
| **裁判或宿主基础设施故障** (`INFRA_FAULT`) | 裁判主进程未捕获异常、内置参考策略崩溃、宿主机磁盘满或宿主级 OOM、随机种子样本缺失、调度器被外部信号中断 | 立即中止当前批次并写入故障审计快照，禁止自动删除失败局用剩余 63 局凑数平均 | **整批评测标记为 `INVALID_EVALUATION`（无效）**，不给选手记零分或残缺分，修复宿主环境后重新完整抽样评测 |

---

### 3. 大世界紧凑编码、持续计划自治与增量自愈协议（`Q22`）

1. **`512×512` 大地图紧凑传输与增量体积预算**：
   - **`init` 静态包（目标 `<= 600 KiB`）**：`512×512` L1 地块属性（地形、高程档、肥力档、风光档、所属 `region_id`）采用确定性调色板 + 游程编码（RLE）数组下发，解析仅需数毫秒 Python 标准库循环；L2 分区与走廊图以显式邻接表下发。
   - **`obs` 窗口增量包（每 `5 tick` 一次，常规目标 `<= 40 KiB`）**：仅下发本窗口内发生变化的本国设施、库存、任务及视野内外部动态目标，每个实体附带单调递增 `entity_version: int` 与显式销毁列表 `deleted_ids: list[str]`。
   - **周期自愈快照（每 `24` 个窗口 = `120 tick`，目标 `<= 150 KiB`）**：自动下发本国可见权限内的全量对齐快照，彻底防止策略因单次超时跳过窗口解析而永久丢失状态同步。
2. **高层持续计划（Standing Plans）降低微操负担**：
   大世界即时策略考验宏观协同而非每窗口手写数千条底层搬箱子指令，引擎原生支持四类持续自治计划（下发一次后跨窗口持续运行，直至完成、被覆盖或 `cancel`）：
   - **本地目标库存与自动挂单计划**：为枢纽设定 `(good, min_reserve_mU, target_stock_mU, auto_bid_limit_price_mU)`，低于目标时自动在本枢纽集合竞价挂买单或触发绑定的国内补货路线；
   - **循环车船班列计划**：绑定运载工具编组在 `(hub_u <-> hub_v)` 按优先级列表循环拉运原矿与产成品；
   - **产线连续排产与换线队列**：材料齐备即自动按设定配方连续生产，满仓自动待机；
   - **编队任务式指挥条令（ROE）**：一次设定巡逻/护航/驻防区域、关联补给枢纽、交战姿态（`preserve / assault / bombard`）与自动撤退战损阈值 `retreat_loss_mU`。
3. **标准化回执状态与错误码枚举**：
   - 状态枚举：`ACCEPTED`、`WAITING_RESOURCE`、`EXECUTING`、`COMPLETED`、`CANCELLED`、`REJECTED`。
   - 拒绝错误码：`ERR_SCHEMA`、`ERR_DUPLICATE_ID`（幂等命中返回原状态）、`ERR_STALE_VERSION`、`ERR_NO_PERMISSION`、`ERR_TECH_LOCKED`、`ERR_INSUFFICIENT_CASH`、`ERR_INSUFFICIENT_STOCK`、`ERR_SITE_INCOMPATIBLE`、`ERR_CORRIDOR_BLOCKED`、`ERR_WINDOW_ORDER_CAP`（单窗口上限 `256` 条命令）。

---

## 已做检查和证据

- 核对 CPU 预留公式 $\min(C-1, \max(1, \lceil C/4 \rceil))$ 与内存硬隔离设计，完全满足用户既定的“允许并行评分但给系统预留 CPU 和内存余量”及 `07_scoring_and_resources.md` 的安全边界要求。
- 核对增量同步 + 24 窗口快照 + 幂等命令回执，闭合大世界 `1,920` 窗口管理协议。

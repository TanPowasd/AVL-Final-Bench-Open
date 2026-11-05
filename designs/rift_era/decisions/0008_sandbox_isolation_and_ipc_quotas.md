# 0008：零信任子进程硬沙箱、双门限余量并发调度、故障归责矩阵与可扩展持续计划协议

状态：已采纳并合入设计规范（单局实测内存包络 $M_{\text{match\_est}}$ 与消息字节实测分布待引擎实现后标定）。

## 背景与旧语义

- `06_agent_interface.md` 与 `07_scoring_and_resources.md` 提出了子进程运行、行分隔 JSON 通信及预留系统 CPU/内存的原则，但尚未明确跨平台 OS 级单进程内存/进程硬上限、参赛故障与裁判故障的二分矩阵，以及 `512×512` 大世界下的紧凑编码、持续计划自治与标准错误码（对应 `Q07`、`Q22`）。

## 新语义

1. **物理隐藏状态隔离与 OS 级硬资源沙箱**：
   - 参赛子进程运行于空白临时工作目录、剥离环境变量并禁绝一切网络套接字，物理隔绝裁判内存、随机 seed 与对手信息。
   - 通过 Linux `cgroup v2 / rlimit` 与 Windows `Job Object` 施加硬上限：子进程提交内存峰值 `<= 1,024 MiB`、活跃进程数 `= 1`、线程数 `<= 4`、单行输出 `<= 256 KiB`、`stderr` 环形日志 `<= 4 MiB`。
   - 并发调度同时受逻辑核心预留 $C_{\text{reserve}} = \min(C-1, \max(1, \lceil C/4 \rceil))$ 与物理内存预留 $M_{\text{reserve}} = \max(4\text{ GiB}, 0.20 M_{\text{total}})$ 双门限动态约束。
2. **故障责任严格二分**：
   - 参赛进程崩溃/OOM/超时转为空命令 `{}` 持续计划托管跑完本局并正常计分；裁判引擎异常、参考策略启动失败或宿主资源枯竭立即标记整批 `INVALID_EVALUATION`，绝不误判选手零分，也绝不删减异常种子仅对残余局取均值。
3. **紧凑 RLE 初始地图、高层持续计划与标准错误码**：
   - `init` 采用调色板+RLE 紧凑编码传输 `512×512` L1 地图；每 `24` 窗口附带权限内对齐快照；原生支持库存自动补货、循环运输班列、产线连续排产与编队 ROE 自治条令（单窗口命令上限 `256` 条），并冻结 10 类结构化错误回执码。

## 受影响文件与迁移方式

- 更新：[06_agent_interface.md](../06_agent_interface.md)、[07_scoring_and_resources.md](../07_scoring_and_resources.md)、[08_open_questions.md](../08_open_questions.md)、[CHANGELOG.md](../CHANGELOG.md)。
- 详细规范见 [../proposals/F-sandbox-isolation-budgets-and-scalable-ipc.md](../proposals/F-sandbox-isolation-budgets-and-scalable-ipc.md)。

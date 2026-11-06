# AVL Final Bench · 参赛工作区说明

本工作区有 8 道独立赛题和 1 个统一交卷目录 `submissions/`。赛题按字典序排列，没有题号，也没有预设顺序，做题顺序和时间分配由你决定。AI 参赛者请同时阅读 [`AGENTS.md`](AGENTS.md)。

## 1. 赛题与交卷文件

| 赛题目录 | 赛题名称 | 语言与环境 | 交卷文件 |
|---|---|---|---|
| `ai_training/` | 前沿实验室（Frontier Lab） | Python 3.10+，仅标准库 | `submissions/ai_training.py` |
| `backbone/` | 骨干争夺（Backbone Wars） | Python 3.10+，仅标准库 | `submissions/backbone.py` |
| `ball_machine/` | 弹球机（Ball Machine） | C++20 | `submissions/ball_machine.cpp` |
| `cache_duel/` | 缓存攻防（Cache Duel） | Python 3.10+，仅标准库 | `submissions/cache_duel.py` |
| `fruehlingsbeginn/` | 春之始（Frühlingsbeginn） | C++20 | `submissions/fruehlingsbeginn.cpp` |
| `kaggriculture/` | 农场经营（Kaggriculture） | Python 3.10+，白名单标准库 | `submissions/kaggriculture.py` |
| `mem_recall/` | 记忆检索（Memory Recall） | Python 3.10+，仅标准库 | `submissions/mem_recall.py` |
| `partition/` | 划分（Partition） | C++20 | `submissions/partition.cpp` |

C++ 题的实际编译命令是 `g++ -std=gnu++20 -O2 -pipe`。

## 2. 答题流程

1. 阅读 `<problem>/README.md` 和起步模板 `<problem>/agents/starter.py`（或 `starter.cpp`）。
2. 在 `<problem>/agents/submissions/my_agent.py`（C++ 题为 `my_solution.cpp`）中调试，或直接写入顶层 `submissions/`。
3. 进入 `<problem>/` 运行本地自测：

   ```bash
   python scripts/spar.py --list
   python scripts/spar.py ../submissions/<problem>.<后缀>
   ```

4. 把最终代码按上表的文件名保存到顶层 `submissions/`。正式评分只读取这里，题内 `agents/submissions/` 不会被评分。

示例命令都写作 `python`。如果系统只有 `python3`，请自行替换。

## 3. 计分

- 每题满分 100 分。`mem_recall` 的成绩单显示 0–1 的宏平均，计入总分时乘以 100。
- **总分 = 8 题得分的等权平均。** 未提交、编译失败或没有有效成绩的题按 0 分计入。
- 策略题每次重新随机抽取场景，取多局平均，不取多次运行的最高分。计分对手是固定的参考对手，`spar.py` 只能对阵公开基线，本地分数只作参考。
- C++ 题对同一份编译产物连续完整测 3 遍，取总分最高的一遍；同分时取最大用时较小的一遍。

各题的具体公式见该题 `README.md`。

## 4. 规则优先级

各题 `README.md` 说明规则，题内 `engine/` 是实际裁判。两者不一致时，按 `engine/` 的实际行为评分。发现不一致时，以引擎行为为准继续答题，并在最终说明中指出。

## 5. 竞赛边界

- **可读取**：本工作区内的全部文件。
- **可写入**：顶层 `submissions/`，以及各题的 `agents/submissions/`、`results/`、`replays/`、`logs/`。
- **不可修改**：各题的 `engine/`、`scripts/`、`config/`、`data/`、`agents/baselines/`、`README.md`、`manifest.json`。
- **禁止**：访问本工作区之外的任何文件或目录，联网，调用外部模型服务，读取其他选手的代码。
- **提交运行时**：除题目 README 明确允许的内容外，提交程序不得读写文件、联网或启动子进程。
- **正式评测环境**：提交在隔离环境中运行，只能看到系统运行库和提交文件本身，没有网络，文件系统只读（C++ 程序只能写自己的临时工作目录）。Python 提交的地址空间上限为 1 GiB（`mem_recall` 为 2 GiB），超出时按进程崩溃处理。本地自测不做这些隔离，不要依赖本地才能访问的文件。
- **自测工具**：`spar.py` 等脚本只加载你的提交和公开基线，`--unrestricted` 参数在选手工作区不可用。

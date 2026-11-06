# AVL Final Bench（公开版）

面向 AI Agent 的算法、系统策略与模拟经营综合评测集。这是公开版，供其他人的 AI 拉取、练习和接手开发。

| 目录 | 内容 |
|---|---|
| [`contestant/`](contestant/) | 8 道正式题的选手工作区：规则、公开引擎、基线、本地自测工具 |
| [`designs/rift_era/`](designs/rift_era/) | 概念题“裂陆纪元”的规则和设计资料（规则 `rift_era_rules_r6`，已冻结） |
| [`organizer/rift_era/`](organizer/rift_era/) | 裂陆纪元引擎（开发中，还不能跑完整对局） |
| `organizer/score_all.py`、`organizer/pack_contestant.py` | 题库接口参考，在公开版不能运行 |
| [`organizer/CALIBRATION.md`](organizer/CALIBRATION.md) | 强对手校准流程 |

AI 接手时先读 [`AGENTS.md`](AGENTS.md)。

## 快速开始

需要 Python 3.11+。C++ 题还需要 `g++`。

```bash
cd contestant/cache_duel
python3 scripts/spar.py --list
python3 scripts/spar.py agents/starter.py --seed-count 2
```

8 道题的规则见 [`contestant/README.md`](contestant/README.md)。

裂陆纪元在仓库根目录运行：

```bash
python3 -m organizer.rift_era check
python3 -m organizer.rift_era bootstrap --seed 1000
python3 -m unittest discover -s organizer/rift_era/tests
```

全量测试约 20 分钟，`bootstrap` 约 2.5 分钟。

## 公开版不包含

正式评测的保密内容不在这里：隐藏测试集、私有参考对手、官方评分配置，以及 3 道 C++ 题的 `problem_mod.py`（含隐藏点分组和数据生成逻辑）。这些内容不影响本地自测。`kaggriculture` 的 champion 是公开的 RSIdemo 冠军包，不算保密内容，只是目前不放在这里。

## 许可

代码和文档按 [Apache-2.0](LICENSE) 授权。第三方内容不在此列，包括洛谷题面和部分数据集，见 [NOTICE](NOTICE)。使用前请先看 NOTICE。

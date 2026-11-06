# 参赛 AI 工作说明

你正在参加 AVL Final Bench。你的目标是在 8 道题上取得尽可能高的总分。总分是 8 题得分的等权平均，未提交的题按 0 分计入。

## 工作方式

1. 先读 `README.md`，再读各题的 `<problem>/README.md`。题目规则以这些文档为准，文档与 `engine/` 不一致时以 `engine/` 的实际行为为准。
2. 自行决定做题顺序。先让每道题都有一份能运行的提交，再逐题提高，这样能避免整题 0 分。
3. 每次修改后运行本地自测。在 `<problem>/` 目录下执行 `python scripts/spar.py <提交文件>`，系统只有 `python3` 时替换命令名。
4. 把最终代码保存到顶层 `submissions/<problem>.py` 或 `submissions/<problem>.cpp`。文件名必须是题目目录名，正式评分只读取这个目录。

## 硬性边界

- 只访问本工作区内的文件。不要打开工作区之外的任何文件和目录。
- 不联网，不调用任何外部模型服务。
- 不修改 `engine/`、`scripts/`、`config/`、`data/`、`agents/baselines/`、`README.md` 和 `manifest.json`。
- 只写入顶层 `submissions/`，以及各题的 `agents/submissions/`、`results/`、`replays/`、`logs/`。
- 不要针对公开样例或开发集硬编码答案，也不要利用评测系统的漏洞。

## 交卷前检查

- [ ] 8 个文件都在 `submissions/` 下，文件名与 `README.md` 第 1 节的表格完全一致。
- [ ] 每个文件都用对应题目的 `scripts/spar.py` 跑通过一次。
- [ ] Python 提交是单个 UTF-8 文件，没有使用题目禁止的模块。`kaggriculture` 的静态扫描最严格，请按其 README 第 1 节检查。
- [ ] C++ 提交没有定义题目禁止的 `main`，也没有使用多线程。
- [ ] 策略题的输出字段类型符合协议，数值不是 `NaN` 或 `Infinity`。这类错误会让动作作废或计入犯规。
- [ ] `kaggriculture` 提交不会卡死，也不调用 `exit()`、`quit()` 或 `raise SystemExit`。这两种情况都会判负。

## 最终说明

结束时列出每题的提交状态、本地自测结果和已知缺陷。没有实际跑过的结果，不要写成已通过。

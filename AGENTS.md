# AVL Final Bench 接手说明

本文件给接手本仓库的 AI。

## 做练习题

进入 `contestant/`，读那里的 `AGENTS.md` 和 `README.md`。提交文件放在 `contestant/submissions/`。

## 接手裂陆纪元引擎

按顺序阅读：

1. `organizer/rift_era/REMAINING_WORK.md`：待实现清单，按优先级排列。
2. `organizer/rift_era/HANDOFF.md`：已完成内容和分支交接。
3. `designs/rift_era/README.md`：规则入口。
4. `organizer/rift_era/FULL_MATCH_TEST_TODO.md`：完整对局验收任务单。

规则：

- 权威规则是 `rift_era_rules_r6`，不要修改 `designs/rift_era/data/` 下的规则 JSON。规则未定义的细节按真实运作、守恒、防刷分补充，记入 `organizer/rift_era/IMPLEMENTATION_CONTRACT.md`，并用新版本标记。
- 不要打开或绕过 `organizer/rift_era/engine/release.py` 的发布门禁来宣称完成。门禁只能靠实际验收解除。
- 测试夹具的外生资源不算正式实现。
- 单局墙钟上限由接手 AI 决定，写明数值和依据。

改动后运行 `python3 -m unittest discover -s organizer/rift_era/tests`。

## 边界

- 这是公开版，隐藏测试集、私有参考对手和各题评分脚本不在仓库里，也不会提供。`kaggriculture` 的 champion 是公开对手，只是目前不放在这里。校准强对手走 `organizer/CALIBRATION.md`。
- `organizer/score_all.py`、`organizer/pack_contestant.py` 仅作接口参考，在这里不能运行，请不要修改。裂陆纪元接入题库按 `organizer/rift_era/INTEGRATION.md` 交付。
- `contestant/` 是从私有仓库导出的生成物，3 道 C++ 题（`ball_machine`、`fruehlingsbeginn`、`partition`）的规则是洛谷原题题面，不要改。
- 不要把裂陆纪元的改动和 `contestant/` 的改动混在一起提交。

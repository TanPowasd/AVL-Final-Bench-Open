# 裂陆纪元题库接入规范

本文件说明裂陆纪元要交付什么，才能接入题库。接入方式依据 `organizer/score_all.py` 和 `organizer/pack_contestant.py` 的实际行为。

这两个脚本在公开仓库里**不能直接运行**，它们调用的 8 道题 `scripts/score.py`、`contestant_suite/` 和 `strategy_scoring.py` 都在私有组委会环境。这里仅作接口参考，请不要修改。最后的注册由仓库作者在私有环境完成。

## 目录结构

| 路径 | 要求 |
|---|---|
| `scripts/score.py` | 正式评分入口 |
| `scripts/spar.py` | 选手本地自测，支持 `--list`，始终标记 `official = false` |
| `scripts/pack_contestant.py` | 导出选手工作区 |
| `config/contestant_readme.md` | 选手规则，导出后成为 `README.md` |
| `config/format.json` | 公开的赛制参数 |
| `agents/starter.py` | 起步模板 |
| `agents/baselines/` | 公开基线 |
| `agents/reference/` | 参考策略，**不得导出** |
| `agents/submissions/.gitkeep` | 待评提交目录 |
| `tests/` | 含导出物不泄漏隐藏内容的测试 |

## `scripts/score.py`

- `score_all.py` 以 `organizer/rift_era` 为工作目录调用它，不传提交路径。它要自己读取 `agents/submissions/`。
- 文件名以 `_` 开头的提交不参与评测。
- 支持 `--seed-count N`，默认 64；支持 `--jobs N`，并受资源准入限制。
- 退出码：评测完成返回 0。裁判或宿主故障返回非 0，并标记 `INVALID_EVALUATION`。
- 结果写入 `results/leaderboard.json`。

### `leaderboard.json`

```json
{
  "scoring_version": "rift_era_rules_r6",
  "official": true,
  "profile_id": "规则、引擎、地图、参考池、运行环境的联合指纹",
  "seeds": [],
  "table": [
    {
      "bot": "提交标识名",
      "status": "OK",
      "score": 50.0,
      "ci95": [48.0, 52.0],
      "components": {"pop": 0.0, "ind": 0.0, "ctrl": 0.0, "tech": 0.0, "intl": 0.0},
      "raw": {"V": 0.0, "R": 1.0},
      "n_games": 64,
      "fault_games": 0,
      "submission_sha256": "提交 zip 的 SHA256"
    }
  ]
}
```

- `score` 按 `scoring.json` 的 `score_formula` 计算，范围 0–100。自参考为 50。
- `components` 是五支柱的展示分。指定 `--seeds` 重放或缩短局长的运行必须写 `official: false`。
- 不要把隐藏种子写进选手可见的文件。批次结束前，回放和种子只留在组委会侧。

## `scripts/pack_contestant.py`

- 接受 `--out <目录>` 和 `--force`。
- 用白名单 `PUBLIC_MAP` 逐文件导出，不要按目录复制，也不要用黑名单。
- 目标已存在且未传 `--force` 时要报错。

## 仓库作者在私有环境完成的事

这些改动不需要接手方做：

1. 在 `score_all.py` 的 `PROBLEM_EXT` 加入 `"rift_era": ".zip"`，在 `pack_contestant.py` 的 `PROBLEMS` 加入本题。
2. 在 `extract_problem_metric` 增加本题分支。现有策略题分支写死了 `.py` 文件和 `scoring_v2` 配置校验，裂陆纪元提交的是 zip，不能复用。
3. 决定题库总分的计算方式。现在是 8 题等权，加入后是否改成 9 题，由作者决定。

## 验收

- `scripts/score.py` 能用 `agents/reference/` 跑完一批，且自参考得 50。
- 导出的选手工作区中，`spar.py --list` 能运行，且不含 `agents/reference/`。
- 通过 `FULL_MATCH_TEST_TODO.md` 全部项目。发布门禁未打开时不能写 `official: true`。

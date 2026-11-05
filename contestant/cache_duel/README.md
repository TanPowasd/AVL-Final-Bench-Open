# 缓存攻防（Cache Duel）

双角色在线缓存博弈。你需要提交**单个 Python 文件**，根据开局消息中的 `role` 同时支持扮演**防守方（defender）**与**攻击方（attacker）**，与固定参考对手互换攻守进行评测。

---

## 1. 交付与运行限制

- **最终统一提交路径（必交）**：`../submissions/cache_duel.py`（即工作区顶层 `submissions/cache_duel.py`；本地测试通过后必须将最终文件以题目名保存至此目录，正式评分仅从顶层 `submissions/` 自动分发）。
- **题内开发自测路径（可选）**：`agents/submissions/my_agent.py`（`scripts/spar.py` 同时支持直接读取 `../submissions/cache_duel.py` 或 `agents/submissions/my_agent.py`）。
- **语言与依赖**：单个 UTF-8 Python 3.10+ 源文件，仅允许使用 Python 标准库，禁止联网、禁止读写外部文件或读取对手代码。
- **运行方式**：裁判为每局的攻击方与防守方各启动一个独立子进程，通过 `stdin / stdout` 按行交互 UTF-8 JSON。子进程的工作目录是提交文件所在目录，`stderr` 被丢弃，环境变量 `PYTHONHASHSEED=0`。
- **时间限制**：握手、每一步和结束消息都按单步计时，上限 `1.0` 秒；单局累计思考时间上限 `20.0` 秒。
- **时间池**：另有独立于单步的时间池 `5` 秒（整局一份、不回补）。某步用时超出单步时限的部分从池里扣，池有余量就不算超时；池用完后恢复单步硬时限。剩余池量见每条消息的字段 `time_pool`（秒），也计入累计总预算。
- **超时与犯规处理**：
  - 进程超时、崩溃、提前关闭输出或输出非合法 JSON 后，该进程被终止，本局剩余所有步均按犯规处理。
  - 输出合法 JSON 但内容不合规（不是对象、缺字段、取值非法）只判当步犯规，进程继续运行。
  - **攻击方犯规**（超时或返回的 `key` 不为满足 `0 <= key < K` 的整数）：该步请求由裁判替换为 `t % (C + 1)`。
  - **防守方犯规**（需要驱逐时超时，或返回的 `evict` 不在当前缓存中）：裁判强制驱逐**最新放入缓存**的那个 key。
  - `need_evict` 为 `false` 时裁判不看回复内容，但仍要在时限内回复一行。
  - `key` 和 `evict` 必须是整数，布尔值、浮点数等都算犯规。
- **工作区边界**：读写范围和规则优先级见工作区顶层 `README.md`。权威裁判源码位于 `engine/engine.py`。

---

## 2. 游戏规则

一局比赛给定三个正整数参数：
- `C`（`cache_size`）：缓存容量（缓存最多同时容纳 `C` 个不同 key，所有 key 大小均为 `1`，初始缓存为空集）。
- `K`（`universe`）：合法请求 key 的全集为整数 `{0, 1, ..., K - 1}`（满足 `K > C`）。
- `N`（`length`）：一局总步数，`t = 0, 1, ..., N - 1`。

每一步 `t`（共 `N` 步）严格按以下顺序执行：
1. 裁判向攻击方发送上一步是否命中的反馈 `last_hit`（`t = 0` 时为 `null`，`t > 0` 时为布尔值 `true` 或 `false`）。攻击方不知道防守方缓存的内部集合，也不知道防守方驱逐了哪个 key。
2. 攻击方返回本步请求的整数 `key`（`0 <= key < K`）。
3. 裁判检查 `key` 是否在当前缓存集合中：
   - **命中（`hit = true`）**：缓存集合不变，`need_evict = false`，裁判通知防守方本次命中，防守方回复 `{}`。
   - **缺失且缓存未满（`hit = false, len(cache) < C`）**：`need_evict = false`，裁判通知防守方后直接将 `key` 加入缓存，防守方回复 `{}`。
   - **缺失且缓存已满（`hit = false, len(cache) == C`）**：`need_evict = true`，裁判通知防守方本次请求的 `key`，防守方必须从**当前已在缓存中的 `C` 个 key** 里指定一个 `evict` 将其移除，随后裁判将新 `key` 放入缓存。

---

## 3. 通信协议（`stdin / stdout` 逐行 JSON）

1. **初始化握手**（启动后第 1 行）：
   - 收到：`{"role": "attacker" | "defender", "cache_size": C, "universe": K, "length": N, "seed": S}`（`S` 由场景种子按角色派生，攻防双方收到的值不同）
   - 回复：任意一行合法 JSON 对象（如 `{}`），并立即 `flush`。
2. **逐步交互**（`t = 0 .. N - 1`）：
   - 若 `role == "attacker"`：
     - 收到：`{"t": t, "last_hit": null | true | false, "time_pool": 秒}`
     - 回复：`{"key": k}`（`int`，`0 <= k < K`）
   - 若 `role == "defender"`：
     - 收到：`{"t": t, "key": k, "hit": true | false, "need_evict": true | false, "time_pool": 秒}`
     - 回复：当 `need_evict` 为 `true` 时回复 `{"evict": e}`（`e` 必须在当前缓存内）；当 `need_evict` 为 `false` 时回复 `{}`。
3. **对局结束**：
   - 收到：`{"t": N, "end": true}`
   - 回复：`{}` 后退出进程。

---

## 4. 计分规则与评测配置

### 4.1 单局得分
一局 `N` 步结束后，记实际发生的请求序列为 `R = (r_0, r_1, ..., r_{N-1})`（含攻击方犯规后的替代请求），防守方实际发生的总缺失次数为 `M_def`。

裁判在相同序列 `R` 与容量 `C` 上计算**离线最优缓存算法（Belady / Farthest-in-Future，每次在必须驱逐时淘汰下一次出现位置最晚或不再出现的 key）**的最小可能缺失次数 `OPT`：
- 当 `M_def == 0` 时，`def_score = 1.0`；
- 否则：
  $$\text{def\_score} = \frac{\text{OPT}}{M_{\text{def}}} \in (0, 1]$$
- 对应攻击方在该局的攻击得分为：
  $$\text{att\_score} = 1 - \text{def\_score} \in [0, 1)$$

### 4.2 正式百分制评分（scoring_v2）

固定对阵 5 个参考对手，在本次抽到的每个种子与每种配置上分别扮演攻击、防守角色。其他参赛程序不进入计分对手池，提交与某个参考对手同名或源码相同也不删除该对局。

- **防守分** = `100 * mean(def_score)`。
- **攻击分** = `100 * mean(1 - 对手 def_score)`。
- **总分** = `0.5 * 防守分 + 0.5 * 攻击分`，范围 `[0,100]`。
- 对手、配置、种子分别等权，不能按请求总数加权。每份提交默认运行 `5 * 3 * 64 * 2 = 1920` 局；N 个场景共 `30N` 局。
- 第 1 节的非法请求与驱逐替代规则在正式评分中同样生效，不因某局出现犯规就丢弃该局。

- **每次随机、多局平均**：每次评测重新均匀抽取 N 个互不重复的场景种子，默认 N=64，可增加场景数。参考对手池、生成规则、完整局长及运行限制固定；同一批提交共享本次种子并交换座位/角色，对手、场景、座位分别等权。不会选择多次运行中的最好成绩，也不跨次缓存成绩。每个 seed 直接从操作系统安全随机源抽取（secrets/SystemRandom），去重并排除校准种子，不经固定主种子扩展；报告保存完整 seed 列表，供重放核查。
- **统计与版本**：成绩包含 `scoring_version = "scoring_v2"`、`score`（0–100）、`max_score = 100`、分项、原始指标、出错局数和 95% bootstrap 区间。区间按完整种子块重采样，保留同种子下座位、对手与赛制之间的相关性；只有一个种子时不报告区间。
- **可比性**：报告包含冻结配置、引擎、对手及提交源码指纹。旧版成绩和练习成绩不进入新版正式汇总；不同配置指纹的成绩不直接混用。这里的百分制表示本题固定评测下的表现，不是最优率或胜率。
- **本地自测**：`scripts/spar.py` 同样默认随机抽取场景，使用 `--seed-count N` 指定数量，使用 `--seeds 123 456` 重放。公式与正式赛一致，但对阵公开基线，始终标记 `official = false`。正式入口中显式指定 `--seeds` 的重放或缩短局长的运行也标为非正式。

标准评测配置表（`config/format.json`，正式评测更换随机种子）：

| 配置 | 缓存容量 `C` | 键空间 `K` | 步数 `N` |
|---:|---:|---:|---:|
| 1 | 8 | 64 | 3000 |
| 2 | 32 | 256 | 5000 |
| 3 | 64 | 4096 | 5000 |

---

## 5. 本地自测

公开基线位于 `agents/baselines/`（`fifo`、`lru`、`marking`、`rand`），起步模板位于 `agents/starter.py`。不指定对手时 `spar.py` 对阵 `lru`。

```bash
python scripts/spar.py --list
python scripts/spar.py ../submissions/cache_duel.py
python scripts/spar.py agents/submissions/my_agent.py lru --seed-count 2
python scripts/spar.py agents/submissions/my_agent.py --all --seed-count 2
```

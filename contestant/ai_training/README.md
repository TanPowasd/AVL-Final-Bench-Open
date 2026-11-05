# 前沿实验室（Frontier Lab）

AI 实验室三年研发与商业化综合博弈。一局模拟 `3` 年（`26280` tick，`1` tick = `1` 小时），你控制一家前沿 AI 实验室，在每个随机种子独立生成的**隐藏缩放律（Scaling Law）与隐藏数据域质量/污染参数**下，竞拍或租用 5 代 GPU、建设机房与电力协议、招募研究员研发科技树、采购/合成/蒸馏数据、调度训练任务并发布大模型参与 API 市场竞争。同一份程序同时参加**单人赛（比最高模型真实能力 `C`）**与**对抗赛（比终局公司股权价值）**。

---

## 1. 交付与运行限制

- **最终统一提交路径（必交）**：`../submissions/ai_training.py`（即工作区顶层 `submissions/ai_training.py`；本地测试通过后必须将最终文件以题目名保存至此目录，正式评分仅从顶层 `submissions/` 自动分发）。
- **题内开发自测路径（可选）**：`agents/submissions/my_agent.py`（`scripts/spar.py` 同时支持直接读取 `../submissions/ai_training.py` 或 `agents/submissions/my_agent.py`）。
- **语言与依赖**：单个 UTF-8 Python 3.10+ 源文件，仅允许使用 Python 标准库，禁止联网、禁止读写外部文件或读取对手代码。
- **运行方式**：裁判为每局的每位选手启动一个独立子进程，通过 `stdin / stdout` 按行交互 UTF-8 JSON。子进程的工作目录是提交文件所在目录，`stderr` 被丢弃，环境变量 `PYTHONHASHSEED=0`。单人赛的 NPC 实验室在裁判进程内运行。
- **交互时限**：初始化握手限时 `10.0` 秒；每 tick 墙钟时限 `2.0` 秒；单局累计总思考预算 `40` 分钟（`2400` 秒）。
- **时间池**：另有独立于单 tick 的时间池 `120` 秒（整局一份、不回补）。某 tick 用时超出单 tick 时限的部分从池里扣，池有余量就不算超时；池用完后恢复单 tick 硬时限。剩余池量见观测字段 `time_pool`（秒），也计入累计总预算。
- **超时与容错**：单 tick 超时或输出非 JSON 对象按空动作 `{}` 处理，迟到回复按未答消息数对齐丢弃（`obs["missed"]` 报告累计次数）；未回复的消息积压达到 `50` 条、进程崩溃或总预算用尽后，剩余 tick 全按空动作处理。第 `t` tick 观测后返回的动作在第 `t + 1` tick 生效。
- **动作异常**：动作按第 3 节的字段顺序逐项执行。某一项的字段类型错误引发异常时，本 tick 该项之后的动作全部作废，`res` 里出现 `{"act": "?", "ok": false, "why": "exception: ..."}`，对局继续。
- **非法数值**：动作里任何数值是 `NaN`、`Infinity` 或绝对值超过 `1e15` 时，本 tick 的整个动作作废，`res` 里出现 `non-finite or oversized number`，对局继续。
- **工作区边界**：读写范围和规则优先级见工作区顶层 `README.md`。权威实现位于 `engine/engine.py` 与 `engine/world.py`。

---

## 2. 完整规则

**量纲约定**：资金单位为 `M`（百万美元）；模型参数量 `N` 与训练数据量 `D` 单位均为 `B`（十亿）；算力单位为 `G1·h`（`1` 张 G1 运行 `1` tick = `1`）；API 定价单位为「美元 / 百万 token」，故送达 `x`（B token）的收入为 `x * price / 1000`（M）。

### 2.1 时间周期与每 tick 结算顺序
- `1` 天 = `24` tick，`1` 周 = `168` tick，`1` 月 = `730` tick，`1` 季度 = `2190` tick，`1` 年 = `8760` tick，整局 = `26280` tick（`t = 0 .. 26279`）。
- 每 tick 严格按以下 9 步顺序推进：
  1. 触发全局事件（NPC 新模型、开源事件、出口管制预告、地区限电、已发布模型满 24 tick 正式上线）。
  2. 执行双方上一 tick 提交的动作。
  3. **每周一结算（`t % 168 == 0`）**：GPU 统一价拍卖 → 独家数据招标 → 候选人招聘 → 挖角判定 → API 市场份额迁移 → 刷新本周拍卖供给、数据合同与人才池 → 独家数据到期转公共池 → 蒸馏诉讼判定。
  4. 到货与基建完成（中标 GPU 到货装机、机房扩容/新建完工、坏卡修复、二手卖卡回款、合成数据入库）。
  5. 显式 `serve` 先留卡（设了才有）→ 云租现货抢占判定（被抢占的租约立即结束）→ 累加研发点数 → 按创建顺序把卡分给在跑训练任务 → 中断受抢占影响的任务 → 训练任务推进（硬件故障、慢节点、存档、学习率尖峰）。
  6. API 请求服务与收入结算。未设 `serve` 时，训练分完后的剩余卡按需自动用于服务，从最旧型号开始；设了 `serve` 时只用留出的卡，其余卡闲置。
  7. 扣除工资、云租费、电费，并执行破产判定。
  8. **每月度结算（`t % 730 == 0` 且 `t > 0`）**：已上线模型污染独立复测、核心研究员离职抽签、训练数据版权诉讼判定（数值见 2.7 节）。
  9. 生成并发送本 tick 观测（`loss`、`rent`、`elec`、`pts` 每日 `t % 24 == 23` 附带；`wk` 每周 `t % 168 == 0` 附带）。

### 2.2 起始状态与破产清算
- 双方起始完全相同：现金 `200 M`，创始人股权 `equity = 1.0`（100%），声誉 `rep = 0.0`；研究员 `20` 人（能力 `1..4`）；`0` 号地区机房 `1` 座（电力上限 `2 MW`，该地区扩容上限 `>= 8 MW`）；自有已装机 `G1` 共 `512` 张；仅持有初始公共数据。开局 API 份额由 `N1`、`N2` 各占 `0.5`，玩家份额为 `0`。
- **破产**：若现金连续 `720` tick（30 天）`< 0` 则破产（`bk = true`），停止全部训练、清空租约与研究员、下架全部模型、后续动作作废，终局估值固定按清算价 `equity * max(0, cash + 0.3 * 硬件账面)` 计算。

### 2.3 五代 GPU、统一价拍卖、云租与机房电力

| 型号 `model` | 单卡算力 | 单卡功耗 (kW) | 上市季度（0 起） | 每季度全市场供给（张） | 基准单价 (M/张) |
|:---:|---:|---:|---:|---:|---:|
| `G1` | 1.0 | 0.7 | 0 | 8192 | 0.05 |
| `G2` | 2.2 | 1.0 | 1 | 6144 | 0.09 |
| `G3` | 5.0 | 1.4 | 3 | 4096 | 0.17 |
| `G4` | 11.0 | 1.8 | 6 | 3072 | 0.32 |
| `G5` | 24.0 | 2.4 | 10 | 2048 | 0.60 |

- **每周拍卖 `bid_gpu {"model", "lots", "price"}`**：每批 `1 lot = 256 张`（`lots` 超过 `64` 时按 `64` 计）。本周可拍的型号、批数和保留价见 `wk.offer`，报价在下一个周一结算后清空，下周要重新提交，第一次结算在 `t = 168`。保留价 `reserve = 0.8 * 基准价 * AI热度`（低于保留价无效）。每周放出季度供给的 `1/13`（完成科技 `early_supply` 的玩家可在 `G3 / G4 / G5` 正式上市前 1 个季度参与 `25%` 提前配额拍卖，NPC 不参与提前季）。两家 NPC 每周亦提交随机报价。周一按单价从高到低**统一出清价**成交（`出清价 = max(reserve, 最高未完全成交报价)`；同价且剩余批数不够平分时每家分 `floor(剩余 / 同价家数)`，余数流拍；现金不足时按可支付批数成交），中标后经 `4..10` 周交付，优先装入有电力余量的自有地区机房，超出电力上限的部分暂存库存。
- **云租 `rent {"model", "n", "kind": "ondemand"|"spot", "ticks"}`**：`kind` 省略时为 `ondemand`，`ticks` 省略时为 `168`。云租卡不占机房电力，也不计电费。已上市型号云租池容量为 `2 * 季度供给`；池占用率 `u = clip(clip(0.3 + 0.6 * (热度 - 0.75), 0.15, 0.85) + 双方在租总数 / 池容量, 0, 1)`；单次租量超过 `int(池容量 * (1 - u))` 时截断，余量为 `0` 时失败；按需单价 `基准价 / 8000 * (1 + 2 * u)` M/(卡·tick)，`spot` 现货单价为按需的 `0.4` 倍，每 tick 按当期价格扣费，`end_rent [id]` 可提前退租。每份 `spot` 租约每 tick 有 `0.02 + 0.08 * u` 概率被整体抢占：租约立即结束，并中断创建最晚的、正在使用该型号的训练任务（同一型号同 tick 只中断一个任务）。
- **二手出售 `sell_gpu {"model", "n"}`**：当代最新已上市型号退 `65%` 基准价、落后 1 代退 `50%`、落后 `>= 2` 代退 `30%`，`48` tick 后到账。可售数量不含坏卡。提前配额买到的卡在该型号正式上市前出售，按 `65%` 退款。
- **机房与电力（`build_site` / `expand_site` / `ppa`）**：共 `5` 个地区（`init` 给出基础电价 `0.0004..0.0012` M/MWh、单座机房容量上限 `cap_mw` `5..40` MW、工期 `8..20` 周、建设费及监管系数 `reg`）。`build_site` 建成后直接获得 `cap_mw` 的全部容量；同一地区可以重复建，`cap_mw` 是单座上限，不是地区总量。每方最多 `8` 座机房；扩容每 `+1 MW` 花费 `2 M`、`8` 周生效且单座不超过 `cap_mw`。装机硬约束：`sum(卡数 * 单卡kW) * PUE <= 机房MW * 1000`（默认 `PUE = 1.30`，研发 `cooling` 后 `1.15`；电网限电事件期间上限临时 `×0.7`，超出部分按比例不可用）。卡在到货、扩容生效、机房完工、`cooling` 完成和每周 `t % 168 == 1` 时自动装机，不能手动卸载。每 tick 用电量为 `在用自有卡kW * PUE / 1000` MWh，训练卡按满功率计，服务卡按 `0.3 + 0.7 * 利用率` 计。`ppa {"site", "mwh"}` 按签约时该地区电价 `×0.8` 锁定 `8760` tick，每 tick 照付不议支付 `(mwh / 8760) * 锁定价` 抵扣该机房用电，超出部分按当期电价计。

### 2.4 研究员、科技树与数据池
- **招聘 `hire` 与挖角 `poach`**：每周一人才池刷新 `10..30` 人（真实能力 `1..5`，招聘前仅显示能力区间 `[lo, lo + 2]` 与最低要价 `ask`，见 `wk.talent`），下一个周一密封最高薪得人（同价均不得，每方上限 `300` 人，约 `12%` 的候选人还有 NPC 报价），未中的报价随之清空。在职研究员每 tick 贡献等于其能力值的研究点，按 `research [{"tech": ..., "share": ...}]` 分配给前置已满足的未完成科技。`research` 是持续设定，新值整体替换旧值，`share` 在可研科技之间归一化。`poach` 每周最多 `5` 次，`target` 取 `opp`、`N1` 或 `N2`。挖对手时 `idx` 是对手员工列表的下标（名单不可见，只知道总人数），成功率 `sigmoid(3 * (salary / 现薪 - 1.3) - (0.5 + 在职年数))`。挖 NPC 时成功率 `sigmoid(3 * (salary / 0.0003 - 1.3) - 1)`，能力在 `1..5` 中随机，`idx` 不起作用。薪资单位都是 M/tick。

| 科技 ID | 所需研究点 | 效果 |
|---|---:|---|
| `algo_eff_1` / `2` / `3` | 20k / 60k / 150k | 依次前置；新建训练任务有效参数 `N_eff = N * (1.15 / 1.30 / 1.50)` |
| `stable` | 30k | 学习率过高导致的 loss 尖峰触发概率减半 |
| `filter:<域>`（8 个域各一） | 各 25k | 揭示该域真实质量 `q` 与污染率 `c`（`tech` 事件里的 `c` 已是减半后的值），并将此后该域训练污染减半 |
| `curriculum` | 40k | 训练中途执行 `set_mix` 不再产生 `-5%` 配比效率惩罚 |
| `infer_opt_1` / `2` | 25k / 70k | 依次前置；API 推理单 token 算力消耗 `×0.80` / `×0.60` |
| `synth` | 35k | 解锁 `synth` 合成数据生成动作 |
| `early_supply` | 50k | `G3 / G4 / G5` 提前 1 季度参与 25% 配额拍卖 |
| `cooling` | 20k | 全部机房 `PUE` 从 `1.30` 降至 `1.15` |

- **数据池**：共 7 个真实评测域（`web, code, math, books, science, dialog, multiling`）+ `synth` 域 + `distill` 池。每个域有隐藏真实质量 `q in [0.5, 1.5]`（`synth` 域的公共数据为 `[0.6, 0.9]`；`init` 给带 `U(-0.3, 0.3)` 误差的 `q_hint`）与隐藏污染率 `c in [0, 0.12]`（`synth` 无污染）。
  - `bid_data {"deal", "price"}`：每周 `0..2` 份独家数据合同（见 `wk.deals`），下一个周一密封首价开标。`price` 须 `>= reserve` 且开标时现金足够，NPC 也可能投标，同价流拍。玩家中标后独享 1 年，到期后数据进入另一方的公共池；NPC 中标的合同不公开。
  - `synth {"tokens_b"}`：需完成 `synth` 且有已上线模型（最高能力模型参数量为 `N`、真实能力 `C`），花费 `tokens_b * N * 100 * 当期G1按需租价`，`24` tick 后入库，质量 `0.6 + 0.6 * C`；若单任务累计合成数据占比 `> 0.30`，触发模型坍缩使各域 loss 增加 `0.5 * (占比 - 0.30)`。
  - `distill {"model", "tokens_b"}`：按目标在架模型（对手、NPC 或免费开源模型 `OPEN-1`）API 价格付费购买输出，立即进入 `distill` 池，质量 `0.5 + 0.8 * 老师C`；若某存档蒸馏占比 `>= 0.10`，其真实能力 `C` 被封顶为 `最强被蒸馏老师C - 0.03`；若在跑任务蒸馏占比 `> 0.20`，每周 `10%` 概率被起诉罚没 `15%` 现金且 `rep -= 0.2`。

### 2.5 训练动力学与隐藏缩放律（每种子独立采样）
- **创建任务 `run`**：指定 `name`（截断到 `32` 字符，本方整局不可重名，已结束的任务也算）, `params_b` ($N$), `tokens_b` ($D$)，以及可选 `gpus`, `lr` ($\eta$，默认 $0.0010 \cdot N^{-0.3}$), `batch`（默认 `512`）, `sched`（`cosine` / `wsd` / `const`）, `mix`（数据池权重，自动归一化）, `ckpt_every`（默认 `24` tick）, `paused`。最多同时存在 `12` 个未完成任务，整局上限 `400` 个。每个任务保留最近 `300` 个存档。
- **每 tick 吞吐量**：
  - 原始算力 `raw = sum(分配卡数 * 单卡算力)`，总卡数 `cards`；
  - 利用率 `util = min(1.0, 2.0 * batch / cards)`；
  - 隐藏临界批大小 $B_{\text{crit}} = bc_0 \cdot (1 + D_{\text{trained}} / 50)^{0.4}$（$bc_0 \in [192, 640]$ 随种子隐藏），浪费系数 `waste = (min(batch, B_crit) + 0.5 * max(0, batch - B_crit)) / batch`；
  - 本 tick 前进 token 数 = `raw * util * waste * 慢节点系数 * (0.8 if 本tick存档 else 1.0) / (300 * N)`。
- **各真实域 Loss 公式**（$d \in \{0..6\}$）：
  $$L_d = \left(E_d + \frac{A}{N_{\text{eff}}^{\alpha}} + \frac{B}{D_{\text{eff}, d}^{\beta}} + P_{\text{lr}} + P_{\text{collapse}}\right) \times M_{\text{spike}} \times (1 - b_{\text{sched}})$$
  - 隐藏常数范围：$\alpha \in [0.28, 0.40]$，$\beta \in [0.25, 0.35]$，$A \in [0.3, 0.6]$，$B \in [0.9, 1.5]$，$E_d \in [1.4, 2.2]$。
  - 数据池 $k$ 重复衰减后的有效 token：$E_k = 4 U_k (1 - \exp(-S_k / (4 U_k))) \cdot \bar{q}_k$（$U_k$ 为当前持有去重量，$S_k$ 为该任务在池 $k$ 已训量，$\bar{q}_k$ 为加权质量）；经隐藏跨域迁移矩阵 $T_{d,k}$ 汇总得 $D_{\text{eff}, d} = \max(0.01, \sum_k T_{d,k} E_k \cdot \text{mix\_pen})$。$\text{mix\_pen}$ 初始为 `1`，未完成 `curriculum` 时每次 `set_mix` 累乘 `0.95`。
  - 隐藏最优学习率 $\eta^* = c_{\text{lr}} \cdot N^{-0.3}$（$c_{\text{lr}} \in [0.0012, 0.0028]$），超参惩罚 $P_{\text{lr}}$ 为按已训 token 加权的 $0.08 (\ln(\eta / \eta^*))^2$。当 $\eta > \eta^*$ 时每 tick 以概率 $0.002 \exp(3(\eta/\eta^* - 1))$（有 `stable` 减半）发生尖峰：使 $M_{\text{spike}} \leftarrow 1.02 M_{\text{spike}}$，其中 `10%` 概率直接进入 `diverged` 停滞状态；存档在尖峰判定前写入，`run_ctl {"name", "op": "rollback"}` 可恢复至存档状态（停 `1` tick）。
  - 退火项 $b_{\text{sched}}$：`cosine` 为 $0.02 \times \text{progress}^2$，`wsd` 在最后 `15%` 进度线性升至 `0.03`，`const` 为 `0`。
  - 观测中的 `loss` 读数（每日一次及任务 `done` 时）每个域独立乘以观测噪声 `(1 + 0.01 * N(0, 1))`。
- **硬件中断、慢节点与监管审核**：在跑任务每 tick 硬件故障中断概率 `cards / 50000`（回退至最近存档，没有存档时回到零进度，停 `2` tick，任务用到自有卡时损坏 1 张 `72` tick；现货抢占的中断同样回退并停 `2` tick，但不损坏卡）；每日 `1%` 概率出现慢节点使吞吐下降 `10%..30%` 直至执行 `run_ctl diagnose`（停 `1` tick）；单任务累计有效算力达 `20,000,000 G1·h` 且未获批时强制停审 `336 * reg` tick（亦可提前调用 `audit_request` 审核，审核期间不占 GPU）。`reg` 取电力最大的机房所在地区。强制停审的 `review` 事件里，`until` 是实际结束时间。

### 2.6 模型能力、榜单复测、API 市场与季度融资
- **真实能力 `C` 与公开榜分 `pub`**：6 个隐藏评测基准将 7 域真实无噪 `L_d` 线性加权后经 `sigmoid` 映射为 `[0, 1]` 分数并取均值得到真实能力 `C`；`C` 在提交 `release` 时按所选存档算定，之后不变；上线时 `公开榜分 pub = C + 加权污染占比`。每月独立复测检查所有未被处罚过的玩家已上线模型（含已下架模型）：若 `pub - C > 0.05`，扣声誉 `rep -= 0.3` 并将 `pub` 强制修正为 `C`。
- **发布与 API 市场**：`release {"run", "ckpt", "price"}` 提交后 `24` tick 上线，局末最晚在 `t = 26254` 的观测后提交才来得及。各方在架模型中 `4 * pub - 1.5 * ln(price)` 最高者作为该方旗舰参与市场竞争，全部需求都由旗舰按旗舰价格服务（玩家效用额外 `+ rep - 5 * 丢单率`；份额迁移先用现有的 `rep` 和丢单率计算，随后才用刚结束一周的数据更新这两个值），每周一按 logit 更新目标份额（单周迁移上限 `±0.15`）。服务 `1 B token / tick` 消耗 `25 * N * 推理系数` 的 G1 等效算力，`N` 取旗舰模型的参数量。
- **声誉演化**：每周一结算。上周丢单率超过 `0.1%` 时 `rep -= min(0.1, 丢单率)`，否则 `rep += 0.01`。`rep` 范围为 `[-3, 0]`。
- **季度融资 `raise {"amount"}`**：每季度（`2190` tick）限 `1` 次，投前估值 `V_pre = 240 * 近730tick营业收入 + 300 * max(0, 本方最高pub - 其他各方最高pub) + cash`，`amount` 超过 `V_pre` 时截断为 `V_pre`，`V_pre <= 0` 时融资失败，融资后 `equity <- equity * V_pre / (V_pre + amount)`。`raise` 事件双方都能看到。对手蒸馏你的模型时支付的费用计入现金和 `V_pre` 的营收窗口，但不计入终局服务毛利。

### 2.7 需求、价格与全局事件

这些参数对每个种子公开适用，具体随机值仍随种子变化。

- **API 全市场需求**：开局 `5 B token/tick`。第 1、2、3 年分别按月增长 `8%`、`5%`、`3%`，再乘 AI 热度和季度冲击。
- **AI 热度**：每周一个值，由周期为 `78` 周、振幅 `0.25` 的正弦波叠加 AR(1) 噪声得到，范围 `[0.6, 1.5]`。
- **季度冲击**：每季度有 `20%` 概率把累计系数乘以 `0.85` 或 `1.15`。
- **电价**：`地区基础电价 * (1 + 0.12 * t / 8760) * (1 + 0.1 * sin(2π * t / 8760 + 地区相位))`。
- **出口管制**：`60%` 的种子发生。第 `4..8` 季度（0 起）之一的 `G3` 供给减半，提前 `2` 周公告。
- **开源事件**：`30%` 的种子发生，时间在第 `6..24` 个月。`OPEN-1` 能力为发布方当时能力减 `0.05`，价格 `3`，蒸馏免费。
- **地区限电**：每周每地区 `3%` 概率触发，持续 `1..3` 周，机房电力上限 `×0.7`。
- **NPC 模型**：首代能力 `0.30..0.50`，每 `6..9` 个月出新一代，能力增加 `0.05..0.10`，上限 `0.97`；定价 `10 * (0.5 + C)`，开源事件后再乘 `0.7`。
- **核心研究员离职**：每月一个公共随机数，低于 `2%` 时双方各走能力最高的一人。
- **版权诉讼**：当月训练量中 `web` 与 `books` 的占比超过 `50%` 时，当月触发概率为 `0.1 * (占比 - 0.5) / 0.5`，罚当前现金的 `3%`。

---

## 3. 通信协议（`stdin / stdout` 逐行 JSON）

1. **握手 `init`**：
   ```json
   {"type": "init", "seat": 0, "ticks": 26280, "rules": {...},
    "gpus": [{"model": "G1", "power": 1.0, "kw": 0.7, "launch_q": 0, "supply_q": 8192, "price": 0.05}],
    "regions": [{"id": 0, "price": 0.0006, "cap_mw": 12.0, "build_weeks": 12, "build_cost": 25.0, "reg": 1.1}],
    "domains": [{"name": "web", "public_b": 1824.7, "q_hint": 1.42}],
    "pools": ["web", "code", "math", "books", "science", "dialog", "multiling", "synth", "distill"],
    "real": ["web", "code", "math", "books", "science", "dialog", "multiling"],
    "techs": {"algo_eff_1": {"cost": 20000, "pre": null}},
    "start": {"cash": 200, "gpus": {"G1": 512}, "site": {"region": 0, "mw": 2}, "staff": [...]},
    "npcs": ["N1", "N2"], "models": [...]}
   ```
   收到后回复 `{}`（座位 `0` 发布的模型 ID 前缀为 `A1, A2, ...`，座位 `1` 为 `B1, B2, ...`）。
2. **每 tick 观测 `obs`**：
   - 每 tick 必含：`t` (`0..26279`), `cash`, `eq`（股权）, `rep`（声誉）, `bk`（是否破产）, `gpu`（各型号 `[可用自有, 坏卡, 库存未装, 在租, 在用]`，没有卡的型号不出现）, `runs`（`[name, status, trained_b, target_b, tok_per_tick, eff, last_ckpt_id, cards]`，状态含 `running / paused / review / diverged / done / stopped`；`done`、`stopped` 的任务只在出现相关事件的那个 tick 列出）, `api`（`[需求B, 送达B, 收入M]`）, `missed`, `time_pool`（剩余时间池秒数）。
   - 有内容时才出现（请用 `obs.get("ev", [])` 读取）：`ev`（事件列表：`done / failure / preempted / spike / diverged / slow_node / review / review_done / delivered / expanded / site_ready / tech / quit / poached / lawsuit / bankrupt / release / open_source / export_ctrl / curtail / raise / audit_fail`）, `res`（动作回执与失败原因）。
   - 每日附带（`t % 24 == 23`）：`loss`（`running`、`paused`、`diverged` 且已训量大于 `0` 的任务的 7 个真实域带噪 loss）、`rent`（已上市型号的按需租价）、`elec`（5 地区当前电价）、`pts`（在研科技点数进度）。
   - 每周附带（`t % 168 == 0`）：`wk`，字段如下。

     | 字段 | 内容 |
     |---|---|
     | `week`, `heat`, `demand` | 周序号、AI 热度、当前全市场需求 |
     | `auction` | 上周拍卖结果 `{型号: {lots, clear, winners, delay_weeks}}` |
     | `deal_results`, `hires` | 上周数据招标和招聘结果 |
     | `offer` | 本周可拍 `{型号: {lots, reserve, early}}` |
     | `deals` | 本周数据合同 `[{id, domain, tokens_b, q_hint, reserve}]` |
     | `talent` | 本周候选人 `[{id, lo, hi, ask}]` |
     | `shares` | 各方 API 份额 |
     | `models` | 未下架模型 `[{id, owner, pub, price, N}]` |
     | `staff` | 人数 `{opp, N1, N2}` |
     | `my` | 本方 `staff`（`[id, 能力, 薪资]`）、`sites`、`data`（各池去重量）、`tech`、`share`、`drop`、`deliv`（`[到货 tick, 型号, 张数]`） |

3. **每 tick 动作 JSON**（全部字段可选，省略表示维持原持续设定或不执行）：
   ```json
   {"bid_gpu": [{"model": "G2", "lots": 2, "price": 0.15}],
    "rent": [{"model": "G1", "n": 256, "kind": "spot", "ticks": 168}],
    "end_rent": [3],
    "sell_gpu": [{"model": "G1", "n": 256}],
    "build_site": [{"region": 3}],
    "expand_site": [{"site": 0, "mw": 4}],
    "ppa": [{"site": 0, "mwh": 20000}],
    "hire": [{"cand": 17, "salary": 0.0003}],
    "poach": [{"target": "opp", "idx": 3, "salary": 0.0006}],
    "fire": [5],
    "research": [{"tech": "algo_eff_1", "share": 0.6}, {"tech": "filter:web", "share": 0.4}],
    "bid_data": [{"deal": 9, "price": 12}],
    "synth": [{"tokens_b": 20}],
    "distill": [{"model": "N1-1", "tokens_b": 15}],
    "run": [{"name": "r1", "params_b": 20, "tokens_b": 400, "gpus": {"G1": 512},
             "lr": 0.0004, "batch": 256, "sched": "cosine",
             "mix": {"web": 0.4, "code": 0.3, "math": 0.3}, "ckpt_every": 24, "paused": false}],
    "run_ctl": [{"name": "r1", "op": "pause|resume|stop|rollback|set_lr|set_mix|ckpt|diagnose|set_gpus|set_tokens|set_batch|set_ckpt", "arg": null}],
    "audit_request": [{"run": "r1"}],
    "release": [{"run": "r1", "ckpt": 16, "price": 10}],
    "price": [{"model": "A1", "value": 12}],
    "delist": ["A1"],
    "serve": [{"gpus": {"G2": 512}}],
    "raise": [{"amount": 300}]}
   ```
4. **动作参数说明**：

   | 动作 | 说明 |
   |---|---|
   | `bid_gpu` | 同型号新报价覆盖旧报价；`lots <= 0` 撤销该型号报价 |
   | `run.gpus` | 省略时占用全部剩余卡；写了字典时，未列出的型号为 `0` |
   | `run.mix` | 省略时 8 个域均匀，`distill` 不参与；键必须是 `pools` 中的名称 |
   | `run.sched` | 省略或取值非法时按 `cosine` |
   | `run.params_b`、`run.tokens_b` | 范围分别为 `0.01..5000`、`0.01..1e6` |
   | `run_ctl.arg` | `rollback` 为存档 id，省略时回最新存档；`set_lr` 为学习率；`set_mix` 为配比字典；`set_gpus` 为卡数字典；`set_tokens` 为目标量，须大于已训量；`set_batch` 为批大小；`set_ckpt` 为存档间隔；其余 `op` 不用 |
   | `run_ctl.op` | `pause` 只对 `running` 生效，`resume` 只对 `paused` 生效；`stop` 后不可恢复；`rollback` 和 `set_tokens` 可让 `done` 恢复运行，`rollback` 也可让 `diverged` 恢复运行；`ckpt` 立即写存档，不扣吞吐 |
   | `hire.salary`、`poach.salary` | 单位 M/tick；`hire.salary` 低于候选人 `ask` 时无效 |
   | `fire` | 本方员工 id，见 `wk.my.staff` |
   | `end_rent` | 租约 id，见 `rent` 动作的 `res` 回执 |
   | `release.ckpt`、`release.price` | `ckpt` 省略时取最新存档；`price` 省略时为 `10` |
   | `price.value` | 范围 `0.5..100`，超出时截断；每个模型每天限改 `1` 次 |
   | `serve` | 设置后持续生效，详见 2.1 节第 5 步；传 `[]` 或 `null` 恢复自动分配 |

   **显式 `serve` 与自动分配互斥**：设了 `serve` 后只用指定的卡服务 API，不再用剩余卡补足。卡数不够时需求送不出去，丢单率上升，声誉和份额都会下降。

5. **结束消息**：收到 `{"type": "end", ...}` 后回复 `{}` 退出。

---

## 4. 终局估值与双赛制正式计分

- **终局公司股权价值**：
  `Value = equity * (cash + 待收卖卡款 + 硬件3年折旧账面 + 机房10年折旧账面 + 96 * 最后730tick服务毛利 + 300 * 已上线模型最高真实能力C)`
  其中服务毛利是 API 收入减去服务卡分摊的租金和电费（电费按 `0` 号地区电价估算），`C` 含已下架模型但不含未上线模型。破产时按 2.2 节的清算价计算。
- **正式评分版本：`scoring_v2`，满分 100。** 单人赛与对抗赛各占 50%。
  1. 单人赛仍与固定 NPC 实验室交互，你固定坐 `0` 号座位，协议里没有赛制标记。每个种子得分为 `100 * C`，其中 `C` 是三年内已上线模型的最高真实能力，取各种子平均得到 `solo`。
  2. 对抗赛固定使用 3 个参考对手，每个种子交换座位。每局得分为：
     $$s = 50 + 50\tanh\left(\frac{Value_T-Value_R}{4846.39}\right).$$
     对全部对手、种子与座位等权平均得到 `adv`；收益相同为 50 分，参考对手不固定为满分。
  3. **总分** = `0.5 * solo + 0.5 * adv`。每份提交默认运行 `64 + 3 * 64 * 2 = 448` 局；N 个场景共 `7N` 局。
- 对抗映射尺度 `4846.39`（M）来自独立完整基线对局的正绝对分差中位数除以 `atanh(0.6)`，在评分前冻结；不根据参赛程序成绩调整。
- **世界盐值**：世界由种子和盐值共同生成。本地工具用 `practice`，正式评分用私有盐值，所以同一种子在本地和正式赛的世界不同。

- **每次随机、多局平均**：每次评测重新均匀抽取 N 个互不重复的场景种子，默认 N=64，可增加场景数。参考对手池、生成规则、完整局长及运行限制固定；同一批提交共享本次种子并交换座位/角色，对手、场景、座位分别等权。不会选择多次运行中的最好成绩，也不跨次缓存成绩。每个 seed 直接从操作系统安全随机源抽取（secrets/SystemRandom），去重并排除校准种子，不经固定主种子扩展；报告保存完整 seed 列表，供重放核查。
- **统计与版本**：成绩包含 `scoring_version = "scoring_v2"`、`score`（0–100）、`max_score = 100`、分项、原始指标、出错局数和 95% bootstrap 区间。区间按完整种子块重采样，保留同种子下座位、对手与赛制之间的相关性；只有一个种子时不报告区间。
- **可比性**：报告包含冻结配置、引擎、对手及提交源码指纹。旧版成绩和练习成绩不进入新版正式汇总；不同配置指纹的成绩不直接混用。这里的百分制表示本题固定评测下的表现，不是最优率或胜率。
- **本地自测**：`scripts/spar.py` 同样默认随机抽取场景，使用 `--seed-count N` 指定数量，使用 `--seeds 123 456` 重放。公式与正式赛一致，但对阵公开基线，始终标记 `official = false`。正式入口中显式指定 `--seeds` 的重放或缩短局长的运行也标为非正式。


---

## 5. 本地自测

公开基线位于 `agents/baselines/`（`blank`、`scaling`、`merchant`），单人赛背景对手位于 `engine/solo_npc.py`，协议起步模板位于 `agents/starter.py`。

```bash
python scripts/spar.py --list
python scripts/spar.py ../submissions/ai_training.py
python scripts/spar.py agents/submissions/my_agent.py merchant --ticks 2000
python scripts/spar.py agents/submissions/my_agent.py --solo --seed-count 1
python scripts/spar.py agents/submissions/my_agent.py --all --seed-count 1
python scripts/run_match.py agents/submissions/my_agent.py agents/baselines/merchant.py --seed 1 --ticks 2000 --out replays/s1.json
```

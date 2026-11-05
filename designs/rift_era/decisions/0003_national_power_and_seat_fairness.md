# 0003：抗刷分原始国力公式 $V$、四纪元可持续积分与 8 国影子反事实座位公平

状态：已采纳并合入设计规范（尺度常数 $\tau$、要素影子权重系数 $(\alpha_E, \alpha_L, \alpha_K)$ 与 8 风格参考策略池强度仍待原型实现后在独立校准集上实测冻结）。

## 背景与旧语义（`v0.1`–`v0.2`）

- `07_scoring_and_resources.md` 确定了连续 `0–100` 分、OS 安全随机源默认 64 个互不重复 seed 平均及 `50 + 50 * tanh(...)` 候选映射方向，但原始国力 $V$ 的五大分项尚无精确数学定义、单位、权重与反套利规则。
- 8 国非对称超大陆地图下，参考值 $V_{\text{reference}}$、64 seed 对应的场数成本、座位运气消除机制及防“自杀式焦土搅局”机制均未锁定。

## 新语义

1. **静态目录影子价值表 $w_g^{\text{cat}}$（切断价格操纵）**：
   - 所有生产增值、物流服务与外贸交割估值统一使用由 [../data/industry.json](../data/industry.json) 静态配方图唯一派生的公开影子权重 $w_g^{\text{cat}}$，完全独立于局内现货成交价。废钢/废铜影子权重固定为钢材/铜材的 `0.45`，配合拆解仅返还直接建造金属 `50%` 的规则，使“反复建拆设施”净增值为严格负数。
2. **单周期瞬时国力 $U_p(c)$ 五大支柱（每周期 `c = 0..959` 结算）**：
   $$U_p(c) = 0.25\, U_p^{\text{pop}}(c) + 0.25\, U_p^{\text{ind}}(c) + 0.20\, U_p^{\text{ctrl}}(c) + 0.15\, U_p^{\text{tech}}(c) + 0.15\, U_p^{\text{intl}}(c)$$
   - **国民福祉与本土安全 `25%`（$U^{\text{pop}}$）**：按地区治理合法性 $\psi_{\text{legit}}$、人口规模、实际入户消费满足度 $Q_{\text{life}}$（口粮 `< 80%` 二次方骤降，民用品与医疗凹函数边际递减）与安全系数 $Q_{\text{sec}}$ 结算。
   - **可持续净生产与净物流增值 `25%`（$U^{\text{ind}}$）**：按产权份额 `owner_shares[p]` 核算产线净要素增值，乘以消纳系数 $\eta_{\text{sink}}$（国内库存超出近 `20` 周期实际平均消耗 `6` 倍的超额累库部分计分衰减至 `0`）；物流仅计同一走廊同品类**单向净物理流量** $|\text{Flow}_{u\to v} - \text{Flow}_{v\to u}|$，往返空转对冲为 `0`。
   - **有效战略空间、基建与战备控制 `20%`（$U^{\text{ctrl}}$）**：摒弃纯格子面积计分，仅对实际具备采掘产出、通水通电城市槽位或全球净航道流量的陆海分区赋权（无人无矿荒漠/远洋 $\sigma = 0$），加上完好跨区基建、在轨工作卫星与后勤可承载范围内的现役战备力量。
   - **已落地技术应用深度 `15%`（$U^{\text{tech}}$）**：完成科技验证仅得该阶权重的 `15%` 知识底分，其余 `85%` 按当期真实投产运行规模（含 `R06` 对外授权并实际运转的规模）线性解锁。
   - **国际履约信用与外部耦合 `15%`（$U^{\text{intl}}$）**：以国际信用 $\text{Credit}_p \in [0.1, 1.0]$ 为总乘子，统计各品类净互补外贸交割额 $|\text{Exp}_{p,g} - \text{Imp}_{p,g}|$、对 `36..48` 个中立城邦的真实依托度 $\text{Align}_{m \leftarrow p}$ 与已兑现共同防御/物资互助实绩。
3. **四纪元轨迹积分与终局可持续性锚点**：
   $$V_p = 0.08\,\bar{U}_{p,\text{I}} + 0.14\,\bar{U}_{p,\text{II}} + 0.24\,\bar{U}_{p,\text{III}} + 0.29\,\bar{U}_{p,\text{IV}} + 0.25\, U_p^{\text{end}} \cdot \Phi_{\text{sustain}}(p)$$
   其中 $U_p^{\text{end}}$ 为最后 `40` 周期（`c = 920..959`，跨两次设施维护结算点）均值，$\Phi_{\text{sustain}}(p) \in [0.35, 1.00]$ 扣除终局维护欠账、口粮/电力/军需断供、核心区动乱与破产违约惩罚。
4. **分层轮换座位 + 同种子全参考影子局反事实基准**：
   - 64 个种子中参赛策略座位按 $s_k = k \bmod 8$ 严格平衡轮换（每座位各 8 次），其余 7 座由 `seed_k` 从 8 风格固定参考池确定性洗牌填充。
   - 每个种子对应 1 场无外部 IPC 的纯引擎内**全参考影子基准局**（同批评测多选手可全局复用）与 1 场**选手正式评测局**，按混合净优势映射得分：
     $$\Delta V(k) = 0.70 \cdot \big(V_{\text{player}}(k, s_k) - V_{\text{shadow}}(k, s_k)\big) + 0.30 \cdot \big(V_{\text{player}}(k, s_k) - \bar{V}_{\text{peers}}(k)\big)$$
     $$s(k) = 50 + 50 \cdot \tanh(\Delta V(k) / \tau), \qquad S_{\text{final}} = \frac{1}{64}\sum_{k=0}^{63} s(k)$$

## 受影响文件与迁移方式

- 更新：[07_scoring_and_resources.md](../07_scoring_and_resources.md)、[08_open_questions.md](../08_open_questions.md)、[README.md](../README.md)、[CHANGELOG.md](../CHANGELOG.md)、[proposals/E-national-power-scoring-and-seat-fairness.md](../proposals/E-national-power-scoring-and-seat-fairness.md)。
- 详细推导与防套利证明保留在 [../proposals/E-national-power-scoring-and-seat-fairness.md](../proposals/E-national-power-scoring-and-seat-fairness.md)。

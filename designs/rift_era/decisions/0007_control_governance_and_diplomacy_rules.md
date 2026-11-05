# 0007：六维控制权状态转移矩阵、三级可预测治理预警与结构化外交及城邦效用模型

状态：已采纳并合入设计规范（支持度弹性与城邦 EMA 平滑时间常数可在原型测试中微调）。

## 背景与旧语义

- 先前文档提出了六层控制权分离、国内支持度响应、结构化外交合同与中立城邦合作的原则，但尚未锁定占领/国有化/合资对第三方权益的精确状态转移矩阵、治理预警阈值方程、不可抗力违约界定及城邦防瞬时贿赂决策函数（对应 `Q13`、`Q14`、`Q15`）。

## 新语义

1. **六维控制权完整状态转移矩阵**：
   - 严格区分军事占领（仅变更 `controller` 并冻结敌国国有经营权，完整保护中立第三方 `owner_shares` 与商业通行权）、强制国有化（没收外资份额但立扣 `-0.30` 信用并赋予受害国合法对等扣押权）、合资经营（`owner_shares` 毫单位守恒切分，`P7` 自动分账）与主权移交（须和约割让或满 `240` 周期高支持度整合）。
2. **全透明国内治理递推方程与三级预警机制**：
   - `local_support` 完全由可观测的口粮、民用品、医疗、就业、税率、动员疲劳、污染与地方自治留成线性/有界递推驱动，设置黄警（`< 450` 降效迁移）、橙警（`< 300` 罢工抗税）与红警（`< 150` 满 6 周期治理瘫痪）及确定性恢复路径，零随机刷怪惩罚。
3. **九类结构化合同条款、不可抗力豁免与城邦 EMA 效用函数**：
   - 严格拆分引擎托管直执条款与主权行为承诺；若卖方已足额锁仓而纯因第三方战火切断走廊，判定为 `VERIFIED_FORCE_MAJEURE` 退还预付款并豁免恶意违约重罚。
   - `36..48` 个中立城邦按近 `20` 周期 EMA 贸易互利、刚需保障、安全制衡与国际信用确定性选择合作伙伴，免疫终局前瞬时贿赂。

## 受影响文件与迁移方式

- 更新：[01_world_and_realtime.md](../01_world_and_realtime.md)、[02_economy_logistics_governance.md](../02_economy_logistics_governance.md)、[03_warfare_diplomacy_intelligence.md](../03_warfare_diplomacy_intelligence.md)、[08_open_questions.md](../08_open_questions.md)、[CHANGELOG.md](../CHANGELOG.md)。
- 详细矩阵与方程见 [../proposals/D-control-transitions-governance-and-diplomacy.md](../proposals/D-control-transitions-governance-and-diplomacy.md)。

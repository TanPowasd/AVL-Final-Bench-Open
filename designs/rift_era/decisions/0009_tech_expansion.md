# 0009：科技树扩增至 120 节点、6 阶、11 主干

- 日期：2026-11-03
- 状态：已采纳为候选规范（数值待原型验证）
- 详细推导：[../proposals/G-tech-expansion-tiers5-6-and-new-branches.md](../proposals/G-tech-expansion-tiers5-6-and-new-branches.md)

## 背景

用户要求扩增科技线，“作为最终测试，多难都不为过”。原 64 节点树按理论点总量计算可在单局内大部分完成，路线取舍压力不足。

## 决定

1. 新增 5 阶与 6 阶研究档位，对应新验证物资 `frontier_pack`（P100，需 R07）与 `breakthrough_pack`（P101，需 R09）。
2. 原 8 条主干各增 4 个节点，新增 `S` 网络与安全、`V` 环境与气候、`F` 金融与制度三条主干，各 8 节点，合计 120 节点。
3. 新增 S5 前沿综合设施（配方等级 4）、`additive` 增材线模式、23 种物品与 28 个配方。
4. 新增节点字段 `exclusive_group`，以及顶层 `exclusive_groups`（`science_doctrine`、`economic_doctrine`）。`tools/catalog.py` 校验组成员的一致性，并生成互斥制度表。
5. 全球累积排放公共存量与 TierWeight `1, 2, 4, 7, 11, 16` 作为候选机制写入提案 G，待冻结。

## 后果

- 全树理论点总量由约 51,680 增至约 545,600，一局内无法完成。
- 网络、轨道、气候、金融四类外部性要求参考策略池具备相应的攻防能力（Q23）。
- 引擎状态与观测体积增加，需要重新核算 `init`、`obs` 体积上限（Q22）。

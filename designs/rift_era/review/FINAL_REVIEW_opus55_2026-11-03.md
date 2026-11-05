# 裂陆纪元规则终审（Opus 5.5）

- 审核人：Claude Opus 5.5（Claude Code），应用户要求做最终审核。
- 时间：2026-11-03 12:45 UTC 开审，13:08 UTC 完成核对后写入本文件；13:14 UTC 按用户决定补记下一节。
- 边界：只读审核。没有修改任何正文、`data/*.json`、工具、原型或 `review/LOG.md`；本文件是本次唯一的写入。审阅在开审时复制的快照上进行。

## 补记：用户已定口径（2026-11-03 13:14 UTC）

用户在本终审线程中确认：**任何损人行动都必须严格扣分，包括行动方自身没有其他代价的情况**；实现方向是把作战和敌对行动的消耗按目录值记为自身损耗。这条是对用户原有要求"损人不利己在任何比例下都要扣分"的明确化，属于已确定的用户要求，取代 07:96 的"不承诺无成本伤敌严格扣分"。

修订时按以下要求执行（OR-001、OR-011 的关闭条件以本节为准；OR-002 仍按 §3 原文处理）：

1. 部队射击、行军（含不交战的机动与巡逻）消耗的弹药、燃油、电池，以及敌对行动耗用的物资（如 S03/S08 的网络战与电子战套件、S06 假目标、C12 密码分析、反卫星与高超声速打击的弹药），一律不进入 eta 的消耗分母、不生成运输或出口回执，改按目录值计入行动国自己的损耗。
2. 07:96 改写为：固定影子时，任何敌对行动都使自身 V 严格下降；删除"不承诺无成本伤敌严格扣分"。README 的"已确定的用户要求"同步记下这条口径。
3. 维修件只能按一种方式处理：若按 07:27 加回 book，就不再另记损耗；若记损耗，就不加回 book，避免同一份材料扣两次。
4. 补断言：无收益的持续炮击使 V 严格下降；信用已在 0.1 下限时，零收益的网络攻击仍使 V 严格下降；绕圈行军不增加 V；维修不重复扣分。
5. 已知代价：防守方开火同样记损耗。正常的封锁、断能和战争，只要换来的领土、出口份额、城邦合作国等收益大于消耗，仍可净得分；具体系数须在正式引擎上做攻防平衡测试，确认防守方不会被罚得过重。

## 0. 审的是哪个版本

- 目录 `designs/rift_era/`，git HEAD `0cab57e`，加上工作区未提交的改动（Round 4、Round 5 的修订都还没有提交）。
- 开审时对 87 个文件（不含 `prototype/results/` 与 `__pycache__`）计算 SHA-256。这份按路径排序的 87 行 `sha256sum` 清单本身的 SHA-256 为 `0b8c61933d0cd7e623362960a70e7f67002a3cc657adfd8d2e06ff8552d88354`。规则、数据、说明与日志文件的全量指纹见附录 A。
- 与日志对照：23 个规则/数据/说明文件与 LOG 中 Astra Round 5 的指纹表、以及 Round 4 表里本轮未改的文件逐一一致。**本次审的正是 Gemini Round 5 写下"审核通过"的那一版。** 派单说明里"Astra 正在改第 4 轮"已经过时：开审时 Round 5 修订和 Gemini 复审都已写进 LOG。
- 13:08 UTC 写入前重算一次，87 个文件与开审时完全相同，审阅期间无人改动。

方法：逐章通读 00–11、6 份 JSON 和 LOG 的全部轮次；在快照上独立运行 `tools/catalog.py check` 与 `tools/coupling.py check`（均 PASS；两个工具都自述只查目录、引用和图，不查玩法、数量与平衡）；用一份只读脚本按 JSON 复算关键数字（附录 B）。没有运行原型长局，也没有写引擎。

## 1. 结论

**不通过。不建议以当前版本冻结规则或移交正式引擎实现。** 本次记 P0 3 项、P1 8 项、P2 6 组。

计分的骨架是对的：分数只取决于选手自己的 V 与同 seed、同座位全 Super 影子局的 V，他国 V 的变化不直接进分数，经典的"把别人打烂来抬相对分"已经堵住。问题出在 V 的记账，以及支撑"正常竞争"的那几块规则：

- 开火、行军耗掉的弹药燃油被当作真实消费，给整条生产链带来满额增加值；另有几项科技的落地分直接按"对他国实施敌对行动的次数"计。两者都让"损人"可以直接换分，违反用户定下的防焦土硬要求（OR-001、OR-002）。
- 判定谁与谁交战的敌对/战争状态机没有定义，作战引擎无法确定地选目标（OR-003）。
- 封锁怎么执行、电站和线路被打坏后产能怎么降、怎么修，都没有规则；"封锁、断能能得分"既没有机制链，也没有测试，而封锁方自己的咽喉通量分反而会下降（OR-004、OR-005）。

GR-001～003 及 P2-001 确实修好了（§4 有独立核对），但 Gemini Round 5"AR-001～046 共 46/46 全部 CLOSED、未发现新增冲突"的结论不成立。

## 2. 对两条用户硬要求的判定

### 2.1 防焦土："损人不利己在任何比例下都要扣分"——不满足

成立的部分：`score = 100·R^1.2/(R^1.2+1)`，`R = Ṽ(V_player)/Ṽ(V_shadow)`（07:94–96，scoring.json `relative_scoring`）。影子是固定的，所以只要一次行动让自己的 V 下降，不论给他国造成多大损失，分数都严格下降。11 个测试向量用两种写法复算一致（附录 B[6]）。

不成立的部分：

1. 自己的 V 会因为"损人"而上升：作战耗弹被算作真实消耗（OR-001）。
2. 有的科技分就是"对他国做了几次敌对行动"（OR-002）。
3. 07:96 明写"自身V不变分数不变……不承诺无成本伤敌严格扣分"，而 README:14 记录的用户要求是"损人不利己在任何比例下严格亏分"。弹药等库存不计入 V，信用有 0.1 的下限，所以零自身成本的伤害现在分数不变（OR-011）。

### 2.2 "封锁、切断能源这类正常竞争要能得分"——未证明，且有直接项方向相反

- 计分里和封锁直接相关的只有受害方 Qsec 中的 Blockade 项（07:19），封锁方没有任何直接回报。
- 封锁方分区的 sigma 含 `.02·Phi_chokepoint`，Phi 按"通过该区消环路径的末端回执目录值"计（07:43）。封锁截断流量，封锁方自己的这一项会下降。
- 能否得分只剩间接渠道：出口份额转移、城邦唯一合作国（03:88–90）、世界池的有限资金。规范没有量化这些渠道，也没有一条场景测试；scoring.json 的 `legitimate_blockade_gain` 只是"假设 V=115"的映射算例，不是机制证据。
- 封锁本身如何发生（OR-004）、打坏电站或输电线如何影响产能（OR-005）都没有定义，"断能"在规则层面还无法发生。

## 3. 问题清单

每项列：级别、位置、问题与证据、关闭条件。行号对应附录 A 指纹的版本。

### P0

#### OR-001 [P0] 作战消耗算作"真实末端使用"：开火、行军成了没有上限的增加值出口，炮击他国或城邦可以净增国力

- 位置：11:53；07:23、07:25；scoring.json `pillars.ind.eta_sink.consumption_definition`；03:9、03:43、03:84。
- 机理：`VA_eff = 批量 ×(Σ产出·w·eta − Σ投入·w)`，eta 由库存与 20 周期真实消耗之比决定（07:25）。产出没有去处时 eta→0，VA 变成负的投入值；只要被"消耗"，eta=1。11:53 把"部队真实射击/行军/维修定额内消费"列为末端使用，于是任何军需都可以靠开火或行军烧掉，换回满额 VA，同时生成运输回执（NetTransit）。施工消耗被限制在非施工消耗的 25% 以内（07:25），作战消耗没有任何上限。
- 复算（附录 B[2][3]，均按 data/*.json）：
  - 12 辆 armored 的编队持续行军（`patrol` 任务即可），每周期烧油 144，相当于 P006 48 批。相对"不生产"，ΔVA = +264/周期，U 约 +23/周期；相对"生产但不消耗"，差额达 630。作为对照，开局整国的人口支柱在 U 里只占约 27。
  - 12 门 artillery 对一个有可见设施的分区持续 bombard，每周期耗弹 240，弹药链（P044+P001+P007，不计采掘）VA 约 542，U 约 +47/周期。代价只有每周期 Credit −0.05（03:43），18 个周期后到达下限 0.1（03:84），之后继续炮击没有边际信用代价；规则也没有禁止炮击中立城邦（见 OR-003）。
- 后果：违反"损人不利己必须扣分"；"巡逻绕圈""对弱目标持续炮击"会成为中后期的刷分路线，抵消 eta 防囤货的设计初衷。
- 关闭条件（建议）：射击、行军、维修消耗不进入 eta 的消耗分母，不生成运输或出口回执，改按目录值记作自身损耗。这样每次敌对行动都有严格为正的自身成本，顺带解决 OR-011。如需保留和平战备的消耗价值，只承认不需要目标、按 MilCap 封顶的训练定额。补三条红队断言：无领土/市场/城邦收益的持续炮击使 V 严格下降；零收益的网络攻击使 V 严格下降；绕圈行军不增加 V。

#### OR-002 [P0] 几项科技的落地分直接按"对他国实施的行动次数"计

- 位置：scoring.json `pillars.tech.active_scale_rules`（S08 `paid_systemic_actions`、C12 `paid_cryptanalysis_actions`、S06 `paid_decoy_actions`、V08 `paid_climate_actions`）；07:49 的科技式与 07:55 起的计量表。
- S08 是对单一目标国的网络+电子战+欺骗打击，必然归因（03:66）；C12 读取他国未升级 S05 研究所的机密项目（03:64）；S06 往他国观测里投假目标（03:66）；V08 会让下风向 3 个分区反向变化，可能落在他国（02:185）。这几项的落地规模只能靠对外实施获得。满额时各给 U 约 0.84（S06 约 0.42，附录 B[4]）。
- 量不大，但性质是"损人即得分"，与防焦土原则直接冲突。
- 关闭条件：改用防御性或在线能力计量（例如具备并供电的装置、覆盖的本国枢纽），V08 只计对本国或已签约分区的干预；不以对他国实施的次数计分。

#### OR-003 [P0] 没有敌对/战争状态机，"敌单元"无法判定

- 位置：03:29、03:33（"敌对""敌单元"）；03:79–84；06:173（declare_war）；02:127（"本国处于战争"）；10 的 C25–C27。
- 缺少：两国何时处于交战（是否必须先 declare_war，不宣而战是否允许、后果为何）；战争如何结束（truce_dmz 只写"到期停战"，和约只出现在 01:49 的控制权表里，没有媾和合同）；"被侵略方/主动宣战方"如何判定；能否攻击城邦，AnnexCount 里的"吞并"走什么程序；盟友、防务条约成员和中立第三方在交战区内是否自动交火。
- 影响：P4 选目标、Qsec 的 Combat、金融危机触发、C25/C26/C27、防务条约、城邦 security 里的"非敌驻军"都依赖这一谓词。AR-021（P0）已被标为关闭，但"目标"的前提没有定义。
- 关闭条件：写出有限状态机（和平、交战、停战、同盟等）及进入与退出条件，给出各状态下的自动交火与命令合法性矩阵（含城邦与中立第三方），并让 01/02/03/06/10 中所有"交战/战争"都引用这一状态。

### P1

#### OR-004 [P1] "封锁"没有执行规则，对封锁方的直接计分项是负的

- 位置：07:19、07:43；01:34（边有"封锁/损毁状态"但没有转移规则）；02:189（"海空破交封锁"）；06:152（corridor_policy）。
- Qsec 的 Blockade 是"入区走廊按正常容量加权的敌方阻断运力比例"，但"阻断"怎样产生没有规则：只有走廊控制方能用 `access_tier=0` 关闭自己控制的边；海上封锁、港口封锁、截击运输车船（Shipment 没有 HP 与火力，不能被攻击）都没有定义。
- 关闭条件：定义封锁与破交的执行方式（控制边、海域控制、对 Shipment 的截击与损失分配）以及阻断比例的计算；给出至少一组场景测试，证明有效封锁能经由出口份额、城邦或通行费让封锁方 V 上升，并处理 Phi 项的方向。不得为此重新引入"他国 V 下降即加分"的相对项。

#### OR-005 [P1] 设施与走廊的战损效果、毁坏/断边阈值和修复流程都没有定义

- 位置：03:43（附伤"同区设施/边按剩余耐久同比"）；03:54（N10 毁线、外壳损失 25% 耐久）；11:49（"途中断边"）；07:27（"维修材料加回book"）。
- 未定义：耐久下降对产线可用率、对走廊容量的影响；耐久降到多少算毁坏、算断边；设施和走廊怎么修、耗什么料、多少工期、用哪条命令。部队修理有规则（03:49），设施和走廊没有。
- 影响："打电站、断线路"这类切断能源的竞争无法确定地实现；07 的"维修加回 book"没有对应的成本定义。
- 关闭条件：补"耐久→可用率/容量"函数、毁坏与断边阈值、设施和走廊的修复配方与工期，并在 06 增加对应命令。

#### OR-006 [P1] NFA 按当期汇率折算本币基线与发行负债，单纯改变本币汇率就能改变分数（AR-008 标题中的"估值缩债残余"未闭合）

- 位置：07:80（NFA 式）、07:86；02:83（"彻底封死印钞换汇刷分"）；02:72、02:203（汇率方程与央行干预项）；01:98（e∈[0.05, 20]）。
- `NFA_p = Σ_q Cash_p[q]·e_q − 150000·e_p + … − CB_Issued[p]·e_p − …`。记本国本币净空头 `D = 150000 + CB_Issued − Cash_p[p]`，则 e_p 每下降 Δe，NFA 增加 `D·Δe` RU；本币净多头的国家反过来，升值即加分。e_p 受本国央行干预（I 项每周期最多 ±0.03）、资本流动、危机项和本国能影响的 CPI 驱动。
- 静态算例（附录 B[5]）：国库 100000 本币按 e=1 换成 99800 RU，之后若 e 降到 0.5，NFA 从 −200 变为 49800 RU（U 约 +4.4）；降到下限 0.05 则为 94800 RU（U 约 +8.3）。对外头寸没有任何实际变化，分数却变了。汇率方程里的 PPP 锚会拖慢这一过程，大幅贬值也伴随通胀等真实代价；但这属于度量单位可以被本国操纵的缺陷，不是 07:86 所说的"跨时间投机"。
- 关闭条件：本币基线与 CB_Issued 不按当期可操纵的汇率折算（例如按开局或发行时汇率冻结，或 NFA 只计对外资产负债），补断言"只改变本币汇率不改变 NFA"；同时删去 02:83 的"彻底封死"。

#### OR-007 [P1] NetTransit 每张回执计多少没有定义（AR-002 残余）

- 位置：07:23、07:33；scoring.json `transit_definition`。
- 现文只规定承运人分得"距离份额"及其上限，没有说一张回执贡献多少：是距离，物量×距离，还是物量×w×距离？距离以 L1 格计还是以边数计？历史提案 E 用的是 `|flow|·w·dist`，但已改为回执制，旧式不再适用。U_ind 因此不能唯一实现。
- 关闭条件：在 07 与 scoring.json 写出 NetTransit_p 的完整公式与单位，补测试向量。

#### OR-008 [P1] 同一层级的正文仍有直接冲突（AR-009 等已被标为关闭）

1. 强平顺序：02:129"跌破维持线者在下一次竞价按账户 ID 升序强平"；11:37"强平优先档同档比例分配，不按账户ID先后抢世界池"。
2. 做空借款利率：02:9 的 `ShortDebt[p,q]` 按 i_q 计息、02:130 写年利率 i_p；02:211 写外币做空借款年利率 max(0, i_q+2)。02:130 还写成单下标 `ShortDebt[p]`。
3. 钉住：02:130"钉住国央行必须用储备承接"；02:203 的钉住只是受 ±0.03 夹取、池或储备不足即缩量的自动干预计划。

00 章规定同层冲突是缺陷。关闭条件：改写 02:129、02:130，统一做空借款利率与账户下标。

#### OR-009 [P1] 人口只能被动迁移，没有国家组织的迁居手段；与 Hub0 集中开局叠加，其余核心 Hub 无法正常开发

- 位置：01:16（全部平民在 Hub0，Hub1–4 无居民）；02:165–168、11:57（迁移只由黄警、橙警触发）；06 命令表（没有迁居命令）；01:74、11:9（劳动按枢纽本地供给）。
- 要在 Hub1–4 开矿开厂，只能让 Hub0 支持度跌破 450 触发外流，或者"在 Hub0 征兵→开到 Hub1→解散"，把军人变成当地居民（02:64、06:214）。两条都是扭曲玩法。
- 关闭条件：增加有成本、受客运运力约束的迁居命令（复用 Passenger Shipment），或者调整开局模板。

#### OR-010 [P1] 并发调度按"每局 1 核"计算 Kmax，与每局实际的 CPU 需求不符

- 位置：07:104（`Kmax = min(C−reserve, floor((Mavail−Mreserve)/Mmatch))`，内存按"引擎峰值+活跃策略数×1GiB"计）；06:28（每个策略 1 核）；06:55（墙钟时限）。
- 每局至少有引擎和选手策略两个进程，参考策略若走同一协议还会更多；CPU 项却只按局数扣核。策略时限按墙钟计，超订会造成非选手原因的超时，同一份提交重跑结果不同，也背离"给系统留余量"这条用户要求的本意。
- 关闭条件：CPU 项改为 `floor((C−reserve)/每局核数)`，每局核数 = 引擎实测 + 活跃策略数。

#### OR-011 [P1] 零自身成本的伤害分数不变，与用户"任何比例都扣分"的字面要求差一步

- 位置：07:96；README:14；03:84（信用下限 0.1）。
- 弹药、网络套件等库存不计入 V；信用到下限后不再下降；超出 MilCap 的部队战备不计分。因此"损人而自身 V 不变"的行动分数不变，而不是严格下降。
- 关闭条件：请用户确认"零自身成本时分数不变"是否可以接受。若不可以，按 OR-001 的建议把敌对行动的消耗按目录值计入自身损耗，使每次敌对行动都有严格为正的 V 成本。
- 13:14 UTC 补记：用户已选"必须扣分"，按文首补记一节执行。

### P2（不阻塞，下一轮顺手修）

- **OR-012** 07:27 的算例数值有误：按 industry.json 与 scoring.json，S1 建材目录值应为 108.5672（文中 113.5664），完好拆解损失应为 100.2422（文中 105.2414）；同句 P066 的 −0.950 是对的（附录 B[1]）。
- **OR-013** 07:23–25 没有写明 eta 的库存与消耗是逐物品还是全部物品合计，也没有写明采掘场产出是否计入 `Σ_f VA_eff(f)`（sigma 已按采掘量计过一次）。
- **OR-014** 用户要求的"同批提交共享评测条件"只出现在 README 与历史决策 0003；07 没有定义"批"，也没写同批全部提交使用同一组 64 个 seed 与同一版本。
- **OR-015** 04:17：解锁 R05+C02 后，超过 4 个 Hub 时"其余分别以单Hub .75汇总"，把实验室分散成每个 Hub 一条线就能完全绕开 N^α 递减。
- **OR-016** 世界池按 10·w_cat RU 对每种库存货每国每周期收购 40 单位（02:121），资金有限且同批比例缩量，开局会出现抽干竞赛；卖给世界池既能保住 eta 又能增加 NFA，平衡测试时需要专门观察。
- **OR-017** 零散的不一致与缺口：
  - 06:131 写"每周期初自动触发"，06:168 写"单窗口最多触发 1 次、冷却 5 tick"；
  - 11:64 的 `ERR_NO_VISIBLE_TARGET` 不在 06:233 的标准错误码表里；
  - 06:176 的 orbital_launch 只有 plane 与 slot，N12 扩为每槽 2 位后无法指定第二位；
  - industry.json 中 P090 的条件"Q_life 食品多样性项按 70% 计入"，与 02、07 的 θ>0.30 线性封顶表述不同；
  - 04:270 把 A01 水库写成"G2 调蓄水库"，06:216 则是 `install_system` 的 reservoir；
  - AI 部署：06:190 按全国 C̄_p 校验，04:342 要求按 Hub 本地预算；
  - 03:43 bombard 的"支持 −300mU"没说是谁的支持度；Qsec 的 Combat 记在射击方分区还是目标分区也没说；
  - 06:152 的 `toll_rate_bp` 没有定义计费基数；
  - 04:406 的 NPC 推理需求是否算服务出口没有写明；
  - 居民按参考价采购时，多个供货方之间如何分配没有规则；
  - CHANGELOG 顶部"v1.4 规范冻结"一段仍是旧公式（R_k 含 q_eff、scrap_steel=1.575、Bid-Stack 出清、分枢纽启动包）；STATUS 与 README 仍写"待 Gemini 复审"。

## 4. 对既有审核结论的复核

**独立确认已修好：**

- GR-001：05:60、05:64、05:516 核泵只读上一拍、不回填；05:102、05:118 与 runtime_rules 的 `grid.storage_auto_policy` 一致；05:71 黑启动只允许已选放电的储能参与。
- GR-002：06 的实体上限、永久 seq 水位、256KiB/16MiB 分页、四个纯域、情报 op 枚举，与 runtime_rules、military.json、11 一致。
- GR-003：coupling.json 的 E76、C20 与 02:117、02:204 一致。
- P2-001：01:18 与 runtime_rules 的 `legacy_vehicle_tech_exemption` 一致。
- 开局静态预算复算一致：生产用工 84、生产用电 23.4、总用电 35.78、用水 31.156、用煤 18.156、劳动 124.5 ≤ 126.1（附录 B[7]）。
- 11 个评分测试向量、债限式 `max(1000, 2·Σ20Tax)`、穿透权益恒等式、P066 的 −0.950，复算一致。

**不同意"46/46 全部 CLOSED、未发现新增冲突"：**

- AR-009 已标关闭，但 02:129 与 11:37 仍然冲突（OR-008）；
- AR-008 标题中的"估值缩债残余"仍在（OR-006）；
- AR-002 要求的"五支柱可执行完整函数"缺 NetTransit 计量（OR-007）；
- AR-021 的"目标"缺少敌对状态这一前提（OR-003）；
- AR-006 关闭时依据的 07 算例数值有误（OR-012）；
- Astra 在 Round 5 已指出 Gemini Round 4 报告把 AR-013、014、023 的规则写错，说明部分关闭依据是复述，而不是对原文的核对。

建议把 Gemini Round 5 的结论限定为"GR-001～003 及 P2-001 已闭合"，不作为整体通过。

## 5. 修订建议（按优先级）

1. OR-001、OR-002、OR-011 一起改：作战消耗改记为自身损耗（口径已由用户确定，见文首补记），敌对行动类科技改用防御或在线计量；补"无收益持续炮击使 V 严格下降""零收益网络攻击使 V 严格下降""绕圈行军不增加 V"三条红队断言。
2. OR-003：补敌对/战争状态机，再核对 01/02/03/06/10 所有"交战"引用。
3. OR-004、OR-005：补封锁、破交与战损/修复规则，并为"封锁、断能能得分"写能在正式引擎上运行的场景测试（含 Phi 项的方向）。
4. OR-006、OR-007：修 NFA 折算与 NetTransit 公式，补测试向量。
5. OR-008～010 与 P2 各项清理。

修完后只需针对上述编号做一次定点复核，不必重走全量轮次。

## 附录 A 开审快照指纹（2026-11-03 12:45 UTC）

规则、数据、说明与日志文件：

| 文件 | SHA-256 |
|---|---|
| `00_design_principles.md` | `a7fb9c36a49c957c2ade24a85f924e4deb72a6d1db0bee5557aaf57e6f7c2acd` |
| `01_world_and_realtime.md` | `e269d2dd01410f31c9eb479aed1dad2304b2fc10ff9da52802d7c43c7f9634bd` |
| `02_economy_logistics_governance.md` | `0d8a82ac566f486bafc9d6ca4c9a41917eece8ebb2df977c9759a1c8405dcbf5` |
| `03_warfare_diplomacy_intelligence.md` | `3662e0fee455c6957946aa301eb100afd24b09f0bfa6693b14a253aeb9eb4755` |
| `04_technology_tree.md` | `ce5d7c2dd0dd4c77b069b932a53676be2cfb8d35e45545e309af5b80d3475269` |
| `05_industry_system.md` | `cd3a54858d368976d1479b78796804d7bc11c0923c73567e1d602a1aaa9c873d` |
| `06_agent_interface.md` | `fc5d629a7fe04b32ea626fa7307eae4d860fc5a93a3d386d37d295a0874d3b74` |
| `07_scoring_and_resources.md` | `c333ff9a9f4af80e00f36c9305083dcdc873d6b881fa06dc9939f1752567c82e` |
| `08_open_questions.md` | `5365f744d0e1237b7d53d2eb588520f279d37ad25e0debb616e20209c09bb105` |
| `09_collaboration_plan.md` | `8ce22fcf544d470ca6eabe8df04fe1d3777dd7605d16c795bfc4f82e205b1e0d` |
| `10_system_coupling.md` | `47aaca74cdab0a6f7737262dc1469ec215c98565442b857d5f3a64415305fefa` |
| `11_execution_contract.md` | `5cf98a7e0a5415244ed55a5a7a7ed26df16969f3a8d79d61432d1677645eeb0f` |
| `AGENTS.md` | `39bbc1d0d7ec7d83e47a104f53e724d2728b62e19fc43be4d5ac96210690e4b3` |
| `CHANGELOG.md` | `c27e1d96b80bdf9e21b5c6a1d3e6090ac88ba8203d40982e0cd4eaf7136f934f` |
| `README.md` | `00f32f65e9bdefa708de21a46805a1067468a92fca424c72a928e76f81743213` |
| `STATUS.md` | `47dc5980b9f2356bb79206c6e4bbcc5fc391f956f9a69c86ee9348488aba9ec7` |
| `data/README.md` | `5b5475ea64c80e840fe40635b6dc5a4e748f3cf5b8e4b09439d6d6e93d86451f` |
| `data/coupling.json` | `82e6eb15fe56e735e4b5190b7b2a2426a1dac9c3eaa91197cffff7211b5b23c7` |
| `data/industry.json` | `7265c4f7449c3acbede38aa2414c0ff8e2a6d64c21fa7059cfc79afa0c8c5513` |
| `data/military.json` | `7c94cbc4045cf0add6e92b4397ed58fa87a9f6f4621b933724e646506b571e1a` |
| `data/review_cases.json` | `f1113b4b9cc872e6b7c36cd8450766087513914c47af3d66838e103e750548a6` |
| `data/runtime_rules.json` | `dfaf01f447b6d5e8614fdf71edf26bacaf3882db54b60555c1700d2396a882aa` |
| `data/scoring.json` | `743defdef1608a5e2734bc32ce38f33d916a618eb9920baea4e647f1979f7985` |
| `data/technology.json` | `50f2c8b9807997fb419f780ec4307d1d2620f36c885ce89e1a925b0276b16293` |
| `review/LOG.md` | `dfc4fc974192abd5f5349e7d263d05b28e5884a653a1ceef46ee2cbd681cf1e2` |

其余 62 个文件（decisions、proposals、diagrams、tools、prototype；本次只作版本记录，未逐行审阅原型代码）：

```text
2b2e8d8bd4fcb54b23bef9e0df1f6fae22c7340c905e3894cfaa867dc97f5aa8  decisions/0001_scope.md
0271a0d5169b9c99f5b91c39162c64bdab45f39d1197703451c56555e7f0f527  decisions/0002_topology_timing_and_tick_order.md
6da52fe6867564cf5b72eef9eb063edc4176943e0a9618dafc73330a339a5c91  decisions/0003_national_power_and_seat_fairness.md
50a028eee45185f9e72052df10f46cccaa055b9ae1889e21e2f96d6b586d5d43  decisions/0004_world_init_extraction_and_macroeconomy.md
7511328daae8fb87414881457dd2d3968119a0e71a7dbbf786a69fe48fff17a7  decisions/0005_tech_modifiers_factory_and_grid_physics.md
613ba2cda78f0c2b7259b3c06b38c4fc94ac1087d59d962aa3b98ec14abbf1c7  decisions/0006_combat_intelligence_and_orbital_rules.md
6827bde0b4851dd3248fc534291a2c239f378aafe450b79462c137bdb5cb7f7d  decisions/0007_control_governance_and_diplomacy_rules.md
cb61be743bd372ff12f262190c3c7a4a287819b8a7d60a542485a373b7f1ac60  decisions/0008_sandbox_isolation_and_ipc_quotas.md
57364bf30e1e6760cc1d8302bd5c7f596deeb4f43e439384113f1635e86716d6  decisions/0009_tech_expansion.md
167aadfb25672b4a82d2f0f294ff52c647166b0e1d177a322c4dd4527627a831  decisions/0010_longer_game_population_and_package_submission.md
00d450a317383fdead221a83c43cb3bc3cacbd68e55e947a951401c18d4778e8  decisions/0011_opponent_pool_and_scoring_set.md
bf02015aa4f351d769b9e4b5314a1a935cb80b40af48311b4fe7739a86c6cf8b  decisions/0012_v13_population_finance_forex_ai.md
0c8e1189516f6caa53df592fb608aab5f1b7b9870c153a40a647cb9cf8463041  decisions/0013_system_coupling.md
74e743d45e665df0333cc7ca45f2058872a0aa4a35b24b0e0e88814522c0095a  diagrams/industry_overview.mmd
96e02adfdeef64d1e13cee1b35a5ec82574d65b492a95a1ce678b99a59493ad3  diagrams/technology_tree.mmd
bb6813fcf5cac347539a40896bc569fa01dac88aba5e477b409f59bb9f7879a0  proposals/AF-world-topology-timing-and-tick-conservation.md
7f5b9a5dd0593286cf63a19045f0b97ee837e932953dd95fb86bbd8985ee0991  proposals/B-tech-modifiers-factory-ops-and-power-grid.md
d9d194a4d19c3aeda14900b156952c1f788d934d56e58d0e4b4f63138c37746a  proposals/BA-world-init-extraction-and-macroeconomy.md
03a1f0f8f1028e9ed0b2d493ff0a378e3c4f9e7c1756242aa29e598a6bd8a7a5  proposals/C-combat-formations-intelligence-and-orbital.md
2da36376bb16ec7c267889dd297c27566ade35529c284f5ade4f8f6728ea24ae  proposals/D-control-transitions-governance-and-diplomacy.md
0de9c5f43417c66911c94659a9adb9c7ca233123c4a7d25dc7e33303ac9e9a8a  proposals/E-national-power-scoring-and-seat-fairness.md
221b70c1388baf64b57e0b554dc1f80c7678a0b7f0792bb1a6ad226d30a7748f  proposals/F-sandbox-isolation-budgets-and-scalable-ipc.md
2fe8798f302becac5ab54f00f9bb77a8755ecd962959d89f97a716a5fb3cedbb  proposals/G-tech-expansion-tiers5-6-and-new-branches.md
1c9b3e4658443d983b7a91a00eb497c4f72c79fffb20a6e96e5b7621773a2cea  proposals/H-v13-population-finance-forex-ai.md
187e072b647640b602dae14505fc62eb1abce8a7a0e362c753753ef317da9f2b  proposals/README.md
60c407483dad3c86630845c3a6c9d31f6ae04375101e610165f7c732223618ea  proposals/TEMPLATE.md
d92e719e07223fb4b525c80e918bfcf05dbef466c844ff3195bb39c9051e6c3d  prototype/README.md
85316b7df4a09ec6a7d8603b54725a002588fe1ccfdca6c949f85c683aa152c1  prototype/advanced.py
361beda76fc3faf3a37910ac35edfec9207228e7316d2f11efa5fccc22419ec3  prototype/bots.py
b9bdd54c6e83a0097b7b8a5552b2c92e33b696d1d2b069f97baae85068883726  prototype/bound.py
6c69a51f6602fe16ffe1a4bdef1b076bcbce10c82a425dece89501cab35854d2  prototype/brk.py
683befef8051b725ea080b4ead8ff8b82c26c537ae162531f31e6684263ea8ff  prototype/calib.py
411c318d8e12e8b49728428ae8363c2eeb9f091d21095c944e9b381aac4008a7  prototype/calib5.py
d5e6edb37fd1f145b0a2a8b3d39d73f182d2415d7da2f9267278c709a360ac19  prototype/dbg.py
93a5c3ad565f62e3afffadf6760fc6dd62b88e8035b105e48d9e275b67cff605  prototype/dbg5.py
7f0b5eada3c4510b0b52653c81c587fbc96e3e1e93bc31f86aeb61178f0d3a74  prototype/exploit.py
4c2b9d17cf7b4205c3a79e487208aa08447ebe20aef5fd8a165e7c5e641d9219  prototype/medium.py
de043940df2406c4b621420352260e843625e5e5d14a9aa602f34afae23936e5  prototype/ref.py
15abf5584bf967abdb23bb4ee1a3477f48762eb5bc5a77ec2c759264d0b69793  prototype/ref2.py
946c2f39fd0b2e48230aed9e849da066c37b4fdffa626cf7eb3805d693f794a7  prototype/ref3.py
517eda569bf616e51442d2fa96240528175f596010b50793534ce8d172c01b7e  prototype/ref4.py
db3c61c629f52014350a7cc2259e393b2e35b2e839a971187070f12514e6e435  prototype/ref5.py
92c9969ad54504a230ef62ddbeafa2f013ac41440d3205ca319060316749ccb8  prototype/replay/REPLAY_FORMAT.md
c719b96be923bb77f21788f2c0c3432413d62abe2bbb948abf90b692feddda40  prototype/replay/make_demo.py
cfd6b5a7347b54f04fe50d05bd3a483750caaf68d97cd4d80531a97e7cbbea41  prototype/replay/record.py
159014eb09ab297db543617298cb225a81005f3825d66547cc68b41683d7a55d  prototype/replay/replay_seed1000.json
79ac7f7b68d452ad2a9573799c8a3074e7869f221cd528b2d0b70ace41f1cd93  prototype/replay/test_viewer.py
ccbfb65cc2cb6e892224ac2203f07c28a586b1b3507eac076007861115983bf5  prototype/replay/verify_nochange.py
769c7290b7e38c16b7c56eab67f81f2d4a68c6a76df1bc84dc98277db9db6e15  prototype/replay/viewer.html
37aaba2088c8167aae7eb791c5a417764a3cc03af733784c6a82a7d7c3013763  prototype/replay/viewer_demo.html
4cc642835b8a82a03cffb1070c8d0487806582209bd5f59f3ca4e203fb04c8c4  prototype/run.py
3d882fe9699fa00bf6243d94d25e853443fe4e4bb1809d29f93e1eb1e0b6e1fb  prototype/runref.py
ac4a42bf3142a36903b7e9e16bb4706cb71ff03dccceb5086ac3b9b21d6f86eb  prototype/score.py
1ae30844f05229a1ffc38abd9d50866e070be03e03679cdff429c4af1fbb3b89  prototype/seat1.py
e5aad64f960f572ae52cd84c171c02af520ac9f36a979717392873fff4ecb8ee  prototype/seats.py
555c24ef318ecbe36285492e710ac1d5fe854d9a40ffcfea9ebd106f26ec8d6e  prototype/sens.py
4018252d382a26c648aa2fabd15453123889c7818749ae66583817dd3ee2629d  prototype/shock.py
0b1d0bdbe190bd7641e608aae66aa467a8596537ee509379e5b97a58ed19af58  prototype/sim.py
2f1b67435877e83a8f982fab1970b0cfce0d26a02446fae8a23a45679f2fe533  prototype/tiers.py
d389aae7927daec7abbf420613e57a76c8b6d2852416c076e7c9293cf06653fe  prototype/tune.py
fba8ab8c5f6dc8bdcbfcb596f46ee2dda24523010b402b6e20f22e9218c59397  tools/catalog.py
ef0d39adf385939a3e9ac65574f15bdc60131cf4399c750980eedb968a391dd1  tools/coupling.py
```

## 附录 B 复算脚本与输出

只读脚本，读取 `data/*.json`，不写任何文件。在仓库根目录运行：`python3 -I verify.py designs/rift_era`。

```python
# 终审复算（只读：读取快照 data/*.json，不写任何规则文件）
import json, math, sys
root = sys.argv[1]
J = lambda p: json.load(open(f"{root}/{p}"))
sc, ind, rt, mil = J("data/scoring.json"), J("data/industry.json"), J("data/runtime_rules.json"), J("data/military.json")
w = sc["shadow_weights"]["w_cat"]; R = {r["id"]: r for r in ind["recipes"]}
def va(rid, eta=None):
    r = R[rid]; eta = eta or {}
    out = sum(q * w[g] * eta.get(g, 1.0) for g, q in r["outputs"].items() if g not in ("research_points", "housing_capacity"))
    return out - sum(q * w[g] for g, q in r["inputs"].items())
K = 0.35 * 0.25  # U_ind 系数 × 支柱权重：VA 每 1 点 → U 增量
print("[1] 07:27 S1 拆解算例")
S1 = next(f for f in ind["facility_profiles"] if f["id"] == "S1")["build"]
b = sum(q * w[g] for g, q in S1.items()); print(f"  S1 建材目录值 = {b:.4f}（07 写 113.5664）; 拆解损失 = {b - 3*w['scrap_steel']:.4f}（07 写 105.2414）")
print("[2] 行军烧油：12 辆 armored 每 tick 移动")
fuel = 12 * mil["units"]["armored"]["combat_tick"]["fuel"] * 10; batches = fuel / 3
print(f"  耗油 {fuel:.0f}/周期 = P006 {batches:.0f} 批; VA/批 eta=1: {va('P006'):.3f}; 燃料 eta=0: {va('P006', {'fuel': 0}):.3f}")
print(f"  相对不生产: ΔVA={batches*va('P006'):.1f} → ΔU={batches*va('P006')*K:.1f}/周期; 相对生产不消耗: ΔVA={batches*(va('P006')-va('P006',{'fuel':0})):.1f}")
print("[3] 炮击：12 门 artillery 每 tick 射击")
ammo = 12 * mil["units"]["artillery"]["combat_tick"]["ammunition"] * 10; nb = ammo / 4
chain = nb * va("P044") + (nb / 3) * va("P001") + (nb / 2) * va("P007")
print(f"  耗弹 {ammo:.0f}/周期 = P044 {nb:.0f} 批; 链上 VA(不计采掘) = {chain:.1f} → ΔU ≈ {chain*K:.1f}/周期")
psi = min(1, max(.5, .5 + .5 * .72)); upop = 100 * psi * 194 / 200 * 1.3
print(f"  对照：开局人口支柱 U_pop≈{upop:.1f}，在 U 中贡献 .25×U_pop≈{.25*upop:.1f}")
print("[4] 敌对行动类科技计量")
tw = sc["pillars"]["tech"]["tier_weights"]; tech = {t["id"]: t for t in J("data/technology.json")["technologies"]}
for k in ["S08", "C12", "V08", "S06"]:
    a = sc["pillars"]["tech"]["active_scale_rules"][k]; W = tw[str(tech[k]["tier"])]
    print(f"  {k}: meter={a['meter']} theta={a['theta']} 满额落地增量 U = .15×.6×{W}×.85 = {.15*.6*W*.85:.3f}")
print("[5] NFA 自贬本币：国库 100000 本币按 e=1 换 RU 后贬到 0.05")
fee = rt["financial"]["fx_fee"]; ru = 100000 * (1 - fee)
for e in (1.0, 0.5, 0.05):
    nfa = 50000 * e + ru - 150000 * e
    print(f"  e={e}: NFA={nfa:,.0f} RU → U_ind 项 .01*NFA/10 = {nfa/1000:.1f} → ΔU={nfa/1000*K:.2f}")
print("[6] 计分映射与导数抽查")
s = lambda R: 100 * R**1.2 / (R**1.2 + 1)
vt = lambda V: V + 10 if V >= 0 else 100 / (10 - V)
for t in sc["relative_scoring"]["test_vectors"]:
    Rk = vt(t["V_p"]) / vt(t["V_s"]); assert abs(s(Rk) - t["s_k"]) < 1e-9 and abs(50 + 50*math.tanh(.6*math.log(Rk)) - t["s_k"]) < 1e-9
print("  11 个测试向量 PASS（两种写法一致，误差<1e-9）")
print("[7] 开局静态预算复算")
elig = set(ind["mode_eligible_facilities"]); W = P = 0
for rid, n in rt["initial_world"]["productive_lines"].items():
    if rid == "P070": continue
    r = R[rid]; ld = rt["initial_world"]["load_overrides"].get(rid, 1.0); fw, fp = (1.4, .8) if r["facility"] in elig else (1, 1)
    W += r["workers"] * fw * n * ld; P += r["power"] * fp * n * ld
print(f"  生产用工 {W:.1f}（文档 84）用电 {P:.1f}（文档 23.4）; 194×.65={194*.65:.1f}")
```

输出（在附录 A 的快照上运行）：

```text
[1] 07:27 S1 拆解算例
  S1 建材目录值 = 108.5672（07 写 113.5664）; 拆解损失 = 100.2422（07 写 105.2414）
[2] 行军烧油：12 辆 armored 每 tick 移动
  耗油 144/周期 = P006 48 批; VA/批 eta=1: 5.500; 燃料 eta=0: -7.625
  相对不生产: ΔVA=264.0 → ΔU=23.1/周期; 相对生产不消耗: ΔVA=630.0
[3] 炮击：12 门 artillery 每 tick 射击
  耗弹 240/周期 = P044 60 批; 链上 VA(不计采掘) = 542.5 → ΔU ≈ 47.5/周期
  对照：开局人口支柱 U_pop≈108.4，在 U 中贡献 .25×U_pop≈27.1
[4] 敌对行动类科技计量
  S08: meter=paid_systemic_actions theta=1.0 满额落地增量 U = .15×.6×11.0×.85 = 0.842
  C12: meter=paid_cryptanalysis_actions theta=1.0 满额落地增量 U = .15×.6×11.0×.85 = 0.842
  V08: meter=paid_climate_actions theta=1.0 满额落地增量 U = .15×.6×11.0×.85 = 0.842
  S06: meter=paid_decoy_actions theta=2.0 满额落地增量 U = .15×.6×5.5×.85 = 0.421
[5] NFA 自贬本币：国库 100000 本币按 e=1 换 RU 后贬到 0.05
  e=1.0: NFA=-200 RU → U_ind 项 .01*NFA/10 = -0.2 → ΔU=-0.02
  e=0.5: NFA=49,800 RU → U_ind 项 .01*NFA/10 = 49.8 → ΔU=4.36
  e=0.05: NFA=94,800 RU → U_ind 项 .01*NFA/10 = 94.8 → ΔU=8.29
[6] 计分映射与导数抽查
  11 个测试向量 PASS（两种写法一致，误差<1e-9）
[7] 开局静态预算复算
  生产用工 84.0（文档 84）用电 23.4（文档 23.4）; 194×.65=126.1
```
# 裂陆纪元规则复审报告（Opus 5.5，Round 6 核对）

- 审核人：Claude Opus 5.5，同一会话（`claude:39c7b409-46a4-48b7-b082-4c9f6c744cdc`）。
- 时间：2026-11-03 14:17 UTC 开审（记录 89 个文件新指纹），14:40 UTC 写入本文件。
- 边界：只读核对。审阅在 14:17 UTC 复制的快照上进行；复审期间唯一变动文件是本次终审报告（已有的 `FINAL_REVIEW_opus55_2026-11-03.md`）。规则文件、JSON 和 LOG.md 无改动。
- 版本：`rift_era_rules_r6`，Round 6 由 Astra 修订（13:18–13:54 UTC），Gemini Round 6 复审（14:03 UTC 写"审核通过"）。

## 1. 结论

**通过，条件如下（§3）。**

OR-001～017 全部对应规则/数据改动经逐项核对均已落地，没有发现新的阻塞性冲突或刷分口子。初次终审报告中的三个 P0 问题（作战消耗刷 VA、敌对行动科技计分、缺战争状态机）均已在正文与 JSON 中实质解决，用户 13:14 UTC 确认的"任何损人行动严格扣分"口径已写入 README、00 章和 07 章，符合设计要求。

七项需要在正式引擎阶段验证的工程/平衡风险已明确标注（§3），不阻塞规则文档冻结。

## 2. 各 OR 项复核结果

| 编号 | 级别 | 复核结论 | 核验方式 |
|---|---|---|---|
| OR-001 | P0 | **已解决** | scoring.json `action_loss.floor=0.001`；`eta_sink` 含 `SinkDebt` 字段；07:31–37 完整条文。独立复算：12门炮兵持续炮击，直接目录损耗 1307.5/周期（240弹×4.1354+72燃×4.375），链 VA 上界 674.5，ΔU ≤ −110.7/周期；12 辆装甲绕圈，损耗 630.0，链 VA 上界 264.0，ΔU ≤ −32.0/周期。两例均为严格净负，符合用户口径。 |
| OR-002 | P0 | **已解决** | scoring.json `active_scale_rules` 四项旧 paid 行动 meter 均已删除：S06=`online_deception_defense_hubs`、S08=`online_system_defense_hubs`、C12=`online_cryptanalysis_defense_hubs`、V08=`consented_climate_regions`，改为本国在线防御能力计量。 |
| OR-003 | P0 | **已解决** | runtime_rules.json `diplomacy` 含 `states:[PEACE,WAR,TRUCE]`、`initial_state:PEACE`、`kinetic_requires_war:true`、`declaration_phase:P1_after_accepted_treaties_before_target_validation`、`simultaneous_declarations:both_aggressors`、`truce_expiry:PEACE`、`city_state_annex_cycles:240`。03 章 §0 写出状态机表，01/02/06/10 "交战"引用已更新。 |
| OR-004 | P1 | **已解决** | runtime_rules.json `blockade` 含 `effective_lag_ticks:1`、`partial_formula`、`interdict_damage_cap`、`neutral_carrier_or_owner_protected`；03 章 §7 有封锁执行规则；正向替代场景算术 round6_scenarios.json 中有 `blockade_food_substitution` 下界 ΔV ≥ +0.0037。|
| OR-005 | P1 | **已解决** | runtime_rules.json `asset_damage` 含 `health_initial_mU:1000`、`capacity_formula`、`repair_material_multiplier:1.0`、`repair_full_duration_cycles:20`；05 章末节补齐健康-产能函数和修复流程；06 章有 `repair_asset` 命令。 |
| OR-006 | P1 | **已解决** | 07:92–107 建立历史 RU 账面体系，`NFA_p` 改用 `CashBookRU`；复算三个汇率场景（e=1/0.5/0.05）NFA 恒为 −200；`彻底封死` 已删除；02 章已统一。|
| OR-007 | P1 | **已解决** | 07:43–47 写出 `TransitReceipt` 完整公式（单位：目录价值 × L1格）；复算 10food A6/B4 D0=8：A=183.12、B=122.08，与 LOG 一致。 |
| OR-008 | P1 | **已解决** | 02 章已统一 `ShortDebt[p,q]` 双下标、年利率 `max(0,i_q+2)%`；11 章"强平优先档同档比例分配，不按账户 ID 先后"；钉住改为有界自动干预计划。 |
| OR-009 | P1 | **已解决** | 06 章有 `governance/relocate_population` 命令（含 `allow_unhoused`）；11 章对应条文；场景契约有迁居守恒算例。 |
| OR-010 | P1 | **已解决** | 07:124–126 写出 `C_match=ceil(C_engine_peak)+N_active_strategies`，消除"每局 1 核"超订；`K_CPU` 公式完整。 |
| OR-011 | P1 | **已解决** | README/00/07 均已写入用户口径；07:35 `L_e=max(0.001,…)` 底额及高精度要求；复算 PASS。 |
| OR-012 | P2 | **已解决** | 07:29 S1建材=108.5672、损失=100.2422，复算 PASS。 |
| OR-013 | P2 | **已解决** | scoring.json `extraction_rule` 字段确认采掘不进入 VA_eff；07:27 正文同步。 |
| OR-014 | P2 | **已解决** | 07:128 写出 `batch_id` 冻结机制与 manifest 内容；评测批次定义完整。 |
| OR-015 | P2 | **已解决** | 04 章写出完整公式：全局只应用一次 N^(α-1)，默认 α=0.75；R05+C02 均解锁、全部 Hub 联网且 N≤4 时 α=0.92，N>4 整项回退 0.75。8 Hub 各 1 线仍按 N=8、α=0.75 计算，无法利用分 Hub 绕开递减。 |
| OR-016 | P2 | **已标注** | 02:220 保留 40 单位候选上限并说明待测；round6_scenarios 有压力指标。属已知待测风险，不阻塞文档冻结。 |
| OR-017 | P2 | **已解决** | 11 项子问题均已落地：条件计划时钟统一（06）；`ERR_NO_VISIBLE_TARGET`/`ERR_NOT_AT_WAR`/`ERR_REPAIR_QUANTUM` 在 03/06 错误码表；轨道 `berth∈{0,1}` 在 06 orbital_launch 命令；P090 阈值同步；A01 改公共槽；AI 按 `Cbar[hub_id]`；bombard/Combat_r 归属；`toll_rate_bp` 基数定义；NPC 推理出口；居民多供货方分档；README/STATUS/CHANGELOG 状态说明。 |

## 3. 需在正式引擎阶段验证的风险（不阻塞规则冻结）

1. **攻防损耗对称**：防守方还击同样扣 Loss_action，须在 64-seed 实局确认防守不会比弃守更亏。
2. **世界池压力**（OR-016 延伸）：40 单位上限与有限 RU 可能在早期出现抽干；须按 round6_scenarios 的 4 项监测指标实测。
3. **央行跨期 FX 操纵**（OR-006 延伸）：历史账面已堵纯重估，但先低汇率发钞/换汇再反向操纵的多步策略须在多智能体实局红队验证。
4. **场景契约端到端**：round6_scenarios 中 15 个 `formal_engine_acceptance_pending` 场景及 512×512 地图闸门须在正式引擎上跑通。
5. **沙箱与并发压测**：seccomp 白名单、16 MiB 快照吞吐、K_CPU/K_mem 闸门须在目标集群实测。
6. **OR-001 损耗数字说明**：LOG Round 6 写"目录损耗 1307.496"指弹药+燃料的直接目录值（240×4.1354+72×4.375=1307.5），不含上游采购链；本报告复算结果一致（1307.5≈1307.496，精度在 3 位小数）。不是算术错误。

## 4. 初次终审报告 Gemini 复审的独立评价

Gemini Round 6 的逐项核对表与本次独立复算结论一致，且额外提供了"全制造链望远镜求和上界证明"（VA 恒被 Loss_action 严格覆盖）和 SinkDebt 防绕过的形式论证。这与本次独立脚本复算结果相符。Gemini 指出的三项仍需引擎验证的风险与上面 §3 一致。

## 附：复审快照指纹

- 14:17 UTC 89 个文件清单 SHA-256：`b68755141c88a721f20701fb64a230220e7c4338e8d02db39135a13404c34e81`
- 复审期间唯一变动：`review/FINAL_REVIEW_opus55_2026-11-03.md`（本终审报告的前次版本，在本次复审开始前即已写入，非规则文件）

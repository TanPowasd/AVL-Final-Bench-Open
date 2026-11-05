"""三档参考 bot（原型校准用）。每档是独立实现的独立策略（decisions/0011，Q30）。
- Blank   ：白板，分数 0 点。
- Medium  ：公开陪练，约 0.1× Super（medium.Medium：广度优先研究 + 只用基础实验室 + 贪心补缺生产 + 高阶品进口）。
- Advanced：公开陪练，约 0.3× Super（advanced.Advanced：纵深冲刺计算研发 + 固定系数投入产出模板 + 固定 3 座计算实验室）。
- Super   ：评分参考 R，不公开（= ref5.Super14：Super13 + 2026-11-04 五支柱国力适配；Super13 保留作对照）。
Medium/Advanced 不导入、不复制 ref*.py；加强 Super 只改 ref5.py，不影响公开档。
临时口径：score = TP(bot) / TP(Super)，TP = 局末已知科技的理论点合计（白板为 0）。
"""
import bots, ref5, medium, advanced
Blank = bots.Blank
Medium = medium.Medium
Advanced = advanced.Advanced
class Super(ref5.Super14):
    name = 'super'

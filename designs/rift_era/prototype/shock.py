"""S6 冲击响应检验（10_system_coupling.md §2 S6）。
做法：Advanced 单席（其余白板）跑到第 T0 周期，深拷贝出“基线”与“冲击”两局；冲击时刻起两局的 bot
都冻结为“看守”策略（不改产线，只在口粮低于 3 周期用量时补到 6 周期、发电燃料低于 5 周期时补到 10 周期，
两局同一规则；v1.3.1：冻结成白板时进口口粮的经济体约 160 周期后断粮，断粮时刻被冲击推迟几周期就会把人口差放大成
与冲击无关的大偏离），之后各跑 H 周期，比较各宏观量的偏离。
判据：瞬时冲击——偏离在峰值后 W 周期内衰减到峰值的 10% 以下，W = max(50, 存量变量自身衰减到 10% 所需周期)：
流量冲击 W = 50；污染存量冲击按 C21 时间常数 τ = 1/0.02 = 50 周期取 W = 2.5τ = 125 周期（自身按 0.98ᵗ 衰减，
125 周期剩 8%），其派生量随存量同窗判定；污染按存量 Pol 判定（指数 Pollute = Pol/(Pol+100) 是它的单调变换，
非线性会把初始偏离压扁、看起来衰减更慢）；持续冲击（利率 +5pp）——末 30 周期偏离均值与前 30 周期均值之差 < 峰值 10%（收敛到新稳态；
v1.3.1 起用均值而非极差，π 的逐周期噪声约 1e-5，用极差会把噪声当成未收敛）。
同时检查 NaN、负库存、负人口。
用法：python3 shock.py [T0=1500] [seed=1000] [bot=Advanced]
"""
import sys, copy, math
import sim, bots, tiers

T0 = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
H = 130          # v1.3.1：冻结（看守）经济在约 140–170 周期后出现库存耗尽类的状态切换，之后两局的偏离由切换时刻决定，
                 # 与冲击响应无关；130 周期覆盖流量冲击的 50 周期窗口与污染存量的 125 周期窗口
BOT = sys.argv[3] if len(sys.argv) > 3 else 'Advanced'
MATERIAL = 0.01      # 峰值偏离 < 变量量级 1% 视为可忽略（数值噪声级），记为通过
SCALE_FLOOR = {'pi': 1e-3}   # π 自然量级下限取 1e-3/周期（对应年化 10% 通胀；1% 阈值 = 年化 0.1% 噪声底）
VARS = ('e', 'pi', 'cpi', 'elec_price', 'pop', 'support', 'tax', 'eq', 'pollution', 'power_ratio')


class Keeper(bots.Blank):
    """冲击检验用的看守策略：不改产线，只补口粮与发电燃料（基线与冲击局同一规则）。"""
    def act(self, g, n):
        need = n.pop * sim.FOOD_PER_POP
        if n.inv.get('food', 0) < 3 * need: g.buy(n, 'food', 6 * need - n.inv.get('food', 0))
        burn = {}
        for rid, q in getattr(n, '_gen_out', {}).items():
            r = sim.RECIPES[rid]
            for a, qq in r['inputs'].items(): burn[a] = burn.get(a, 0) + qq * q / r['outputs']['electricity']
        for a in sorted(burn):
            if a != 'water' and n.inv.get(a, 0) < 5 * burn[a]: g.buy(n, a, 10 * burn[a] - n.inv.get(a, 0))


def snap(g, n):
    eq = sum(g.fair(c) for c in g.companies if c.nid == n.nid)
    return dict(e=n.e, pi=n.pi, cpi=n.cpi, elec_price=n.elec_price, pop=n.pop, support=n.support,
                tax=n.tax_log[-1] if n.tax_log else 0.0, eq=eq, pollution=n.pollution, power_ratio=n.power_ratio)


def fuel_shock(g, n, k):
    orig = g.ref_price
    fuels = {g_ for r in sim.GENS for g_ in sim.RECIPES[r]['inputs']} - {'water'}
    def rp(nn, good):
        v = orig(nn, good)
        return v * 1.5 if (nn is n and good in fuels and g.cycle < T0 + k) else v
    g.ref_price = rp


def shed_shock(g, n, k):
    orig = g._dispatch
    def dp(nn):
        orig(nn)
        if nn is n and g.cycle < T0 + k: nn._served *= 0.7
    g._dispatch = dp


W_FAST = 50
W_POLL = math.ceil(2.5 / sim.POLL_DECAY)     # C21 存量时间常数 τ = 1/δ = 50 周期，慢存量窗口 W_slow = 2.5τ = 125 周期（0.98¹²⁵ ≈ 8%，130 周期 0.98¹³⁰ ≈ 7.23% < 25%）
SHOCKS = {
    f'燃料价格 +50%（1 周期，快流量窗口 W_fast={W_FAST}）': (lambda g, n: fuel_shock(g, n, 1), False, W_FAST),
    f'全国 30% 切负荷（5 周期，快流量窗口 W_fast={W_FAST}）': (lambda g, n: shed_shock(g, n, 5), False, W_FAST),
    f'汇率 Δln e = −0.05（1 周期，快流量窗口 W_fast={W_FAST}）': (lambda g, n: setattr(n, 'e', n.e * math.exp(-0.05)), False, W_FAST),
    f'利率 +5 个百分点（持续，稳态收敛窗口 {W_FAST}）': (lambda g, n: setattr(n, 'rate', n.rate + 5.0), True, W_FAST),
    f'污染存量 +50%（慢存量窗口 W_slow=2.5τ={W_POLL} 周期）': (lambda g, n: setattr(n, 'pollution', n.pollution * 1.5), False, W_POLL),
    f'污染存量 +500（S5 脉冲，130 周期慢存量窗口 W_slow={W_POLL} 周期）': (lambda g, n: setattr(n, 'pollution', n.pollution + 500.0), False, W_POLL),
}


def main():
    bl = [bots.Blank() for _ in range(8)]; bl[0] = getattr(tiers, BOT)()
    g0 = sim.Game(SEED, bl)
    for _ in range(T0): g0.step()
    g0.nations[0].bot = Keeper()
    base = copy.deepcopy(g0); nb = base.nations[0]
    traj_b = []
    for _ in range(H):
        base.step(); traj_b.append(snap(base, nb))
    ok_all = True
    for name, (fn, persistent, W) in SHOCKS.items():
        g = copy.deepcopy(g0); n = g.nations[0]
        fn(g, n)
        traj = []; bad = False
        for _ in range(H):
            g.step(); s = snap(g, n); traj.append(s)
            if any(not math.isfinite(v) for v in s.values()) or n.pop < 0 or min(n.inv.values()) < -1e-6: bad = True
        res = []
        for v in VARS:
            dev = [abs(a[v] - b[v]) for a, b in zip(traj, traj_b)]
            scale = max(max(abs(b[v]) for b in traj_b), SCALE_FLOOR.get(v, 0.0)) or 1.0
            peak = max(dev); tp_ = dev.index(peak)
            if peak <= 1e-6 * scale:
                res.append((v, 'flat', True)); continue
            if peak < MATERIAL * scale:
                res.append((v, f'峰值 {peak:.3g}（< 量级 1%，可忽略）', True)); continue
            if persistent:
                ok = abs(sum(dev[-30:]) / 30 - sum(dev[-60:-30]) / 30) < 0.1 * peak
            else:
                after = dev[min(len(dev) - 1, tp_ + W):]
                ok = max(after) < 0.1 * peak
            res.append((v, f'峰值 {peak:.4g}（第 {tp_} 周期）→ +{W}: {dev[min(len(dev) - 1, tp_ + W)]:.3g}', ok))
        passed = all(r[2] for r in res) and not bad
        ok_all &= passed
        print(f'## {name}: {"通过" if passed else "未通过"}{"（NaN/负值）" if bad else ""}')
        for v, txt, ok in res:
            if txt != 'flat': print(f'  {v:12s} {txt}  {"✓" if ok else "✗"}')
    print('全部通过' if ok_all else '存在未通过项')


if __name__ == '__main__':
    main()

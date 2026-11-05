import sys
from sim import Game, RECIPES, NODES
from bots import roster
R=roster(0); g=Game(1000,[R[sys.argv[1] if len(sys.argv)>1 else 'cheap']()])
for c in range(960):
    g.step(); n=g.nations[0]
    if c in (1,10,40,100,200,400,600,800,959):
        from collections import Counter
        cnt=Counter(ln[0] for f in n.fac for ln in f.lines if ln)
        print(c,'pop',round(n.pop),'pw',round(n.power_ratio,2),'lab',round(n.labor_ratio,2),'food',round(n.food_ratio,2),'money',round(n.money),'err',n.errors,'rp',round(n.rp_log[-1],1),'known',len(n.known),'maxT',max([NODES[t]['tier'] for t in n.known]+[0]),'proj',n.project and (n.project[0],round(n.project[1]),n.project[2]))
        print('   fac',Counter(f.pid for f in n.fac),'building',sum(1 for f in n.fac if f.remaining),'mines',Counter(m.good+m.level for m in n.mines))
        print('   lines',dict(cnt))
        print('   inv',{k:round(v) for k,v in n.inv.items() if v>=1})

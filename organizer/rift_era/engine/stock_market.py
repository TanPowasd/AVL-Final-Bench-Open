"""Restricted government shares, finite NPC accounts and whole-share trading.

IPO is a transfer of existing stock, never an issue of new shares. NPC cash is
seeded once through the same finite pool batch as command FX. All later quotes
lock actual NPC cash/shares before player orders; their plans never read those
orders or get a mark-to-market cash refill.
"""
from collections import defaultdict,deque
from dataclasses import dataclass

from .command_schema import fields
from .determinism import digest,uniform,normal
from .errors import InvariantViolation,RuleViolation
from .ledger import Account,CashAccount,Inventory,Transfer,CASH_BOOK_SCALE
from .numeric import Q,ceil,floor,clip,max_feasible,proportional
from .payments import PoolPayment
from .production import commit_buffers
from .protocol import PreparedCommand
from .spot import Venue,RestingOrder
from .stock_exchange import StockExchange


NPC_FRACTIONS={'Fund':Q(3,10),'Mom':Q(1,10),'Noise':Q(1,20)}


@dataclass
class EquityIssue:
    shares: int
    book_ru_mU: Q
    entry_fx: Q


@dataclass
class LongLot:
    shares: int
    book_ru_mU: Q


class StockMarket:
    def __init__(self,state):
        self.s=state;self.shares=Inventory()
        from .equity_finance import EquityFinance
        self.finance=EquityFinance(state)
        self.exchange=StockExchange(self.shares,state.initial.cash,discounts=lambda p:state.fx.fee_discounts.get(p,Q(0)))
        for cid,company in state.initial.companies.items():
            self.shares.initialize(self.account(company.issuer,cid,'strategic'),510000000)
            self.shares.initialize(self.account(company.issuer,cid,'unissued'),490000000)
            self.exchange.add_venue(Venue(cid,'securities',cid,company.issuer,100,1000,Q(1,500),CashAccount('world','pool',company.issuer)))
        self.shares.seal()
        self.references={};self.pending={};self.issue_orders={};self.listed_cycle={}
        self.close_history={cid:deque(maxlen=2) for cid in state.initial.companies}
        self.turnover={cid:deque(maxlen=20) for cid in state.initial.companies}
        self.external_issues={cid:[] for cid in state.initial.companies}
        self.long_lots={};self.position_rounding_steps=0
        self.npc_plans={};self.signals={p:{} for p in state.initial.known}
        self.reports={p:[] for p in state.initial.known}
        self.last_cycle=-1;self.last_cleared_cycle=-1

    @staticmethod
    def account(owner,company,bucket='available'):return Account(owner,'securities',bucket,company)

    @staticmethod
    def npc(company,kind):return 'npc:'+company+':'+kind

    @staticmethod
    def cash(owner,currency):return CashAccount(owner,'portfolio' if owner.startswith('npc:') else 'treasury',currency)

    def fair(self,cid,cycle,*,neutral=False):
        state=self.s;company=state.initial.companies[cid]
        multiplier=Q(10) if neutral else clip(1/(Q(state.interest_bp[company.issuer],10000)+Q(1,20)),Q(5),Q(15))
        base=max(Q(100),multiplier*100*state.industry_activity.mean20(cid)/1000000)
        blackout=max(Q(7,10),1-Q(1,2)*state.industry_activity.shortage5(cid))
        crisis=Q(4,5) if state.macro.crisis_until_cycle[company.issuer]>cycle else Q(1)
        return max(1,round(base*blackout*crisis))

    def vwap20(self,cid):
        history=self.turnover[cid]
        quantity=sum(q for _,_,q in history)
        return Q(sum(paid for _,paid,_ in history),quantity) if quantity else Q(self.exchange.prices[cid])

    def conservative_price(self,cid,cycle):return min(self.vwap20(cid),Q(self.fair(cid,cycle,neutral=True)))

    def ownership(self,cid):
        result=defaultdict(int)
        for account,quantity in self.shares.balances().items():
            if account.asset==cid:
                if quantity%1000:raise InvariantViolation('fractional physical stock claim')
                result[account.owner]+=quantity//1000
        return dict(result)

    def _sync(self):
        for cid,company in self.s.initial.companies.items():company.shares=self.ownership(cid)

    def _npc_policy(self):
        caps=[cap for p,(amount,cap) in self.s.industry_activity.completed_finance.items()
              if amount>=20000 and 'AI07' in self.s.research[p].known and self.s.ai.suspended_until[p]<=self.last_cycle]
        cap=max(caps,default=Q(0))
        return max(Q(1,50),Q(1,20)-cap/1000),1-min(Q(1,2),cap/70)

    def _order(self,owner,cid,side,shares,price,command_id,tick,expires=None,*,source='available',proceeds=None):
        return RestingOrder('stock:'+owner+':'+command_id,command_id,owner,cid,side,shares*1000,price,
            expires if expires is not None else tick+1,proceeds or self.cash(owner,self.s.initial.companies[cid].issuer),
            self.account(owner,cid,source if side=='sell' else 'next_available'),cash_namespace='stock_cash:')

    def begin_tick(self,ctx):
        self.finance.begin_tick(ctx)
        self.exchange.expire(ctx.tick)
        if ctx.tick%ctx.clock.cycle_ticks:return
        self.last_cycle=ctx.cycle
        for cid,rows in self.turnover.items():
            while rows and rows[0][0]<ctx.cycle-19:rows.popleft()
        for cid,company in self.s.initial.companies.items():
            if company.listed:self.freeze_npc(cid,ctx)
        self.publish_signals(ctx)

    def pool_claims(self):
        claims=[]
        for cid,reference in sorted(self.pending.items()):
            currency=self.s.initial.companies[cid].issuer
            for kind,fraction in NPC_FRACTIONS.items():
                claims.append(PoolPayment('ipo:'+cid+':'+kind,self.cash(self.npc(cid,kind),currency),
                                          floor(reference*1000000*fraction)))
        return tuple(claims)+self.finance.pool_claims()

    def after_commands(self,ctx):
        for cid,reference in sorted(self.pending.items()):
            company=self.s.initial.companies[cid]
            company.listed=True;self.references[cid]=reference;self.listed_cycle[cid]=ctx.cycle
            self.exchange.prices[cid]=reference;self.close_history[cid].extend((reference,reference))
            self.freeze_npc(cid,ctx)
        self.pending.clear();self.finance.after_commands(ctx);self.publish_signals(ctx)

    def freeze_npc(self,cid,ctx):
        if (cid,ctx.cycle) in self.npc_plans:return
        state=self.s;company=state.initial.companies[cid];previous=self.exchange.prices[cid]
        fair=self.fair(cid,ctx.cycle);threshold,momentum_factor=self._npc_policy()
        ipo=self.listed_cycle[cid]==ctx.cycle
        low,high=(100,2*self.references[cid]) if ipo else (max(1,ceil(previous*Q(4,5))),max(1,floor(previous*Q(6,5))))
        net=0
        for kind in NPC_FRACTIONS:
            owner=self.npc(cid,kind);payer=self.cash(owner,company.issuer)
            cash=state.initial.cash.balance(payer).native_mU
            shares=self.shares.balance(self.account(owner,cid))//1000
            side=None;quantity=0;center=Q(previous)
            if kind=='Fund':
                center=Q(fair)
                if previous<(1-threshold)*fair:side='buy';quantity=min(1000,floor(Q(cash,20*previous)))
                elif previous>(1+threshold)*fair:side='sell';quantity=min(1000,shares//20)
            elif kind=='Mom':
                prices=self.close_history[cid]
                change=Q(prices[-1]-prices[-2],prices[-2]) if len(prices)>=2 else Q(0)
                center=previous*(1+clip(change,Q(-1,20),Q(1,20)))
                if change:side='buy' if change>0 else 'sell'
                base=min(500,floor(Q(cash,20*previous))) if side=='buy' else min(500,shares//20)
                quantity=floor(base*momentum_factor) if side else 0
            else:
                retry=0
                while True:
                    choice=int.from_bytes(digest(ctx.seed,'stock_noise_side',ctx.cycle,cid,retry)[:8],'big')
                    if choice<2**64-2**64%3:break
                    retry+=1
                side=('buy','sell',None)[choice%3]
                center=previous*(Q(98,100)+Q(4,100)*Q.from_float(uniform(ctx.seed,'stock_noise_quote',ctx.cycle,cid)))
                if state.macro.crisis_until_cycle[company.issuer]>ctx.cycle:center*=Q(98,100)
                quantity=min(100,floor(Q(cash,100*previous))) if side=='buy' else min(100,shares//100) if side=='sell' else 0
            price=max(1,round(clip(center,Q(low),Q(high))))
            if side=='buy':quantity=max_feasible(quantity,lambda n:n*price+ceil(n*price*Q(1,500))<=cash)
            if not quantity:continue
            order=self._order(owner,cid,side,quantity,price,'npc-'+str(ctx.cycle),ctx.tick,
                (ctx.cycle+1)*ctx.clock.cycle_ticks)
            self.exchange.prepare_order(order,tick=ctx.tick).commit()
            net+=quantity*1000*(1 if side=='buy' else -1)
        self.npc_plans={k:v for k,v in self.npc_plans.items() if k[1]>=ctx.cycle}
        self.npc_plans[cid,ctx.cycle]=net

    def publish_signals(self,ctx):
        result={p:{} for p in self.signals}
        for owner,(amount,cap) in self.s.industry_activity.completed_finance.items():
            if amount<=0 or 'AI07' not in self.s.research[owner].known or self.s.ai.suspended_until[owner]>ctx.cycle:continue
            for (cid,cycle),net in self.npc_plans.items():
                if cycle!=ctx.cycle:continue
                sigma=max(abs(net),1000)*max(Q(1,20),1-cap/40)
                estimate=net+sigma*Q.from_float(normal(ctx.seed,'stock_npc_signal:'+owner,ctx.cycle,cid))
                result[owner][cid]=round(estimate)
        self.signals=result

    def cancel(self,owner,command_id):
        oid='stock:'+owner+':'+command_id
        if oid in self.finance.orders:
            return PreparedCommand(lambda:self.finance.finish(oid,None))
        return self.exchange.prepare_cancel(owner,oid) if oid in self.exchange.orders else None

    def _remove_long(self,owner,cid,shares,ctx):
        lots={str(i):lot.shares for i,(key,lot) in enumerate(sorted(self.long_lots.items())) if key[:2]==(owner,cid)}
        keys={str(i):key for i,(key,_) in enumerate(sorted(self.long_lots.items())) if key[:2]==(owner,cid)}
        if sum(lots.values())<shares:raise InvariantViolation('stock sale exceeds historical long positions')
        selected=proportional(shares,lots,key=(ctx.seed,'stock_long_disposal',ctx.tick,owner,cid))
        for name,quantity in selected.items():
            if not quantity:continue
            key=keys[name];lot=self.long_lots[key]
            exact=lot.book_ru_mU*quantity/lot.shares
            removed=Q(round(exact*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
            self.position_rounding_steps+=int(removed!=exact)
            lot.book_ru_mU-=removed;lot.shares-=quantity
            if not lot.shares:del self.long_lots[key]

    def _buyback(self,cid,shares,ctx):
        rows=self.external_issues[cid]
        if sum(row.shares for row in rows)<shares:raise InvariantViolation('issuer repurchase exceeds actual external stock')
        selected=proportional(shares,{str(i):row.shares for i,row in enumerate(rows)},
            key=(ctx.seed,'equity_buyback',ctx.tick,cid))
        for i,row in enumerate(rows):
            quantity=selected[str(i)]
            if not quantity:continue
            exact=row.book_ru_mU*quantity/row.shares
            removed=Q(round(exact*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
            self.position_rounding_steps+=int(removed!=exact)
            row.book_ru_mU-=removed;row.shares-=quantity
        self.external_issues[cid]=[row for row in rows if row.shares]

    def clear(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle<=self.last_cleared_cycle:raise InvariantViolation('stock auction repeated')
        self.reports={p:[] for p in self.reports}
        for cid,company in self.s.initial.companies.items():
            if not company.listed:continue
            previous=self.exchange.prices[cid]
            band=(100,2*self.references[cid]) if self.listed_cycle[cid]==ctx.cycle else (max(1,ceil(previous*Q(4,5))),max(1,floor(previous*Q(6,5))))
            prior=dict(self.exchange.orders)
            result=self.exchange.clear(ctx.seed,ctx.tick,cid,allowed=lambda a,b:a!=b,price_band=band)
            sells=defaultdict(int);buys=defaultdict(int)
            new_issues=[]
            for oid,q in result.sell_fills.items():
                order=prior[oid];quantity=q//1000
                if oid in self.issue_orders:
                    new_issues.append(EquityIssue(quantity,result.seller_capital_book_ru_mU[oid],self.s.rates[company.issuer]))
                elif oid not in self.finance.orders:sells[order.owner]+=quantity
            for owner,quantity in sells.items():
                if owner!=company.issuer:self._remove_long(owner,cid,quantity,ctx)
            for oid,q in result.buy_fills.items():
                owner=prior[oid].owner;quantity=q//1000;buys[owner]+=quantity
                financed=self.finance.orders.get(oid)
                if financed is not None and financed.kind=='cover':continue
                if owner==company.issuer:self._buyback(cid,quantity,ctx)
                else:
                    key=(owner,cid,self.s.rates[company.issuer]);cost=result.buyer_cost_book_ru_mU[oid]
                    if key not in self.long_lots:self.long_lots[key]=LongLot(0,Q(0))
                    self.long_lots[key].shares+=quantity;self.long_lots[key].book_ru_mU+=cost
            self.finance.on_clear(cid,result,prior,ctx)
            self.external_issues[cid].extend(new_issues)
            for buyer,seller,paid in result.cash_flows:
                ru=paid*self.s.rates[company.issuer]
                if buyer in self.s.macro.cycles:self.s.macro.cycles[buyer].portfolio_inflow_ru_mU-=ru
                if seller in self.s.macro.cycles:self.s.macro.cycles[seller].portfolio_inflow_ru_mU+=ru
            for owner in set(sells)|set(buys)|{prior[oid].owner for oid in result.sell_fills}:
                if owner in self.reports:self.reports[owner].append({'company':cid,'bought':buys[owner],
                    'sold':sum(q//1000 for oid,q in result.sell_fills.items() if prior[oid].owner==owner),'price_mU':result.price_mU})
            self.close_history[cid].append(result.price_mU)
            self.turnover[cid].append((ctx.cycle,result.paid_mU,result.quantity_mU//1000))
        for order in tuple(self.exchange.orders.values()):
            if order.owner.startswith('npc:'):self.exchange.prepare_cancel(order.owner,order.id).commit()
        commit_buffers(self.shares)
        for cid,company in self.s.initial.companies.items():
            held=self.shares.balance(self.account(company.issuer,cid))
            if held:self.shares.transfer(self.account(company.issuer,cid),self.account(company.issuer,cid,'unissued'),held)
        self.issue_orders={oid:kind for oid,kind in self.issue_orders.items() if oid in self.exchange.orders}
        self._sync();self.finance.mark(ctx);self.last_cleared_cycle=ctx.cycle

    def long_book(self,owner,cycle):
        return sum((min(lot.book_ru_mU,lot.shares*self.conservative_price(cid,cycle)*rate)
                    for (p,cid,rate),lot in self.long_lots.items() if p==owner),Q(0))

    def external_equity_book(self,owner,cycle):
        return sum((max(row.book_ru_mU,row.shares*self.fair(cid,cycle,neutral=True)*row.entry_fx)
                    for cid,rows in self.external_issues.items() if self.s.initial.companies[cid].issuer==owner for row in rows),Q(0))

    def visible(self,owner):
        return {'financing':self.finance.visible(owner),'companies':[{'id':cid,'issuer':c.issuer,'sector':c.sector,'price_mU':self.exchange.prices[cid],
                              'shares_held':c.shares.get(owner,0)} for cid,c in self.s.initial.companies.items() if c.listed],
                'orders':[{'command_id':o.command_id,'company':o.venue,'side':o.side,'remaining_shares':o.remaining_mU//1000,
                           'limit_price_mU':o.limit_price_mU} for o in self.exchange.orders.values() if o.owner==owner],
                'fills':[dict(row) for row in self.reports[owner]],'npc_net_estimates_mU':dict(self.signals[owner])}

    def audit(self):
        self.shares.audit();self.exchange.audit();self.finance.audit()
        for cid,company in self.s.initial.companies.items():
            actual=self.ownership(cid)
            if actual!=company.shares or sum(actual.values())!=1000000 or actual.get(company.issuer,0)<510000:
                raise InvariantViolation('company physical share ownership or government floor failed')
            if sum(row.shares for row in self.external_issues[cid])!=1000000-actual.get(company.issuer,0):
                raise InvariantViolation('external equity basis does not match actual outside stock')
        for (owner,cid,_),lot in self.long_lots.items():
            if lot.shares<=0 or lot.book_ru_mU<0:raise InvariantViolation('invalid historical stock position')

    def snapshot(self):
        return {'financing':self.finance.snapshot(),'shares':self.shares.snapshot(),'exchange':self.exchange.snapshot(),'references':self.references,
                'pending':self.pending,'issue_orders':self.issue_orders,'listed_cycle':self.listed_cycle,
                'close_history':{c:list(rows) for c,rows in self.close_history.items()},
                'turnover':{c:list(rows) for c,rows in self.turnover.items()},'external_issues':self.external_issues,
                'long_lots':[[*key,lot] for key,lot in sorted(self.long_lots.items())],
                'position_rounding_steps':self.position_rounding_steps,'npc_plans':[[*k,v] for k,v in sorted(self.npc_plans.items())],
                'signals':self.signals,'reports':self.reports,'last_cycle':self.last_cycle,'last_cleared_cycle':self.last_cleared_cycle}


class StockCommands:
    OPERATIONS=frozenset({('finance','ipo'),('finance','stock_order')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        state=self.state;market=state.stock_market;cid=order.get('company')
        company=state.initial.companies.get(cid) if type(cid) is str else None
        if company is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if state.active_order_count(owner)>=state.catalog.runtime['limits']['active_orders_per_nation']:raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        command_id=str(int(order['id']))
        if order['op']=='ipo':
            fields(order,{'company':str,'qty':int,'min_price_mU':int})
            if company.issuer!=owner:raise RuleViolation('ERR_NO_PERMISSION')
            if 'F01' not in state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
            if company.listed or cid in market.pending:raise RuleViolation('ERR_NO_PERMISSION')
            if not 1<=order['qty']<=490000 or order['min_price_mU']<=0:raise RuleViolation('ERR_SCHEMA')
            reference=max(100,market.fair(cid,ctx.cycle,neutral=True))
            resting=market._order(owner,cid,'sell',order['qty'],order['min_price_mU'],command_id,ctx.tick,
                ctx.clock.total_ticks,source='unissued')
            prepared=market.exchange.prepare_issue(resting,tick=ctx.tick)
            def commit():
                prepared.commit();market.pending[cid]=reference;market.issue_orders[resting.id]='ipo'
            return PreparedCommand(commit)
        fields(order,{'company':str,'side':str,'qty':int,'limit_price_mU':int,'margin':bool,'expire_cycles':int})
        if (order['side'] not in ('buy','sell','short','cover') or order['qty']<=0 or order['limit_price_mU']<=0
                or not 1<=order['expire_cycles']<=20):raise RuleViolation('ERR_SCHEMA')
        if not company.listed:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if order['margin'] or order['side'] in ('short','cover'):
            if order['margin'] and order['side']!='buy':raise RuleViolation('ERR_SCHEMA')
            # 06 finance/stock_order: margin buys and short sales require the nation's own F03.
            if (order['margin'] or order['side']=='short') and 'F03' not in state.research[owner].known:
                raise RuleViolation('ERR_TECH_LOCKED')
            return market.finance.prepare(owner,order,ctx)
        if order['side']=='sell' and market.shares.balance(market.account(owner,cid))<order['qty']*1000 and market.finance.positions.get((owner,cid,'margin')):
            return market.finance.prepare(owner,order,ctx)
        resting=market._order(owner,cid,order['side'],order['qty'],order['limit_price_mU'],command_id,ctx.tick,
            ctx.tick+order['expire_cycles']*ctx.clock.cycle_ticks)
        return market.exchange.prepare_order(resting,tick=ctx.tick)

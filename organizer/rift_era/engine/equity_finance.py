"""Physical equity financing custody and shared-pool command funding.

Cash loans are requested from the same P1 pool batch as FX/IPO. Borrowed shares
come only from NPC available custody, and every unfilled reservation is returned.
"""
from collections import defaultdict
from dataclasses import dataclass,field,replace
from .debt_funding import FundingClaim
from .errors import InvariantViolation,RuleViolation
from .ledger import CashAccount,CashTransfer,Transfer,DeliveryPayment,CASH_BOOK_SCALE
from .numeric import Q,ceil,floor,proportional,max_feasible,split_payment
from .payments import PoolPayment
from .protocol import PreparedCommand
from .rational_carry import RationalCarry

INITIAL=Q(1,2)
MAINTENANCE=Q(3,10)

@dataclass
class Position:
    owner:str
    company:str
    kind:str
    shares:int=0
    debt_mU:int=0
    debt_book_ru_mU:Q=Q(0)
    entry_fx:Q=Q(1)
    lenders:dict=field(default_factory=dict)
    fees:dict=field(default_factory=dict)
    lender_basis:dict=field(default_factory=dict)
    debt_lots:dict=field(default_factory=dict)
    short_lots:dict=field(default_factory=dict)
    status:str='OPEN'

@dataclass
class FinancedOrder:
    id:str
    owner:str
    company:str
    command_id:str
    kind:str
    quantity:int
    price:int
    expires:int
    own_reserved:int
    borrowed_reserved:int=0
    borrowed_book:Q=Q(0)
    lenders:dict=field(default_factory=dict)
    position_key:tuple|None=None
    forced:bool=False
    lender_basis:dict=field(default_factory=dict)
    collateral_slice:int=0
    extra_reserved:int=0

class EquityFinance:
    def __init__(self,state):
        self.s=state;self.pending={};self.orders={};self.positions={};self.carries={};self.last_cycle=-1
        self.reports=[];self.pool_requests={};self.sequence=0;self.last_accrued_cycle=-1
        self.lender_lots={}
    def currency(self,cid):return self.s.initial.companies[cid].issuer
    def wallet(self,p,cid):return CashAccount(p,'treasury',self.currency(cid))
    def deposit(self,p,cid):return CashAccount(p,'equity_debt_service:'+cid,self.currency(cid))
    def escrow(self,p,cid):return CashAccount(p,'equity_collateral:'+cid,self.currency(cid))
    def funding(self,row):return CashAccount(row.owner,'equity_funding:'+row.id,self.currency(row.company))
    def own(self,row):return CashAccount(row.owner,'equity_own:'+row.id,self.currency(row.company))
    def key(self,p,cid,kind):return (p,cid,kind)
    def custody(self,p,cid,kind):return self.s.stock_market.account(p,cid,'equity_'+kind)
    def float(self,cid):return 1000000-self.s.stock_market.ownership(cid).get(self.currency(cid),0)
    def position(self,p,cid,kind):
        key=self.key(p,cid,kind)
        return self.positions.setdefault(key,Position(p,cid,kind,entry_fx=self.s.rates[self.currency(cid)]))
    def prepare(self,owner,order,ctx):
        market=self.s.stock_market;cid=order['company'];kind='margin' if order['side']=='buy' else 'liquidate_long' if order['side']=='sell' else order['side']
        if kind not in ('margin','short','cover','liquidate_long'):raise RuleViolation('ERR_SCHEMA')
        position=self.positions.get(self.key(owner,cid,'short' if kind=='cover' else 'margin' if kind=='liquidate_long' else kind))
        if position is not None and position.status=='LIQUIDATING' and kind not in ('cover','liquidate_long'):raise RuleViolation('ERR_NO_PERMISSION')
        qty=order['qty'];price=order['limit_price_mU']
        if kind=='liquidate_long':
            if position is None or qty>position.shares:raise RuleViolation('ERR_INSUFFICIENT_STOCK')
            source=self.custody(owner,cid,'margin')
            if market.shares.balance(source)<qty*1000:raise RuleViolation('ERR_INSUFFICIENT_STOCK')
            resting=market._order(owner,cid,'sell',qty,price,str(int(order['id'])),ctx.tick,ctx.tick+order['expire_cycles']*10,proceeds=self.escrow(owner,cid))
            resting=replace(resting,stock_account=source)
            stock=market.shares.prepare_transfers((Transfer(source,resting.stock_lock,qty*1000),))
            row=FinancedOrder(resting.id,owner,cid,str(int(order['id'])),kind,qty,price,resting.expires_tick,0,position_key=self.key(owner,cid,'margin'))
            def sell_commit():
                stock.commit();market.exchange.orders[resting.id]=resting;market.exchange._revision+=1;self.orders[resting.id]=row
            return PreparedCommand(sell_commit)
        if kind=='cover':
            reserved=sum(r.quantity for r in (*self.pending.values(),*self.orders.values()) if r.owner==owner and r.company==cid and r.kind=='cover')
            if position is None or qty>position.shares-reserved:raise RuleViolation('ERR_INSUFFICIENT_STOCK')
            budget=qty*price+ceil(qty*price*Q(1,500))
            # Reserve only this slice of collateral; other open shares remain
            # protected. Extra cost must be supplied from real free cash.
            total=self.s.initial.cash.balance(self.escrow(owner,cid)).native_mU
            available=floor(Q(total*qty,max(1,position.shares-reserved)))
            collateral=min(budget,available)
            oid='stock:'+owner+':'+str(int(order['id']))
            resting=market._order(owner,cid,'buy',qty,price,str(int(order['id'])),ctx.tick,ctx.tick+order['expire_cycles']*10,
                proceeds=self.escrow(owner,cid))
            resting=replace(resting,stock_account=self.custody(owner,cid,'cover'))
            money=[CashTransfer(self.escrow(owner,cid),resting.cash_lock,collateral),
                   CashTransfer(self.wallet(owner,cid),resting.cash_lock,budget-collateral)]
            plan=self.s.initial.cash.prepare_transfers(money)
            row=FinancedOrder(oid,owner,cid,str(int(order['id'])),kind,qty,price,resting.expires_tick,budget,position_key=self.key(owner,cid,'short'),collateral_slice=collateral,extra_reserved=budget-collateral)
            def commit():
                plan.commit();market.exchange.orders[oid]=resting;market.exchange._revision+=1;self.orders[oid]=row
            return PreparedCommand(commit)
        oid='stock:'+owner+':'+str(int(order['id']))
        budget=ceil(Q(qty*price,2))+ceil(qty*price*Q(1,500))
        row=FinancedOrder(oid,owner,cid,str(int(order['id'])),kind,qty,price,ctx.tick+order['expire_cycles']*10,budget)
        plan=self.s.initial.cash.prepare_transfers((CashTransfer(self.wallet(owner,cid),self.own(row),budget),))
        def commit():plan.commit();self.pending[oid]=row
        return PreparedCommand(commit)
    def pool_claims(self):
        self.pool_requests={};groups=defaultdict(dict)
        for oid,row in self.pending.items():
            if row.kind=='margin':groups[row.company][oid]=floor(Q(row.quantity*row.price,2))
        for cid,requests in groups.items():
            mark=self.s.stock_market.exchange.prices[cid]
            used=sum(p.debt_mU for p in self.positions.values() if p.company==cid and p.kind=='margin')+sum(r.borrowed_reserved for r in self.orders.values() if r.company==cid and r.kind=='margin')
            cap=max(0,floor(self.float(cid)*mark*Q(1,4))-used)
            self.pool_requests.update(proportional(cap,requests,key=(self.s.initial.map.seed,'margin_capacity',cid)))
        return tuple(PoolPayment('equity:'+oid,self.funding(self.pending[oid]),amount) for oid,amount in sorted(self.pool_requests.items()) if amount)
    def after_commands(self,ctx):
        market=self.s.stock_market;cash=self.s.initial.cash
        # All short applicants share a single actual NPC inventory snapshot.
        short_grants={};lenders={}
        groups=defaultdict(dict)
        for oid,r in self.pending.items():
            if r.kind=='short':groups[r.company][oid]=r.quantity
        for cid,requests in groups.items():
            available={market.npc(cid,k):market.shares.balance(market.account(market.npc(cid,k),cid))//1000 for k in ('Fund','Mom','Noise')}
            used=sum(p.shares for p in self.positions.values() if p.company==cid and p.kind=='short')+sum(r.quantity for r in self.orders.values() if r.company==cid and r.kind=='short')
            cap=min(sum(available.values()),max(0,self.float(cid)//4-used))
            grants=proportional(cap,requests,key=(ctx.seed,'short_capacity',ctx.tick,cid));short_grants.update(grants)
            remaining=dict(available)
            for oid,q in sorted(grants.items()):
                allocations=proportional(q,remaining,key=(ctx.seed,'short_lenders',ctx.tick,cid,oid))
                lenders[oid]=allocations
                for lender,n in allocations.items():remaining[lender]-=n
        for oid,row in tuple(self.pending.items()):
            owned=cash.balance(self.own(row)).native_mU;funded=cash.balance(self.funding(row)).native_mU
            if row.kind=='margin':
                q=max_feasible(row.quantity,lambda n:floor(Q(n*row.price,2))<=funded and ceil(Q(n*row.price,2))+ceil(n*row.price*Q(1,500))<=owned)
                borrow=floor(Q(q*row.price,2));row.borrowed_reserved=borrow
                before=cash.balance(self.funding(row));row.borrowed_book=(before.book_ru_mU*borrow/funded if funded else Q(0))
                resting=market._order(row.owner,row.company,'buy',q,row.price,row.command_id,ctx.tick,row.expires,proceeds=self.own(row)) if q else None
                if resting:resting=replace(resting,stock_account=self.custody(row.owner,row.company,'margin'))
                budget=q*row.price+ceil(q*row.price*Q(1,500));own_used=budget-borrow
                transfers=[CashTransfer(self.own(row),resting.cash_lock if resting else self.wallet(row.owner,row.company),own_used),
                    CashTransfer(self.funding(row),resting.cash_lock if resting else CashAccount('world','pool',self.currency(row.company)),borrow),
                    CashTransfer(self.funding(row),CashAccount('world','pool',self.currency(row.company)),funded-borrow),
                    CashTransfer(self.own(row),self.wallet(row.owner,row.company),owned-own_used)]
                cash.transfer_many(transfers)
            else:
                q=short_grants.get(oid,0);row.lenders={p:n for p,n in lenders.get(oid,{}).items() if n}
                resting=market._order(row.owner,row.company,'sell',q,row.price,row.command_id,ctx.tick,row.expires,proceeds=self.escrow(row.owner,row.company)) if q else None
                stock=[Transfer(market.account(p,row.company),resting.stock_lock,n*1000) for p,n in row.lenders.items()] if resting else []
                required=ceil(Q(q*row.price,2))
                DeliveryPayment.prepare(market.shares,cash,stock,[CashTransfer(self.own(row),self.escrow(row.owner,row.company),required),
                    CashTransfer(self.own(row),self.wallet(row.owner,row.company),owned-required)]).commit()
            if resting:
                row.quantity=q;market.exchange.orders[oid]=resting;market.exchange._revision+=1;self.orders[oid]=row
            del self.pending[oid]
        market._sync()
    def on_clear(self,cid,result,prior,ctx):
        market=self.s.stock_market;cash=self.s.initial.cash
        for oid,row in tuple(self.orders.items()):
            if row.company!=cid:continue
            quantity=(result.sell_fills if row.kind in ('short','liquidate_long') else result.buy_fills).get(oid,0)//1000
            if not quantity:continue
            if row.kind=='liquidate_long':
                pos=self.positions[row.position_key];old=pos.shares;pos.shares-=quantity
                amount=min(pos.debt_mU,result.seller_receipts.get(oid,0));escrow=self.escrow(row.owner,cid)
                cash.transfer(escrow,CashAccount('world','pool',self.currency(cid)),amount)
                removed=Q(round(pos.debt_book_ru_mU*Q(amount,pos.debt_mU)*CASH_BOOK_SCALE),CASH_BOOK_SCALE) if pos.debt_mU else Q(0)
                pos.debt_mU-=amount;pos.debt_book_ru_mU-=removed
                if pos.debt_lots:
                    parts=proportional(amount,{str(i):v[0] for i,v in enumerate(pos.debt_lots.values())},key=(ctx.seed,'margin_repay_lots',ctx.tick,cid,row.owner))
                    for i,(rate,(face,book)) in enumerate(tuple(pos.debt_lots.items())):
                        paid=parts[str(i)];cut=book*Q(paid,face) if face else Q(0)
                        if paid==face:del pos.debt_lots[rate]
                        else:pos.debt_lots[rate]=(face-paid,book-cut)
                if not pos.shares:
                    if pos.debt_mU and hasattr(self.s,'payables'):
                        creditor=CashAccount('world','pool',self.currency(cid))
                        self.s.payables.add(pos.owner,creditor,self.currency(cid),pos.debt_mU,reason='margin_bad_debt',book_ru_mU=pos.debt_book_ru_mU)
                        pos.debt_mU=0;pos.debt_book_ru_mU=Q(0);pos.debt_lots.clear()
                    pos.status='CLOSED' if not pos.debt_mU else 'LIQUIDATING'
            elif row.kind=='margin':
                pos=self.position(row.owner,cid,'margin');debt=min(row.borrowed_reserved,floor(Q(result.buyer_payments[oid],2)))
                basis=Q(round(row.borrowed_book*Q(debt,row.borrowed_reserved)*CASH_BOOK_SCALE),CASH_BOOK_SCALE) if row.borrowed_reserved else Q(0)
                pos.shares+=quantity;pos.debt_mU+=debt;pos.debt_book_ru_mU+=basis
                rate=self.s.rates[self.currency(cid)];old_debt,old_basis=pos.debt_lots.get(rate,(0,Q(0)))
                pos.debt_lots[rate]=(old_debt+debt,old_basis+basis)
                row.borrowed_reserved-=debt;row.borrowed_book-=basis
            elif row.kind=='short':
                pos=self.position(row.owner,cid,'short');parts=proportional(quantity,row.lenders,key=(ctx.seed,'short_fill_lenders',ctx.tick,oid))
                for p,q in parts.items():pos.lenders[p]=pos.lenders.get(p,0)+q;row.lenders[p]-=q
                pos.shares+=quantity
                rate=self.s.rates[self.currency(cid)];pos.short_lots[rate]=pos.short_lots.get(rate,0)+quantity
                # The stock physically moved to buyer; lender basis moves into
                # its return claim, never a second economic company share.
                for lender,q in parts.items():
                    if not q:continue
                    lots=[(key,lot) for key,lot in market.long_lots.items() if key[:2]==(lender,cid)]
                    allocation=proportional(q,{str(i):lot.shares for i,(_,lot) in enumerate(lots)},key=(ctx.seed,'lender_basis',ctx.tick,oid,lender))
                    for i,(key,lot) in enumerate(lots):
                        n=allocation[str(i)]
                        if not n:continue
                        basis=lot.book_ru_mU*Q(n,lot.shares)
                        claim=(lender,key[2]);old=pos.lender_basis.get(claim,(0,Q(0)))
                        pos.lender_basis[claim]=(old[0]+n,old[1]+basis)
                        lot.shares-=n;lot.book_ru_mU-=basis
                        if not lot.shares:del market.long_lots[key]
            else:
                pos=self.positions[row.position_key];parts=proportional(quantity,pos.lenders,key=(ctx.seed,'cover_return',ctx.tick,oid))
                stock=[Transfer(self.custody(row.owner,cid,'cover'),market.account(lender,cid),q*1000) for lender,q in parts.items() if q]
                market.shares.transfer_many(stock)
                for lender,q in parts.items():
                    pos.lenders[lender]-=q
                    lots={str(i):v[0] for i,(k,v) in enumerate(sorted(pos.lender_basis.items())) if k[0]==lender}
                    keys={str(i):k for i,(k,v) in enumerate(sorted(pos.lender_basis.items())) if k[0]==lender}
                    allocated=proportional(q,lots,key=(ctx.seed,'cover_lender_basis',ctx.tick,oid,lender))
                    for name,n in allocated.items():
                        if not n:continue
                        claim=keys[name];count,basis=pos.lender_basis[claim];removed=basis*Q(n,count)
                        from .stock_market import LongLot
                        lot=market.long_lots.setdefault((lender,cid,claim[1]),LongLot(0,Q(0)))
                        lot.shares+=n;lot.book_ru_mU+=removed
                        if count==n:del pos.lender_basis[claim]
                        else:pos.lender_basis[claim]=(count-n,basis-removed)
                old=pos.shares;pos.shares-=quantity
                if pos.short_lots:
                    lots=proportional(quantity,{str(i):q for i,q in enumerate(pos.short_lots.values())},key=(ctx.seed,'cover_fx_lots',ctx.tick,oid))
                    for i,(rate,n) in enumerate(tuple(pos.short_lots.items())):
                        left=n-lots[str(i)]
                        if left:pos.short_lots[rate]=left
                        else:del pos.short_lots[rate]
                # Cash already reserved for this cover is the only releasable
                # slice. Unrelated collateral stays frozen for remaining shares.
                slice_used=floor(Q(row.collateral_slice*quantity,row.quantity))
                extra_used=floor(Q(row.extra_reserved*quantity,row.quantity))
                actual=result.buyer_payments.get(oid,0)+result.buyer_tariffs.get(oid,0)
                refund=max(0,slice_used+extra_used-actual)
                escrow=self.escrow(row.owner,cid)
                if oid not in market.exchange.orders:
                    cash.transfer(escrow,self.wallet(row.owner,cid),min(refund,cash.balance(escrow).native_mU))
                row.collateral_slice-=slice_used;row.extra_reserved-=extra_used
                if not pos.shares:pos.status='CLOSED'
            row.quantity-=quantity
            if not row.quantity or oid not in market.exchange.orders:self.finish(oid,ctx)
        market._sync()
    def finish(self,oid,ctx):
        row=self.orders.pop(oid,None)
        if row is None:return
        market=self.s.stock_market;cash=self.s.initial.cash
        order=market.exchange.orders.get(oid)
        if order is not None:
            if row.kind=='short':
                # Return unmatched borrowed shares to actual lenders.
                stock=[Transfer(order.stock_lock,market.account(p,row.company),q*1000) for p,q in row.lenders.items() if q]
                market.shares.transfer_many(stock)
                market.exchange.orders.pop(oid);market.exchange._revision+=1
            else:market.exchange.prepare_cancel(row.owner,oid).commit()
        if row.kind=='cover':
            escrow=self.escrow(row.owner,row.company)
            extra=min(row.extra_reserved,self.s.initial.cash.balance(escrow).native_mU)
            self.s.initial.cash.transfer(escrow,self.wallet(row.owner,row.company),extra)
        if row.kind=='margin':
            # Cancelled loan principal still has a real borrower cash lock or
            # refund. Return what remains; any consumed principal stays debt.
            held=self.own(row);amount=min(row.borrowed_reserved,cash.balance(held).native_mU)
            cash.transfer(held,CashAccount('world','pool',self.currency(row.company)),amount)
            cash.transfer(held,self.wallet(row.owner,row.company),cash.balance(held).native_mU)
        elif row.kind=='short':
            unlock=ceil(Q(row.quantity*row.price,2));escrow=self.escrow(row.owner,row.company)
            cash.transfer(escrow,self.wallet(row.owner,row.company),min(unlock,cash.balance(escrow).native_mU))
    def begin_tick(self,ctx):
        for oid,row in tuple(self.orders.items()):
            if row.expires<=ctx.tick:self.finish(oid,ctx)
        self.s.stock_market._sync()
        if not hasattr(ctx,'seed'):return
        # Maintenance failures produce orders in the next auction only. Every
        # forced order uses the same priority and band boundary, so peers are
        # allocated proportionally by the existing auction engine.
        market=self.s.stock_market
        for key,pos in sorted(self.positions.items()):
            if pos.status!='LIQUIDATING' or not pos.shares:continue
            if any(r.position_key==key and r.forced for r in self.orders.values()):continue
            previous=market.exchange.prices[pos.company]
            if pos.kind=='short':
                price=max(1,ceil(previous*Q(6,5)))
                cash=self.s.initial.cash.balance(self.escrow(pos.owner,pos.company)).native_mU+self.s.initial.cash.balance(self.wallet(pos.owner,pos.company)).native_mU
                quantity=max_feasible(pos.shares,lambda n:n*price+ceil(n*price*Q(1,500))<=cash)
                if not quantity:continue
                self.sequence+=1
                order={'id':str(10**30+self.sequence),'company':pos.company,'side':'cover','qty':quantity,'limit_price_mU':price,'expire_cycles':1}
                self.prepare(pos.owner,order,ctx).commit()
                oid='stock:'+pos.owner+':'+order['id'];self.orders[oid].forced=True
                market.exchange.orders[oid]=replace(market.exchange.orders[oid],priority=0)
            else:
                quantity=market.shares.balance(self.custody(pos.owner,pos.company,'margin'))//1000
                if not quantity:continue
                self.sequence+=1;seq=str(10**30+self.sequence)
                order=market._order(pos.owner,pos.company,'sell',quantity,max(1,floor(previous*Q(4,5))),seq,ctx.tick,ctx.tick+10,proceeds=self.escrow(pos.owner,pos.company))
                order=replace(order,stock_account=self.custody(pos.owner,pos.company,'margin'),priority=0)
                plan=market.shares.prepare_transfers((Transfer(order.stock_account,order.stock_lock,quantity*1000),));plan.commit()
                market.exchange.orders[order.id]=order;market.exchange._revision+=1
                self.orders[order.id]=FinancedOrder(order.id,pos.owner,pos.company,seq,'liquidate_long',quantity,order.limit_price_mU,order.expires_tick,0,position_key=key,forced=True)
    def interest_exact(self,key,pos):
        currency=self.currency(pos.company);mark=self.s.stock_market.exchange.prices[pos.company]
        return (pos.debt_mU*Q(max(0,self.s.interest_bp[currency]+200),1000000) if pos.kind=='margin' else
                pos.shares*mark*Q(2,10000))
    def funding_claims(self,ctx):
        claims=[]
        for key,pos in self.positions.items():
            if pos.status=='CLOSED' and not pos.fees:continue
            amount=sum(pos.fees.values())
            if ctx.cycle>self.last_accrued_cycle:
                amount+=self.carries.get(key,RationalCarry()).prepare(self.interest_exact(key,pos)).due
            if amount:claims.append(FundingClaim('equity_fee:'+pos.owner+':'+pos.company+':'+pos.kind,pos.owner,self.currency(pos.company),0,amount,self.deposit(pos.owner,pos.company)))
        return tuple(claims)
    def accrue(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick) or ctx.cycle<=self.last_accrued_cycle:return
        self.last_accrued_cycle=ctx.cycle
        for key,pos in self.positions.items():
            if pos.status=='CLOSED':continue
            due=self.carries.setdefault(key,RationalCarry()).add(self.interest_exact(key,pos))
            if pos.kind=='margin':pos.fees['world']=pos.fees.get('world',0)+due
            else:
                parts=split_payment(due,pos.lenders,key=(ctx.seed,'borrow_fee',ctx.cycle,pos.owner,pos.company)) if pos.lenders else {}
                for p,q in parts.items():pos.fees[p]=pos.fees.get(p,0)+q
    def settle(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick) or ctx.cycle<=self.last_cycle:return
        self.accrue(ctx);self.last_cycle=ctx.cycle;cash=self.s.initial.cash
        grouped=defaultdict(dict)
        for key,pos in self.positions.items():
            for creditor,amount in pos.fees.items():grouped[pos.owner,pos.company][(key,creditor)]=amount
        for (owner,cid),dues in grouped.items():
            names={str(i):key for i,key in enumerate(sorted(dues))}
            deposit=self.deposit(owner,cid)
            paid=proportional(cash.balance(deposit).native_mU,{n:dues[k] for n,k in names.items()},key=(ctx.seed,'equity_fee_pay',ctx.cycle,owner,cid))
            transfers=[]
            for name,q in paid.items():
                key,creditor=names[name];pos=self.positions[key]
                target=CashAccount('world','pool',self.currency(cid)) if creditor=='world' else self.s.stock_market.cash(creditor,self.currency(cid))
                transfers.append(CashTransfer(deposit,target,q));pos.fees[creditor]-=q
                pos.fees={p:a for p,a in pos.fees.items() if a}
            cash.transfer_many(transfers)
        self.mark(ctx)
    def mark(self,ctx):
        cash=self.s.initial.cash
        for key,pos in self.positions.items():
            if pos.status=='CLOSED':continue
            value=pos.shares*self.s.stock_market.exchange.prices[pos.company]
            collateral=cash.balance(self.escrow(pos.owner,pos.company)).native_mU
            collateral+=sum(self.s.initial.cash.balance(self.s.stock_market.exchange.orders[r.id].cash_lock).native_mU
                for r in self.orders.values() if r.kind=='cover' and r.position_key==key and r.id in self.s.stock_market.exchange.orders)
            ratio=Q(value-pos.debt_mU,value) if pos.kind=='margin' and value else Q(collateral-value,value) if value else Q(1)
            if ratio<MAINTENANCE:pos.status='LIQUIDATING'
    def debt_book(self,owner):
        return sum((p.debt_book_ru_mU+sum(p.fees.values())*self.s.rates[self.currency(p.company)] for p in self.positions.values() if p.owner==owner),Q(0))
    def visible(self,owner):return {'positions':[{**p.__dict__,'lender_basis':None,'debt_lots':[[rate,n,b] for rate,(n,b) in sorted(p.debt_lots.items())],'short_lots':[[rate,n] for rate,n in sorted(p.short_lots.items())]} for p in self.positions.values() if p.owner==owner],'orders':[r for r in self.orders.values() if r.owner==owner]}
    def snapshot(self):return {'pending':self.pending,'orders':self.orders,'positions':[[*k,{**p.__dict__,'lender_basis':[[*claim,n,b] for claim,(n,b) in sorted(p.lender_basis.items())],'debt_lots':[[rate,n,b] for rate,(n,b) in sorted(p.debt_lots.items())],'short_lots':[[rate,n] for rate,n in sorted(p.short_lots.items())]}] for k,p in sorted(self.positions.items())],
        'carries':[[*k,c.snapshot()] for k,c in sorted(self.carries.items())],'last_cycle':self.last_cycle,'last_accrued_cycle':self.last_accrued_cycle}
    def audit(self):
        for p in self.positions.values():
            if p.shares<0 or p.debt_mU<0 or p.debt_book_ru_mU<0:raise InvariantViolation('negative equity financing position')
            if p.kind=='short' and p.shares!=sum(p.lenders.values()):raise InvariantViolation('borrowed share claim does not equal short')
        for c in self.carries.values():c.audit()
    def dividend_compensation(self,cid,declared_mU,ctx):
        """Independent owed cash to actual lenders, never extra company shares."""
        from .ledger import CashAccount
        for key,pos in self.positions.items():
            if pos.company!=cid or pos.kind!='short' or not pos.shares:continue
            exact=Q(declared_mU*pos.shares,1000000)
            carry=self.carries.setdefault(('dividend',*key),RationalCarry());amount=carry.add(exact)
            parts=split_payment(amount,pos.lenders,key=(ctx.seed,'short_dividend',ctx.cycle,*key))
            for lender,q in parts.items():
                if q:pos.fees[lender]=pos.fees.get(lender,0)+q
    def short_liability_book(self,owner,cycle):
        return sum((self.s.stock_market.exchange.prices[p.company]*(sum((n*rate for rate,n in p.short_lots.items()),Q(0)) if p.short_lots else p.shares*p.entry_fx)
                    for p in self.positions.values() if p.owner==owner and p.kind=='short'),Q(0))
    def lender_claim_book(self,lender):
        return sum((basis for p in self.positions.values() for (name,fx),(quantity,basis) in p.lender_basis.items() if name==lender),Q(0))

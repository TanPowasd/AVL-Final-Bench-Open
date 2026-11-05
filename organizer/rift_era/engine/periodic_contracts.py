"""Physical two-depot periodic barter with escrowed first obligations.

An exchange changes custody at the declared depot; neither leg moves across the
map. Company-owned goods must be purchased before a sovereign can pledge them.
"""
from dataclasses import dataclass
from .command_schema import fields
from .errors import RuleViolation
from .ledger import Account,Transfer,CashAccount,CashTransfer,DeliveryPayment
from .protocol import PreparedCommand
from .numeric import Q


@dataclass(frozen=True)
class SwapProposal:
    id:str
    proposer:str
    counterparty:str
    command_id:str
    hub_a:str
    hub_b:str
    good_a:str
    good_b:str
    quantity_a:int
    quantity_b:int
    period_cycles:int
    duration_cycles:int
    expires_cycle:int
    cash_currency:str|None=None
    cash_amount:int=0
    cash_side:str|None=None


@dataclass
class PeriodicSwap:
    proposal:SwapProposal
    start_cycle:int
    end_cycle:int
    next_cycle:int
    status:str='ACTIVE'
    delivered_periods:int=0
    defaulted_cycle:int=-1
    blocked_by:tuple[str,...]=()
    penalized_parties:tuple[str,...]=()

    def lock(self,owner,hub,good):return Account(owner,hub,'contract_swap:'+self.proposal.id,good)


class PeriodicContracts:
    def __init__(self,state):self.s=state;self.rows={};self.pending=[];self.last_cycle=-1

    @staticmethod
    def wallet(owner,currency):return CashAccount(owner,'reserves' if currency=='RU' else 'treasury',currency)

    def cash_lock(self,p):
        owner=p.proposer if p.cash_side=='a' else p.counterparty
        return CashAccount(owner,'contract_swap_cash:'+p.id,p.cash_currency)

    def validate(self,p):
        state=self.s
        for owner,hub,good,q in ((p.proposer,p.hub_a,p.good_a,p.quantity_a),(p.counterparty,p.hub_b,p.good_b,p.quantity_b)):
            if p.cash_side==('a' if owner==p.proposer else 'b'):continue
            if hub not in state.initial.map.hubs:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            if not state.authority.controls_hub(owner,hub):raise RuleViolation('ERR_NO_PERMISSION')
            other=p.counterparty if owner==p.proposer else p.proposer
            if not state.export_controls.permits(other,owner,owner,good):raise RuleViolation('ERR_NO_PERMISSION')
            stock=state.initial.inventory.materials.composition(Account(owner,hub,'available',good))
            if any(o.economic_owner is not None for o in stock):raise RuleViolation('ERR_NO_PERMISSION','barter requires government-owned supplies')
            if any(not state.export_controls.permits(other,owner,o.nation,good) for o in stock):
                raise RuleViolation('ERR_NO_PERMISSION','actual origin embargo blocks barter delivery')

    def obligations(self,row,*,locked):
        p=row.proposal
        legs=((p.proposer,p.counterparty,p.hub_a,p.good_a,p.quantity_a),(p.counterparty,p.proposer,p.hub_b,p.good_b,p.quantity_b))
        return tuple(Transfer(row.lock(owner,hub,good) if locked else Account(owner,hub,'available',good),
            Account(other,hub,'available',good) if locked else row.lock(owner,hub,good),q,retitle=locked)
            for owner,other,hub,good,q in legs if p.cash_side!=('a' if owner==p.proposer else 'b'))

    def cash_obligation(self,p,*,locked):
        if p.cash_side is None:return ()
        owner=p.proposer if p.cash_side=='a' else p.counterparty
        other=p.counterparty if p.cash_side=='a' else p.proposer
        return (CashTransfer(self.cash_lock(p) if locked else self.wallet(owner,p.cash_currency),
            self.wallet(other,p.cash_currency) if locked else self.cash_lock(p),p.cash_amount),)

    def prepare_accept(self,p,ctx):
        self.validate(p);row=PeriodicSwap(p,ctx.cycle,ctx.cycle+p.duration_cycles,ctx.cycle)
        plan=DeliveryPayment.prepare(self.s.initial.inventory,self.s.initial.cash,self.obligations(row,locked=False),self.cash_obligation(p,locked=False))
        def accept():
            plan.commit();self.rows[p.id]=row;self.pending.append(p.id)
            self.s.contracts.log(p,'SIGNED');del self.s.contracts.proposals[p.id]
        return PreparedCommand(accept)

    def settle(self,ctx):
        # Contracts whose two actual obligations were reserved are delivered as
        # one material transaction after command streams. New receipts cannot
        # finance or supply other accepts within that command batch.
        ready=[];money=[];permitted={}
        for pid in self.pending:
            row=self.rows[pid]
            permitted[pid]=self.allowed(row)
            if permitted[pid]:
                ready.extend(self.obligations(row,locked=True));money.extend(self.cash_obligation(row.proposal,locked=True))
            else:
                ready.extend(self.refunds(row))
                money.extend(CashTransfer(t.destination,t.source,t.amount) for t in self.cash_obligation(row.proposal,locked=False))
        DeliveryPayment.prepare(self.s.initial.inventory,self.s.initial.cash,ready,money).commit()
        for pid in self.pending:
            row=self.rows[pid]
            if permitted[pid]:row.delivered_periods+=1;row.next_cycle=ctx.cycle+row.proposal.period_cycles
            else:row.status='SUSPENDED'
        self.pending.clear()

    def allowed(self,row):
        p=row.proposal
        for owner,other,hub,good in ((p.proposer,p.counterparty,p.hub_a,p.good_a),(p.counterparty,p.proposer,p.hub_b,p.good_b)):
            if p.cash_side==('a' if owner==p.proposer else 'b'):continue
            origins=self.s.initial.inventory.materials.composition(row.lock(owner,hub,good))
            if any(not self.s.export_controls.permits(other,owner,o.nation,good) for o in origins):return False
        return True

    def refunds(self,row):
        return tuple(Transfer(t.destination,t.source,t.quantity) for t in self.obligations(row,locked=False))

    def begin_tick(self,ctx):
        if ctx.tick%ctx.clock.cycle_ticks:return
        if ctx.cycle<=self.last_cycle:return
        self.last_cycle=ctx.cycle
        for row in self.rows.values():
            if row.status in ('EXPIRED','DEFAULTED'):continue
            if ctx.cycle>=row.end_cycle:row.status='EXPIRED';continue
            if ctx.cycle<row.next_cycle:continue
            p=row.proposal
            try:
                self.validate(p)
                plan=DeliveryPayment.prepare(self.s.initial.inventory,self.s.initial.cash,self.obligations(row,locked=False),self.cash_obligation(p,locked=False))
            except RuleViolation:
                row.status='SUSPENDED'
                offenders=[]
                for owner,good in ((p.proposer,p.good_a),(p.counterparty,p.good_b)):
                    other=p.counterparty if owner==p.proposer else p.proposer
                    if good and not self.s.export_controls.direct_allowed(other,owner,good):offenders.append(owner)
                row.blocked_by=tuple(offenders)
                if row.defaulted_cycle!=ctx.cycle:
                    for owner in set(offenders)-set(row.penalized_parties):self.s.credit[owner]=max(Q(1,10),self.s.credit[owner]-Q(15,100))
                    row.penalized_parties=tuple(sorted(set(row.penalized_parties)|set(offenders)))
                    row.defaulted_cycle=ctx.cycle
                continue
            plan.commit();self.pending.append(p.id);row.status='ACTIVE'

    def offer(self,owner,order,ctx):
        state=self.s;book=state.contracts;other=order['counterparty'];p=order['params']
        common={'hub_a','hub_b','period_cycles'}
        cash_fields={'currency','amount_mU','cash_side'}
        cash=bool(cash_fields & p.keys())
        required=common|({'good_b','qty_b_mU'} if cash and p.get('cash_side')=='a' else {'good_a','qty_a_mU'} if cash else {'good_a','qty_a_mU','good_b','qty_b_mU'})|(cash_fields if cash else set())
        if (other==owner or not 10<=order['duration_cycles']<=720 or set(p)!=required or
                any(type(p[k]) is not str for k in ('hub_a','hub_b')) or
                type(p['period_cycles']) is not int or not 1<=p['period_cycles']<=100):raise RuleViolation('ERR_SCHEMA')
        if cash and (p['cash_side'] not in ('a','b') or type(p['currency']) is not str or p['currency'] not in ('RU',*state.initial.known) or
                     type(p['amount_mU']) is not int or p['amount_mU']<=0):raise RuleViolation('ERR_SCHEMA')
        if other not in state.initial.known:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        good_a=p.get('good_a','');good_b=p.get('good_b','');qty_a=p.get('qty_a_mU',0);qty_b=p.get('qty_b_mU',0)
        for side,good,q in (('a',good_a,qty_a),('b',good_b,qty_b)):
            if cash and p['cash_side']==side:continue
            if type(good) is not str or type(q) is not int or q<=0 or good not in state.catalog.goods or state.catalog.goods[good]['kind']!='inventory':raise RuleViolation('ERR_SCHEMA')
            if state.catalog.goods[good]['indivisible'] and q%1000:raise RuleViolation('ERR_SCHEMA')
        if any(book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation'] for party in (owner,other)):raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        proposal=SwapProposal(f'swap_proposal:{owner}:{int(order["id"])}',owner,other,str(int(order['id'])),p['hub_a'],p['hub_b'],
            good_a,good_b,qty_a,qty_b,p['period_cycles'],order['duration_cycles'],ctx.cycle+order['duration_cycles'],
            p.get('currency'),p.get('amount_mU',0),p.get('cash_side'))
        self.validate(proposal)
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def visible(self,owner):return [r for r in self.rows.values() if owner in (r.proposal.proposer,r.proposal.counterparty)]
    def snapshot(self):return {'rows':self.rows,'pending':self.pending,'last_cycle':self.last_cycle}

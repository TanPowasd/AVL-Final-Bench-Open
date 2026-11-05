"""Ephemeral, locally powered inference delivered against real RU escrow.

P1 bids lock their entire maximum invoice. Each P5 can deliver only that
tick's completed market compute, nations before finite-cash NPC demand. P6
consumers use that receipt once; it can neither enter inventory nor be resold.
"""
from collections import defaultdict
from dataclasses import dataclass, replace

from .ai import TASK_TECH
from .command_schema import fields
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import CashAccount, CashTransfer
from .numeric import Q, ceil, floor, max_feasible
from .protocol import PreparedCommand


USES=frozenset({'research','production','finance','cyber'})


@dataclass(frozen=True)
class ServiceOffer:
    owner: str
    command_id: str
    quantity_mU: int
    price_ru_mU: int


@dataclass(frozen=True)
class ServiceBid:
    owner: str
    command_id: str
    hub: str
    use: str
    remaining_mU: int
    max_price_ru_mU: int
    end_cycle: int

    @property
    def lock(self):
        return CashAccount(self.owner,'ai_service:'+self.command_id,'RU')


@dataclass(frozen=True)
class ServiceUse:
    id: str
    tick: int
    buyer: str
    seller: str
    source_hub: str
    target_hub: str | None
    use: str
    quantity_mU: int
    paid_ru_mU: int
    capability: Q
    credit: Q


class AIServices:
    def __init__(self,state):
        self.s=state
        self.offers={}
        self.bids={}
        self.used={}
        self.residuals={}
        self.cycle=-1
        self.events=[]
        self.consumed={}
        self.npc_remaining_mU=0
        self.npc_quantum_remainder=Q(0)
        self.last_tick=-1
        self.recent={p:[] for p in state.research}

    def begin_tick(self,ctx):
        self.events=[]
        self.consumed={}
        if self.cycle!=ctx.cycle:
            if self.bids:raise InvariantViolation('expired inference bids retained past cycle')
            self.cycle=ctx.cycle;self.used={}
            exact=50000*(1+Q(ctx.cycle,720))+self.npc_quantum_remainder
            self.npc_remaining_mU=floor(exact);self.npc_quantum_remainder=exact-self.npc_remaining_mU

    @staticmethod
    def reserve_account(owner):return CashAccount(owner,'reserves','RU')

    def cancel(self,owner,command_id):
        bid=self.bids.get((owner,command_id))
        if bid:
            payment=self.s.initial.cash.prepare_transfers((CashTransfer(bid.lock,self.reserve_account(owner),
                self.s.initial.cash.balance(bid.lock).native_mU),))
            def commit():payment.commit();del self.bids[owner,command_id]
            return PreparedCommand(commit)
        offer=self.offers.get(owner)
        if offer and offer.command_id==command_id:return PreparedCommand(lambda:self.offers.pop(owner))
        return None

    def clear(self,ctx):
        if ctx.tick<=self.last_tick:raise InvariantViolation('inference supply consumed twice')
        state=self.s;cash=state.initial.cash
        networks={p:state.systems.communication_components(p) for p in state.research}
        supplies={};capabilities={}
        for (owner,hub),tasks in state.compute.effective.items():
            offer=self.offers.get(owner)
            cap=state.ai.capability(owner)
            if offer is None or not cap or not tasks.get('market',0):continue
            supplies[owner,hub]=floor(tasks['market']);capabilities[owner]=cap
        limits={p:max(0,o.quantity_mU-self.used.get(p,0)) for p,o in self.offers.items()}
        residuals=dict(self.residuals);spent=defaultdict(int);taken=defaultdict(int)
        remaining={k:b.remaining_mU for k,b in self.bids.items()}
        money=[];events=[];used=dict(self.used)

        def deliver(buyer,bid_key,site,requested):
            seller,hub=site;offer=self.offers[seller]
            if seller==buyer or not state.export_controls.permits(buyer,seller,seller,'inference_service'):return
            bid=self.bids[bid_key] if bid_key is not None else None
            if bid is not None and state.ai.suspended_until[buyer]>ctx.cycle:return
            # A source without an online communication endpoint only serves
            # its physical Hub. Foreign remote buyers need a real endpoint.
            if bid is None:
                if hub not in networks[seller]:return
            elif hub!=bid.hub and (hub not in networks[seller] or bid.hub not in networks[buyer]):return
            payer=bid.lock if bid else CashAccount('world','pool','RU')
            key=(buyer,seller)
            balance=cash.balance(payer).native_mU-spent[payer]
            maximum=min(requested,supplies[site],limits[seller])
            amount=max_feasible(maximum,lambda q:floor(Q(q*offer.price_ru_mU,1000)+residuals.get(key,Q(0)))<=balance)
            if not amount:return
            exact=Q(amount*offer.price_ru_mU,1000)+residuals.get(key,Q(0))
            paid=floor(exact);residuals[key]=exact-paid
            money.append(CashTransfer(payer,CashAccount(seller,'corp:tech','RU'),paid));spent[payer]+=paid
            supplies[site]-=amount;limits[seller]-=amount;used[seller]=used.get(seller,0)+amount
            if bid:
                remaining[bid_key]-=amount;taken[bid_key]+=amount
            events.append(ServiceUse(f'service:{ctx.tick}:{len(events)}',ctx.tick,buyer,seller,hub,
                bid.hub if bid else None,bid.use if bid else 'npc_civilian',amount,paid,capabilities[seller],state.credit[seller]))

        for key,bid in sorted(self.bids.items(),key=lambda row:(-row[1].max_price_ru_mU,
                digest(ctx.seed,'inference_bid',ctx.tick,row[1].owner,row[1].command_id))):
            for site in sorted(supplies,key=lambda site:(self.offers[site[0]].price_ru_mU,
                    digest(ctx.seed,'inference_offer',ctx.tick,*site))):
                if self.offers[site[0]].price_ru_mU<=bid.max_price_ru_mU:
                    deliver(bid.owner,key,site,remaining[key])
        npc_remaining=self.npc_remaining_mU
        for site in sorted(supplies,key=lambda site:(Q(self.offers[site[0]].price_ru_mU,1000)/
                (capabilities[site[0]]/10),digest(ctx.seed,'inference_npc',ctx.tick,*site))):
            seller,_=site
            if Q(self.offers[seller].price_ru_mU,1000)<=capabilities[seller]/2:
                before=len(events);deliver('world',None,site,npc_remaining)
                if len(events)>before:npc_remaining-=events[-1].quantity_mU
        updates={}
        for key,bid in self.bids.items():
            if not remaining[key] or ctx.clock.is_cycle_end(ctx.tick):
                money.append(CashTransfer(bid.lock,self.reserve_account(bid.owner),
                    cash.balance(bid.lock).native_mU-spent[bid.lock]))
                updates[key]=None
            else:updates[key]=replace(bid,remaining_mU=remaining[key])
        # All cash, capacities, permissions and quantization were pure above.
        cash.prepare_transfers(money).commit()
        self.last_tick=ctx.tick;self.events=events;self.used=used;self.consumed={}
        state.ai_deployment.record_services(events)
        self.residuals={k:v for k,v in residuals.items() if v};self.npc_remaining_mU=npc_remaining
        for key,bid in updates.items():
            if bid:self.bids[key]=bid
            else:
                del self.bids[key]
                state.command_streams[key[0]].plans.pop(key[1],None)
        for event in events:
            if event.use in ('npc_civilian','finance'):self.consumed[event.id]=Q(event.quantity_mU)
            self._record_macro(event)
            self.recent[event.seller].append({'side':'sell','buyer':event.buyer,'qty_mU':event.quantity_mU,
                'paid_ru_mU':event.paid_ru_mU,'tick':ctx.tick})
            if event.buyer in self.recent:
                self.recent[event.buyer].append({'side':'buy','seller':event.seller,'qty_mU':event.quantity_mU,
                    'paid_ru_mU':event.paid_ru_mU,'use':event.use,'hub_id':event.target_hub,'tick':ctx.tick})
        for p in self.recent:self.recent[p]=self.recent[p][-64:]

    def _record_macro(self,event):
        state=self.s;source=state.macro.cycles[event.seller]
        value=Q(event.paid_ru_mU)
        base=event.quantity_mU*10*state.catalog.weights['compute']
        source.output_native_mU+=value/state.rates[event.seller];source.output_base_mU+=base
        source.exports_ru_mU+=value
        from .economic_ownership import record_income
        record_income(state,(CashAccount(event.seller,'corp:tech','RU'),),(event.paid_ru_mU,))
        if event.buyer in state.macro.cycles:
            target=state.macro.cycles[event.buyer]
            target.imports_ru_mU+=value
            target.input_native_mU+=value/state.rates[event.buyer];target.input_base_mU+=base

    def received(self,owner,hub,use):
        return tuple(e for e in self.events if (e.buyer,e.target_hub,e.use)==(owner,hub,use))

    def consume(self,events,fraction=Q(1)):
        if not 0<=fraction<=1:raise InvariantViolation('invalid inference-use fraction')
        for event in events:
            amount=self.consumed.get(event.id,Q(0))+event.quantity_mU*fraction
            if amount>event.quantity_mU:raise InvariantViolation('inference service consumed twice')
            self.consumed[event.id]=amount

    def scoring_events(self):
        return tuple(replace(event,quantity_mU=floor(self.consumed.get(event.id,Q(0)))) for event in self.events
                     if floor(self.consumed.get(event.id,Q(0))))

    def visible(self,owner):
        return {'offers':[{'seller':p,'qty_mU':max(0,o.quantity_mU-self.used.get(p,0)),
                           'price_ru_mU':o.price_ru_mU} for p,o in sorted(self.offers.items())],
                'bids':[{'command_id':b.command_id,'hub_id':b.hub,'use':b.use,'remaining_mU':b.remaining_mU,
                         'max_price_ru_mU':b.max_price_ru_mU,'end_cycle':b.end_cycle}
                        for (p,_),b in self.bids.items() if p==owner],
                'recent':[dict(r) for r in self.recent[owner]]}

    def audit(self):
        if any(not 0<=v<1 for v in self.residuals.values()):raise InvariantViolation('invalid inference invoice remainder')
        if any(v<0 for v in self.used.values()) or self.npc_remaining_mU<0:raise InvariantViolation('negative inference allocation')
        events={event.id:event for event in self.events}
        if any(eid not in events or not 0<=q<=events[eid].quantity_mU for eid,q in self.consumed.items()):
            raise InvariantViolation('inference consumption exceeds actual delivery')
        for owner,offer in self.offers.items():
            if offer.owner!=owner or offer.quantity_mU<=0 or offer.price_ru_mU<=0:
                raise InvariantViolation('invalid inference quote')
        expected={b.lock for b in self.bids.values()}
        actual={a for a in self.s.initial.cash.balances() if a.name.startswith('ai_service:')}
        if not actual<=expected:raise InvariantViolation('orphan inference bid escrow')

    def snapshot(self):
        return {'offers':self.offers,'bids':[[*k,b] for k,b in sorted(self.bids.items())],
                'used':self.used,'residuals':[[*k,v] for k,v in sorted(self.residuals.items())],
                'cycle':self.cycle,'events':self.events,'consumed':self.consumed,'npc_remaining_mU':self.npc_remaining_mU,
                'npc_quantum_remainder':self.npc_quantum_remainder,'last_tick':self.last_tick,'recent':self.recent}


class AIServiceCommands:
    OPERATIONS=frozenset({('ai','service_offer'),('ai','service_bid')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        state=self.state;book=state.ai_services;cid=str(int(order['id']))
        if state.active_order_count(owner)>=state.catalog.runtime['limits']['active_orders_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if order['op']=='service_offer':
            fields(order,{'qty_mU':int,'price_ru_mU':int},{'replaces_id':str})
            if order['qty_mU']<=0 or order['price_ru_mU']<=0:raise RuleViolation('ERR_SCHEMA')
            if 'AI04' not in state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
            if state.ai.active[owner] is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            allocated=sum(d.allocations.get('market',0) for d in state.compute.deployments.values() if d.owner==owner)
            if order['qty_mU']>allocated:raise RuleViolation('ERR_CAPACITY_EXCEEDED')
            old=book.offers.get(owner)
            if 'replaces_id' in order and (old is None or old.command_id!=order['replaces_id']):raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
            offer=ServiceOffer(owner,cid,order['qty_mU'],order['price_ru_mU'])
            def commit():
                if old and 'replaces_id' not in order:state.command_streams[owner].plans.pop(old.command_id,None)
                book.offers[owner]=offer
            return PreparedCommand(commit,persistent=True)
        fields(order,{'qty_mU':int,'max_price_ru_mU':int,'use':str},{'hub_id':str})
        use=order['use'];hub=order.get('hub_id',f'{owner}-h0')
        if use not in USES or order['qty_mU']<=0 or order['max_price_ru_mU']<=0:raise RuleViolation('ERR_SCHEMA')
        if TASK_TECH[use] not in state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
        if state.ai.suspended_until[owner]>ctx.cycle:raise RuleViolation('ERR_NO_PERMISSION','inference deployment is suspended')
        if not state.authority.controls_hub(owner,hub):raise RuleViolation('ERR_NO_PERMISSION')
        if use=='production':
            deployment=state.compute.deployments.get((owner,hub))
            if deployment is None or not deployment.lines:raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if use=='research' and not any(l.owner==owner and l.hub==hub and 'research_points' in l.recipe.outputs
                                      for l in state.initial.lines.values()):raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        bid=ServiceBid(owner,cid,hub,use,order['qty_mU'],order['max_price_ru_mU'],ctx.cycle)
        payment=state.initial.cash.prepare_transfers((CashTransfer(book.reserve_account(owner),bid.lock,
            ceil(Q(bid.remaining_mU*bid.max_price_ru_mU,1000))),))
        def commit():payment.commit();book.bids[owner,cid]=bid
        return PreparedCommand(commit,persistent=True)

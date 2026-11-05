"""Paid, non-recursive data watermarks and model-weight copy licensing.

The wire discriminator is ``license_kind``: JSON cannot contain both a top
level kind='ai' and another kind='weights'. See ai_license_command_v1.
Offers are frozen at P8; accepted cash is locked before cross-seat settlement.
"""
from dataclasses import dataclass

from .ai import Model
from .command_schema import fields
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import CashAccount, CashTransfer
from .numeric import Q, floor
from .protocol import PreparedCommand


LICENSE_GOODS={'weights':'model_weights','data':'data_license'}


@dataclass(frozen=True)
class LicenseOffer:
    seller: str
    kind: str
    command_id: str
    price_ru_mU: int
    target: str | None


@dataclass(frozen=True)
class PublishedLicense:
    offer: LicenseOffer
    model: Model | None
    human_data: Q


@dataclass(frozen=True)
class AcceptedLicense:
    buyer: str
    command_id: str
    source: PublishedLicense
    lock: CashAccount


class AILicenses:
    def __init__(self,state):
        self.s=state
        self.offers={}
        self.published={}
        self.pending=[]
        self.cooldowns={}
        self.recent={p:[] for p in state.research}

    def reserved_slots(self,owner):
        return sum(p.buyer==owner and p.source.offer.kind=='weights' for p in self.pending)

    def publish(self):
        current={}
        for key,offer in self.offers.items():
            ai=self.s.ai
            model=ai.models[offer.seller].get(ai.active[offer.seller])
            if offer.kind=='weights' and model is None:continue
            current[key]=PublishedLicense(offer,model if offer.kind=='weights' else None,
                                         ai.data[offer.seller].curated_human if offer.kind=='data' else Q(0))
        self.published=current

    def cancel(self,owner,command_id):
        offer=next((o for o in self.offers.values() if o.seller==owner and o.command_id==command_id),None)
        if offer is None:return None
        return PreparedCommand(lambda:self.offers.pop((owner,offer.kind)))

    def settle(self,ctx):
        state=self.s;cash=state.initial.cash
        money=[];outcomes=[]
        for accepted in self.pending:
            published=accepted.source;offer=published.offer;buyer=accepted.buyer
            live=self.offers.get((offer.seller,offer.kind))
            allowed=(live==offer and state.export_controls.permits(buyer,offer.seller,offer.seller,LICENSE_GOODS[offer.kind]))
            target=CashAccount(offer.seller if allowed else buyer,'reserves','RU')
            money.append(CashTransfer(accepted.lock,target,offer.price_ru_mU))
            outcomes.append((accepted,allowed))
        # One preflight prevents new seller proceeds from financing this batch.
        cash.transfer_many(money)
        for accepted,allowed in outcomes:
            published=accepted.source;offer=published.offer;buyer=accepted.buyer
            mid=None
            if allowed:
                if offer.kind=='data':
                    self.s.ai.data[buyer].license(offer.seller,published.human_data)
                    self.cooldowns[buyer,offer.seller]=ctx.cycle+100
                else:
                    mid='model:'+digest('licensed',buyer,accepted.command_id).hex()[:24]
                    self.s.ai.models[buyer][mid]=published.model.copy_for(mid,buyer,ctx.cycle,Q(3,5))
            record={'command_id':accepted.command_id,'seller':offer.seller,'license_kind':offer.kind,
                    'status':'FULFILLED' if allowed else 'BLOCKED_REFUNDED','price_ru_mU':offer.price_ru_mU,
                    'model_id':mid}
            self.recent[buyer].append(record);self.recent[buyer]=self.recent[buyer][-64:]
            # The seller gets no buyer-private model ID or command sequence.
            self.recent[offer.seller].append({'buyer':buyer,'license_kind':offer.kind,
                'status':record['status'],'price_ru_mU':offer.price_ru_mU})
            self.recent[offer.seller]=self.recent[offer.seller][-64:]
        self.pending.clear()

    def visible(self,owner):
        return {'offers':[{'seller':o.offer.seller,'license_kind':o.offer.kind,
                           'price_ru_mU':o.offer.price_ru_mU,'target':o.offer.target}
                          for _,o in sorted(self.published.items()) if o.offer.target in (None,owner) or o.offer.seller==owner],
                'data_cooldowns':[{'seller':seller,'until_cycle':cycle} for (buyer,seller),cycle in sorted(self.cooldowns.items()) if buyer==owner],
                'received_data_mU':{s:floor(q*1000) for s,q in self.s.ai.data[owner].licensed_from.items()},
                'recent':[dict(r) for r in self.recent[owner]]}

    def audit(self):
        for (seller,kind),offer in self.offers.items():
            if (offer.seller,offer.kind)!=(seller,kind) or kind not in LICENSE_GOODS or offer.price_ru_mU<0:
                raise InvariantViolation('invalid AI license offer')
        expected={p.lock:p.source.offer.price_ru_mU for p in self.pending if p.source.offer.price_ru_mU}
        actual={a:b.native_mU for a,b in self.s.initial.cash.balances().items() if a.name.startswith('ai_license:')}
        if expected!=actual:raise InvariantViolation('AI license escrow disagrees with pending acceptances')

    def snapshot(self):
        def pairs(table):return [[*k,v] for k,v in sorted(table.items())]
        return {'offers':pairs(self.offers),'published':pairs(self.published),'pending':self.pending,
                'cooldowns':pairs(self.cooldowns),'recent':self.recent}


class AILicenseCommands:
    OPERATIONS=frozenset({('ai','license_offer'),('ai','license_accept')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        state=self.state;book=state.ai_licenses;kind=order.get('license_kind')
        if type(kind) is not str or kind not in LICENSE_GOODS:raise RuleViolation('ERR_SCHEMA')
        if 'AI01' not in state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
        cid=str(int(order['id']))
        if order['op']=='license_offer':
            fields(order,{'license_kind':str,'price_ru_mU':int,'target':(str,type(None))},{'replaces_id':str})
            target=order['target']
            if order['price_ru_mU']<0 or target==owner:raise RuleViolation('ERR_SCHEMA')
            if target is not None and target not in state.research:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            if kind=='weights' and state.ai.active[owner] is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            old=book.offers.get((owner,kind))
            if 'replaces_id' in order and (old is None or old.command_id!=order['replaces_id']):
                raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
            offer=LicenseOffer(owner,kind,cid,order['price_ru_mU'],target)
            def commit():
                if old and 'replaces_id' not in order:state.command_streams[owner].plans.pop(old.command_id,None)
                book.offers[owner,kind]=offer
            return PreparedCommand(commit,persistent=True)
        fields(order,{'seller':str,'license_kind':str,'max_price_ru_mU':int})
        seller=order['seller'];published=book.published.get((seller,kind))
        if seller==owner or order['max_price_ru_mU']<=0:raise RuleViolation('ERR_SCHEMA')
        if published is None or published.offer.target not in (None,owner):raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        offer=published.offer
        if book.offers.get((seller,kind))!=offer:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if offer.price_ru_mU>order['max_price_ru_mU']:raise RuleViolation('ERR_SCHEMA')
        if not state.export_controls.permits(owner,seller,seller,LICENSE_GOODS[kind]):raise RuleViolation('ERR_NO_PERMISSION')
        if kind=='weights':
            if len(state.ai.models[owner])+int(owner in state.ai.training)+book.reserved_slots(owner)>=16:
                raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        elif (ctx.cycle<book.cooldowns.get((owner,seller),-1) or any(
                p.buyer==owner and p.source.offer.seller==seller and p.source.offer.kind=='data' for p in book.pending)):
            raise RuleViolation('ERR_NO_PERMISSION','source data license is cooling down')
        lock=CashAccount(owner,'ai_license:'+cid,'RU')
        payment=state.initial.cash.prepare_transfers((CashTransfer(CashAccount(owner,'reserves','RU'),lock,offer.price_ru_mU),))
        accepted=AcceptedLicense(owner,cid,published,lock)
        def accept():
            payment.commit();book.pending.append(accepted)
        return PreparedCommand(accept)

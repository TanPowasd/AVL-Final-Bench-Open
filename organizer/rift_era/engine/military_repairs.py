"""Cycle-end military repairs with P1 custody and actual fractional input use."""
from collections import defaultdict
from dataclasses import dataclass

from .capital_book import CAPITAL_BOOK_SCALE
from .errors import InvariantViolation
from .ledger import Account,Transfer
from .numeric import Q,ceil,floor,max_feasible,proportional,units
from .provenance import Use


@dataclass
class RepairRequest:
    unit:str
    formation:str
    owner:str
    source_location:str
    physical_location:str
    field:bool
    gain_mU:int
    coefficients:dict

    def lock(self,good):return Account(self.owner,self.source_location,f'locked_unit_repair:{self.unit}',good)
    def source(self,good):return Account(self.owner,self.source_location,'available',good)


class MilitaryRepairs:
    def __init__(self,state):
        self.s=state
        self.credits={}
        self.credit_owners={}
        self.requests={}
        self.reserved_tick=-1
        self.last_tick=-1

    def credit_account(self,uid,good,owner=None):
        return Account(owner or self.credit_owners[uid],uid,'unit_repair_credit',good)

    def virtual_stock(self):
        return {self.credit_account(uid,good):q for (uid,good),q in self.credits.items() if q}

    def integer_need(self,uid,good,quantity):
        return max(0,ceil(quantity-self.credits.get((uid,good),Q(0))))

    def reserve(self,ctx):
        if self.requests:raise InvariantViolation('unsettled military repair reservation')
        self.reserved_tick=ctx.tick
        if not ctx.clock.is_cycle_end(ctx.tick):return
        state=self.s;inventory=state.initial.inventory
        candidates={};demand=defaultdict(dict)
        for formation in state.formations.rows.values():
            friendly=(formation.hub is not None and state.authority.controls_hub(formation.owner,formation.hub)
                      and not state.movement.planned(formation.id))
            field=(formation.slot_b=='medical' and all(inventory.balance(Account(formation.owner,formation.id,'refit:B',g))>=1000
                                                     for g in ('basic_parts','medical_supplies')))
            if not friendly and not field:continue
            factor=Q(1,4) if friendly else Q(1,10) if 'G02' in state.research[formation.owner].known else Q(1,20)
            for uid in formation.unit_ids:
                unit=state.initial.units[uid];maximum=units(state.catalog.units[unit.unit_type]['hp'])
                gain=min(maximum-unit.hp_mU,floor(maximum*factor))
                if gain<=0 or unit.hp_mU<=0:continue
                coefficients={'basic_parts':Q(1000,maximum),'medical_supplies':Q(200,maximum)}
                if formation.slot_b=='composite':coefficients['composite']=Q(200,maximum)
                request=RepairRequest(uid,formation.id,formation.owner,formation.hub if friendly else formation.id,
                                      state.movement.location(formation),not friendly,gain,coefficients)
                candidates[uid]=request
                for good,rate in coefficients.items():
                    demand[request.source(good)][uid]=self.integer_need(uid,good,rate*gain)
        grants={}
        for source,quantities in sorted(demand.items()):
            allocated=proportional(inventory.balance(source),quantities,
                key=(ctx.seed,'military_repair_stock',ctx.tick,source.owner,source.location,source.asset))
            grants.update({(uid,source.asset):q for uid,q in allocated.items()})
        transfers=[];requests={}
        for uid,request in sorted(candidates.items()):
            gain=max_feasible(request.gain_mU,lambda h:all(self.integer_need(uid,g,r*h)<=grants[uid,g]
                                                         for g,r in request.coefficients.items()))
            if not gain:continue
            request.gain_mU=gain;requests[uid]=request
            transfers.extend(Transfer(request.source(g),request.lock(g),self.integer_need(uid,g,rate*gain),
                                      destination_hub=request.physical_location) for g,rate in request.coefficients.items())
        inventory.transfer_many(transfers)
        self.requests=requests

    def advance(self,ctx):
        if ctx.tick<=self.last_tick:raise InvariantViolation('military repair tick repeated or backdated')
        self.last_tick=ctx.tick
        state=self.s;inventory=state.initial.inventory
        for uid,request in sorted(self.requests.items()):
            unit=state.initial.units.get(uid)
            formation=state.formations.rows.get(request.formation)
            eligible=unit is not None and formation is not None and unit.hp_mU>0
            if eligible and not request.field:
                eligible=formation.hub==request.source_location and state.authority.controls_hub(request.owner,formation.hub)
            if eligible:
                maximum=units(state.catalog.units[unit.unit_type]['hp'])
                gain=min(request.gain_mU,maximum-unit.hp_mU)
                if gain:
                    consumed={};draft=inventory.materials.draft();credits={}
                    for good,rate in request.coefficients.items():
                        exact=rate*gain;take=self.integer_need(uid,good,exact)
                        credit=self.credit_account(uid,good,request.owner)
                        draft.add(credit,draft.take(request.lock(good),Q(take)),virtual=True)
                        draft.consume(credit,exact,use=Use.REPAIR,reason='military_repair')
                        consumed[request.lock(good)]=take
                        credits[uid,good]=self.credits.get((uid,good),Q(0))+take-exact
                    inventory.transform(consumed,{},reason='military_repair',material_plan=draft.finish())
                    self.credit_owners[uid]=request.owner
                    for key,value in credits.items():
                        if value:self.credits[key]=value
                        else:self.credits.pop(key,None)
                    limit=sum(Q(q)*1000000*state.catalog.weights[g] for g,q in state.catalog.units[unit.unit_type]['installed'].items())
                    value=sum(q*state.catalog.weights[g] for g,q in unit.book_nmU.items())
                    invested=sum(rate*gain*1000*state.catalog.weights[g] for g,rate in request.coefficients.items())
                    ratio=min(Q(1),max(Q(0),limit-value)/invested) if invested else Q(0)
                    for good,rate in request.coefficients.items():
                        addition=Q(floor(rate*gain*1000*ratio*CAPITAL_BOOK_SCALE),CAPITAL_BOOK_SCALE)
                        unit.book_nmU[good]=unit.book_nmU.get(good,Q(0))+addition
                        unit.book_rounding_steps+=1
                    unit.hp_mU+=gain;formation.version+=1
                    if not any(key[0]==uid for key in self.credits):self.credit_owners.pop(uid,None)
            location=request.physical_location
            if request.field:
                location=state.movement.location(formation) if formation else state.movement.wrecks.get(request.formation,{}).get('location',location)
            inventory.transfer_many(Transfer(request.lock(g),request.source(g),inventory.balance(request.lock(g)),destination_hub=location)
                                    for g in request.coefficients)
        self.requests.clear()

    def destroy_credits(self,uid):
        inventory=self.s.initial.inventory;draft=inventory.materials.draft();value=Q(0)
        keys=[key for key in self.credits if key[0]==uid]
        for _,good in keys:
            quantity=self.credits[uid,good]
            draft.consume(self.credit_account(uid,good),quantity,use=Use.DESTRUCTION,reason='military_repair_kit_destroyed')
            value+=quantity*self.s.catalog.weights[good]/1000
        if keys:
            inventory.transform({}, {},reason='military_repair_kit_destroyed',material_plan=draft.finish())
            for key in keys:del self.credits[key]
            self.credit_owners.pop(uid,None)
        return value

    def audit(self):
        live=self.s.initial.units.keys()|self.s.formations.stored_units.keys()
        if set(self.credit_owners)-live:raise InvariantViolation('repair kit belongs to an absent unit')
        for (uid,good),quantity in self.credits.items():
            if not 0<quantity<1 or self.s.initial.inventory.materials.quantity(self.credit_account(uid,good))!=quantity:
                raise InvariantViolation('military repair credit differs from actual prepaid material')

    def snapshot(self):
        return {'credits':[[uid,g,q] for (uid,g),q in sorted(self.credits.items())],
                'credit_owners':self.credit_owners,'requests':self.requests,
                'reserved_tick':self.reserved_tick,'last_tick':self.last_tick}

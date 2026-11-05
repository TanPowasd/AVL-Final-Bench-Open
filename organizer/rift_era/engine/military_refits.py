"""Two-cycle physical refits, with old hardware retained until completion."""
from dataclasses import dataclass,field
from collections import defaultdict

from .command_schema import fields,version
from .determinism import digest
from .errors import InvariantViolation,RuleViolation
from .ledger import Account,Transfer,CashAccount,CashTransfer,DeliveryPayment
from .numeric import Q,ceil
from .protocol import PreparedCommand

# 03 §3. Precision mode has no invented permanent ammunition installation:
# actual precision rounds are reserved and consumed by each firing tick.
SLOTS_A={None:({},()),'radio':({'communication_equipment':1000},('G01',)),
         'sensor':({'sensor':2000},()),'radar':({'radar_module':1000},('G05',)),
         'ew':({'ew_module':1000},('G09',))}
SLOTS_B={None:({},()),'precision_ammunition':({},()),'composite':({'composite':2000},('I04',)),
         'medical':({'medical_supplies':1000,'basic_parts':1000},())}


@dataclass
class Refit:
    id:str
    command_id:str
    owner:str
    formation:str
    hub:str
    slot_a:str|None
    slot_b:str|None
    ready_tick:int
    materials:dict[str,int]
    reserved_capacity:Q
    prices:dict[str,Q]=field(default_factory=dict)
    waiting_warehouse:bool=False

    def lock(self,good):return Account(self.owner,self.hub,f'locked_refit:{self.id}',good)

    def cash_lock(self):return CashAccount(self.owner,'military_refit:'+self.id,self.owner)


class MilitaryRefits:
    def __init__(self,state):
        self.s=state
        self.jobs={}

    def capacity_reserved(self):
        result=defaultdict(lambda:Q(0))
        for job in self.jobs.values():result[job.hub]+=job.reserved_capacity
        return dict(result)

    def inactive_units(self):
        return frozenset(uid for f in self.s.formations.rows.values() if f.refitting for uid in f.unit_ids)

    def slot_account(self,formation,slot,good):
        return Account(formation.owner,formation.id,f'refit:{slot}',good)

    def apply_effects(self,formation):
        for uid in formation.unit_ids:
            unit=self.s.initial.units[uid]
            unit.precision=formation.slot_b=='precision_ammunition'
            unit.armor_bonus=8 if formation.slot_b=='composite' else 0
            unit.speed_factor=Q(23,20) if formation.slot_b=='composite' else Q(1)
            unit.fuel_factor=Q(17,20) if formation.slot_b=='composite' else Q(1)
            unit.sensor_bonus=Q(1,5) if formation.slot_a=='sensor' else Q(0)
            unit.asw_factor=Q(5,4) if formation.slot_a=='sensor' else Q(1)
            unit.electronic_warfare=formation.slot_a=='ew' and formation.emitter_active and formation.ew_mode=='jamming'

    def cancel(self,job):
        inventory=self.s.initial.inventory
        cash=self.s.initial.cash
        DeliveryPayment.prepare(inventory,cash,
            (Transfer(job.lock(g),Account(job.owner,job.hub,'available',g),inventory.balance(job.lock(g))) for g in job.materials),
            (CashTransfer(job.cash_lock(),self.s.native(job.owner,'treasury'),cash.balance(job.cash_lock()).native_mU),)).commit()
        formation=self.s.formations.rows.get(job.formation)
        if formation is not None:
            formation.refitting=False;formation.version+=1
        del self.jobs[job.id]

    def prepare_cancel(self,owner,command_id):
        job=next((j for j in self.jobs.values() if j.owner==owner and j.command_id==command_id),None)
        return PreparedCommand(lambda:self.cancel(job)) if job else None

    def advance(self,ctx):
        state=self.s;inventory=state.initial.inventory
        for job in sorted(tuple(self.jobs.values()),key=lambda j:digest(ctx.seed,'refit_finish',ctx.tick,j.id)):
            formation=state.formations.rows.get(job.formation)
            if formation is None:
                self.cancel(job);continue
            if ctx.tick+1<job.ready_tick:continue
            if formation.hub!=job.hub or not state.authority.controls_hub(job.owner,job.hub):continue
            transfers=[];old_mass=Q(0);new_mass=Q(0)
            for slot,old,new,table in (('A',formation.slot_a,job.slot_a,SLOTS_A),('B',formation.slot_b,job.slot_b,SLOTS_B)):
                if old==new:continue
                for good,quantity in table[old][0].items():
                    source=self.slot_account(formation,slot,good)
                    transfers.append(Transfer(source,Account(job.owner,job.hub,'available',good),quantity,retitle=True))
                    old_mass+=quantity*state.cargo_weights[good]
                for good,quantity in table[new][0].items():
                    transfers.append(Transfer(job.lock(good),self.slot_account(formation,slot,good),quantity,destination_hub=job.hub,retitle=True))
                    new_mass+=quantity*state.cargo_weights[good]
            occupied=state.warehouse_load()[job.hub]-job.reserved_capacity
            job.waiting_warehouse=(old_mass>new_mass and occupied+old_mass-new_mass>state.initial.warehouse_mU[job.hub])
            if job.waiting_warehouse:continue
            stock=inventory.prepare_transfers(transfers)
            from .economic_ownership import prepare_purchase,record_income
            inputs=tuple(d for d in stock.materials.deliveries if d.source.bucket==f'locked_refit:{job.id}')
            buyer=state.native(job.owner,'treasury')
            payment,recipients=prepare_purchase(state.payments,job.cash_lock(),inputs,job.prices,
                kind='military_refit',key=(ctx.seed,ctx.tick,'military_refit',job.id),buyer=buyer,payer_scope=buyer)
            payment.commit(stock)
            record_income(state,recipients,payment.amounts)
            state.payments.release(job.cash_lock(),buyer)
            if formation.slot_a not in ('radar','ew'):formation.emitter_active=True
            formation.slot_a,formation.slot_b=job.slot_a,job.slot_b
            formation.refitting=False;formation.version+=1
            self.apply_effects(formation)
            del self.jobs[job.id]

    def visible(self,owner):
        return [{'id':j.command_id,'formation_id':j.formation,'hub_id':j.hub,'ready_tick':j.ready_tick,
                 'slot_a':j.slot_a,'slot_b':j.slot_b,'waiting_warehouse':j.waiting_warehouse}
                for j in self.jobs.values() if j.owner==owner]

    def audit(self):
        active={j.formation for j in self.jobs.values()}
        if len(active)!=len(self.jobs):raise InvariantViolation('overlapping formation refits')
        for formation in self.s.formations.rows.values():
            if formation.refitting != (formation.id in active):raise InvariantViolation('refit shutdown differs from actual job')
            for slot,selected,table in (('A',formation.slot_a,SLOTS_A),('B',formation.slot_b,SLOTS_B)):
                for good,quantity in table[selected][0].items():
                    if self.s.initial.inventory.balance(self.slot_account(formation,slot,good))!=quantity:
                        raise InvariantViolation('military module lacks actual installed equipment')
        for job in self.jobs.values():
            if any(self.s.initial.inventory.balance(job.lock(g))!=q for g,q in job.materials.items()):
                raise InvariantViolation('refit materials differ from real custody')
        expected={j.cash_lock() for j in self.jobs.values()}
        if any(a.name.startswith('military_refit:') and a not in expected for a in self.s.initial.cash.balances()):
            raise InvariantViolation('orphan military refit cash escrow')

    def snapshot(self):return {'jobs':self.jobs}


class RefitCommands:
    OPERATIONS=frozenset({('military','refit_formation')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'formation_id':str,'slot_a':(str,type(None)),'slot_b':(str,type(None))},{'expected_version':int})
        state=self.state;refits=state.refits
        formation=state.formations.rows.get(order['formation_id'])
        if formation is None or formation.owner!=owner:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order,formation)
        if formation.refitting:raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        if formation.hub is None or not state.authority.controls_hub(owner,formation.hub):raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if order['slot_a'] not in SLOTS_A or order['slot_b'] not in SLOTS_B:raise RuleViolation('ERR_SCHEMA')
        if (formation.slot_a,formation.slot_b)==(order['slot_a'],order['slot_b']):
            return PreparedCommand(lambda:None)  # unchanged configuration never creates a free shutdown
        materials=defaultdict(int);old_mass=new_mass=Q(0)
        for old,new,table in ((formation.slot_a,order['slot_a'],SLOTS_A),(formation.slot_b,order['slot_b'],SLOTS_B)):
            if old==new:continue
            if not set(table[new][1])<=state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
            for g,q in table[old][0].items():old_mass+=q*state.cargo_weights[g]
            for g,q in table[new][0].items():materials[g]+=q;new_mass+=q*state.cargo_weights[g]
        reserved=max(Q(0),old_mass-new_mass)
        if reserved>max(Q(0),state.initial.warehouse_mU[formation.hub]-state.warehouse_load()[formation.hub]):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED','no capacity for physically returned modules')
        job=Refit('refit:'+digest(owner,str(int(order['id']))).hex()[:24],str(int(order['id'])),owner,formation.id,
                  formation.hub,order['slot_a'],order['slot_b'],ctx.tick+2*ctx.clock.cycle_ticks,dict(materials),reserved)
        job.prices={g:state.price(g,job.hub) for g in materials}
        stock=state.initial.inventory.prepare_transfers(Transfer(Account(owner,job.hub,'available',g),job.lock(g),q)
                                                             for g,q in materials.items())
        from .economic_ownership import income_account
        buyer=state.native(owner,'treasury')
        budget=ceil(sum((d.quantity_mU*job.prices[d.source.asset] for d in stock.materials.deliveries
                         if income_account(d.origin,owner,buyer.currency)!=buyer),Q(0)))
        cash=state.initial.cash.prepare_transfers((CashTransfer(buyer,job.cash_lock(),budget),))
        def commit():
            DeliveryPayment(stock,cash).commit();refits.jobs[job.id]=job
            formation.refitting=True;formation.version+=1
        return PreparedCommand(commit)

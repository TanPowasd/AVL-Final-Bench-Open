"""Actual signed catalog VA and read-only corporate valuation history.

Inventory output follows actual batches, including physical WIP. Electricity
and compute require a real civilian use; research/housing/data flow counters
and extraction never manufacture positive industrial VA. No method issues cash.
"""
from collections import defaultdict,deque
from dataclasses import dataclass,field

from .errors import InvariantViolation
from .numeric import Q,ceil,floor
from .public_systems import SYSTEMS


@dataclass
class Activity:
    asset: str
    company: str
    owner: str
    hub: str
    corporate_share: Q
    inputs_mU: dict[str,Q] = field(default_factory=dict)
    outputs_mU: dict[str,Q] = field(default_factory=dict)
    wages_native_mU: Q = Q(0)
    maintenance_native_mU: Q = Q(0)


class IndustryActivity:
    def __init__(self,state):
        self.s=state
        self.cycle=-1;self.last_tick=-1
        self.rows={};self.rates={}
        self.power_use={};self.compute_use={};self.compute_sources={};self.model_caps={}
        self.power_sources={}
        self.history={cid:deque(maxlen=20) for cid in state.initial.companies}
        self.last_asset_va={};self.last_company_va={};self.last_company_value={}
        self.power_need=defaultdict(lambda:Q(0));self.power_short=defaultdict(lambda:Q(0))
        self.shortage_history={cid:deque(maxlen=5) for cid in state.initial.companies}
        self.finance_used=defaultdict(lambda:Q(0));self.completed_finance={}

    def begin_tick(self,ctx):
        if ctx.cycle!=self.cycle:
            self.cycle=ctx.cycle;self.rows={};self.rates=dict(self.s.rates)
            self.power_need.clear();self.power_short.clear();self.finance_used.clear()
        self.power_use={};self.compute_use={};self.compute_sources={};self.model_caps={}
        self.power_sources={}

    def _row(self,entity,asset=None):
        state=self.s;account=state.operating_account(entity)
        if not account.name.startswith('corp:'):return None
        cid=account.owner+'-'+account.name.split(':',1)[1]
        if cid not in state.initial.companies:return None
        aid=asset or (state.validation_jobs[entity].lab_id if entity in state.validation_jobs else entity)
        key=(aid,cid)
        if key not in self.rows:
            site=state.initial.assets.get(aid)
            hub=site.hub if site else state.frame.work_context[entity][1]
            rights=state.authority.assets.get(aid)
            share=Q(rights.owner_shares.get(account.owner,0),1000) if rights else Q(1)
            self.rows[key]=Activity(aid,cid,account.owner,hub,share)
        return self.rows[key]

    @staticmethod
    def _add(table,good,amount):
        table[good]=table.get(good,Q(0))+amount

    def production(self,line,result):
        if line.recipe.id.startswith('extract:'):return
        row=self._row(line.id)
        if row is None:return
        for good,amount in result.consumed_nmU.items():self._add(row.inputs_mU,good,Q(amount,1000))
        for good,rate in line.recipe.outputs.items():
            if self.s.catalog.goods[good]['kind']=='inventory':
                self._add(row.outputs_mU,good,Q(rate*result.batch_mU,1000))

    def generator_input(self,entity,good,amount):
        row=self._row(entity)
        if row is not None:self._add(row.inputs_mU,good,amount)

    def wages(self,entity,amount):
        row=self._row(entity)
        if row is not None:row.wages_native_mU+=amount*row.corporate_share

    def maintenance(self,asset,amount):
        row=self._row(asset)
        if row is not None:row.maintenance_native_mU+=amount*row.corporate_share

    def use_power(self,entity,amount):
        if amount<0:raise InvariantViolation('negative useful electrical use')
        self.power_use[entity]=self.power_use.get(entity,Q(0))+amount

    def use_compute(self,owner,hub,task,amount):
        if amount<0:raise InvariantViolation('negative useful compute use')
        key=(owner,hub,task)
        self.compute_use[key]=self.compute_use.get(key,Q(0))+amount

    def compute_frame(self,ctx):
        state=self.s
        self.model_caps={p:state.ai.capability(p) for p in state.research}
        for receipt in state.frame.power_receipts:
            source=receipt.source.rsplit(':',1)[0]
            aid=source.split(':',1)[1] if source.startswith('storage:') else source
            row=self._row(source,aid)
            if row is not None:self.power_sources[source]=(row.asset,row.company)
        for store in state.storage.stores.values():
            entity=state.storage.entity(store.id)
            charge=state.frame.power_received.get(entity,0)
            if charge:
                self.use_power(entity,charge)
                row=self._row(entity,store.id)
                self._add(row.inputs_mU,'electricity',Q(charge))
        for entity,(key,_,band) in state.compute.frame_centers.items():
            row=state.compute.centers[key]
            record=self._row(entity,row.line_id)
            self.compute_sources[entity]=(record.asset,record.company,row.owner,row.center.hub,band)
        for entity,system,spec,available in state.systems.operations():
            if system.online and available and system.kind!='listening':
                self.use_power(entity,min(state.frame.power_received.get(entity,0),ceil(Q(spec.power_mU_cycle,ctx.clock.cycle_ticks))))
        for load in state.frame.power_loads:
            if load.id.startswith('municipal:'):self.use_power(load.id,state.frame.power_received.get(load.id,0))
            if load.band not in ('B2','B3','B4') or not load.demand_mU:continue
            if load.id in self.compute_sources:aid=self.compute_sources[load.id][0]
            elif load.id.startswith('ccs:'):aid=load.id.split(':',1)[1]
            else:aid=load.id
            row=self._row(load.id,aid)
            if row is None:continue
            need=Q(load.demand_mU)*row.corporate_share
            received=min(load.demand_mU,state.frame.power_received.get(load.id,0))
            short=need if state.grids[load.hub].recovering else Q(load.demand_mU-received)*row.corporate_share
            self.power_need[row.company]+=need;self.power_short[row.company]+=short

    def _meter_compute(self,ctx):
        state=self.s;used=defaultdict(lambda:Q(0));supplied=defaultdict(lambda:Q(0))
        for (owner,hub,task),amount in self.compute_use.items():
            available=state.compute.effective.get((owner,hub),{}).get(task,Q(0))
            if amount>available:raise InvariantViolation('compute task use exceeds actual receipt')
            used[owner,hub,'B5' if task=='train' else 'B4']+=amount
        finance=defaultdict(dict)
        networks={p:state.systems.communication_components(p) for p in state.research}
        for (owner,hub),tasks in state.compute.effective.items():
            if self.model_caps.get(owner,Q(0)):
                capital=owner+'-h0'
                connected=hub==capital or (hub in networks[owner] and capital in networks[owner] and
                                           networks[owner][hub]==networks[owner][capital])
                if connected:finance[owner][hub]=tasks.get('finance',Q(0))
                grid=tasks.get('grid',Q(0))
                if any(line.owner==owner and line.hub==hub and state.frame.generation.get(lid,0)
                       for lid,line in state.initial.lines.items() if lid in state.initial.plants):
                    threshold=max(Q(1000),Q(state.ai_deployment.current_grid_peak.get(hub,0)*ctx.clock.cycle_ticks,100))
                    used[owner,hub,'B4']+=min(grid,threshold/ctx.clock.cycle_ticks)
        for owner,sites in finance.items():
            total=sum(sites.values(),Q(0))
            remaining=max(Q(0),20000-self.finance_used[owner])
            ratio=min(Q(1),remaining/total) if total else Q(0)
            for hub,amount in sites.items():used[owner,hub,'B4']+=amount*ratio
            self.finance_used[owner]+=total*ratio
        for event in state.ai_services.events:
            if event.use!='cyber':used[event.seller,event.source_hub,'B4']+=state.ai_services.consumed.get(event.id,Q(0))
        for entity,(_,_,owner,hub,band) in self.compute_sources.items():
            supplied[owner,hub,band]+=state.compute.actual_by_entity.get(entity,Q(0))
        for site,amount in used.items():
            if amount>supplied[site]:raise InvariantViolation('useful compute exceeds physical local band supply')
        for entity,(aid,cid,owner,hub,band) in self.compute_sources.items():
            site=(owner,hub,band);actual=state.compute.actual_by_entity.get(entity,Q(0))
            ratio=used[site]/supplied[site] if supplied[site] else Q(0)
            self._add(self.rows[aid,cid].outputs_mU,'compute',actual*ratio)
            self.use_power(entity,state.frame.power_received.get(entity,0)*ratio)

    def finish_tick(self,ctx):
        if ctx.tick<=self.last_tick:raise InvariantViolation('industrial activity metered twice')
        state=self.s
        self._meter_compute(ctx)
        for receipt in state.frame.power_receipts:
            total=state.frame.power_received.get(receipt.load,0)
            useful=min(Q(total),self.power_use.get(receipt.load,Q(0)))
            if not total or not useful:continue
            source=receipt.source.rsplit(':',1)[0]
            key=self.power_sources.get(source)
            row=self.rows.get(key) if key else None
            if row is not None:self._add(row.outputs_mU,'electricity',Q(receipt.received_mU)*useful/total)
        self.last_tick=ctx.tick
        if not ctx.clock.is_cycle_end(ctx.tick):return
        by_company=defaultdict(lambda:Q(0));wages=defaultdict(lambda:Q(0));maintenance=defaultdict(lambda:Q(0));assets=defaultdict(lambda:Q(0))
        physical={p:state.initial.inventory.materials.physical_by_owner(p) for p in state.research}
        eta_cache={}
        from .scoring import eta_sink
        for row in self.rows.values():
            output=Q(0)
            for good,amount in row.outputs_mU.items():
                key=(row.owner,good)
                if key not in eta_cache:
                    eta_cache[key]=(Q(1) if good in ('electricity','compute') else eta_sink(physical[row.owner].get(good,Q(0)),
                        state.consumption.sink_debt.get(key,Q(0)),state.consumption.consumption20(row.owner,good)))
                eta=eta_cache[key]
                output+=amount*state.catalog.weights[good]*eta/1000
            value=output-sum((q*state.catalog.weights[g]/1000 for g,q in row.inputs_mU.items()),Q(0))
            assets[row.asset]+=value
            by_company[row.company]+=value*row.corporate_share
            wages[row.company]+=row.wages_native_mU
            maintenance[row.company]+=row.maintenance_native_mU
        values={}
        for cid,company in state.initial.companies.items():
            native_va=by_company[cid]*state.catalog.tables['scoring']['pillars']['w_ru_anchor']*1000/self.rates[company.issuer]
            reference=max(Q(15,100)*native_va,native_va-wages[cid])-maintenance[cid]
            values[cid]=reference;self.history[cid].append(reference)
            self.shortage_history[cid].append(self.power_short[cid]/self.power_need[cid] if self.power_need[cid] else Q(0))
        self.last_asset_va=dict(assets);self.last_company_va=dict(by_company);self.last_company_value=values
        self.completed_finance={p:(self.finance_used[p],state.ai.capability(p)) for p in state.research}

    def mean20(self,company):return sum(self.history[company],Q(0))/20

    def shortage5(self,company):return sum(self.shortage_history[company],Q(0))/5

    def audit(self):
        if set(self.history)!=set(self.s.initial.companies) or any(len(rows)>20 for rows in self.history.values()):
            raise InvariantViolation('industrial valuation history is unbounded or incomplete')
        for row in self.rows.values():
            if not 0<=row.corporate_share<=1 or row.company not in self.history:
                raise InvariantViolation('invalid corporate activity rights')
            if any(q<0 for q in (*row.inputs_mU.values(),*row.outputs_mU.values())):
                raise InvariantViolation('negative physical activity quantity')

    def snapshot(self):
        return {'cycle':self.cycle,'last_tick':self.last_tick,'rows':list(self.rows.values()),'rates':self.rates,
                'power_use':self.power_use,'compute_use':[[*k,v] for k,v in sorted(self.compute_use.items())],
                'power_sources':self.power_sources,
                'compute_sources':self.compute_sources,'model_caps':self.model_caps,
                'history':{c:list(rows) for c,rows in self.history.items()},'last_asset_va':self.last_asset_va,
                'last_company_va':self.last_company_va,'last_company_value':self.last_company_value,
                'power_need':dict(self.power_need),'power_short':dict(self.power_short),
                'shortage_history':{c:list(rows) for c,rows in self.shortage_history.items()},
                'finance_used':dict(self.finance_used),'completed_finance':self.completed_finance}

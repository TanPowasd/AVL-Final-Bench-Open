"""One-way consumers of local P3 compute, without labor/power self-funding.

P1 may reserve electricity for the declared automation ceiling. P6 production
is capped again using actual compute, and only actual replacement saves wages.
Displaced worker-time remains unavailable until the next cycle boundary.
"""
from collections import defaultdict
from dataclasses import replace

from .ai import TASK_TECH
from .command_schema import fields
from .compute_operations import Deployment, DC_RECIPES
from .errors import InvariantViolation, RuleViolation
from .numeric import Q, ceil, floor, max_feasible, proportional
from .protocol import PreparedCommand


TASKS=frozenset(TASK_TECH)-{'train'}


class AIDeployment:
    def __init__(self,state):
        self.s=state
        self.cycle=-1
        self.held={}
        self.nominal_batches={}
        self.nominal_labor={}
        self.planned={}
        self.actual={}
        self.current_grid_peak={}
        self.previous_grid_peak={}
        self.previous_capability={}
        self.current_capability={}
        self.last_effect_tick=-1
        self.current_services={}
        self.previous_tasks={}

    def begin_tick(self,ctx):
        if ctx.cycle!=self.cycle:
            self.held={}
            self.previous_grid_peak=self.current_grid_peak
            self.current_grid_peak={}
            self.previous_capability=self.current_capability
            self.current_capability={p:self.s.ai.capability(p) for p in self.s.research}
            self.current_services={}
            self.cycle=ctx.cycle
            self.s.fx.fee_discounts={p:self.finance_discount(p,f'{p}-h0') for p in self.s.research}
        self.nominal_batches={};self.nominal_labor={};self.planned={};self.actual={}

    def labor_supply(self,hub,skill,amount):
        return max(0,amount-ceil(self.held_at(hub,skill)))

    def eligible(self,lid):
        state=self.s;line=state.initial.lines.get(lid)
        return (line is not None and lid not in state.initial.plants and line.recipe.id not in DC_RECIPES
                and line.recipe.id!='__idle__' and state.catalog.recipes[line.recipe.id]['facility']!='实验室')

    def _replacement(self,owner,hub,quantity,demands,ctx,*,weighted_extra=Q(0)):
        deploy=self.s.compute.deployments.get((owner,hub))
        if deploy is None or not quantity and not weighted_extra:return {}
        maximum=Q(3,5) if 'AI10' in self.s.research[owner].known else Q(1,2)
        caps={lid:floor(demands[lid]*maximum) for lid in deploy.lines if lid in demands and self.eligible(lid)}
        amount=min(sum(caps.values()),floor(Q(2,100)*(self.s.ai.capability(owner)*quantity+weighted_extra)))
        return proportional(amount,caps,key=(ctx.seed,'automation',ctx.tick,owner,hub))

    def plan_labor(self,ctx,demands,batches):
        targets={lid for d in self.s.compute.deployments.values() for lid in d.lines}
        wanted={lid:sum(d.values()) for lid,d in demands.items() if lid in targets and lid in batches and self.eligible(lid)}
        self.nominal_labor.update(wanted)
        self.nominal_batches.update({lid:batches[lid] for lid in wanted})
        for (owner,hub),deployment in self.s.compute.deployments.items():
            # The ceiling never releases people in P1. A cancelled or
            # underpowered budget can only over-reserve ordinary electricity.
            quantity=Q(deployment.allocations.get('production',0),ctx.clock.cycle_ticks)
            if self.s.ai.suspended_until[owner]>ctx.cycle:quantity=Q(0)
            self.planned.update(self._replacement(owner,hub,quantity,wanted,ctx))
            if hasattr(self.s,'ai_services') and any((b.owner,b.hub,b.use)==(owner,hub,'production')
                                                   for b in self.s.ai_services.bids.values()):
                maximum=Q(3,5) if 'AI10' in self.s.research[owner].known else Q(1,2)
                for lid in deployment.lines:
                    if lid in wanted:self.planned[lid]=floor(wanted[lid]*maximum)

    def planned_batch(self,lid,workers,demand,fallback):
        if lid not in self.nominal_batches or not demand:return fallback
        return floor(self.nominal_batches[lid]*min(Q(1),Q(workers+self.planned.get(lid,0),demand)))

    def receive(self,ctx):
        if ctx.tick<=self.last_effect_tick:raise InvariantViolation('AI deployment effects repeated')
        self.last_effect_tick=ctx.tick
        for (owner,hub),tasks in self.s.compute.effective.items():
            self.actual.update(self._replacement(owner,hub,tasks.get('production',Q(0)),self.nominal_labor,ctx))

    def cap_batches(self,caps):
        f=self.s.frame
        for lid,nominal in self.nominal_batches.items():
            demand=self.nominal_labor[lid]
            if lid not in caps or not demand:continue
            workers=sum(f.labor.get(lid,{}).values())
            caps[lid]=min(caps[lid],floor(nominal*min(Q(1),Q(workers+self.actual.get(lid,0),demand))))

    def apply_services(self,ctx):
        for (owner,hub),deployment in self.s.compute.deployments.items():
            received=self.s.ai_services.received(owner,hub,'production')
            if not received:continue
            weighted=sum((e.capability*e.quantity_mU for e in received),Q(0))
            local=self.s.compute.effective.get((owner,hub),{}).get('production',Q(0))
            self.actual.update(self._replacement(owner,hub,local,self.nominal_labor,ctx,weighted_extra=weighted))

    def worked(self,lid,amount,ctx):
        if not self.actual.get(lid,0):return None
        f=self.s.frame;workers=f.labor.get(lid,{})
        reserved=sum(workers.values())
        maximum=Q(3,5) if 'AI10' in self.s.research[self.s.initial.lines[lid].owner].known else Q(1,2)
        replacement=min(Q(self.actual.get(lid,0)),amount*maximum)
        human=min(Q(reserved),max(Q(0),amount-replacement))
        baseline=min(Q(reserved),Q(amount))
        result={skill:Q(floor(Q(quantity*human,reserved)*10**12),10**12) if reserved else Q(0)
                for skill,quantity in workers.items()}
        # Count only time actually replaced by compute, not general idleness.
        for skill,quantity in workers.items():
            displaced=Q(quantity*(baseline-human),reserved) if reserved else Q(0)
            key=(self.s.initial.lines[lid].hub,skill,lid)
            self.held[key]=max(self.held.get(key,Q(0)),displaced)
        line=self.s.initial.lines[lid]
        services=self.s.ai_services.received(line.owner,line.hub,'production')
        if replacement:
            direct=self.s.compute.effective.get((line.owner,line.hub),{}).get('production',Q(0))
            capability=self.s.ai.capability(line.owner)
            weighted=capability*direct+sum((e.capability*e.quantity_mU for e in services),Q(0))
            if weighted:
                fraction=min(Q(1),replacement/(Q(2,100)*weighted))
                self.s.ai_services.consume(services,fraction)
                self.s.industry_activity.use_compute(line.owner,line.hub,'production',direct*fraction if capability else Q(0))
        return result

    def held_at(self,hub,skill):
        return sum((q for (h,s,_),q in self.held.items() if (h,s)==(hub,skill)),Q(0))

    def research(self,outputs,cycle_ticks):
        grouped=defaultdict(list)
        for owner,rows in outputs.items():
            for lid,row in rows.items():grouped[owner,row.hub_id].append(lid)
        for (owner,hub),lids in grouped.items():
            base=sum(outputs[owner][lid].research_mU for lid in lids)
            compute=self.s.compute.effective.get((owner,hub),{}).get('research',Q(0)) if self.s.ai.capability(owner) else Q(0)
            services=self.s.ai_services.received(owner,hub,'research')
            quantity=compute+sum((e.quantity_mU for e in services),Q(0))
            weighted=self.s.ai.capability(owner)*compute+sum((e.capability*e.quantity_mU for e in services),Q(0))
            denominator=max(Q(1000,cycle_ticks),Q(base,20))
            strength=weighted/quantity*min(Q(1),quantity/denominator) if quantity else Q(0)
            multiplier=1+Q(2,100)*strength
            if services:self.s.ai_services.consume(services,min(Q(1),denominator/quantity))
            if quantity:self.s.industry_activity.use_compute(owner,hub,'research',compute*min(Q(1),denominator/quantity))
            for lid in lids:
                row=outputs[owner][lid]
                outputs[owner][lid]=replace(row,research_mU=round(row.research_mU*multiplier))

    def alignment_demands(self,ctx):
        result={}
        for (owner,hub),deployment in self.s.compute.deployments.items():
            quantity=ceil(Q(deployment.align_engineers_mU,ctx.clock.cycle_ticks))
            if quantity:
                entity=f'alignment:{owner}:{hub}'
                self.s.frame.work_context[entity]=(owner,hub,'treasury')
                result[entity]={'engineer':quantity}
        return result

    def align_engineers(self,ctx):
        state=self.s;f=state.frame
        for entity,workers in f.labor.items():
            if not entity.startswith('alignment:'):continue
            owner,hub,_=f.work_context[entity]
            available=sum(workers.values())
            lock=f.wage_locks[entity];target=state.native(owner,f'payroll:{hub}')
            cash=state.initial.cash.balance(lock).native_mU
            # No arrears-backed alignment: the named engineering time must
            # already be guaranteed payable by this real wage reservation.
            amount=max_feasible(available,lambda q:state.payments.quote(lock,target,
                state.wage_cost(owner,{'engineer':q}),'wage')<=cash)
            f.worked[entity]={'engineer':amount}
            from .ai_operations import data_quantize
            state.ai.align_accum[owner]=data_quantize(state.ai.align_accum[owner]+Q(5*amount,1000))

    def record_grid_loads(self,loads):
        by_hub=defaultdict(int)
        for load in loads:by_hub[load.hub]+=load.demand_mU
        for hub,amount in by_hub.items():self.current_grid_peak[hub]=max(self.current_grid_peak.get(hub,0),amount)

    def grid_multiplier(self,owner,hub):
        if 'AI06' not in self.s.research[owner].known or self.s.ai.suspended_until[owner]>self.cycle:return Q(1)
        # The previous compute cycle is switched at P1 compute_budget, after
        # core reservation and before P3, never inferred from current power.
        amount=self.s.compute.previous_effective.get((owner,hub),{}).get('grid',Q(0))
        peak=self.previous_grid_peak.get(hub,0)*10
        share=min(Q(1),amount/max(Q(1000),Q(peak,100)))
        cap=self.previous_capability.get(owner,Q(0))
        return 1-min(Q(3,10),Q(1,100)*cap*share)

    def end_cycle(self,ctx):
        if ctx.clock.is_cycle_end(ctx.tick):
            self.current_capability={p:self.s.ai.capability(p) for p in self.s.research}
            tasks={}
            for (owner,hub),values in self.s.compute.cycle_effective.items():
                for task in ('finance','cyber'):
                    quantity=values.get(task,Q(0))
                    tasks[owner,hub,task]=(quantity,quantity*self.current_capability[owner])
            for key,(quantity,weighted) in self.current_services.items():
                old=tasks.get(key,(Q(0),Q(0)))
                tasks[key]=(old[0]+quantity,old[1]+weighted)
            self.previous_tasks=tasks

    def record_services(self,events):
        for event in events:
            if event.buyer not in self.s.research or event.use not in ('finance','cyber'):continue
            key=(event.buyer,event.target_hub,event.use)
            quantity,weighted=self.current_services.get(key,(Q(0),Q(0)))
            self.current_services[key]=(quantity+event.quantity_mU,weighted+event.quantity_mU*event.capability)

    def previous_strength(self,owner,hub,task,threshold_mU):
        components=self.s.systems.communication_components(owner)
        quantity=Q(0);weighted=Q(0)
        for (p,source,t),(q,w) in self.previous_tasks.items():
            if p!=owner or t!=task:continue
            if source==hub or source in components and hub in components and components[source]==components[hub]:
                quantity+=q;weighted+=w
        return weighted/quantity*min(Q(1),quantity/threshold_mU) if quantity else Q(0)

    def finance_discount(self,owner,hub):
        if 'AI07' not in self.s.research[owner].known or self.s.ai.suspended_until[owner]>self.cycle:return Q(0)
        return min(Q(1,2),Q(15,1000)*self.previous_strength(owner,hub,'finance',20000))

    def audit(self):
        if any(q<0 for q in self.held.values()):raise InvariantViolation('negative displaced labor')
        for (owner,hub),d in self.s.compute.deployments.items():
            if (d.owner,d.hub)!=(owner,hub) or set(d.allocations)-TASKS or any(q<0 for q in d.allocations.values()):
                raise InvariantViolation('invalid local deployment')
            if d.align_engineers_mU<0:raise InvariantViolation('negative alignment workforce')

    def visible(self,owner):
        return {'displaced_labor_mU':[[hub,skill,ceil(self.held_at(hub,skill))]
                    for hub,skill in sorted({(h,s) for h,s,_ in self.held})
                    if self.s.initial.map.hubs[hub].owner==owner],
                'grid_multiplier_mU':{h:round(1000*self.grid_multiplier(owner,h))
                    for h,site in self.s.initial.map.hubs.items() if site.owner==owner}}

    def snapshot(self):
        return {'cycle':self.cycle,'held':[[*k,v] for k,v in sorted(self.held.items())],
                'nominal_batches':self.nominal_batches,'nominal_labor':self.nominal_labor,
                'planned':self.planned,'actual':self.actual,'last_effect_tick':self.last_effect_tick,
                'current_grid_peak':self.current_grid_peak,'previous_grid_peak':self.previous_grid_peak,
                'current_capability':self.current_capability,'previous_capability':self.previous_capability,
                'current_services':[[*k,v] for k,v in sorted(self.current_services.items())],
                'previous_tasks':[[*k,v] for k,v in sorted(self.previous_tasks.items())]}


class DeploymentCommands:
    OPERATIONS=frozenset({('ai','deploy')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'hub_id':str,'lines':list}|{k:int for k in TASKS},{'align_engineers_mU':int,'replaces_id':str})
        state=self.state;hub=order['hub_id'];lines=order['lines'];engineering=order.get('align_engineers_mU',0)
        if any(type(lid) is not str for lid in lines) or len(set(lines))!=len(lines) or engineering<0:
            raise RuleViolation('ERR_SCHEMA')
        if not state.authority.controls_hub(owner,hub):raise RuleViolation('ERR_NO_PERMISSION')
        if engineering and 'AI03' not in state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
        for lid in lines:
            line=state.initial.lines.get(lid)
            if line is None or line.owner!=owner or line.hub!=hub:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            if not state.ai_deployment.eligible(lid):raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        allocations={k:order[k] for k in TASKS}
        old=state.compute.deployments.get((owner,hub))
        if 'replaces_id' in order and (old is None or old.command_id!=order['replaces_id']):
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        state.compute.validate(owner,hub,allocations,state.compute.training_requests.get((owner,hub),0),ctx.cycle)
        deployment=Deployment(owner,hub,allocations,tuple(lines),str(int(order['id'])),engineering)
        def commit():
            old=state.compute.deployments.get((owner,hub))
            if old and old.command_id and 'replaces_id' not in order:state.command_streams[owner].plans.pop(old.command_id,None)
            state.compute.deployments[owner,hub]=deployment
        return PreparedCommand(commit,persistent=True)

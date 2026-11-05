"""Local, physically installed datacenter capacity and actual P3 electricity.

Capacity is not an inventory good. A task reserves local cohorts, then receives
only the compute paid for by its own B4/B5 electrical load. Idle datacenters do
not manufacture a transferable compute stock or consume full-load electricity.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from .ai import AcceleratorCohort, DataCenter, TASK_TECH
from .asset_lifecycle import AssetLoss
from dataclasses import replace
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .numeric import Q, ceil, floor, proportional
from .provenance import Use


DC_RECIPES = {'P109': (1,'accelerator'), 'P118': (2,'accelerator_g2'), 'P119': (3,'accelerator_g3')}


@dataclass
class InstalledCompute:
    line_id: str
    owner: str
    good: str
    center: DataCenter


@dataclass
class Deployment:
    owner: str
    hub: str
    allocations: dict[str,int] = field(default_factory=dict)
    lines: tuple[str,...] = ()
    command_id: str = ''
    align_engineers_mU: int = 0


class ComputeOperations:
    def __init__(self,state):
        self.s=state
        self.centers={}
        self.deployments={}
        self.training_requests={}
        self.training_prices_mU={}
        self.frame_tasks={}
        self.frame_power={}
        self.frame_centers={}
        self.frame_labor={}
        self.effective={}
        self.actual_by_line={}
        self.actual_by_entity={}
        self.cycle_effective={}
        self.frame_cycle_ticks=10
        self.last_aged_cycle=-1
        self.last_budget_cycle=-1
        self.previous_effective={}
        for line in state.initial.lines.values():
            if line.recipe.id in DC_RECIPES:
                good=DC_RECIPES[line.recipe.id][1]
                self.installed(line.id,{good:50000},0)

    def installed(self,line_id,materials_mU,commissioned_cycle,*,old_health_mU=1000):
        line=self.s.initial.lines[line_id]
        if line.recipe.id not in DC_RECIPES:return
        generation,good=DC_RECIPES[line.recipe.id]
        key=f'{line_id}:G{generation}'
        for previous,row in tuple(self.centers.items()):
            if row.line_id==line_id and previous!=key:
                asset=self.s.initial.assets[line_id]
                self._lose(row,asset.book_nmU.get(row.good,Q(0)),f'compute_retool:{previous}')
                asset.original_nmU.pop(row.good,None)
                asset.book_nmU.pop(row.good,None)
                del self.centers[previous]
        if key in self.centers and old_health_mU<1000:
            center=self.centers[key].center
            center.cohorts=[replace(c,quantity=c.quantity*Q(old_health_mU,1000)) for c in center.cohorts if c.quantity and old_health_mU]
        if key not in self.centers:
            self.centers[key]=InstalledCompute(line_id,line.owner,good,DataCenter(key,line.hub,generation))
        quantity=materials_mU.get(good,0)
        if quantity<0 or quantity*1000>self.s.initial.assets[line_id].book_nmU.get(good,Q(0)):
            raise InvariantViolation('compute installation lacks actual capital input')
        if quantity:
            center=self.centers[key].center
            existing=next((i for i,c in enumerate(center.cohorts) if c.installed_cycle==commissioned_cycle),None)
            if existing is None:center.cohorts.append(AcceleratorCohort(commissioned_cycle,Q(quantity,1000)))
            else:center.cohorts[existing]=replace(center.cohorts[existing],quantity=center.cohorts[existing].quantity+Q(quantity,1000))
        if sum((c.online(commissioned_cycle) for c in self.centers[key].center.cohorts),Q(0))>50:
            raise InvariantViolation('installed compute exceeds fifty accelerator equivalents')

    def available(self,row,cycle):
        state=self.s;line=state.initial.lines.get(row.line_id)
        if line is None or line.recipe.id not in DC_RECIPES or DC_RECIPES[line.recipe.id][1]!=row.good:
            return 0
        if row.line_id in state.paused_lines:return 0
        asset=state.initial.assets[row.line_id]
        host=state.initial.assets[row.line_id.rsplit('/line',1)[0]]
        # Apply physical availability once; cohort age is a separate factor.
        center=row.center
        nominal=sum((cohort.online(cycle) for cohort in center.cohorts),Q(0))
        return floor(nominal*(1,3,8)[center.generation-1]*1000*asset.availability*host.availability)

    def capacity(self,owner,hub,cycle):
        return sum(self.available(row,cycle) for row in self.centers.values()
                   if row.owner==owner and row.center.hub==hub)

    def validate(self,owner,hub,allocations,training,cycle):
        if set(allocations)-TASK_TECH.keys() or any(type(q) is not int or q<0 for q in allocations.values()):
            raise RuleViolation('ERR_SCHEMA')
        known=self.s.research[owner].known
        if any(q and TASK_TECH[k] not in known for k,q in allocations.items()):
            raise RuleViolation('ERR_TECH_LOCKED')
        if sum(allocations.values())+training>self.capacity(owner,hub,cycle):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')

    def reserve(self,ctx):
        state=self.s
        self.frame_cycle_ticks=ctx.clock.cycle_ticks
        if ctx.cycle != self.last_budget_cycle:
            self.previous_effective=self.cycle_effective
            self.cycle_effective={}
            self.last_budget_cycle=ctx.cycle
        self.frame_tasks={};self.frame_power={};self.frame_centers={};self.frame_labor={}
        self.effective={}
        for key,row in tuple(self.centers.items()):
            if row.line_id not in state.initial.assets:del self.centers[key]
        sites={(d.owner,d.hub) for d in self.deployments.values()} | set(self.training_requests)
        for owner,hub in sorted(sites):
            deployment=self.deployments.get((owner,hub))
            requested=dict(deployment.allocations) if deployment else {}
            requested['train']=self.training_requests.get((owner,hub),0)
            if self.s.ai.suspended_until[owner]>ctx.cycle:
                requested={k:(q if k in ('train','align') else 0) for k,q in requested.items()}
            capacity=self.capacity(owner,hub,ctx.cycle)
            actual=proportional(capacity,requested,key=(ctx.seed,'compute_decay_budget',ctx.tick,owner,hub))
            self.frame_tasks[owner,hub]=actual
            # Keep declared intent; temporary maintenance/power outages must
            # not erase a persistent deployment. Only this frame is shrunk.
            remaining={'B4':sum(q for k,q in actual.items() if k!='train'),'B5':actual.get('train',0)}
            rows=sorted(((key,row) for key,row in self.centers.items() if row.owner==owner and row.center.hub==hub),
                        key=lambda item:(-item[1].center.generation,digest(ctx.seed,'compute_center',ctx.tick,item[0])))
            available={key:self.available(row,ctx.cycle) for key,row in rows}
            for band in ('B4','B5'):
                for key,row in rows:
                    quantity=min(remaining[band],available[key])
                    if not quantity:continue
                    entity=f'compute:{band}:{key}'
                    efficiency=(Q(2),Q(1),Q(3,8))[row.center.generation-1]
                    power=ceil(Q(quantity,ctx.clock.cycle_ticks)*efficiency)
                    self.frame_power[entity]=(hub,4 if band=='B4' else 5,power)
                    state.frame.work_context[entity]=(owner,hub,'corp:tech')
                    self.frame_centers[entity]=(key,quantity,band)
                    available[key]-=quantity;remaining[band]-=quantity
                    self.frame_labor[row.line_id]=self.frame_labor.get(row.line_id,Q(0))+Q(quantity,50*(1,3,8)[row.center.generation-1]*1000)*200
            if any(remaining.values()):raise InvariantViolation('compute plan exceeds frozen local capacity')
        for lid,labor in self.frame_labor.items():self.frame_labor[lid]=ceil(labor)

    def training_electricity_limit(self,entity):
        """Native/energy bid after actual staffing wages per unit compute.

        Both bands share this center's actual wages in proportion to completed
        compute. The 1 mU electrical quantum uses a conservative, quantized
        compute yield, so rounding cannot lift runtime cost above the limit.
        None means wages alone are unaffordable, even with free electricity.
        """
        key,quantity,_=self.frame_centers[entity]
        row=self.centers[key];lid=row.line_id
        workers=self.s.frame.labor.get(lid,{})
        wanted=self.frame_labor.get(lid,0)
        staffed=min(Q(1),Q(sum(workers.values()),wanted)) if wanted else Q(1)
        planned=sum(Q(q,self.frame_cycle_ticks) for k,q,_ in self.frame_centers.values()
                    if self.centers[k].line_id==lid)
        if not staffed or not planned:return None
        wage_rate=self.s.wage_cost(row.owner,workers)/(planned*staffed)
        maximum=Q(self.training_prices_mU.get((row.owner,row.center.hub),50000),1000)
        if wage_rate>maximum:return None
        power=self.frame_power[entity][2]
        compute_per_energy=Q(floor(Q(quantity,self.frame_cycle_ticks*power)*10**12),10**12)
        return min(Q(50),(maximum-wage_rate)*compute_per_energy)

    def bid_ru(self,entity):
        key,_,band=self.frame_centers[entity]
        row=self.centers[key]
        limit=self.training_electricity_limit(entity) if band=='B5' else Q(50)
        return (limit if limit is not None else Q(0))*self.s.rates[row.owner]

    def labor_demands(self):
        return {lid:{'general_worker':quantity} for lid,quantity in self.frame_labor.items()}

    def power_demands(self):
        result={}
        for entity,(hub,rank,power) in self.frame_power.items():
            key,_,band=self.frame_centers[entity]
            if band=='B5' and self.training_electricity_limit(entity) is None:
                result[entity]=(hub,rank,0)
                continue
            lid=self.centers[key].line_id
            requested=self.frame_labor.get(lid,0)
            workers=sum(self.s.frame.labor.get(lid,{}).values())
            result[entity]=(hub,rank,floor(power*min(Q(1),Q(workers,requested))) if requested else power)
        return result

    def receive(self,ctx):
        supplied=defaultdict(lambda:defaultdict(lambda:Q(0)))
        line_actual=defaultdict(lambda:Q(0));line_planned=defaultdict(lambda:Q(0))
        state=self.s
        self.actual_by_entity={}
        for entity,(key,quantity,band) in self.frame_centers.items():
            row=self.centers[key]
            full=self.frame_power[entity][2]
            power=state.frame.power_received.get(entity,0)
            exact=Q(quantity,ctx.clock.cycle_ticks)*min(Q(1),Q(power,full)) if full else Q(0)
            compute=Q(floor(exact*10**12),10**12)
            self.actual_by_entity[entity]=compute
            supplied[row.owner,row.center.hub][band]+=compute
            line_actual[row.line_id]+=compute
            line_planned[row.line_id]+=Q(quantity,ctx.clock.cycle_ticks)
        self.effective={}
        for site,tasks in self.frame_tasks.items():
            b4=sum(q for k,q in tasks.items() if k!='train')
            ratio=min(Q(1),supplied[site]['B4']/Q(b4,ctx.clock.cycle_ticks)) if b4 else Q(0)
            result={k:Q(floor(Q(q,ctx.clock.cycle_ticks)*ratio*10**12),10**12) for k,q in tasks.items() if k!='train'}
            result['train']=supplied[site]['B5']
            self.effective[site]=result
            cumulative=self.cycle_effective.setdefault(site,{})
            for task,amount in result.items():cumulative[task]=cumulative.get(task,Q(0))+amount
        self.actual_by_line=dict(line_actual)
        for lid,wanted in self.frame_labor.items():
            workers=state.frame.labor.get(lid,{})
            staffed=min(Q(1),Q(sum(workers.values()),wanted)) if wanted else Q(1)
            usable=line_planned[lid]*staffed
            ratio=min(Q(1),line_actual[lid]/usable) if usable else Q(0)
            # Account real fractional worker-time; do not assign a rounding
            # tail to a more expensive skill and violate the quoted wage rate.
            state.frame.worked[lid]={skill:Q(floor(quantity*ratio*10**12),10**12)
                                     for skill,quantity in workers.items()}

    def age(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle<=self.last_aged_cycle:raise InvariantViolation('datacenter age advanced twice')
        self.last_aged_cycle=ctx.cycle
        state=self.s
        for key,row in tuple(self.centers.items()):
            asset=state.initial.assets.get(row.line_id)
            if asset is None:
                del self.centers[key];continue
            current=sum((c.online(ctx.cycle) for c in row.center.cohorts),Q(0))
            after=sum((c.online(ctx.cycle+1) for c in row.center.cohorts),Q(0))
            if current<=0:continue
            # Remaining installed historical cost follows the same linear-age
            # fraction, even after physical damage; no fresh hardware appears.
            from .capital_book import proportional_loss
            ratio=(current-after)/current
            removed=proportional_loss({row.good:asset.book_nmU.get(row.good,Q(0))},ratio.numerator,ratio.denominator)
            amount=removed.get(row.good,Q(0))
            if amount:self._lose(row,amount,f'accelerator_age:{ctx.cycle}:{key}')
            row.center.cohorts=[c for c in row.center.cohorts if c.online(ctx.cycle+1)>0]

    def cleanup(self):
        for key,row in tuple(self.centers.items()):
            if row.line_id not in self.s.initial.assets:
                del self.centers[key]

    def _lose(self,row,amount,event_id):
        if not amount:return
        state=self.s;asset=state.initial.assets[row.line_id]
        key=(event_id,asset.id)
        if key in state.asset_lifecycle.seen or not 0<amount<=asset.book_nmU.get(row.good,Q(0)):
            raise InvariantViolation('invalid or repeated accelerator retirement')
        draft=state.initial.inventory.materials.draft()
        draft.consume(state.asset_lifecycle.account(asset,row.good),amount/1000,
                      use=Use.DESTRUCTION,reason='accelerator_retirement')
        state.initial.inventory.transform({}, {},reason='accelerator_retirement',material_plan=draft.finish())
        asset.book_nmU[row.good]-=amount
        asset.book_rounding_steps+=1;asset.version+=1
        state.asset_lifecycle.seen.add(key)
        state.asset_lifecycle.events.append(AssetLoss(event_id,state.asset_lifecycle.tick,asset.id,asset.owner,
            amount*state.catalog.weights[row.good]/1000000,{row.good:amount},{}))

    def audit(self):
        for key,row in self.centers.items():
            if row.line_id not in self.s.initial.lines or key!=row.center.id:
                raise InvariantViolation('compute registry lost its physical line')
            if any(c.quantity<=0 or c.installed_cycle<0 for c in row.center.cohorts):
                raise InvariantViolation('invalid accelerator cohort')
            cycle=max(self.last_budget_cycle,0)
            if sum((c.online(cycle) for c in row.center.cohorts),Q(0))>50:
                raise InvariantViolation('local accelerator capacity exceeded')

    def visible(self,owner,cycle):
        return {'deployments':[{'hub_id':hub,'command_id':d.command_id,'allocations_mU':dict(d.allocations),
                               'lines':list(d.lines),'align_engineers_mU':d.align_engineers_mU}
                              for (p,hub),d in self.deployments.items() if p==owner],
                'centers':[{'id':key,'line_id':r.line_id,'hub_id':r.center.hub,'generation':r.center.generation,
                            'capacity_mU':self.available(r,cycle)} for key,r in self.centers.items() if r.owner==owner],
                'effective':[{'hub_id':hub,'tasks_mU':{k:floor(q) for k,q in values.items()}}
                             for (p,hub),values in self.effective.items() if p==owner]}

    def snapshot(self):
        def sites(table):return [{'owner':p,'hub':h,'value':value} for (p,h),value in sorted(table.items())]
        return {'centers':self.centers,'deployments':sites(self.deployments),'training_requests':sites(self.training_requests),'training_prices_mU':sites(self.training_prices_mU),
                'frame_tasks':sites(self.frame_tasks),'frame_power':self.frame_power,'frame_centers':self.frame_centers,
                'frame_labor':self.frame_labor,'actual_by_line':self.actual_by_line,'actual_by_entity':self.actual_by_entity,
                'effective':sites(self.effective),'cycle_effective':sites(self.cycle_effective),
                'frame_cycle_ticks':self.frame_cycle_ticks,'last_aged_cycle':self.last_aged_cycle,'last_budget_cycle':self.last_budget_cycle,
                'previous_effective':sites(self.previous_effective)}

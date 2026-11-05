"""Actual-compute training, country data books and bounded model ownership.

This service never mints compute or sells a national capacity estimate. Every
training increment comes from the local, paid P3 compute receipt. Model/data
licenses and the remaining deployment consumers have separate adapters.
"""
from dataclasses import dataclass, replace

from .ai import DataBook, Model, train_model, unlock_milestones, incident_probability, log10_q
from .command_schema import fields
from .determinism import digest, uniform
from .errors import InvariantViolation, InfrastructureFault, RuleViolation
from .numeric import Q, floor, proportional
from .protocol import PreparedCommand


DATA_SCALE = 10**15  # units; 10^12 auxiliary quanta per public mU.


def data_quantize(value):
    return Q(round(value*DATA_SCALE),DATA_SCALE)


@dataclass
class TrainingRun:
    id: str
    owner: str
    command_id: str
    hub: str
    allocation_mU: int
    started_tick: int
    deadline_tick: int | None
    max_price_mU: int
    base: Model | None
    compute_mU: Q = Q(0)


class AIOperations:
    def __init__(self,state):
        self.s=state
        self.data={p:DataBook() for p in state.initial.known}
        self.models={p:{} for p in state.initial.known}
        self.active=dict.fromkeys(state.initial.known,None)
        self.algorithm=dict.fromkeys(state.initial.known,Q(1))
        self.algorithm_pending=dict.fromkeys(state.initial.known,Q(0))
        self.align_accum=dict.fromkeys(state.initial.known,Q(0))
        self.suspended_until=dict.fromkeys(state.initial.known,-1)
        self.training={}
        self.sensor_ticks=dict.fromkeys(state.initial.known,0)
        self.sensor_seen=set()
        self.current_tick=-1
        self.last_cycle=-1
        self.recent={p:[] for p in state.initial.known}
        self.incidents={p:[] for p in state.initial.known}

    def begin_tick(self,ctx):
        self.sensor_seen.clear();self.current_tick=ctx.tick

    def boundary(self):
        for owner,gain in self.algorithm_pending.items():
            self.algorithm[owner]+=gain
            self.algorithm_pending[owner]=Q(0)

    def capability(self,owner):
        model=self.models[owner].get(self.active[owner])
        return model.capability(self.algorithm[owner]) if model else Q(0)

    def best(self,owner):
        return max(self.models[owner].values(),key=lambda m:(m.effective_compute,m.id),default=None)

    def model_slots_used(self,owner):
        licenses=getattr(self.s,'ai_licenses',None)
        return len(self.models[owner])+int(owner in self.training)+(licenses.reserved_slots(owner) if licenses else 0)

    def max_capability(self,owner):
        model=self.best(owner)
        return model.capability(self.algorithm[owner]) if model else Q(0)

    def record_activity(self,line):
        physical=self.s.validation_jobs[line.id].lab_id if line.id in self.s.validation_jobs else line.id
        if physical in self.sensor_seen:return
        asset=self.s.initial.assets[physical]
        if any(asset.book_nmU.get(g,0)>0 for g in ('sensor','control_module')):
            self.sensor_seen.add(physical);self.sensor_ticks[line.owner]+=1

    def consume_compute(self,ctx):
        for owner,run in self.training.items():
            amount=self.s.compute.effective.get((owner,run.hub),{}).get('train',Q(0))
            run.compute_mU+=amount
            self.s.industry_activity.use_compute(owner,run.hub,'train',amount)
        for lid,amount in self.s.compute.actual_by_line.items():
            if amount:self.record_activity(self.s.initial.lines[lid])
        for (owner,hub),tasks in self.s.compute.effective.items():
            if 'AI03' in self.s.research[owner].known:
                self.align_accum[owner]=data_quantize(self.align_accum[owner]+tasks.get('align',Q(0))/1000)
                self.s.industry_activity.use_compute(owner,hub,'align',tasks.get('align',Q(0)))

    def constrain_data_lines(self,caps,lines,ctx):
        grouped={}
        for line in lines:
            if line.recipe.id=='P117':grouped.setdefault(line.owner,{})[line.id]=caps[line.id]
        for owner,requests in grouped.items():
            # Each 1 mU of the P117 batch consumes 5 mU raw data.
            available=floor(self.data[owner].raw*1000)//5
            allocated=proportional(available,requests,key=(ctx.seed,'raw_data_budget',ctx.tick,owner))
            caps.update(allocated)

    def production(self,line,result):
        if result.batch_mU:self.record_activity(line)
        if line.recipe.id!='P117':return
        amount=Q(result.released_mU.get('curated_data',0),1000)
        data=self.data[line.owner]
        if amount>data.raw:raise InvariantViolation('data engineering consumed unavailable raw data')
        data.raw-=amount;data.curated_human+=amount
        if 'AI04' in self.s.research[line.owner].known:
            data.synthetic=data_quantize(data.synthetic+amount*min(Q(1,2),Q(2,100)*self.capability(line.owner)))

    def _finish(self,owner,cycle,*,cancelled):
        run=self.training[owner]
        model=None
        if run.compute_mU:
            amount=run.compute_mU*(Q(1,2) if cancelled else Q(1))/1000
            model=train_model('model:'+digest(owner,run.id).hex()[:24],owner,cycle,amount,self.data[owner].effective,run.base)
            if self.model_slots_used(owner)>16:raise InvariantViolation('reserved training model slot was lost')
            self.models[owner][model.id]=model
        self.recent[owner].append({'command_id':run.command_id,'status':'CANCELLED' if cancelled else 'COMPLETED',
                                  'model_id':model.id if model else None,'compute_mU':floor(run.compute_mU)})
        self.recent[owner]=self.recent[owner][-64:]
        self.s.compute.training_requests.pop((owner,run.hub),None)
        self.s.compute.training_prices_mU.pop((owner,run.hub),None)
        self.s.command_streams[owner].plans.pop(run.command_id,None)
        del self.training[owner]

    def cancel(self,owner,command_id,ctx):
        run=self.training.get(owner)
        if run is None or run.command_id!=command_id:return None
        return PreparedCommand(lambda:self._finish(owner,ctx.cycle,cancelled=True))

    def settle(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle<=self.last_cycle:raise InvariantViolation('AI cycle settled twice')
        self.last_cycle=ctx.cycle
        state=self.s
        for owner,data in self.data.items():
            population=sum(p.total_mU for hub,p in state.initial.populations.items()
                           if state.authority.controls_hub(owner,hub))
            generated=Q(population,1000)*Q(2,100)+Q(self.sensor_ticks[owner],ctx.clock.cycle_ticks)*Q(1,10)
            data.raw=data_quantize(data.raw*Q(499,500)+generated)
            self.sensor_ticks[owner]=0
        for owner,run in tuple(self.training.items()):
            if run.deadline_tick is not None and ctx.tick+1>=run.deadline_tick:self._finish(owner,ctx.cycle,cancelled=False)
        # Freeze all incident choices before applying any cross-nation leaks.
        events=[]
        for owner in sorted(self.models):
            model=self.best(owner)
            cap=self.max_capability(owner)
            align=10*log10_q(1+self.align_accum[owner]/500) if 'AI03' in state.research[owner].known else Q(0)
            probability=incident_probability(cap,align,'AI10' in state.research[owner].known)
            if uniform(ctx.seed,'ai_incident',ctx.cycle,owner)>=float(probability):continue
            kind=min(3,int(uniform(ctx.seed,'ai_incident_kind',ctx.cycle,owner)*4))
            recipient=None
            if kind==3:
                others=sorted(p for p in self.models if p!=owner)
                recipient=others[min(len(others)-1,int(uniform(ctx.seed,'ai_incident_recipient',ctx.cycle,owner)*len(others)))]
            events.append((owner,kind,model,recipient))
        fines={}
        for owner,kind,model,recipient in events:
            if kind==0:self.suspended_until[owner]=ctx.cycle+11
            elif kind==1:
                if owner in self.training:
                    self.training[owner].compute_mU=data_quantize(self.training[owner].compute_mU*Q(9,10000))*1000
                elif model is not None:self.models[owner][model.id]=replace(model,
                    raw_compute=data_quantize(model.raw_compute*Q(9,10)),effective_compute=data_quantize(model.effective_compute*Q(9,10)))
            elif kind==2:
                fines[owner]=Q(1,20)*sum((company.shares.get(owner,0)*state.stock_market.exchange.prices[cid]*
                    state.rates[company.issuer] for cid,company in state.initial.companies.items() if company.listed),Q(0))
            elif model is not None and self.model_slots_used(recipient)<16:
                mid='model:'+digest('leak',owner,recipient,ctx.cycle).hex()[:24]
                self.models[recipient][mid]=model.copy_for(mid,recipient,ctx.cycle,Q(7,10))
            # Source feedback does not reveal another country's model capacity.
            self.incidents[owner].append({'cycle':ctx.cycle,'kind':kind+1})
            self.incidents[owner]=self.incidents[owner][-64:]
        if fines:state.payables.assess_ru(ctx,fines,reason='ai_slippage')
        for owner in sorted(self.models):
            known,gain=unlock_milestones(state.catalog,frozenset(state.research[owner].known),self.max_capability(owner))
            state.pending_knowledge[owner].update(known-state.research[owner].known)
            self.algorithm_pending[owner]+=gain

    def visible(self,owner):
        data=self.data[owner];run=self.training.get(owner)
        return {'active_model':self.active[owner],'algorithm_mU':round(self.algorithm[owner]*1000),
                'capability_mU':round(self.capability(owner)*1000),'max_capability_mU':round(self.max_capability(owner)*1000),
                'data':{'raw_mU':floor(data.raw*1000),'curated_human_mU':floor(data.curated_human*1000),
                        'synthetic_mU':floor(data.synthetic*1000),'effective_mU':floor(data.effective*1000),
                        'quality_mU':floor(1000*data.curated_human/max(Q(1),data.curated_human+data.raw+data.synthetic))},
                'models':[{'id':m.id,'created_cycle':m.created_cycle,'raw_compute_mU':floor(m.raw_compute*1000),
                           'effective_compute_mU':floor(m.effective_compute*1000)} for m in self.models[owner].values()],
                'training':None if run is None else {'id':run.id,'command_id':run.command_id,'hub_id':run.hub,
                    'allocation_mU':run.allocation_mU,'max_price_mU':run.max_price_mU,'deadline_tick':run.deadline_tick,'compute_mU':floor(run.compute_mU)},
                'recent':[dict(r) for r in self.recent[owner]],'incidents':[dict(r) for r in self.incidents[owner]]}

    def audit(self):
        for owner,models in self.models.items():
            if self.model_slots_used(owner)>16:raise InvariantViolation('model slot limit exceeded')
            if self.active[owner] is not None and self.active[owner] not in models:raise InvariantViolation('active model missing')
            if any(m.owner!=owner or m.raw_compute<0 or m.effective_compute<0 for m in models.values()):
                raise InvariantViolation('invalid owned model')
            data=self.data[owner]
            if min(data.raw,data.curated_human,data.synthetic)<0:raise InvariantViolation('negative data stock')

    def snapshot(self):
        return {'data':self.data,'models':self.models,'active':self.active,'algorithm':self.algorithm,
                'algorithm_pending':self.algorithm_pending,'align_accum':self.align_accum,'suspended_until':self.suspended_until,
                'training':self.training,'sensor_ticks':self.sensor_ticks,'sensor_seen':self.sensor_seen,
                'current_tick':self.current_tick,'last_cycle':self.last_cycle,'recent':self.recent,'incidents':self.incidents}


class AICommands:
    OPERATIONS=frozenset({('ai','train'),('ai','model'),('ai','prune_model')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        state=self.state;ai=state.ai;op=order['op']
        if op in ('model','prune_model'):
            key='active_model' if op=='model' else 'model_id'
            fields(order,{key:str});mid=order[key]
            if mid not in ai.models[owner]:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            if op=='prune_model' and ai.active[owner]==mid:raise RuleViolation('ERR_NO_PERMISSION')
            return PreparedCommand(lambda:ai.active.__setitem__(owner,mid) if op=='model' else ai.models[owner].pop(mid))
        fields(order,{'hub_id':str,'alloc_compute_mU':int,'cycles':int,'base_model':(str,type(None)),'max_price_mU':int})
        if 'AI02' not in state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
        hub=order['hub_id'];allocation=order['alloc_compute_mU']
        if allocation<=0 or order['cycles']<5 or order['max_price_mU']<=0:raise RuleViolation('ERR_SCHEMA')
        if not state.authority.controls_hub(owner,hub):raise RuleViolation('ERR_NO_PERMISSION')
        if owner in ai.training or ai.model_slots_used(owner)>=16:raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        base=ai.models[owner].get(order['base_model'])
        if order['base_model'] is not None and base is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        deployment=state.compute.deployments.get((owner,hub))
        state.compute.validate(owner,hub,deployment.allocations if deployment else {},allocation,ctx.cycle)
        command_id=str(int(order['id']))
        remaining_cycles=(ctx.clock.total_ticks-ctx.tick+ctx.clock.cycle_ticks-1)//ctx.clock.cycle_ticks
        deadline=None if order['cycles']>remaining_cycles else ctx.tick+order['cycles']*ctx.clock.cycle_ticks
        run=TrainingRun('training:'+digest(owner,command_id).hex()[:24],owner,command_id,hub,allocation,ctx.tick,
                        deadline,order['max_price_mU'],base)
        def commit():
            ai.training[owner]=run
            state.compute.training_requests[owner,hub]=allocation
            state.compute.training_prices_mU[owner,hub]=run.max_price_mU
        return PreparedCommand(commit,persistent=True)

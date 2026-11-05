"""Paid-in, co-located industrial venture construction and economic shares.

Capital is real same-Hub inventory before signing, consumed over the catalog
construction time. Operator command authority is distinct from economic shares.
"""
from dataclasses import dataclass,replace
from .assets import Asset
from .authority import AssetRights
from .errors import RuleViolation,InvariantViolation
from .initialization import Facility
from .ledger import Account,Transfer
from .numeric import Q,units
from .protocol import PreparedCommand
from .facility_ownership import FacilityOwnership
from .construction import WorkOrder


@dataclass(frozen=True)
class JointVentureProposal:
    id:str
    proposer:str
    counterparty:str
    command_id:str
    hub:str
    profile:str
    recipe:str
    shares:tuple[int,int]
    inputs:tuple[dict,dict]
    mode:str
    duration_cycles:int
    expires_cycle:int
    tech:str|None=None


@dataclass
class IndustrialVenture:
    proposal:JointVentureProposal
    asset:str
    duration_ticks:int
    elapsed:int=0
    status:str='BUILDING'
    active_members:tuple[str,...]=()
    contribution:dict=None
    joint_rp:int=0
    target:int=0


class JointVentures:
    def __init__(self,state):
        self.s=state;self.rows={};self.last_tick=-1
        if not hasattr(state,'facility_ownership'):state.facility_ownership=FacilityOwnership(state)
    def count(self,owner):return sum(owner in (r.proposal.proposer,r.proposal.counterparty) for r in self.rows.values())
    def lock(self,p,good):return Account(p.proposer,p.hub,'joint_venture:'+p.id,good)

    def validate(self,p):
        state=self.s
        if not state.authority.controls_hub(p.proposer,p.hub):raise RuleViolation('ERR_NO_PERMISSION')
        if state.relations.will_be_at_war(p.proposer,p.counterparty):raise RuleViolation('ERR_NO_PERMISSION')
        recipe=state.catalog.recipes[p.recipe]
        for owner in (p.proposer,p.counterparty):
            if not set(recipe['requires_all'])<=state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
            if p.mode=='research' and ('R10' not in state.research[owner].known or not state.catalog.researchable(p.tech,state.research[owner].known) or state.governance.active(owner,'R12')):
                raise RuleViolation('ERR_TECH_LOCKED')
        costs={g:units(q) for g,q in state.catalog.profiles[p.profile]['build'].items()}
        if {g:p.inputs[0].get(g,0)+p.inputs[1].get(g,0) for g in set(p.inputs[0])|set(p.inputs[1])}!=costs:
            raise RuleViolation('ERR_SCHEMA','joint inputs must exactly fund catalog construction')
        for owner,inputs in zip((p.proposer,p.counterparty),p.inputs):
            for good,q in inputs.items():
                source=Account(owner,p.hub,'available',good)
                if any(o.economic_owner is not None for o in state.initial.inventory.materials.composition(source)):
                    raise RuleViolation('ERR_NO_PERMISSION','joint venture requires government-owned contributions')
        builder=getattr(state.work.on_complete,'__self__',None)
        if builder is None:raise InvariantViolation('industrial lifecycle not installed')
        if recipe['grade']>state.catalog.profiles[p.profile]['recipe_grade']:raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if p.profile in ('S2','S3','S4'):builder._tech(p.proposer,{'L01'})
        builder._slots(p.proposer,p.hub)
        if p.mode=='research' and any(r.proposal.tech==p.tech and owner in r.active_members and r.status not in ('THEORY_COMPLETE','DISSOLVED')
            for r in self.rows.values() for owner in (p.proposer,p.counterparty)):
            raise RuleViolation('ERR_NO_PERMISSION','one joint project per nation and technology')
        return costs,builder

    def offer(self,owner,order,ctx):
        state=self.s;p=order['params'];other=order['counterparty']
        if (other==owner or not 10<=order['duration_cycles']<=720 or
                set(p)!=({'hub_id','profile_id','recipe_id','shares_mU','inputs','mode','tech_id'} if p.get('mode')=='research' else {'hub_id','profile_id','recipe_id','shares_mU','inputs','mode'}) or
                any(type(p[k]) is not str for k in ('hub_id','profile_id','recipe_id','mode')) or
                p['mode'] not in ('industry','research') or type(p['shares_mU']) is not list or len(p['shares_mU'])!=2 or
                any(type(q) is not int or q<=0 for q in p['shares_mU']) or sum(p['shares_mU'])!=1000 or
                type(p['inputs']) is not list or len(p['inputs'])!=2 or any(type(i) is not dict for i in p['inputs'])):
            raise RuleViolation('ERR_SCHEMA')
        if other not in state.initial.known or p['hub_id'] not in state.initial.map.hubs:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if p['profile_id'] not in state.catalog.profiles or p['recipe_id'] not in state.catalog.recipes:raise RuleViolation('ERR_SCHEMA')
        if p['mode']=='research' and (type(p['tech_id']) is not str or p['tech_id'] not in state.catalog.technologies or state.catalog.technologies[p['tech_id']]['tier'] not in (5,6) or
                'research_points' not in state.catalog.recipes[p['recipe_id']]['outputs']):raise RuleViolation('ERR_SCHEMA')
        if any(type(g) is not str or type(q) is not int or q<=0 for i in p['inputs'] for g,q in i.items()):raise RuleViolation('ERR_SCHEMA')
        for party in (owner,other):
            if state.contracts.count(party)>=state.catalog.runtime['limits']['contracts_per_nation']:raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        proposal=JointVentureProposal(f'jv_proposal:{owner}:{int(order["id"])}',owner,other,str(int(order['id'])),p['hub_id'],p['profile_id'],p['recipe_id'],
            tuple(p['shares_mU']),tuple(dict(i) for i in p['inputs']),p['mode'],order['duration_cycles'],ctx.cycle+order['duration_cycles'],p.get('tech_id'))
        self.validate(proposal)
        return PreparedCommand(lambda:state.contracts.proposals.__setitem__(proposal.id,proposal))

    def prepare_accept(self,p,ctx):
        state=self.s;costs,builder=self.validate(p);aid='venture_asset:'+p.id
        transfers=tuple(Transfer(Account(owner,p.hub,'available',g),self.lock(p,g),q,retitle=True)
            for owner,inputs in zip((p.proposer,p.counterparty),p.inputs) for g,q in inputs.items())
        plan=state.initial.inventory.prepare_transfers(transfers)
        if p.mode=='research':
            for owner in (p.proposer,p.counterparty):
                if state.governance.active(owner,'R12'):raise RuleViolation('ERR_NO_PERMISSION')
        asset=Asset(aid,p.proposer,p.hub,{g:q*1000 for g,q in costs.items()},book_nmU={g:Q(0) for g in costs},under_construction=True)
        row=IndustrialVenture(p,aid,state.catalog.profiles[p.profile]['build_cycles']*ctx.clock.cycle_ticks,active_members=(p.proposer,p.counterparty),contribution={p.proposer:0,p.counterparty:0})
        def accept():
            plan.commit();state.initial.assets[aid]=asset
            state.initial.facilities[aid]=Facility(aid,p.proposer,p.hub,p.profile,state.catalog.recipes[p.recipe]['facility'],(),False)
            state.authority.sync_assets(state.initial.assets)
            state.authority.assets[aid]=AssetRights(p.proposer,dict(zip((p.proposer,p.counterparty),p.shares)))
            self.rows[p.id]=row
            state.facility_ownership.register(aid,aid)
            self.rows[p.id]=row;state.contracts.log(p,'SIGNED');del state.contracts.proposals[p.id]
        return PreparedCommand(accept)

    def advance(self,ctx):
        if ctx.tick<=self.last_tick:raise InvariantViolation('joint venture construction repeated')
        self.last_tick=ctx.tick;state=self.s
        for row in self.rows.values():
            if row.status!='BUILDING':continue
            p=row.proposal;asset=state.initial.assets[row.asset]
            if asset.health_mU<1000:row.status='DAMAGED';continue
            if not state.authority.controls_hub(p.proposer,p.hub):continue
            due={g:q*(row.elapsed+1)//row.duration_ticks-q*row.elapsed//row.duration_ticks for g,q in
                 ((g,units(q)) for g,q in state.catalog.profiles[p.profile]['build'].items())}
            draft=state.initial.inventory.materials.draft();inputs={}
            for good,quantity in due.items():
                if not quantity:continue
                source=self.lock(p,good);inputs[source]=quantity
                pieces=draft.take(source,Q(quantity))
                titled=state.facility_ownership.split_output(row.asset,pieces)
                capital=state.asset_lifecycle.account(asset,good)
                draft.add(capital,titled,virtual=True)
            plan=state.initial.inventory.prepare_transform(inputs,{},reason='joint_asset_investment',material_plan=draft.finish())
            plan.commit()
            for g,q in due.items():asset.book_nmU[g]+=q*1000
            row.elapsed+=1
            if row.elapsed==row.duration_ticks:
                state.asset_lifecycle.commission(row.asset)
                builder=getattr(state.work.on_complete,'__self__')
                facility=builder._new_lines(state.initial.facilities[row.asset]);state.initial.facilities[row.asset]=facility
                builder._maintenance(row.asset,{g:units(q) for g,q in state.catalog.profiles[p.profile]['maintenance'].items()},commissioned_tick=ctx.tick+1)
                state.authority.sync_assets(state.initial.assets)
                for lid in facility.line_ids:
                    state.authority.assets[lid]=AssetRights(p.proposer,dict(zip((p.proposer,p.counterparty),p.shares)))
                    state.facility_ownership.register(lid,row.asset)
                for lid in facility.line_ids:
                    order={'id':str(10**16+int.from_bytes(row.asset.encode()[-6:],'big')),'kind':'industry','op':'set_line','line_id':lid,'recipe':p.recipe,'mode':'general'}
                    # Recipe activation follows the existing installation work
                    # command; a chassis requiring installed equipment cannot
                    # be granted by a construction-only contribution.
                    if not state.catalog.recipes[p.recipe]['installed']:
                        builder.prepare(p.proposer,order,ctx).commit()
                row.status='COMMISSIONED'

    def route_research(self,outputs,ctx):
        from .research import combine_labs,SECURITY_MULTIPLIER,Project
        result={owner:dict(rows) for owner,rows in outputs.items()}
        used=set()
        for row in sorted(self.rows.values(),key=lambda r:r.proposal.id):
            p=row.proposal
            if p.mode!='research' or row.status!='COMMISSIONED':continue
            if ctx.cycle>=p.expires_cycle:row.active_members=();row.joint_rp=0;row.status='DISSOLVED';continue
            active=tuple(owner for owner in row.active_members if not self.s.governance.active(owner,'R12') and
                         'R10' in self.s.research[owner].known and self.s.catalog.researchable(p.tech,self.s.research[owner].known))
            row.active_members=active
            if not active:row.joint_rp=0;row.status='DISSOLVED';continue
            for owner in active:
                research=self.s.research[owner]
                rows=[]
                for lid,output in tuple(result.get(owner,{}).items()):
                    if (owner,lid) in used or research.focus.get(lid)!=p.tech:continue
                    mode=research.security.get(lid,'standard')
                    rows.append(replace(output,research_mU=round(output.research_mU*SECURITY_MULTIPLIER[mode])))
                    used.add((owner,lid));del result[owner][lid]
                amount=combine_labs(tuple(rows),organized={'R05','C02'}<=research.known)
                row.contribution[owner]+=amount;row.joint_rp+=amount
            row.target=min(self.s.research[owner].projects[p.tech].target_mU if p.tech in self.s.research[owner].projects else
                           self.s.research[owner].target_cost(p.tech,{}) for owner in active)
            if row.joint_rp<row.target:continue
            total=sum(row.contribution[o] for o in active)
            shares=dict(zip((p.proposer,p.counterparty),p.shares));active_shares=sum(shares[o] for o in active)
            for owner in active:
                if Q(row.contribution[owner],max(1,total))<Q(shares[owner]*4,active_shares*5):continue
                research=self.s.research[owner]
                project=research.projects.setdefault(p.tech,Project(p.tech,research.target_cost(p.tech,{})))
                project.theory_mU=project.target_mU;project.theory_complete=True
            row.status='THEORY_COMPLETE'
        return result

    def audit(self):
        self.s.facility_ownership.audit()
        for row in self.rows.values():
            if not 0<=row.elapsed<=row.duration_ticks or row.asset not in self.s.initial.assets:
                raise InvariantViolation('invalid joint venture asset or construction time')
            if not set(row.active_members)<={row.proposal.proposer,row.proposal.counterparty} or row.joint_rp<0:
                raise InvariantViolation('invalid joint research custody')

    def visible(self,owner):return [r for r in self.rows.values() if owner in (r.proposal.proposer,r.proposal.counterparty)]
    def snapshot(self):return {'rows':self.rows,'last_tick':self.last_tick}

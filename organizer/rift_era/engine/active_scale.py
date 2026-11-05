"""Development scaffold for P7 technology meters; NOT in the game assembly.

The registry distinguishes implemented and pending r6 meters. Unbuilt
infrastructure has zero scale; a technology ID or a hostile action is never
proof of use.
The pending meter set below is deliberately incomplete. No formal score may
use this module until all required consumers and licensed-scale rules are
integrated and validated. See HANDOFF.md.
"""
from collections import defaultdict

from .errors import InvariantViolation
from .numeric import Q


SYSTEM_METERS={
    'online_smart_grid_hubs':'smart_grid', 'online_fusion_sensor_hubs':'fusion_sensor',
    'online_reservoirs':'reservoir', 'online_closed_ecology_hubs':'closed_ecology',
    'online_automated_terminals':'automated_terminal', 'online_listening_stations':'listening',
    'counterintel_covered_hubs':'counterintel', 'post_quantum_hubs':'post_quantum',
    'online_deception_defense_hubs':'deception_defense', 'resilient_hubs':'resilience',
    'online_system_defense_hubs':'system_defense', 'online_environment_stations':'environment',
    'online_water_recycling_hubs':'water_recycle', 'online_climate_models':'climate_model',
    'online_cryptanalysis_defense_hubs':'cryptanalysis_defense',
}
COMPUTE_METERS={
    'effective_research_compute':'research', 'effective_training_compute':'train',
    'effective_production_compute':'production', 'effective_finance_compute':'finance',
    'effective_cyber_compute':'cyber', 'effective_alignment_compute_div10':'align',
}


class ActiveScale:
    def __init__(self,state):
        self.s=state;self.cycle=-1;self.last_tick=-1
        self.recipes={};self.lines={};self.systems={};self.compute={};self.formations={};self.heat=set()
        self.values={};self.licensed={}

    def _reset(self,cycle):
        self.cycle=cycle;self.recipes=defaultdict(lambda:Q(0));self.lines=defaultdict(lambda:Q(0))
        self.systems=defaultdict(int);self.compute=defaultdict(lambda:Q(0));self.formations=defaultdict(int);self.heat=set()

    def advance(self,ctx):
        if ctx.tick<=self.last_tick:raise InvariantViolation('technology usage metered twice')
        if ctx.cycle!=self.cycle:self._reset(ctx.cycle)
        state=self.s;frame=state.frame
        for system in state.systems.instances.values():
            if system.online and state.systems.availability(system)>0:self.systems[system.owner,system.kind,system.hub]+=1
        for (owner,hub),tasks in state.compute.effective.items():
            for task,quantity in tasks.items():
                # Cyber capacity is not a civilian active scale when consumed
                # by an actual hostile operation; defense consumers must mark
                # their own useful task through IndustryActivity.
                used=state.industry_activity.compute_use.get((owner,hub,task),Q(0))
                if task=='finance':used=min(quantity,Q(20000,ctx.clock.cycle_ticks)) if state.ai.capability(owner) else Q(0)
                self.compute[owner,task]+=min(quantity,used)
        for formation in state.formations.rows.values():
            if formation.refitting:continue
            need=frame.military_need.get(formation.id,{})
            got=frame.military.get(formation.id,{})
            supplied=all(got.get(g,0)>=n for g,n in need.items())
            if supplied and formation.slot_a=='radio':self.formations[formation.owner,'radio',formation.id]+=1
        self.last_tick=ctx.tick
        if ctx.clock.is_cycle_end(ctx.tick):self._finish(ctx)

    def production(self,line,result):
        if self.cycle<0:raise InvariantViolation('usage cycle has not begun')
        if result.batch_mU:
            self.recipes[line.owner,line.recipe.id]+=Q(result.batch_mU,1000)
            self.lines[line.owner,line.id]+=Q(result.batch_mU,1000)
            if line.id in self.s.heat_supported:self.heat.add(line.id)

    def begin_tick(self,ctx):
        if ctx.cycle!=self.cycle:self._reset(ctx.cycle)

    def _online(self,owner,kind,ticks):
        return sum(min(Q(1),Q(n,ticks)) for (p,k,_),n in self.systems.items() if (p,k)==(owner,kind))

    def _meter(self,owner,tech,rule,ctx):
        state=self.s;kind=rule['kind'];meter=rule['meter'];ticks=ctx.clock.cycle_ticks
        if kind=='active_recipe_batches':return sum((self.recipes.get((owner,r),Q(0)) for r in rule['ref_ids']),Q(0))
        running=[state.initial.lines[lid] for (p,lid),n in self.lines.items() if p==owner and n and lid in state.initial.lines]
        if kind=='active_mode_lines':return Q(sum(state.initial.line_policies[l.id].manufacturing_mode in rule['ref_ids'] for l in running))
        if meter in SYSTEM_METERS:return self._online(owner,SYSTEM_METERS[meter],ticks)
        if meter in COMPUTE_METERS:return self.compute.get((owner,COMPUTE_METERS[meter]),Q(0))/(10000 if tech=='AI03' else 1000)
        if meter=='effective_total_compute':return sum((q for (p,t),q in self.compute.items() if p==owner),Q(0))/1000
        if meter=='running_high_temp_lines':return Q(sum(l.id in self.heat for l in running))
        if meter=='running_precision_agri_lines':return Q(sum(state.line_options.get(l.id,{}).get('precision_agri',False) for l in running))
        if meter=='running_gene_edited_lines':return Q(sum(state.line_options.get(l.id,{}).get('gene_edited',False) for l in running))
        if meter=='running_molecular_lines':return Q(sum(l.recipe.id=='P101' and 'ai_core' in state.line_options.get(l.id,{}).get('addons',()) and
             state.initial.facilities[l.id.rsplit('/line',1)[0]].profile=='S5' for l in running))
        if meter=='listed_companies':return Q(sum(c.listed and c.issuer==owner for c in state.initial.companies.values()))
        if meter=='central_bank_active':return Q(int(owner in state.macro.active_policies))
        if meter=='social_paid_ratio':return state.governance.social_ratio(owner)
        if meter in ('active_plan_regime','active_market_regime'):return Q(int(state.governance.active(owner,'F07' if tech=='F07' else 'F08')))
        if meter=='direct_foreign_equity_assets':return Q(sum(a.owner!=owner and state.authority.assets[aid].owner_shares.get(owner,0)>=100
             for aid,a in state.initial.assets.items() if not a.under_construction))
        if meter=='settled_external_financial_trades_capped':return Q(int(any(state.stock_market.reports[owner]) or any(state.bond_market.reports[owner])))
        if meter=='external_native_settlements_capped':
            amount=sum((paid for c in state.market_events if state.spot.venues[c.venue].currency==owner
                        for b,s,paid in c.cash_flows if b!=s and owner in (b,s)),0)
            return min(Q(1),Q(amount,100000))
        if meter=='online_service_constellations':return Q(int(state.orbit.coverage(owner) is not None))
        if meter=='online_surveillance_constellations':return Q(int(state.orbit.coverage(owner)==('land_sea',Q(9,10))))
        if meter=='supplied_radio_formations':return sum((min(Q(1),Q(n,ticks)) for (p,k,_),n in self.formations.items() if p==owner and k=='radio'),Q(0))
        if meter=='completed_canal_tunnel_dam':return Q(sum(l.owner==owner and l.mode=='water' and state.utilities.availability(l)>0 for l in state.utilities.links.values()))
        if meter=='owned_running_lab_lines_with_node_effect':
            labs=[l for l in running if l.recipe.id in rule['ref_ids']]
            if tech=='R01':return Q(len(labs))
            if tech=='R02':return Q(sum(state.frame.worked.get(l.id,{}).get('engineer',0)>0 for l in labs))
            if tech=='R05':
                network=state.systems.communication_components(owner)
                return Q(sum(l.hub in network for l in labs))
            if tech=='R06':return Q(sum(bool(state.research[owner].licenses) for l in labs))
            if tech=='R10':return Q(sum(any(owner in r.active_members and r.proposal.mode=='research' for r in state.contracts.joint_ventures.rows.values()) for l in labs))
            if tech=='R12':return Q(len(labs)*int(state.governance.active(owner,'R12')))
            if tech=='R08':return Q(0) # requires a real asynchronous backup receipt
        # These meters depend on entities whose actual business implementations
        # must supply receipts. They remain zero until that receipt exists.
        pending={'flowing_intercontinental_cables','online_offshore_mines','active_automated_routes',
                 'completed_megaprojects','supplied_field_depots','ready_combined_arms_formations',
                 'ready_joint_command_formations','restored_closed_mines','consented_climate_regions'}
        if meter in pending:return Q(0)
        raise InvariantViolation('unregistered technology usage meter: '+meter)

    def _finish(self,ctx):
        state=self.s;values={p:{} for p in state.research};licensed={p:{} for p in state.research}
        rules=state.catalog.tables['scoring']['pillars']['tech']['active_scale_rules']
        for owner in values:
            for tech in state.research[owner].known:
                values[owner][tech]=self._meter(owner,tech,rules[tech],ctx)
        for buyer,records in state.contracts.licenses.items():
            for tech,record in records.items():
                if ctx.cycle>=record['end_cycle']:continue
                owner=record['licensor'];scale=values[buyer].get(tech,Q(0))
                licensed[owner][tech]=licensed[owner].get(tech,Q(0))+scale
        self.values,self.licensed=values,licensed

    def snapshot(self):
        return {'cycle':self.cycle,'last_tick':self.last_tick,'recipes':[[*k,v] for k,v in sorted(self.recipes.items())],
                'lines':[[*k,v] for k,v in sorted(self.lines.items())], 'systems':[[*k,v] for k,v in sorted(self.systems.items())],
                'compute':[[*k,v] for k,v in sorted(self.compute.items())], 'formations':[[*k,v] for k,v in sorted(self.formations.items())],
                'heat':self.heat,'values':self.values,'licensed':self.licensed}

"""Domain-legal physical military corridors with conserved tick time."""
from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop,heappush

from .command_schema import fields,version
from .determinism import digest
from .diplomacy import Relation
from .errors import InvariantViolation,RuleViolation
from .ledger import Account,Transfer
from .march import Position,Segment,trajectory,simultaneous_trajectories
from .numeric import Q,floor,ceil,proportional,units,split_payment
from .protocol import PreparedCommand
from .provenance import Leg,Use
from .nonland_navigation import base_online,legal_edge

# 03: life support, firing consumables and repair inputs. Durable equipment
# remains equipment even when carried as a spare rather than installed.
SUPPLY_GOODS=frozenset({'food','water','ammunition','precision_ammunition','fuel','battery',
                        'medical_supplies','basic_parts','composite','hypersonic_missile','guidance_module'})

TERRAIN_COST={0:Q(1),1:Q(1),2:Q(3,2),3:Q(2),4:Q(3,2),5:Q(5,2)}


@dataclass
class MarchPlan:
    owner:str
    command_id:str
    formation:str
    task:str
    target_region:str
    supply_hub:str
    retreat_loss_mU:int
    baseline_hp_mU:int
    path:tuple[str,...]
    edges:tuple[str,...]
    end_progress:Q=Q(1000)
    auto_retreat:bool=False
    abandoned:bool=False
    retreat_status:str='none'
    patrol_home:str|None=None  # patrol_v1: the other end of the loop


class MilitaryMovement:
    def __init__(self,state):
        self.s=state
        self.plans={}
        self.positions={}
        self.last_hub={}
        self.retreating={}
        self.wrecks={}
        self.encounters=()
        self.moved_formations=frozenset()
        self.abandon_remainders={}
        self._graph_key=None
        self._graph={}
        self._palette=tuple(sorted(state.initial.map.regions))

    def position(self,formation):
        if formation.id in self.positions:return self.positions[formation.id]
        return Position(formation.hub)

    def location(self,formation):
        return self.last_hub.get(formation.id,formation.hub)

    def cells(self,edge,origin):
        row=self.s.cargo.corridors[edge]
        path=self.s.cargo.paths[edge]
        return path if origin==row.a else tuple(reversed(path))

    def region(self,position):
        if position.destination is None or position.progress==0:
            return self.s.initial.map.hubs[position.node].region
        edge=self.s.cargo.corridors[position.edge]
        progress=position.progress if position.node==edge.a else 1000-position.progress
        cells=self.s.cargo.paths[position.edge]
        index=floor(progress*(len(cells)-1)/1000)
        return self._palette[self.s.initial.map.region_cells[cells[index]]]

    def profile(self,edge,origin,formation=None):
        if formation is not None and formation.domain!='Land':return ((Q(1000),Q(1)),)
        cells=self.cells(edge,origin);distance=len(cells)-1;result=[]
        for i,cell in enumerate(cells[1:],1):
            cost=TERRAIN_COST[self.s.initial.map.terrain_cells[cell]]
            end=Q(1000*i,distance)
            if result and result[-1][1]==cost:result[-1]=(end,cost)
            else:result.append((end,cost))
        return tuple(result)

    def permitted(self,owner,edge,*,preview=False,formation=None,origin=None):
        row=self.s.cargo.corridors[edge]
        if formation is not None:
            if not legal_edge(self.s,formation,edge):return False
            position=self.position(formation)
            regions=tuple(self._palette[self.s.initial.map.region_cells[c]] for c in self.cells(edge,position.node if origin is None else origin))
            if not self.s.contracts.dmz_path_allows(owner,regions):return False
        if not row.health_mU:return False
        if row.tier(owner)>=3:return True
        if self.s.contracts.transit_right(owner,edge) is not None:return True
        war=self.s.relations.will_be_at_war if preview else lambda a,b:self.s.initial.diplomacy.relation(a,b)==Relation.WAR
        for cell in self.s.cargo.paths[edge]:
            controller=self.s.authority.regions[self._palette[self.s.initial.map.region_cells[cell]]].controller
            if controller not in (None,owner) and not war(owner,controller):return False
        return True

    def graph(self):
        key=frozenset(self.s.cargo.corridors)
        if key!=self._graph_key:
            graph=defaultdict(list)
            for eid,edge in sorted(self.s.cargo.corridors.items()):
                graph[edge.a].append((eid,edge.b));graph[edge.b].append((eid,edge.a))
            self._graph=dict(graph);self._graph_key=key
        return self._graph

    def route(self,formation,target,ctx,*,position=None):
        position=self.position(formation) if position is None else position
        goal_hubs={h.id for h in self.s.initial.map.hubs.values() if h.region==target}
        if (position.destination is None and position.node in goal_hubs or
                not goal_hubs and self.region(position)==target):return (),(),position.progress or Q(1000)
        prefix,edges=(),()
        start=position.node;distance=Q(0)
        if position.destination is not None:
            if not self.permitted(formation.owner,position.edge,preview=True,formation=formation):
                raise RuleViolation('ERR_CORRIDOR_BLOCKED')
            cells=self.cells(position.edge,position.node)
            for index,cell in enumerate(cells):
                p=Q(1000*index,len(cells)-1)
                if not goal_hubs and p>position.progress and self._palette[self.s.initial.map.region_cells[cell]]==target:
                    return (position.destination,),(position.edge,),p
            prefix,edges=(position.destination,),(position.edge,)
            start=position.destination;distance=1-position.progress/1000
        queue=[(distance,b'',start,prefix,edges)];seen={};best=None
        while queue:
            cost,_,node,path,route=heappop(queue)
            if node in seen and seen[node]<=cost:continue
            seen[node]=cost
            if best is not None and cost>best[0]:break
            if node in goal_hubs:
                candidate=(cost,digest(ctx.seed,'march_goal',ctx.tick,formation.id,*route),path,route,Q(1000))
                best=candidate if best is None or candidate[:2]<best[:2] else best
            if len(path)>=128:continue
            for eid,neighbor in self.graph().get(node,()):
                if not self.permitted(formation.owner,eid,preview=True,formation=formation,origin=node):continue
                cells=self.cells(eid,node)
                for index,cell in enumerate(cells[1:],1):
                    if not goal_hubs and self._palette[self.s.initial.map.region_cells[cell]]==target:
                        progress=Q(1000*index,len(cells)-1)
                        # A destination Hub in this region is the actual landing
                        # point; non-Hub regions use a real corridor waypoint.
                        if self.s.initial.map.hubs[neighbor].region==target:progress=Q(1000)
                        candidate=(cost+progress/1000,digest(ctx.seed,'march_goal',ctx.tick,formation.id,*route,eid),
                                   path+(neighbor,),route+(eid,),progress)
                        best=candidate if best is None or candidate[:2]<best[:2] else best
                        break
                if cost+1<seen.get(neighbor,cost+2):
                    heappush(queue,(cost+1,digest(ctx.seed,'march_path',ctx.tick,formation.id,eid),
                                    neighbor,path+(neighbor,),route+(eid,)))
        if best is None:raise RuleViolation('ERR_CORRIDOR_BLOCKED','no legal physical route')
        return best[2:]

    def planned(self,fid):
        plan=self.plans.get(fid)
        if self.s.formations.rows[fid].refitting:return False
        if plan is None or not plan.path:return False
        position=self.position(self.s.formations.rows[fid])
        return not (len(plan.path)==1 and position.destination==plan.path[0] and position.progress==plan.end_progress)

    def reserve(self,ctx):
        """Load a balanced endurance stock from the selected, co-located depot."""
        state=self.s;inventory=state.initial.inventory;requests=defaultdict(dict)
        for fid,plan in self.plans.items():
            formation=state.formations.rows.get(fid)
            if formation is None or not self.planned(fid) or formation.hub is None:continue
            hub=formation.hub
            if not base_online(state,formation.owner,hub,formation.domain):continue
            rates=defaultdict(lambda:Q(0))
            for uid in formation.unit_ids:
                unit=state.initial.units[uid];spec=state.catalog.units[unit.unit_type]
                rates['food']+=Q(unit.crew_mU*6,1000);rates['water']+=Q(unit.crew_mU*4,1000)
                for good,q in spec['combat_tick'].items():
                    if good=='ammunition' and unit.precision:good='precision_ammunition'
                    rates[good]+=units(q*unit.fuel_factor if good=='fuel' else q)
            mass=sum(q*state.cargo_weights[g] for g,q in rates.items())
            if not mass:continue
            capacity=20000*len(formation.unit_ids)
            occupied=sum(q*state.cargo_weights[a.asset] for a,q in inventory.balances().items() if a.location==fid and not a.bucket.startswith('refit:'))
            targets={g:max(0,floor(Q(capacity)*rate/mass)-inventory.balance(Account(formation.owner,fid,'available',g)))
                     for g,rate in rates.items() if rate}
            weights={g:ceil(q*state.cargo_weights[g]) for g,q in targets.items()}
            grants=proportional(max(0,floor(capacity-occupied)),weights,key=(ctx.seed,'march_pack',ctx.tick,fid))
            for good,grant in grants.items():
                quantum=1000 if state.catalog.goods[good]['indivisible'] else 1
                quantity=floor(Q(grant)/state.cargo_weights[good])//quantum*quantum
                requests[Account(formation.owner,hub,'available',good)][fid]=quantity
        transfers=[]
        for source,demand in sorted(requests.items()):
            quantum=1000 if state.catalog.goods[source.asset]['indivisible'] else 1
            grants=proportional(inventory.balance(source)//quantum,{f:q//quantum for f,q in demand.items()},
                                key=(ctx.seed,'march_depot',ctx.tick,source.owner,source.location,source.asset))
            for fid,q in grants.items():
                transfers.append(Transfer(source,Account(source.owner,fid,'available',source.asset),q*quantum,destination_hub=source.location))
        inventory.transfer_many(transfers)

    def speed(self,formation,supplies):
        rates=[]
        for uid in formation.unit_ids:
            unit=self.s.initial.units[uid];spec=self.s.catalog.units[unit.unit_type]
            fuel=units(spec['combat_tick']['fuel']*unit.fuel_factor)
            ratio=min(Q(1),Q(supplies.get(uid,{}).get('fuel',0),fuel)) if fuel else Q(1)
            if unit.unit_type=='artillery':ratio=max(Q(35,100),ratio)
            rates.append(Q(spec['hops_per_cycle']*100)*unit.speed_factor*ratio if unit.crew_mU else Q(0))
        return min(rates,default=Q(0))

    def advance(self,ctx,supplies):
        state=self.s;proposals={};used_plans={}
        for fid,formation in state.formations.rows.items():
            position=self.position(formation);plan=self.plans.get(fid)
            if plan is not None:
                self.last_hub.setdefault(fid,formation.hub or position.node)
            if formation.refitting:
                proposals[fid]=(Segment(Q(0),Q(1),position,position),);continue
            if position.edge and not state.cargo.corridors[position.edge].health_mU and fid not in self.retreating:
                self.retreating[fid]=position.node
                position=Position(position.destination,position.node,1000-position.progress,position.edge)
                self.positions[fid]=position
            if fid in self.retreating:
                path,edges,end=(position.destination,),(position.edge,),Q(1000)
            elif not self.planned(fid) or not self.permitted(formation.owner,plan.edges[0],formation=formation):
                proposals[fid]=(Segment(Q(0),Q(1),position,position),);continue
            else:path,edges,end=plan.path,plan.edges,plan.end_progress
            origins=(position.node,)+path[:-1]
            profiles=tuple(self.profile(edge,origin,formation) for edge,origin in zip(edges,origins))
            proposals[fid]=trajectory(position,path,self.speed(formation,supplies),lambda a,b:Q(1),
                                       edge_ids=edges,end_progress=end,profiles=profiles)
            used_plans[fid]=plan
        hostile=lambda a,b:state.formations.rows[a].domain==state.formations.rows[b].domain and state.initial.diplomacy.relation(
            state.formations.rows[a].owner,state.formations.rows[b].owner)==Relation.WAR
        paths,self.encounters=simultaneous_trajectories(proposals,hostile)
        moved=set();departing=[];moved_formations=set()
        for fid,path in paths.items():
            formation=state.formations.rows[fid]
            if fid not in used_plans:continue
            plan=used_plans[fid]
            retreat=fid in self.retreating
            for segment in path:
                if segment.before!=segment.after and segment.end>segment.start:
                    moved.update(formation.unit_ids);moved_formations.add(fid)
                if segment.before.destination and segment.after.destination is None:
                    if retreat:continue
                    accounts=tuple(a for a in state.initial.inventory.balances() if a.owner==formation.owner and
                                   (a.location==fid or a.location in formation.unit_ids))
                    accounts+=tuple(a for a in state.military_repairs.virtual_stock() if a.location in formation.unit_ids)
                    edge=segment.before.edge
                    state.initial.inventory.record_transport(accounts,Leg(segment.before.node,segment.after.node,
                        formation.owner,state.cargo.corridors[edge].length_cells,eligible=False))
                    self.last_hub[fid]=segment.after.node
                    plan.path,plan.edges=plan.path[1:],plan.edges[1:]
            if plan is not None and plan.auto_retreat and not plan.abandoned and any(u in moved for u in formation.unit_ids):
                departing.append(formation)
            position=path[-1].after;self.positions[fid]=position
            formation.hub=position.node if position.destination is None or position.progress==0 else None
            formation.region=self.region(position)
            if retreat and position.destination is None:
                self.retreating.pop(fid,None)
                if plan is not None and plan.path and plan.path[0]==position.node:
                    plan.path,plan.edges=plan.path[1:],plan.edges[1:]
            for uid in formation.unit_ids:state.initial.units[uid].region=formation.region
            if plan is not None and plan.auto_retreat and not self.planned(fid):plan.retreat_status='at_supply'
            if (plan is not None and plan.task=='patrol' and plan.patrol_home and not plan.auto_retreat and not retreat
                    and position.destination is None and not plan.path):
                self._patrol_turn(formation,plan,ctx)
        self.moved_formations=frozenset(moved_formations)
        return frozenset(moved),self.abandon_supplies(ctx,departing)

    def _patrol_turn(self,formation,plan,ctx):
        """patrol_v1 (06 formation_task patrol): on reaching one end of the loop the
        formation turns at once for the other end, so it is on the move every tick
        until the task is replaced or cancelled. A blocked return ends the loop."""
        try:
            path,edges,end=self.route(formation,plan.patrol_home,ctx)
        except RuleViolation:
            plan.patrol_home=None
            return
        if not path:
            return
        plan.target_region,plan.patrol_home=plan.patrol_home,plan.target_region
        plan.path,plan.edges,plan.end_progress=path,edges,end

    def abandon_supplies(self,ctx,formations):
        """User-confirmed scope: available onboard supplies, never crew/gear."""
        inventory=self.s.initial.inventory
        requests=defaultdict(dict);accounts={}
        for formation in formations:
            plan=self.plans.get(formation.id)
            if plan is None or not plan.auto_retreat or plan.abandoned:
                raise InvariantViolation('retreat abandonment repeated or outside a retreat')
            radio=(getattr(formation,'slot_a',None)=='radio' and inventory.balance(
                Account(formation.owner,formation.id,'refit:A','communication_equipment'))>=1000)
            coefficient=2 if radio else 5  # denominator 25: 8% or 20%
            for account,quantity in inventory.balances().items():
                if account.owner==formation.owner and account.location==formation.id and account.bucket=='available' and account.asset in SUPPLY_GOODS:
                    requests[formation.owner,account.asset][formation.id]=quantity*coefficient
                    accounts[formation.id,account.asset]=account
        consumed={};by_unit=defaultdict(dict);remainders={}
        for (owner,good),weights in sorted(requests.items()):
            quantum=1000 if self.s.catalog.goods[good]['indivisible'] else 1
            exact=self.abandon_remainders.get((owner,good),Q(0))+Q(sum(weights.values()),25)
            total=floor(exact/quantum)*quantum
            remainders[owner,good]=exact-total
            allocations=split_payment(total//quantum,{fid:Q(w) for fid,w in weights.items()},
                                      key=(ctx.seed,'retreat_abandon',ctx.tick,owner,good))
            for fid,count in allocations.items():
                amount=count*quantum;account=accounts[fid,good]
                if amount>inventory.balance(account):raise InvariantViolation('retreat rounding exceeded actual supplies')
                if not amount:continue
                consumed[account]=amount
                ids=self.s.formations.rows[fid].unit_ids
                parts=split_payment(count,{uid:Q(1) for uid in ids},key=(ctx.seed,'retreat_carry',ctx.tick,fid,good))
                for uid,q in parts.items():
                    if q:by_unit[uid][good]=q*quantum
        inventory.transform(consumed,{},reason='military_retreat_abandon',use=Use.ACTION_LOSS)
        self.abandon_remainders.update(remainders)
        for formation in formations:self.plans[formation.id].abandoned=True
        return dict(by_unit)

    def evaluate_retreat(self,ctx):
        state=self.s
        contacts=state.battles.contacts()
        for formation in tuple(state.formations.rows.values()):
            plan=self.plans.get(formation.id)
            hp=sum(state.initial.units[u].hp_mU for u in formation.unit_ids)
            low_morale=any(state.initial.units[u].morale<Q(1,5) for u in formation.unit_ids)
            damaged=plan is not None and (plan.baseline_hp_mU-hp)*1000>=plan.baseline_hp_mU*plan.retreat_loss_mU
            if plan is not None and plan.auto_retreat and plan.retreat_status=='returning':
                if formation.id in self.retreating or self.planned(formation.id) and self.permitted(formation.owner,plan.edges[0],formation=formation):continue
            if not low_morale and not damaged:continue
            if plan is None:
                plan=MarchPlan(formation.owner,'',formation.id,'garrison',formation.region,
                    formation.home_hub or f'{formation.owner}-h0',1000,hp,(),())
                self.plans[formation.id]=plan
            visible_enemies={c.target for uid,rows in contacts.items() if state.initial.units[uid].owner==formation.owner for c in rows}
            unsafe={state.initial.units[uid].region for uid in visible_enemies if uid in state.initial.units}
            position=self.position(formation)
            starts=[(False,position)]
            if position.destination is not None and position.progress>0 and formation.id not in self.retreating:
                starts.append((True,Position(position.destination,position.node,1000-position.progress,position.edge)))
            candidates=[]
            for hub in state.initial.map.hubs.values():
                if not base_online(state,formation.owner,hub.id,formation.domain) or hub.region in unsafe:continue
                if any(state.initial.inventory.balance(Account(formation.owner,hub.id,'available',g))<=0 for g in ('food','water')):continue
                for reverse,start in starts:
                    try:path,edges,end=self.route(formation,hub.region,ctx,position=start)
                    except RuleViolation:continue
                    cost=Q(0) if not path else len(path)-1+end/1000-(start.progress/1000 if start.destination else 0)
                    candidates.append((cost,digest(ctx.seed,'retreat_goal',ctx.tick,formation.id,hub.id,int(reverse)),
                                       hub.id,reverse,start,path,edges,end))
            old_status=plan.retreat_status
            plan.auto_retreat=True;formation.task='retreat'
            if not candidates:
                plan.path,plan.edges=(),()
                plan.retreat_status='blocked'
                continue
            _,_,hub,reverse,start,path,edges,end=min(candidates,key=lambda row:row[:2])
            if reverse:
                self.positions[formation.id]=start
                self.last_hub.setdefault(formation.id,position.node)
                self.retreating[formation.id]=position.node
            plan.target_region=state.initial.map.hubs[hub].region
            plan.supply_hub=hub;plan.path=path;plan.edges=edges;plan.end_progress=end
            plan.retreat_status='returning' if path else 'at_supply'
            if path and old_status=='at_supply':plan.abandoned=False
            formation.version+=1

    def cancel(self,owner,command_id):
        found=next((p for p in self.plans.values() if p.owner==owner and p.command_id==command_id),None)
        if found is None:return None
        def commit():
            self.s.formations.rows[found.formation].task='garrison'
            del self.plans[found.formation]
        return PreparedCommand(commit)

    def retire(self,formation,*,wreck=True):
        plan=self.plans.pop(formation.id,None)
        if plan:self.s.command_streams[formation.owner].plans.pop(plan.command_id,None)
        stock={a:q for a,q in self.s.initial.inventory.balances().items() if a.location==formation.id and a.bucket=='available'}
        remaining=any(a.location==formation.id and not a.bucket.startswith('locked_military:') for a in self.s.initial.inventory.balances())
        if wreck and remaining:
            self.wrecks[formation.id]={'owner':formation.owner,'location':self.location(formation),'position':self.position(formation)}
        elif not wreck:
            self.s.initial.inventory.transfer_many(Transfer(a,Account(formation.owner,formation.hub,'available',a.asset),q) for a,q in stock.items())
        self.positions.pop(formation.id,None);self.last_hub.pop(formation.id,None);self.retreating.pop(formation.id,None)

    def visible(self,owner):
        def point(position):
            return {'origin_hub':position.node,'destination_hub':position.destination,
                    'corridor_id':position.edge,'progress_mU':floor(position.progress),'region_id':self.region(position)}
        return {'positions':[{'formation_id':fid,**point(p)} for fid,p in self.positions.items()
                             if self.s.formations.rows[fid].owner==owner],
                'tasks':[{'formation_id':p.formation,'command_id':p.command_id or None,'task':p.task,
                          'target_region':p.target_region,'supply_hub':p.supply_hub,'remaining_path':p.path,
                          'retreat_status':p.retreat_status,'retreat_loss_mU':p.retreat_loss_mU}
                         for p in self.plans.values() if p.owner==owner],
                'wrecks':[{'id':fid,**point(row['position'])} for fid,row in self.wrecks.items() if row['owner']==owner]}

    def audit(self):
        formations=self.s.formations.rows
        if set(self.plans)-formations.keys() or set(self.positions)-formations.keys():
            raise InvariantViolation('orphan military movement state')
        for fid,position in self.positions.items():
            if position.node not in self.s.initial.map.hubs:
                raise InvariantViolation('military position has no physical anchor')
            if position.destination is not None:
                edge=self.s.cargo.corridors.get(position.edge)
                if edge is None or edge.other(position.node)!=position.destination:
                    raise InvariantViolation('military position is off its corridor')
            formation=formations[fid]
            hub=position.node if position.destination is None or not position.progress else None
            if formation.hub!=hub or formation.region!=self.region(position):
                raise InvariantViolation('formation view differs from physical military position')
        for fid,plan in self.plans.items():
            if len(plan.path)!=len(plan.edges) or len(plan.path)>128 or not 0<plan.end_progress<=1000:
                raise InvariantViolation('invalid military task route')
        for (owner,good),remainder in self.abandon_remainders.items():
            quantum=1000 if self.s.catalog.goods[good]['indivisible'] else 1
            if not 0<=remainder<quantum:
                raise InvariantViolation('invalid retreat supply remainder')

    def snapshot(self):
        return {'plans':self.plans,'positions':self.positions,'last_hub':self.last_hub,
                'retreating':self.retreating,'wrecks':self.wrecks,'encounters':self.encounters,'moved_formations':self.moved_formations,
                'abandon_remainders':[[p,g,q] for (p,g),q in sorted(self.abandon_remainders.items()) if q]}


class MarchCommands:
    OPERATIONS=frozenset({('military','formation_task')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'formation_id':str,'task':str,'target_region':str,'posture':str,'supply_hub':str,
                      'retreat_loss_mU':int},{'expected_version':int,'replaces_id':str})
        state=self.state;formation=state.formations.rows.get(order['formation_id'])
        if formation is None or formation.owner!=owner:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order,formation)
        if (order['task'] not in ('move','garrison','attack','escort','patrol') or order['posture'] not in ('preserve','assault','bombard')
                or order['target_region'] not in state.initial.map.regions or not 100<=order['retreat_loss_mU']<=900):
            raise RuleViolation('ERR_SCHEMA')
        if formation.domain!='Land' and not base_online(state,owner,order['supply_hub'],formation.domain):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE','no completed friendly military base')
        if not state.contracts.dmz_allows(owner,formation.region,order['target_region'],ctx.tick):
            raise RuleViolation('ERR_NO_PERMISSION','target is inside a signed demilitarized zone')
        if not base_online(state,owner,order['supply_hub'],formation.domain):raise RuleViolation('ERR_NO_PERMISSION')
        if order['task']=='attack' and not any(state.relations.will_be_at_war(owner,p) for p in state.initial.diplomacy.actors):
            raise RuleViolation('ERR_NOT_AT_WAR')
        old=state.movement.plans.get(formation.id)
        if old is not None and old.command_id and order.get('replaces_id')!=old.command_id:raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        path,edges,end=state.movement.route(formation,order['target_region'],ctx)
        plan=MarchPlan(owner,str(int(order['id'])),formation.id,order['task'],order['target_region'],order['supply_hub'],
                       order['retreat_loss_mU'],sum(state.initial.units[u].hp_mU for u in formation.unit_ids),path,edges,end)
        if order['task']=='patrol' and formation.region!=order['target_region']:
            plan.patrol_home=formation.region
        def commit():
            if formation.id not in state.movement.positions:
                state.movement.positions[formation.id]=Position(formation.hub)
            state.movement.last_hub.setdefault(formation.id,formation.hub)
            state.movement.plans[formation.id]=plan
            formation.task=plan.task;formation.version+=1
            for uid in formation.unit_ids:state.initial.units[uid].posture=order['posture']
        return PreparedCommand(commit,persistent=True)

"""Legal sensor coverage from actually staffed units and installed equipment."""
from collections import defaultdict,deque
from dataclasses import dataclass

from .command_schema import fields,version
from .diplomacy import Relation
from .errors import RuleViolation
from .numeric import Q,floor
from .protocol import PreparedCommand
from .world import SIZE


@dataclass(frozen=True)
class Signal:
    id:str
    owner:str
    domain:str
    region:str
    cell:int
    strength_mU:int
    emitting:bool=False


@dataclass(frozen=True)
class Source:
    owner:str
    kind:str
    region:str
    cell:int
    radius:int
    quality:Q
    bonus:Q=Q(0)


class MilitarySensors:
    def __init__(self,state):
        self.s=state
        self._distances={}

    def distances(self,region):
        if region not in self._distances:
            distances={region:0};queue=deque([region])
            while queue:
                current=queue.popleft()
                if distances[current]==8:continue
                for neighbor in self.s.initial.map.regions[current].adjacent:
                    if neighbor not in distances:
                        distances[neighbor]=distances[current]+1;queue.append(neighbor)
            self._distances[region]=distances
        return self._distances[region]

    def formation_cell(self,formation):
        position=self.s.movement.position(formation)
        if position.destination is None or not position.progress:
            hub=self.s.initial.map.hubs[position.node]
            return hub.y*SIZE+hub.x
        edge=self.s.cargo.corridors[position.edge]
        progress=position.progress if position.node==edge.a else 1000-position.progress
        cells=self.s.cargo.paths[position.edge]
        return cells[floor(progress*(len(cells)-1)/1000)]

    def emitting(self,formation):
        return not formation.refitting and (formation.slot_a=='radio' or
            formation.slot_a in ('radar','ew') and formation.emitter_active and
            (formation.slot_a!='ew' or formation.ew_mode=='jamming'))

    def signal(self,formation):
        return Signal(formation.id,formation.owner,formation.domain,formation.region,self.formation_cell(formation),
                      sum(self.s.initial.units[u].hp_mU for u in formation.unit_ids),self.emitting(formation))

    def frame(self):
        state=self.s;sources=[];jammers=set();dedup={}
        for formation in state.formations.rows.values():
            living=[state.initial.units[u] for u in formation.unit_ids if state.initial.units[u].hp_mU and state.initial.units[u].crew_mU]
            if not living:continue
            cell=self.formation_cell(formation)
            if formation.domain in ('Land','Air','Sea-Surface','Sea-Sub'):
                bonus=max((u.sensor_bonus for u in living),default=Q(0)) if not formation.refitting else Q(0)
                source=Source(formation.owner,'visual',formation.region,cell,0,Q(3,5),bonus)
                key=(source.owner,source.kind,source.region,source.cell)
                if key not in dedup or dedup[key].bonus<bonus:dedup[key]=source
            if formation.refitting:continue
            if formation.slot_a=='sensor':sources.append(Source(formation.owner,'sonar',formation.region,cell,0,Q(1,5)))
            if formation.slot_a=='radio':sources.append(Source(formation.owner,'radio',formation.region,cell,1,Q(65,100)))
            if formation.slot_a=='radar' and formation.emitter_active:
                sources.append(Source(formation.owner,'radar',formation.region,cell,3,Q(4,5)))
            if formation.slot_a=='ew' and formation.emitter_active and formation.ew_mode=='jamming':
                jammers.add((formation.owner,formation.region))
        sources.extend(dedup.values())
        for system in state.systems.instances.values():
            if system.kind=='listening' and system.online:
                hub=state.initial.map.hubs[system.hub]
                sources.append(Source(system.owner,'listening',hub.region,hub.y*SIZE+hub.x,6,Q(55,100)))
        for owner in state.initial.known:
            coverage=state.orbit.coverage(owner)
            if coverage:
                scope,quality=coverage
                sources.append(Source(owner,'orbital_'+scope,'',0,0,quality))
        grouped=defaultdict(list)
        for source in sources:grouped[source.owner].append(source)
        return dict(grouped),frozenset(jammers)

    def occluded(self,a,b):
        x,y=a%SIZE,a//SIZE;tx,ty=b%SIZE,b//SIZE
        dx,dy=abs(tx-x),-abs(ty-y);sx=1 if x<tx else -1;sy=1 if y<ty else -1
        error=dx+dy
        while True:
            if self.s.initial.map.terrain_cells[y*SIZE+x] in (3,5):return True
            if (x,y)==(tx,ty):return False
            twice=2*error
            if twice>=dy:error+=dy;x+=sx
            if twice<=dx:error+=dx;y+=sy

    def quality(self,observer,signal,frame=None):
        sources,jammers=self.frame() if frame is None else frame
        contributions={}
        for source in sources.get(observer,()):
            orbital=source.kind.startswith('orbital_')
            if orbital:
                if signal.domain not in ('Land','Sea-Surface'):continue
                if source.kind=='orbital_sea' and self.s.initial.map.regions[signal.region].terrain!='ocean':continue
            elif self.distances(source.region).get(signal.region,99)>source.radius:continue
            if source.kind=='listening' and not signal.emitting:continue
            if signal.domain=='Sea-Sub' and source.kind not in ('listening','sonar'):continue
            if source.kind=='sonar' and signal.domain!='Sea-Sub':continue
            q=min(Q(95,100),source.quality*(Q(7,10) if not orbital and source.kind!='sonar' and self.occluded(source.cell,signal.cell) else 1)+source.bonus)
            contributions[source.kind]=max(contributions.get(source.kind,Q(0)),q)
        unseen=Q(1)
        for q in contributions.values():unseen*=1-q
        quality=min(Q(95,100),1-unseen)
        jammed=any(region==signal.region and owner!=observer and
                   self.s.initial.diplomacy.relation(observer,owner)==Relation.WAR for owner,region in jammers)
        if jammed:quality*=Q(3,5)
        return Q(round(quality*1000),1000),'+'.join(sorted(contributions))


class RadarCommands:
    OPERATIONS=frozenset({('intelligence','radar_toggle')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'entity_id':str,'active':bool},{'ew_mode':str,'expected_version':int})
        formation=self.state.formations.rows.get(order['entity_id'])
        if formation is None or formation.owner!=owner:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order,formation)
        if formation.slot_a not in ('radar','ew'):raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if 'ew_mode' in order and (formation.slot_a!='ew' or order['ew_mode'] not in ('passive','jamming')):
            raise RuleViolation('ERR_SCHEMA')
        def commit():
            formation.emitter_active=order['active'];formation.ew_mode=order.get('ew_mode',formation.ew_mode)
            formation.version+=1;self.state.refits.apply_effects(formation)
        return PreparedCommand(commit)

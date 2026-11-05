"""Non-recursive treaty forwarding of frozen, directly observed military tracks."""
from copy import deepcopy
from .diplomacy import Relation
from .errors import InvariantViolation
from .numeric import Q,ceil,floor
from .combat import Unit,TargetContact
from types import SimpleNamespace


class IntelligenceSharing:
    def __init__(self,state):
        self.s=state
        self.received={p:{} for p in state.initial.known}
        self.sequence=dict.fromkeys(state.initial.known,0)
        self.last_cycle=-1
        self.targets={p:{} for p in state.initial.known}
        self.time=0

    def advance(self,ctx):
        self.time=ctx.tick+1
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle<=self.last_cycle:raise InvariantViolation('intelligence cycle forwarded twice')
        self.last_cycle=ctx.cycle
        state=self.s;time=ctx.tick+1
        # Freeze every sender before forwarding anything. Only direct native
        # vision is read, never self.received or another country's shared view.
        direct={p:deepcopy(state.vision.views[p]['tracks']) for p in self.received}
        for treaty in state.contracts.intelligence:
            if not treaty.start_cycle<=ctx.cycle<treaty.end_cycle:continue
            a,b=treaty.parties
            if state.initial.diplomacy.relation(a,b)==Relation.WAR:continue
            for sender,recipient in ((a,b),(b,a)):
                for row in direct[sender]:
                    if row['last_region'] not in treaty.regions or row['quality_mU']<treaty.minimum_quality_mU:continue
                    key=(sender,row['id'])
                    old=self.received[recipient].get(key)
                    if old is not None and old['observed_tick']>=row['observed_tick']:continue
                    if old is None:
                        self.sequence[recipient]+=1
                        alias=f'intel-{self.sequence[recipient]}'
                    else:alias=old['id']
                    # A forwarded stale row already contains aged uncertainty.
                    # Preserve its raw original estimate when available, so the
                    # receiver does not rebase old data to the forwarding tick.
                    source=state.vision.observers[sender]._tracks.get(row['id'])
                    if source is None:continue
                    observer=state.vision.observers[sender]
                    trajectory=next((k for k,a in getattr(observer,'_aliases',{}).items() if a==row['id']),None)
                    identity=getattr(state.vision,'identities',{}).get(sender,{}).get(trajectory)
                    formation=getattr(state,'formations',SimpleNamespace(rows={})).rows.get(identity)
                    ids=formation.unit_ids if formation is not None else ()
                    representative=next((kind for kind,spec in sorted(getattr(state.catalog,'units',{}).items()) if spec['domain']==source['domain']),None)
                    if ids and representative is not None and row['currently_visible'] and source['observed_tick']==time:
                        hp=max(1,source['estimated_center_mU']//len(ids))
                        self.targets[recipient][key]=tuple(Unit(uid,source['owner'],representative,source['last_region'],hp,1000) for uid in ids)
                    self.received[recipient][key]={**deepcopy(source),'id':alias,'shared_by':sender,
                        'received_tick':time,'source':'shared:'+sender+':'+source['source']}
        for owner,rows in self.received.items():
            for key,row in tuple(rows.items()):
                speed=state.vision.speeds[row['domain']]
                if Q(row['quality_mU'],1000)/(1+speed*Q(max(0,time-row['observed_tick']),ctx.clock.cycle_ticks))<Q(1,20):
                    del rows[key];self.targets[owner].pop(key,None)
            cap=state.catalog.runtime['limits']['visible_tracks_per_observer']
            if len(rows)>cap:
                for key in sorted(rows,key=lambda k:(rows[k]['observed_tick'],rows[k]['id']))[:len(rows)-cap]:
                    del rows[key];self.targets[owner].pop(key,None)

    def visible(self,owner,time):
        result=[]
        for frozen in self.received[owner].values():
            row=deepcopy(frozen);age=max(0,time-row['observed_tick'])
            distance=self.s.vision.speeds[row['domain']]*Q(age,10)
            width=ceil(row['estimated_center_mU']*(Q(15,100)*(1-Q(row['quality_mU'],1000))+Q(age,50)))
            row.update(quality_mU=floor(Q(row['quality_mU'])/(1+distance)),position_radius_hops=ceil(distance),
                estimated_low_mU=max(0,row['estimated_center_mU']-width),estimated_high_mU=row['estimated_center_mU']+width,
                currently_visible=False)
            result.append(row)
        return sorted(result,key=lambda r:r['id'])

    def contacts(self,owner,region):
        contacts=[]
        for key,row in self.received[owner].items():
            age=max(0,self.time-row['observed_tick'])
            quality=Q(row['quality_mU'],1000)/(1+self.s.vision.speeds[row['domain']]*Q(age,10))
            if quality<Q(1,5):continue
            hops=self.s.sensors.distances(region).get(row['last_region'],99)
            for unit in self.targets[owner].get(key,()):
                if self.s.initial.diplomacy.relation(owner,unit.owner)!=Relation.WAR:continue
                contacts.append(TargetContact(unit.id,quality,hops,row['last_region'],deepcopy(unit)))
        return tuple(contacts)

    def snapshot(self):
        return {'received':{p:[[sender,alias,row] for (sender,alias),row in sorted(rows.items())] for p,rows in self.received.items()},
                'sequence':self.sequence,'last_cycle':self.last_cycle,'time':self.time,
                'targets':{p:[[sender,alias,units] for (sender,alias),units in sorted(rows.items())] for p,rows in self.targets.items()}}

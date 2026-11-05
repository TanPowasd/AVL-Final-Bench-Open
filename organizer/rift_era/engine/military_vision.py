"""Observer-local military contacts, frozen aging and visibility-only deaths."""
from copy import deepcopy
import math

from .determinism import normal,digest
from .errors import RuleViolation,InfrastructureFault
from .numeric import Q,ceil
from .observation import Observer,Sighting


class MilitaryVision:
    def __init__(self,state):
        self.s=state
        self.speeds={domain:max(Q(row['hops_per_cycle'])*Q(23,20) for row in state.catalog.units.values()
                               if row['domain']==domain) for domain in state.catalog.tables['military']['domain_channel']}
        self.observers={p:Observer(p,state.catalog.runtime['limits']['visible_tracks_per_observer']) for p in state.initial.known}
        self.current={p:{} for p in self.observers}
        self.identities={p:{} for p in self.observers}
        self.sequences=dict.fromkeys(self.observers,0)
        self.pending_deaths={p:{} for p in self.observers}
        self.events={p:{name:set() for name in ('deleted_ids','lost_contact_ids','expired_track_ids','own_deleted_ids')}
                     for p in self.observers}
        self.views={}
        self.investigated={p:{} for p in self.observers}
        self.exposure_events={p:set() for p in self.observers}
        self._observe(0,0)

    def begin_tick(self,ctx):
        if ctx.clock.is_decision(ctx.tick):
            for ids in self.exposure_events.values():ids.clear()
            for events in self.events.values():
                for ids in events.values():ids.clear()

    def own_retired(self,owner,entity):
        if owner in self.events:self.events[owner]['own_deleted_ids'].add(entity)

    def record_deaths(self,formations):
        frame=self.s.sensors.frame()
        for formation in formations:
            self.own_retired(formation.owner,formation.id)
            signal=self.s.sensors.signal(formation)
            for owner in self.observers:
                if owner==signal.owner:continue
                quality,source=self.s.sensors.quality(owner,signal,frame)
                if quality>0:self.pending_deaths[owner][signal.id]=(signal,quality,source)

    def _key(self,owner,target):
        key=self.current[owner].get(target)
        if key is None:
            self.sequences[owner]+=1
            key=f'trajectory-{self.sequences[owner]}'
            self.identities[owner][key]=target
        return key

    def _observe(self,time,window):
        state=self.s;frame=state.sensors.frame()
        signals=tuple(state.sensors.signal(f) for f in state.formations.rows.values()) + tuple(state.deception.signal(d) for d in state.deception.rows.values())
        for owner,observer in self.observers.items():
            legal=[]
            for signal in signals:
                if signal.owner==owner or state.deception.exposed(owner,signal.id):continue
                quality,source=state.sensors.quality(owner,signal,frame)
                if quality>0:legal.append((signal,quality,source,False))
            legal.extend((signal,q,source,True) for signal,q,source in self.pending_deaths[owner].values())
            if sum(not dead for _,_,_,dead in legal)>observer.max_tracks:
                raise InfrastructureFault('current legal military contacts exceed observer capacity')
            sightings=[];current={}
            for signal,quality,source,dead in sorted(legal,key=lambda row:(row[0].region,row[2],
                    digest(state.initial.map.seed,'contact_admission',owner,window,row[0].id))):
                key=self._key(owner,signal.id)
                alias=observer.alias(key)
                if self.investigated[owner].get(alias)!=window:
                    self.investigated[owner][alias]=window
                    if state.deception.investigate(owner,signal.id,window,alias):
                        self.exposure_events[owner].add(alias)
                        observer.retire(alias)
                        continue
                z=normal(state.initial.map.seed,'military_sighting:'+owner,window,alias)
                estimate=max(0,round(signal.strength_mU*math.exp(.35*(1-.75*float(quality))*z)))
                width=ceil(estimate*Q(15,100)*(1-quality))
                sightings.append(Sighting(key,time,source,signal.region,round(quality*1000),max(0,estimate-width),
                    estimate+width,signal.domain,confirmed_destroyed=dead,owner=signal.owner,estimated_center_mU=estimate))
                if not dead:current[signal.id]=key
            changes=observer.update(time,tuple(sightings))
            expired=observer.expire(time,self.speeds)
            for name in ('deleted_ids','lost_contact_ids'):
                self.events[owner][name].update(changes[name])
            self.events[owner]['expired_track_ids'].update(changes['expired_track_ids'])
            self.events[owner]['expired_track_ids'].update(expired)
            self.current[owner]=current
            self.identities[owner]={key:target for key,target in self.identities[owner].items() if key in observer._aliases}
            self.pending_deaths[owner].clear()
            self.investigated[owner]={a:w for a,w in self.investigated[owner].items() if a in observer._aliases.values()}
            self.views[owner]=observer.view(time,self.speeds)|{name:sorted(ids) for name,ids in self.events[owner].items()}
            self.views[owner]['decoy_exposed']=sorted(self.exposure_events[owner])

    def advance(self,ctx):self._observe(ctx.tick+1,ctx.window)

    def visible(self,owner):return deepcopy(self.views[owner])

    def target(self,owner,alias,*,minimum_quality,current_only=True):
        """Resolve only the received trace, never test hidden target existence."""
        row=next((r for r in self.views[owner]['tracks'] if r['id']==alias),None)
        if row is None or Q(row['quality_mU'],1000)<minimum_quality or current_only and not row['currently_visible']:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        key=next((k for k,a in self.observers[owner]._aliases.items() if a==alias),None)
        target=self.identities[owner].get(key)
        if target is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        return target,deepcopy(row)

    def snapshot(self):
        return {'observers':{p:o.state_snapshot() for p,o in self.observers.items()},'current':self.current,
                'identities':self.identities,'sequences':self.sequences,'pending_deaths':self.pending_deaths,
                'events':self.events,'investigated':self.investigated,'exposure_events':self.exposure_events,'views':self.views,'domain_speeds':self.speeds}

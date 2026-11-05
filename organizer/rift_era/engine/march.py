"""Exact piecewise military trajectories and simultaneous hostile encounters.

Times span [0,1] within a tick. Every segment records actual normalized edge
progress, so crossing a node never grants an extra tick's movement budget.
"""
from dataclasses import dataclass
from .numeric import Q
from .errors import InvariantViolation


@dataclass(frozen=True)
class Position:
    node: str
    destination: str | None = None
    progress: Q = Q(0)
    edge: str | None = None

    def __post_init__(self):
        if not 0 <= self.progress < 1000 or self.destination == self.node or self.destination is None and (self.progress or self.edge is not None):
            raise ValueError('invalid military position')


@dataclass(frozen=True)
class Segment:
    start: Q
    end: Q
    before: Position
    after: Position

    def position(self,time):
        if not self.start <= time <= self.end:
            raise ValueError('time outside trajectory segment')
        if self.before == self.after or self.end == self.start:
            return self.before
        destination=self.before.destination or self.after.node
        initial=self.before.progress
        final=Q(1000) if self.after.destination is None else self.after.progress
        progress=initial+(final-initial)*(time-self.start)/(self.end-self.start)
        if progress==1000:return Position(destination)
        return Position(self.before.node,destination,progress,self.before.edge)


def trajectory(position,path,speed,terrain_cost,*,edge_ids=None,end_progress=Q(1000),profiles=None):
    """Path lists subsequent nodes; optional profiles integrate actual L1 terrain.

    A profile is an ascending sequence of (end_progress, terrain_cost), ending
    at 1000. No edge, terrain boundary or waypoint replenishes the tick budget.
    """
    if not 0 < end_progress <= 1000 or edge_ids is not None and len(edge_ids)!=len(path):
        raise InvariantViolation('invalid military route endpoint')
    if profiles is not None and len(profiles)!=len(path):
        raise InvariantViolation('military terrain profile count mismatch')
    if speed < 0 or position.destination and (not path or path[0] != position.destination):
        raise InvariantViolation('invalid military route/speed')
    time=Q(0);current=position;segments=[]
    for index,destination in enumerate(path):
        if speed==0 or time==1:break
        if current.destination not in (None,destination) or destination==current.node:
            raise InvariantViolation('discontinuous military path')
        profile=profiles[index] if profiles is not None else ((Q(1000),terrain_cost(current.node,destination)),)
        if (not profile or profile[-1][0]!=1000 or any(cost<=0 for _,cost in profile)
                or any(not 0<end<=1000 for end,_ in profile)
                or any(a[0]>=b[0] for a,b in zip(profile,profile[1:]))):
            raise InvariantViolation('invalid military terrain profile')
        limit=end_progress if index==len(path)-1 else Q(1000)
        if limit<current.progress:raise InvariantViolation('route endpoint lies behind the formation')
        edge=edge_ids[index] if edge_ids is not None else current.edge
        if current.edge is not None and edge!=current.edge:
            raise InvariantViolation('route changed corridor while in transit')
        for boundary,cost in profile:
            if boundary<=current.progress:continue
            end=min(boundary,limit)
            rate=speed/cost
            needed=(end-current.progress)/rate
            elapsed=min(1-time,needed)
            before=Position(current.node,destination,current.progress,edge)
            progress=current.progress+elapsed*rate
            after=Position(destination) if progress==1000 else Position(current.node,destination,progress,edge)
            segments.append(Segment(time,time+elapsed,before,after))
            time+=elapsed;current=after
            if time==1 or progress==limit:break
        if current.destination is not None:break
    if time<1:segments.append(Segment(time,Q(1),current,current))
    return tuple(segments)


def _coordinate(segment,time,edge):
    p=segment.position(time)
    if p.destination is None:
        return Q(0) if p.node==edge[0] else Q(1000) if p.node==edge[1] else None
    if p.edge is not None and edge[2] is not None and p.edge!=edge[2]:return None
    if frozenset((p.node,p.destination)) != frozenset(edge[:2]):return None
    return p.progress if p.node==edge[0] else 1000-p.progress


def _physical_point(position):
    if position.destination is None or position.progress==0:
        return ('node',position.node)
    a,b=sorted((position.node,position.destination))
    progress=position.progress if position.node==a else 1000-position.progress
    return ('edge',position.edge,a,b,progress)


def first_meeting(left,right):
    # Units already co-located may disengage. Only a meeting after physical
    # separation is a new interception; otherwise retreat could never begin.
    separated=_physical_point(left[0].before)!=_physical_point(right[0].before)
    intervals=sorted(((max(a.start,b.start),min(a.end,b.end),i,j)
                      for i,a in enumerate(left) for j,b in enumerate(right)
                      if max(a.start,b.start)<=min(a.end,b.end)))
    earliest=None
    for start,end,i,j in intervals:
        a,b=left[i],right[j]
        if earliest is not None and start>earliest[0]:break
        same_start=_physical_point(a.position(start))==_physical_point(b.position(start))
        was_separate=separated or not same_start
        middle=(start+end)/2
        middle_separate=_physical_point(a.position(middle))!=_physical_point(b.position(middle))
        for time,eligible in ((start,was_separate),(end,was_separate or middle_separate)):
            pa,pb=a.position(time),b.position(time)
            if eligible and _physical_point(pa)==_physical_point(pb) and (earliest is None or time<earliest[0]):
                earliest=(time,Position(pa.node) if pa.destination is None or pa.progress==0 else pa)
        edge=next(((p.node,p.destination,p.edge) for p in (a.before,a.after,b.before,b.after) if p.destination),None)
        if edge is not None:
            ax,bx=_coordinate(a,start,edge),_coordinate(b,start,edge)
            ay,by=_coordinate(a,end,edge),_coordinate(b,end,edge)
            if None not in (ax,bx,ay,by):
                first,last=ax-bx,ay-by
                time=None
                if first==0 and was_separate:time=start
                elif first and first*last<=0 and first!=last:
                    time=start+(end-start)*first/(first-last)
                if time is not None and (earliest is None or time<earliest[0]):earliest=(time,a.position(time))
        separated=was_separate or middle_separate or _physical_point(a.position(end))!=_physical_point(b.position(end))
    return earliest


def stop_at(segments,time):
    result=[]
    for segment in segments:
        if segment.start>time:break
        if segment.end<time:
            result.append(segment);continue
        point=segment.position(time)
        if segment.start<time:result.append(Segment(segment.start,time,segment.before,point))
        result.append(Segment(time,Q(1),point,point))
        break
    return tuple(result)


def simultaneous_trajectories(proposals,hostile):
    """Stop at the first actual encounter, including later arrivals at a stop."""
    paths=dict(proposals);seen=set();encounters=[]
    ids=sorted(paths)
    while True:
        candidates=[]
        for i,a in enumerate(ids):
            for b in ids[i+1:]:
                if (a,b) in seen or not hostile(a,b):continue
                meeting=first_meeting(paths[a],paths[b])
                if meeting is not None:candidates.append((meeting[0],a,b,meeting[1]))
        if not candidates:break
        time=min(row[0] for row in candidates)
        # All equal-time contacts use the same pre-stop paths.
        batch=[row for row in candidates if row[0]==time]
        for _,a,b,point in batch:
            seen.add((a,b));encounters.append((a,b,time,point))
        for actor in {a for _,a,b,_ in batch}|{b for _,a,b,_ in batch}:
            paths[actor]=stop_at(paths[actor],time)
    return paths,tuple(encounters)

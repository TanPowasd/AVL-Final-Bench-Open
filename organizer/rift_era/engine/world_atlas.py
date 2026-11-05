"""Versioned coastal atlas and hidden geological sites with an actual bootstrap gate.

The generator's public geometry and hidden resource records are separate. A
candidate remains unaccepted until a caller executes the full 150-tick opening;
no resource is topped up after a rejection.
"""
from array import array
from collections import deque
from dataclasses import dataclass, replace
import json
from pathlib import Path

from .determinism import digest
from .errors import InfrastructureFault
from .world import (Region, Hub, Route, SpatialWorld, BootstrapResult, SIZE, ROWS, COLS,
                    draw, _region_id, _manhattan)

PROFILE = json.loads((Path(__file__).resolve().parents[1] / 'data' / 'world_atlas_v1.json').read_text())
VERSION = PROFILE['version']


@dataclass(frozen=True)
class ResourceSite:
    id: str
    hub: str
    region: str
    good: str
    reserve_mU: int
    grade_mU: int
    renewable: bool = False
    starting_mine: str | None = None


def generate_candidate(seed: int, attempt: int = 0) -> SpatialWorld:
    if type(seed) is not int or not 0 <= seed < 2**64 or type(attempt) is not int or not 0 <= attempt < 256:
        raise ValueError('world generation requires uint64 seed and attempt 0..255')
    count = draw(seed, attempt, 'atlas_continents', '', 2, 4)
    coords = {(r, c): _region_id(r, c) for r in range(ROWS) for c in range(COLS)}
    positions = {rid: rc for rc, rid in coords.items()}
    adjacency = {rid: tuple(coords[p] for p in ((r-1,c),(r+1,c),(r,c-1),(r,c+1)) if p in coords)
                 for (r,c),rid in coords.items()}
    # Ocean channels meander coherently so coastlines form bays and fjords,
    # while every mainland retains a continuous north/south land spine.
    channels = []
    for i in range(1, count):
        center = i * COLS // count
        offsets = []
        value = 0
        for r in range(ROWS):
            value = max(-1, min(1, value + draw(seed, attempt, 'coast_step', f'{i}:{r}', -1, 1)))
            offsets.append(center + value)
        channels.append(offsets)
    sea = {coords[r,c] for r in range(ROWS) for c in range(COLS) if any(c == channel[r] for channel in channels)}
    # Bays connect to channels and enlarge them to exactly the frozen profile's
    # sea count. An articulation test preserves 2..4 connected mainlands.
    def components(land):
        result=[]; unseen=set(land)
        while unseen:
            first=min(unseen); q=[first]; comp=set()
            while q:
                rid=q.pop()
                if rid not in unseen:continue
                unseen.remove(rid);comp.add(rid);q.extend(adjacency[rid])
            result.append(comp)
        return result
    land=set(positions)-sea
    # Repair channel diagonal steps into connected ocean cells.
    for channel in channels:
        for r in range(ROWS-1):
            if channel[r] != channel[r+1]:
                sea.add(coords[r+1,channel[r]])
    land=set(positions)-sea
    target=len(positions)-PROFILE['land_regions']
    while len(sea)<target:
        options=sorted((rid for rid in land if any(n in sea for n in adjacency[rid])),
                       key=lambda r:digest(seed,'atlas_bay',attempt,len(sea),r))
        chosen=None
        for rid in options:
            comps=components(land-{rid})
            if len(comps)==count and min(map(len,comps))>=45:
                chosen=rid;break
        if chosen is None:raise InfrastructureFault('atlas cannot carve connected bays')
        land.remove(chosen);sea.add(chosen)
    continents=sorted(components(land),key=lambda c:min(positions[r][1] for r in c))
    if len(continents)!=count:raise InfrastructureFault('atlas mainland connectivity failed')
    continent={rid:i for i,c in enumerate(continents) for rid in c}
    # Exchange a few removable shoreline regions for true offshore islands.
    # The land/sea totals stay fixed and countries are placed only on mainland.
    islands=[]
    ocean_interiors=sorted((r for r in sea if all(n in sea for n in adjacency[r])),
                           key=lambda r:digest(seed,'atlas_island',attempt,r))
    for island in ocean_interiors[:3]:
        if any(n in islands for n in adjacency[island]):continue
        coast=sorted((r for r in land if any(n in sea for n in adjacency[r])),key=lambda r:digest(seed,'atlas_island_exchange',attempt,island,r))
        for remove in coast:
            ci=continent[remove]
            if len(continents[ci])<50 or len(components(continents[ci]-{remove}))!=1:continue
            continents[ci].remove(remove);land.remove(remove);sea.add(remove);continent.pop(remove)
            land.add(island);sea.remove(island);islands.append(island)
            ir,ic=positions[island]
            nearest=min(continent,key=lambda r:(abs(positions[r][0]-ir)+abs(positions[r][1]-ic),r))
            continent[island]=continent[nearest]
            break
    # Seats are assigned after geography by an independent uniform permutation.
    permutation=tuple(sorted(range(8),key=lambda p:digest(seed,'atlas_seat',attempt,p)))
    seats=[[] for _ in continents]
    for i,p in enumerate(permutation):seats[i%count].append(p)
    owners={};countries={}
    for ci,people in enumerate(seats):
        free=set(continents[ci])
        for index,p in enumerate(people):
            target_row=(2*index+1)*ROWS//(2*len(people))
            choices=sorted(free,key=lambda r:(abs(positions[r][0]-target_row),digest(seed,'atlas_capital',attempt,p,r)))
            picked=None
            for start in choices:
                queue=deque([start]); seen={start};taken=[]
                while queue and len(taken)<20:
                    rid=queue.popleft();taken.append(rid)
                    neighbors=sorted((n for n in adjacency[rid] if n in free and n not in seen),key=lambda n:digest(seed,'atlas_territory',attempt,p,n))
                    queue.extend(neighbors);seen.update(neighbors)
                if len(taken)==20:
                    picked=taken;break
            if picked is None:raise InfrastructureFault('atlas cannot place contiguous starting territory')
            owner=f'n{p}';countries[owner]=tuple(picked);owners.update({r:owner for r in picked});free.difference_update(picked)
    regions={}
    for rid,(r,c) in positions.items():
        if rid in sea:terrain='ocean';elevation=0
        else:
            ridge=abs((c*3+r+draw(seed,attempt,'ridge','',0,8))%13-6)
            terrain=('mountain' if ridge<1 else 'hill' if ridge<3 else ('plain','forest','plain','desert')[draw(seed,attempt,'atlas_terrain',rid,0,3)])
            elevation={'plain':15,'forest':30,'desert':25,'hill':80,'mountain':200}[terrain]+draw(seed,attempt,'atlas_elevation',rid,0,15)
        regions[rid]=Region(rid,r,c,continent.get(rid),terrain,elevation,owners.get(rid),adjacency[rid],
            draw(seed,attempt,'atlas_fertility',rid,700,1300),draw(seed,attempt,'atlas_wind',rid,180,600),
            draw(seed,attempt,'atlas_cloud',rid,150,650),600+400*min(r,ROWS-1-r)//9)
    # Rivers descend in strictly decreasing drainage rank; ocean is the only
    # sink. Elevation is conditioned consistently, rather than cycles hidden by
    # arbitrary iteration ordering.
    distance={r:0 for r in sea};queue=deque(sorted(sea))
    while queue:
        rid=queue.popleft()
        for n in adjacency[rid]:
            if n not in distance:distance[n]=distance[rid]+1;queue.append(n)
    downstream={}
    for rid in sorted(land,key=lambda r:(distance[r],r)):
        choices=[n for n in adjacency[rid] if distance[n]<distance[rid]]
        outlet=min(choices,key=lambda n:(regions[n].elevation,digest(seed,'atlas_river',attempt,rid,n)))
        downstream[rid]=(outlet,)
        regions[rid]=replace(regions[rid],elevation=max(regions[rid].elevation,regions[outlet].elevation+1))
    hubs={};occupied=set()
    def add(hid,rid,owner,core=None):
        r,c=positions[rid];x=(c*SIZE//COLS+(c+1)*SIZE//COLS)//2;y=(r*SIZE//ROWS+(r+1)*SIZE//ROWS)//2
        hubs[hid]=Hub(hid,rid,x,y,owner,core);occupied.add(rid)
    for owner,territory in sorted(countries.items()):
        for i,rid in enumerate(territory[:5]):add(f'{owner}-h{i}',rid,owner,i)
    city_count=draw(seed,attempt,'atlas_city_count','',36,48)
    free=sorted(land-set(owners),key=lambda r:digest(seed,'atlas_city',attempt,r))
    cities=tuple(f'city-{i}' for i in range(city_count))
    for city,rid in zip(cities,free):
        regions[rid]=replace(regions[rid],sovereign=city);add(f'{city}-h0',rid,city)
    for rid in sorted(land-occupied,key=lambda r:digest(seed,'atlas_extra_hub',attempt,r))[:PROFILE['hub_count']-len(hubs)]:add(f'h-{rid}',rid,owners.get(rid))
    palette={'ocean':0,'plain':1,'hill':2,'forest':3,'desert':4,'mountain':5}
    indices={rid:i for i,rid in enumerate(sorted(regions))};cells=array('H');terrain=bytearray()
    for y in range(SIZE):
        r=min(ROWS-1,((y+1)*ROWS-1)//SIZE)
        for x in range(SIZE):
            c=min(COLS-1,((x+1)*COLS-1)//SIZE);rid=coords[r,c]
            cells.append(indices[rid]);terrain.append(palette[regions[rid].terrain])
    coastal=[];port_water={}
    for hid,hub in tuple(hubs.items()):
        oceans=[n for n in adjacency[hub.region] if n in sea]
        if not oceans:continue
        water=min(oceans,key=lambda n:digest(seed,'atlas_port',attempt,hid,n));r,c=positions[hub.region];wr,wc=positions[water]
        x=(c+1)*SIZE//COLS-1 if wc>c else c*SIZE//COLS if wc<c else hub.x
        y=(r+1)*SIZE//ROWS-1 if wr>r else r*SIZE//ROWS if wr<r else hub.y
        hubs[hid]=replace(hub,x=x,y=y);coastal.append(hid)
        port_water[hid]=(y+(1 if wr>r else -1 if wr<r else 0))*SIZE+x+(1 if wc>c else -1 if wc<c else 0)
    def region_path(a,b,allowed):
        queue=deque([a]);prev={a:None}
        while queue:
            rid=queue.popleft()
            if rid==b:break
            for n in sorted(adjacency[rid]):
                if n in allowed and n not in prev:prev[n]=rid;queue.append(n)
        if b not in prev:return None
        path=[];node=b
        while node is not None:path.append(node);node=prev[node]
        return tuple(reversed(path))
    def cell_path(a,b,rpath):
        points=[a]
        for rid in rpath[1:-1]:
            r,c=positions[rid];points.append(Hub('',rid,(c*SIZE//COLS+(c+1)*SIZE//COLS)//2,(r*SIZE//ROWS+(r+1)*SIZE//ROWS)//2,None))
        points.append(b);result=[]
        for p,q in zip(points,points[1:]):
            segment=_manhattan(p,q);result.extend(segment if not result else segment[1:])
        return tuple(result)
    routes={};pairs=set()
    def road(a,b):
        pair=tuple(sorted((a,b)))
        if pair in pairs:return
        rp=region_path(hubs[a].region,hubs[b].region,land)
        if rp is None:return
        route=cell_path(hubs[a],hubs[b],rp)
        # Center-based elbow can clip an ocean corner when an endpoint is at
        # the coast. Fall back to a true cell BFS for this exceptional path.
        if any(terrain[c]==0 for c in route):route=_cell_bfs(hubs[a].y*SIZE+hubs[a].x,hubs[b].y*SIZE+hubs[b].x,terrain,False)
        if not route:return
        eid='road:'+':'.join(pair);routes[eid]=Route(eid,a,b,'road',route,True);pairs.add(pair)
    for owner in countries:
        for i in range(1,5):road(f'{owner}-h{i-1}',f'{owner}-h{i}')
    # The sparse public atlas must connect every mainland Hub, including distant
    # corners that a global nearest-pairs cutoff would otherwise leave isolated.
    for mainland in continents:
        members=sorted(hid for hid,h in hubs.items() if h.region in mainland)
        if not members:continue
        connected={members[0]};unconnected=set(members[1:])
        while unconnected:
            _,a,b=min((abs(hubs[a].x-hubs[b].x)+abs(hubs[a].y-hubs[b].y),a,b) for a in connected for b in unconnected)
            road(a,b);connected.add(b);unconnected.remove(b)
    candidates=[];ordered=sorted(hubs)
    for i,a in enumerate(ordered):
        for b in ordered[i+1:]:
            if continent[hubs[a].region]==continent[hubs[b].region]:
                candidates.append((abs(hubs[a].x-hubs[b].x)+abs(hubs[a].y-hubs[b].y),digest(seed,'atlas_road',attempt,a,b),a,b))
    for _,_,a,b in sorted(candidates):
        road(a,b)
        if len(routes)>=PROFILE['road_target']:break
    for ci in range(count-1):
        left=[h for h in coastal if continent[hubs[h].region]==ci];right=[h for h in coastal if continent[hubs[h].region]==ci+1]
        for _,a,b in sorted((abs(hubs[a].x-hubs[b].x)+abs(hubs[a].y-hubs[b].y),a,b)for a in left for b in right)[:16]:
            waterpath=_cell_bfs(port_water[a],port_water[b],terrain,True)
            if waterpath:
                path=(hubs[a].y*SIZE+hubs[a].x,)+waterpath+(hubs[b].y*SIZE+hubs[b].x,)
                eid=f'sea:{a}:{b}';routes[eid]=Route(eid,a,b,'sea',path,False)
    for island in islands:
        island_hubs=[h for h in coastal if hubs[h].region==island]
        for a in island_hubs:
            options=sorted((abs(hubs[a].x-hubs[b].x)+abs(hubs[a].y-hubs[b].y),b) for b in coastal if b!=a and hubs[b].region not in islands)
            for _,b in options:
                path=_cell_bfs(port_water[a],port_water[b],terrain,True)
                if path:
                    eid=f'sea:{a}:{b}';routes[eid]=Route(eid,a,b,'sea',(hubs[a].y*SIZE+hubs[a].x,)+path+(hubs[b].y*SIZE+hubs[b].x,),False)
                    break
    region_hubs={h.region:h for h in hubs.values()}
    for rid,outlets in downstream.items():
        if rid not in region_hubs or outlets[0] not in region_hubs:continue
        a,b=region_hubs[rid],region_hubs[outlets[0]]
        route=cell_path(a,b,(rid,outlets[0]))
        if any(terrain[c]==0 for c in route):route=_cell_bfs(a.y*SIZE+a.x,b.y*SIZE+b.x,terrain,False)
        if route:
            eid=f'river:{a.id}:{b.id}';routes[eid]=Route(eid,a.id,b.id,'river',route,False)
    resources={}
    for owner in countries:
        for good,count_sites,grade in (('coal',2,1250),('iron_ore',1,1125),('copper_ore',1,1000),('stone',1,1000),('quartz',1,1000),('water',2,1000),('biomass',1,1000)):
            for i in range(count_sites):
                mid=f'{owner}/mine/{good}/{i}';hub=f'{owner}-h0';resources[mid]=ResourceSite(mid,hub,hubs[hub].region,good,
                    PROFILE['starting_finite_reserves_mU'][good],grade,good in ('water','biomass'),mid)
    for hid,hub in hubs.items():
        region=regions[hub.region]
        eligible=('stone','iron_ore','copper_ore','rare_ore','uranium_ore') if region.terrain in ('mountain','hill') else ('crude_oil','natural_gas','bauxite','coal','quartz','biomass','water')
        chosen=sorted(eligible,key=lambda g:digest(seed,'atlas_geology_type',attempt,hid,g))[:2]
        for good in chosen:
            sid=f'deposit:{hid}:{good}';reserve=0 if good=='water' else draw(seed,attempt,'atlas_reserve',sid,8000,48000)*1000
            resources[sid]=ResourceSite(sid,hid,hub.region,good,reserve,draw(seed,attempt,'atlas_grade',sid,600,1500),good in ('water','biomass'))
    world=SpatialWorld(seed,attempt,regions,hubs,routes,countries,cities,cells,bytes(terrain),generator_version=VERSION,
        hidden_resources=resources,river_downstream=downstream,coastal_hubs=tuple(sorted(coastal)),port_water_cells=port_water,seat_permutation=permutation)
    world.validate_geometry();validate_atlas(world)
    return world


def _cell_bfs(start,end,terrain,water):
    queue=deque([start]);prev={start:None}
    while queue:
        cell=queue.popleft()
        if cell==end:break
        x,y=cell%SIZE,cell//SIZE
        for n in ((cell-1 if x else -1),(cell+1 if x<SIZE-1 else -1),(cell-SIZE if y else -1),(cell+SIZE if y<SIZE-1 else -1)):
            if n>=0 and n not in prev and (terrain[n]==0)==water:prev[n]=cell;queue.append(n)
    if end not in prev:return ()
    result=[];node=end
    while node is not None:result.append(node);node=prev[node]
    return tuple(reversed(result))


def validate_atlas(world):
    for rid,outlets in world.river_downstream.items():
        if len(outlets)!=1 or outlets[0] not in world.regions[rid].adjacent or world.regions[outlets[0]].elevation>=world.regions[rid].elevation:
            raise InfrastructureFault('atlas river is not a descending adjacency DAG')
    for route in world.routes.values():
        if route.mode=='sea' and any(world.terrain_cells[c]!=0 for c in route.cells[1:-1]):
            raise InfrastructureFault('sea route traverses inland terrain')
    if len(world.seat_permutation)!=8 or set(world.seat_permutation)!=set(range(8)):
        raise InfrastructureFault('invalid seat permutation')


def generate_world(seed, bootstrap):
    for attempt in range(PROFILE['maximum_attempts']):
        try:world=generate_candidate(seed,attempt)
        except InfrastructureFault:continue
        result=bootstrap(world)
        if isinstance(result,BootstrapResult) and result.accepted and result.ticks_executed==150 and not result.invariant_failures:
            world.accepted=True;return world
    raise InfrastructureFault('INFRA_FAULT_WORLDGEN: 256 atlas/bootstrap candidates rejected')

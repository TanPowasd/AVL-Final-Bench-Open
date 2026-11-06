"""04/03 L12 artificial island (artificial_island_v1).

`industry/build_facility {hub_id, facility_type: "public", profile_id: "L12_island"}`
at an owned coastal Hub builds, over 240 cycles, a new near-shore land region
with one new Hub on the ocean cell nearest the site. All materials
(structure_module 80, steel 160, cement 300, metamaterial 8) are locked at the
site; nothing is drawn from the capital.

The frozen map is extended in place at completion, never rebuilt:
- the new region id starts with `~` so it sorts after every generated id and
  the sorted region palette used by `region_cells` keeps every old index;
- the island cell becomes plain land of that region; adjacency is added both
  ways to the site's region and the ocean region the cell belonged to;
- the region is the builder's sovereign territory and starts under its
  control, with no population, no river (no natural water) and the default
  public warehouse of a Hub.
The island has no corridor of its own; a port (sea) or an L12 bridge connects it.
"""
from __future__ import annotations

from dataclasses import replace

from .errors import RuleViolation, InvariantViolation
from .world import Hub, Region, SIZE

COSTS = {'structure_module': 80000, 'steel': 160000, 'cement': 300000, 'metamaterial': 8000}
CYCLES = 240
PLAIN = 1


def island_cell(world, hub_id):
    """Nearest ocean cell to the Hub (Manhattan distance, then cell index)."""
    hub = world.hubs[hub_id]
    best = None
    radius = 24
    for y in range(max(0, hub.y - radius), min(SIZE, hub.y + radius + 1)):
        for x in range(max(0, hub.x - radius), min(SIZE, hub.x + radius + 1)):
            cell = y * SIZE + x
            if world.terrain_cells[cell] != 0:
                continue
            key = (abs(x - hub.x) + abs(y - hub.y), cell)
            if best is None or key < best:
                best = key
    if best is None:
        raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'no sea within reach of the site')
    return best[1]


def found(state, job):
    world = state.initial.map
    owner, cell = job.owner, job.payload['cell']
    if world.terrain_cells[cell] != 0:
        raise InvariantViolation('island cell is no longer sea')
    palette = sorted(world.regions)
    sea_region = palette[world.region_cells[cell]]
    host_region = world.hubs[job.hub].region
    count = sum(r.startswith('~isl') for r in world.regions)
    rid = f'~isl{count:04d}'          # monotonic: later islands sort after earlier ones
    hub_id = f'{owner}-isl{count:04d}'
    if rid in world.regions or hub_id in world.hubs:
        raise InvariantViolation('island founded twice')
    host = world.regions[host_region]
    world.regions[rid] = Region(rid, cell // SIZE * 20 // SIZE, cell % SIZE * 24 // SIZE, None, 'plain', 0, owner,
                                tuple(sorted({host_region, sea_region})), 0, host.wind_mean_mU, host.cloud_mean_mU,
                                host.latitude_mU)
    for neighbour in (host_region, sea_region):
        row = world.regions[neighbour]
        world.regions[neighbour] = replace(row, adjacent=tuple(sorted({*row.adjacent, rid})))
    if sorted(world.regions)[-1] != rid:
        raise InvariantViolation('island region would shift the region palette')
    world.region_cells[cell] = len(palette)
    terrain = bytearray(world.terrain_cells)
    terrain[cell] = PLAIN
    world.terrain_cells = bytes(terrain)
    world.hubs[hub_id] = Hub(hub_id, rid, cell % SIZE, cell // SIZE, owner)
    if owner in world.country_regions:
        world.country_regions[owner] = tuple(world.country_regions[owner]) + (rid,)
    world.coastal_hubs = tuple(sorted({*world.coastal_hubs, hub_id}))
    # Per-Hub and per-region books that every other Hub got at initialization.
    from .authority import RegionAuthority
    state.authority.regions[rid] = RegionAuthority(owner, owner)
    # The island structure itself is the Hub's public warehouse (300 base, no modules).
    state.initial.warehouse_modules[hub_id] = {job.target: 0}
    state.initial.warehouse_mU[hub_id] = 300000
    state.initial.housing_mU.setdefault(hub_id, 0)
    for book, value in (('pollution', 0), ('grid_prices_ru', None), ('rain', None), ('water_used_mU', 0),
                        ('natural_water_mU', 0), ('wind_mU', None), ('cloud_mU', None), ('sun_mU', None)):
        table = getattr(state, book, None)
        if not isinstance(table, dict):
            continue
        key = rid if book in ('wind_mU', 'cloud_mU', 'sun_mU') else hub_id
        if key in table:
            continue
        source = host_region if book in ('wind_mU', 'cloud_mU', 'sun_mU') else job.hub
        table[key] = table.get(source, value) if value is None else value
    state.megaprojects[rid] = {'kind': 'L12_island', 'owner': owner, 'asset': job.target, 'hub': hub_id, 'cell': cell}
    return hub_id

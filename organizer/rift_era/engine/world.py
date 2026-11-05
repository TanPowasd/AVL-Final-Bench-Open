"""Versioned deterministic spatial candidate generation and bootstrap gate.

Generation and acceptance are different operations. A candidate is never an
official initial world until the caller's actual 150-tick bootstrap has passed.
The v1 stratified generator is an explicit implementation distribution, not an
assertion that this distribution has passed route/seat balance calibration.
"""

from __future__ import annotations

from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace, field

from .determinism import digest
from .errors import InfrastructureFault

MAP_VERSION = 'rift_spatial_stratified_v1'
SIZE = 512
COLS, ROWS = 24, 20


def draw(seed: int, attempt: int, tag: str, entity: str, low: int, high: int) -> int:
    """Unbiased rejection sampling of a bounded integer using independent keys."""
    span = high - low + 1
    ceiling = 2**64 - (2**64 % span)
    retry = 0
    while True:
        value = int.from_bytes(digest(seed, 'worldgen', attempt, tag, entity, retry)[:8], 'big')
        if value < ceiling:
            return low + value % span
        retry += 1


@dataclass(frozen=True)
class Region:
    id: str
    row: int
    column: int
    continent: int | None
    terrain: str
    elevation: int
    sovereign: str | None
    adjacent: tuple[str, ...]
    fertility_mU: int
    wind_mean_mU: int
    cloud_mean_mU: int
    latitude_mU: int


@dataclass(frozen=True)
class Hub:
    id: str
    region: str
    x: int
    y: int
    owner: str | None
    core_index: int | None = None


@dataclass(frozen=True)
class Route:
    id: str
    a: str
    b: str
    mode: str
    cells: tuple[int, ...]
    initial_operational: bool

    @property
    def length(self) -> int:
        return max(0, len(self.cells) - 1)


@dataclass(frozen=True)
class BootstrapResult:
    accepted: bool
    ticks_executed: int
    invariant_failures: tuple[str, ...] = ()


@dataclass
class SpatialWorld:
    seed: int
    attempt: int
    regions: dict[str, Region]
    hubs: dict[str, Hub]
    routes: dict[str, Route]
    country_regions: dict[str, tuple[str, ...]]
    city_states: tuple[str, ...]
    region_cells: array
    terrain_cells: bytes
    accepted: bool = False
    generator_version: str = MAP_VERSION
    hidden_resources: dict = field(default_factory=dict)
    river_downstream: dict = field(default_factory=dict)
    coastal_hubs: tuple[str, ...] = ()
    port_water_cells: dict = field(default_factory=dict)
    seat_permutation: tuple[int, ...] = ()

    def public_map(self) -> dict:
        """Whitelist only public geometry, never the seed or hidden deposits."""
        runs = []
        for y in range(SIZE):
            row = self.region_cells[y * SIZE:(y + 1) * SIZE]
            values = []
            for value in row:
                if values and values[-1][0] == value:
                    values[-1][1] += 1
                else:
                    values.append([value, 1])
            runs.append(values)
        return {'map_version': self.generator_version, 'width': SIZE, 'height': SIZE,
                'region_palette': sorted(self.regions), 'region_rle_rows': runs,
                'terrain_by_region': {r.id: r.terrain for r in self.regions.values()},
                'adjacency': {r.id: r.adjacent for r in self.regions.values()},
                'hubs': self.hubs, 'routes': self.routes,
                'river_downstream': self.river_downstream, 'coastal_hubs': self.coastal_hubs,
                'elevation_by_region': {r.id: r.elevation for r in self.regions.values()},
                'fertility_by_region': {r.id: r.fertility_mU for r in self.regions.values()},
                'wind_by_region': {r.id: r.wind_mean_mU for r in self.regions.values()},
                'cloud_by_region': {r.id: r.cloud_mean_mU for r in self.regions.values()}}

    def manifest(self) -> dict:
        return {'seed': self.seed, 'attempt': self.attempt, 'generator_version': self.generator_version,
                'regions': self.regions, 'hubs': self.hubs, 'routes': self.routes,
                'countries': self.country_regions, 'city_states': self.city_states,
                'accepted': self.accepted, 'hidden_resources': self.hidden_resources,
                'river_downstream': self.river_downstream, 'coastal_hubs': self.coastal_hubs,
                'port_water_cells': self.port_water_cells, 'seat_permutation': self.seat_permutation}

    def validate_geometry(self) -> None:
        land = {r.id for r in self.regions.values() if r.continent is not None}
        sea = set(self.regions) - land
        continents = {r.continent for r in self.regions.values() if r.continent is not None}
        if not 340 <= len(land) <= 420 or not 80 <= len(sea) <= 120 or not 2 <= len(continents) <= 4:
            raise InfrastructureFault('world geometry count bounds failed')
        if not 220 <= len(self.hubs) <= 280 or not 1100 <= len(self.routes) <= 1500:
            raise InfrastructureFault('hub/route count bounds failed')
        if len(self.region_cells) != SIZE**2 or len(self.terrain_cells) != SIZE**2:
            raise InfrastructureFault('invalid L1 dimensions')
        claimed = set()
        for owner, owned in self.country_regions.items():
            if not 18 <= len(owned) <= 24 or claimed.intersection(owned):
                raise InfrastructureFault('overlapping or incorrectly sized initial territory')
            claimed.update(owned)
            visited, queue = set(), [owned[0]]
            while queue:
                region = queue.pop()
                if region in visited:
                    continue
                visited.add(region)
                queue.extend(r for r in self.regions[region].adjacent if r in owned and r not in visited)
            if visited != set(owned):
                raise InfrastructureFault('disconnected initial territory')
            cores = [h for h in self.hubs.values() if h.owner == owner and h.core_index is not None]
            if {h.core_index for h in cores} != set(range(5)):
                raise InfrastructureFault('country must have exactly five core hubs')
        for route in self.routes.values():
            if route.a not in self.hubs or route.b not in self.hubs or not route.cells:
                raise InfrastructureFault('invalid route endpoints')
            a, b = self.hubs[route.a], self.hubs[route.b]
            if route.cells[0] != a.y * SIZE + a.x or route.cells[-1] != b.y * SIZE + b.x:
                raise InfrastructureFault('route does not reach its hubs')
            for p, q in zip(route.cells, route.cells[1:]):
                if abs(p % SIZE - q % SIZE) + abs(p // SIZE - q // SIZE) != 1:
                    raise InfrastructureFault('route jumps across L1 cells')
            if route.mode == 'road' and any(self.terrain_cells[cell] == 0 for cell in route.cells):
                raise InfrastructureFault('road crosses unbridged ocean')


def _region_id(row: int, column: int) -> str:
    return f'r{row:02d}-{column:02d}'


def _manhattan(a: Hub, b: Hub) -> tuple[int, ...]:
    x, y = a.x, a.y
    path = [y * SIZE + x]
    while x != b.x:
        x += 1 if b.x > x else -1
        path.append(y * SIZE + x)
    while y != b.y:
        y += 1 if b.y > y else -1
        path.append(y * SIZE + x)
    return tuple(path)


def generate_candidate(seed: int, attempt: int = 0) -> SpatialWorld:
    if type(seed) is not int or not 0 <= seed < 2**64 or type(attempt) is not int or not 0 <= attempt < 256:
        raise ValueError('world generation requires a uint64 seed and attempt in 0..255')
    continent_count = draw(seed, attempt, 'continents', '', 2, 4)
    # 20 land columns and 4 ocean columns. Countries have sufficient contiguous
    # interior territory even in the four-continent case.
    widths = [20 // continent_count + int(i < 20 % continent_count) for i in range(continent_count)]
    gaps = [4 // (continent_count - 1) + int(i < 4 % (continent_count - 1)) for i in range(continent_count - 1)]
    column_continent = []
    for i, width in enumerate(widths):
        column_continent.extend([i] * width)
        if i < len(gaps):
            column_continent.extend([None] * gaps[i])
    coords = {(y, x): _region_id(y, x) for y in range(ROWS) for x in range(COLS)}
    adjacency = {rid: tuple(coords[p] for p in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)) if p in coords)
                 for (y, x), rid in coords.items()}
    countries: dict[str, tuple[str, ...]] = {}
    owners = {}
    # Distribute eight countries over continents and separated north/south bands.
    seats_on_continent = [[p for p in range(8) if p % continent_count == i] for i in range(continent_count)]
    for continent, seats in enumerate(seats_on_continent):
        columns = [x for x, c in enumerate(column_continent) if c == continent]
        for index, seat in enumerate(seats):
            low, high = index * ROWS // len(seats), (index + 1) * ROWS // len(seats)
            available = {coords[y, x] for y in range(low, high) for x in columns}
            start = coords[(low + high - 1) // 2, columns[len(columns) // 2]]
            queue, taken = deque([start]), []
            seen = {start}
            while queue and len(taken) < 20:
                rid = queue.popleft()
                taken.append(rid)
                neighbors = sorted((n for n in adjacency[rid] if n in available and n not in seen),
                                   key=lambda n: digest(seed, 'territory', attempt, str(seat), n))
                queue.extend(neighbors)
                seen.update(neighbors)
            if len(taken) < 20:
                raise InfrastructureFault('generator cannot allocate starting territory')
            owner = f'n{seat}'
            countries[owner] = tuple(taken)
            owners.update({r: owner for r in taken})
    terrains = ('plain', 'plain', 'plain', 'hill', 'forest', 'desert', 'mountain')
    regions = {}
    for (y, x), rid in coords.items():
        continent = column_continent[x]
        terrain = 'ocean' if continent is None else terrains[draw(seed, attempt, 'terrain', rid, 0, len(terrains)-1)]
        elevation = 0 if continent is None else {'plain': 10, 'hill': 60, 'forest': 30, 'desert': 20, 'mountain': 180}[terrain]
        regions[rid] = Region(rid, y, x, continent, terrain, elevation, owners.get(rid), adjacency[rid],
                             draw(seed, attempt, 'fertility', rid, 800, 1200),
                             draw(seed, attempt, 'wind', rid, 200, 500),
                             draw(seed, attempt, 'cloud', rid, 200, 600), 600 + 400 * min(y, ROWS-1-y) // 9)
    hubs = {}
    occupied = set()

    def hub_at(hid, rid, owner, core=None):
        region = regions[rid]
        x = (region.column * SIZE // COLS + (region.column + 1) * SIZE // COLS) // 2
        y = (region.row * SIZE // ROWS + (region.row + 1) * SIZE // ROWS) // 2
        hubs[hid] = Hub(hid, rid, x, y, owner, core)
        occupied.add(rid)

    for owner, territory in countries.items():
        for index, rid in enumerate(territory[:5]):
            hub_at(f'{owner}-h{index}', rid, owner, index)
    free = sorted((r.id for r in regions.values() if r.continent is not None and r.id not in owners),
                  key=lambda rid: digest(seed, 'city_state', attempt, rid))
    city_count = draw(seed, attempt, 'city_count', '', 36, 48)
    city_states = tuple(f'city-{i}' for i in range(city_count))
    for city, rid in zip(city_states, free):
        regions[rid] = replace(regions[rid], sovereign=city)
        hub_at(f'{city}-h0', rid, city)
    target_hubs = draw(seed, attempt, 'hub_count', '', 220, 280)
    extras = sorted((r.id for r in regions.values() if r.continent is not None and r.id not in occupied),
                    key=lambda rid: digest(seed, 'extra_hub', attempt, rid))
    for rid in extras[:target_hubs - len(hubs)]:
        hub_at(f'h-{rid}', rid, owners.get(rid))
    routes = {}
    pairs = set()

    def add_route(a, b, mode='road', operational=True):
        pair = tuple(sorted((a, b)))
        if pair in pairs:
            return
        pairs.add(pair)
        rid = f'{mode}:{pair[0]}:{pair[1]}'
        routes[rid] = Route(rid, pair[0], pair[1], mode, _manhattan(hubs[pair[0]], hubs[pair[1]]), operational)

    for owner in countries:
        for i in range(1, 5):
            add_route(f'{owner}-h{i-1}', f'{owner}-h{i}')
    ordered_hubs = sorted(hubs)
    candidates = []
    for i, a in enumerate(ordered_hubs):
        for b in ordered_hubs[i+1:]:
            same_continent = regions[hubs[a].region].continent == regions[hubs[b].region].continent
            distance = abs(hubs[a].x - hubs[b].x) + abs(hubs[a].y - hubs[b].y)
            if same_continent:
                candidates.append((distance, digest(seed, 'route', attempt, a, b), a, b))
    # Prebuilt BASE road atlas. Sea routes are geometry reservations pending
    # actual ports/vessels; they never supply free transport capacity.
    for _, _, a, b in sorted(candidates):
        add_route(a, b)
        if len(routes) >= 1180:
            break
    for continent in range(continent_count - 1):
        left = [h for h in hubs.values() if regions[h.region].continent == continent]
        right = [h for h in hubs.values() if regions[h.region].continent == continent + 1]
        pairs_by_distance = sorted((abs(a.x-b.x)+abs(a.y-b.y), a.id, b.id) for a in left for b in right)
        for _, a, b in pairs_by_distance[:20]:
            add_route(a, b, 'sea', False)
    palette = {'ocean': 0, 'plain': 1, 'hill': 2, 'forest': 3, 'desert': 4, 'mountain': 5}
    indices = {rid: i for i, rid in enumerate(sorted(regions))}
    cells, terrain_bytes = array('H'), bytearray()
    for y in range(SIZE):
        row = min(ROWS-1, ((y+1) * ROWS - 1) // SIZE)
        for x in range(SIZE):
            column = min(COLS-1, ((x+1) * COLS - 1) // SIZE)
            rid = coords[row, column]
            cells.append(indices[rid])
            terrain_bytes.append(palette[regions[rid].terrain])
    world = SpatialWorld(seed, attempt, regions, hubs, routes, countries, city_states, cells, bytes(terrain_bytes))
    world.validate_geometry()
    return world


def generate_world(seed: int, bootstrap: Callable[[SpatialWorld], BootstrapResult]) -> SpatialWorld:
    for attempt in range(256):
        world = generate_candidate(seed, attempt)
        result = bootstrap(world)
        if result.accepted and result.ticks_executed == 150 and not result.invariant_failures:
            world.accepted = True
            return world
    raise InfrastructureFault('INFRA_FAULT_WORLDGEN: 256 bootstrap attempts rejected')

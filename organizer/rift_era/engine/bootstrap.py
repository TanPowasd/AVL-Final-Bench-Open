"""Run the real opening economy and emit evidence with an explicit scope."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import time

from .catalog import Catalog
from .determinism import engine_source_fingerprint, runtime_fingerprint, fingerprint
from .economy import opening_economy
from .initialization import initialize
from .ledger import Account
from .errors import InfrastructureFault, InvariantViolation
from .world import BootstrapResult, generate_candidate
from . import world_atlas


@dataclass(frozen=True)
class OpeningReport:
    profile: str
    seed: int
    ticks: int
    passed_core_checks: bool
    full_rules_accepted: bool
    failures: tuple[str, ...]
    countries: dict[str, dict]
    rules_hash: str
    engine_hash: str
    endowment_hash: str
    final_state_hash: str
    runtime: dict[str, str]
    wall_seconds: float


def run_opening(seed: int, *, debug: bool = True, world=None) -> OpeningReport:
    """150-tick opening on `world` (default: legacy candidate for `seed`)."""
    start = time.monotonic()
    catalog = Catalog()
    if world is None:
        world = generate_candidate(seed)
    engine = opening_economy(catalog, initialize(catalog, world), debug=debug)
    failures = set()
    minimum_food = {p: engine.state.initial.inventory.balance(Account(p, f'{p}-h0', 'available', 'food'))
                    for p in engine.state.initial.known}
    for _ in range(150):
        engine.step()
        state = engine.state
        state.audit()
        for owner in state.initial.known:
            hub = f'{owner}-h0'
            food = state.initial.inventory.balance(Account(owner, hub, 'available', 'food'))
            minimum_food[owner] = min(minimum_food[owner], food)
            if food <= 0:
                failures.add(f'{owner}: food buffer exhausted')
            if state.initial.populations[hub].total_mU <= 0:
                failures.add(f'{owner}: population exhausted')
            if state.natural_water_mU[hub] < 32000:
                failures.add(f'{owner}: local natural water below 32/cycle')
        if state.payments.arrears:
            failures.add('unpaid operating obligations')
    state = engine.state
    countries = {}
    for owner in state.initial.known:
        hub = f'{owner}-h0'
        history = state.history[owner]
        countries[owner] = {'population_mU': state.initial.populations[hub].total_mU,
            'minimum_food_mU': minimum_food[owner],
            'treasury_mU': state.initial.cash.balance(state.native(owner, 'treasury')).native_mU,
            'unpaid_wages_mU': sum(c.unpaid_wages_mU for c in history),
            'generation_mU': sum(c.generation_mU for c in history),
            'household_consumption_mU': {g: sum((c.household_consumed.get(g, Fraction()) for c in history), Fraction())
                                         for g in ('food', 'water', 'electricity')},
            'research_mU': {tid: p.theory_mU for tid, p in state.research[owner].projects.items()}}
    return OpeningReport('opening_economy_v1', seed, 150, not failures, False, tuple(sorted(failures)),
                         countries, catalog.fingerprint, engine_source_fingerprint(), state.endowment_hash,
                         fingerprint(state.snapshot()), runtime_fingerprint(), time.monotonic() - start)


def atlas_bootstrap(world, *, debug: bool = False) -> BootstrapResult:
    """The world_atlas acceptance gate: a real 150-tick opening, no rescue."""
    try:
        report = run_opening(world.seed, debug=debug, world=world)
    except (InfrastructureFault, InvariantViolation) as exc:  # rejects this candidate
        return BootstrapResult(False, 0, (f'{type(exc).__name__}: {exc}',))
    return BootstrapResult(report.passed_core_checks, report.ticks, report.failures)


def accepted_atlas_world(seed: int, *, debug: bool = False):
    """First world_atlas_v1 candidate for `seed` whose real opening passes."""
    return world_atlas.generate_world(seed, lambda world: atlas_bootstrap(world, debug=debug))

"""Integrated development fixtures, explicitly NOT full formal matches.

The supply-chain fixture exercises actual kernel phases, physical inventory,
vehicle return, line credits and command replacement. No prototype imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .catalog import Catalog
from .clock import ClockConfig, Phase
from .errors import InvariantViolation, RuleViolation, InfrastructureFault
from .determinism import canonical, engine_source_fingerprint, runtime_fingerprint
from .kernel import Kernel, System
from .ledger import Inventory, Account, CashLedger
from .logistics import Transport, Vehicle, Corridor, TransitMode
from .production import Line, Recipe, Production, commit_buffers
from .protocol import Commands, PreparedCommand
from .replay import verify_replay


@dataclass
class SupplyChainState:
    inventory: Inventory
    cash: CashLedger
    lines: dict[str, Line]
    transport: Transport
    commands: Commands
    batch_limits: dict[str, int]
    shipments_started: int = 0
    observed: dict = field(default_factory=dict)
    phases_visited: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {'inventory': self.inventory.snapshot(), 'cash': self.cash.snapshot(),
                'lines': self.lines, 'shipments': self.transport.shipments,
                'vehicles': self.transport.vehicles, 'batch_limits': self.batch_limits,
                'watermark': self.commands.watermark, 'plans': self.commands.plans,
                'recent': self.commands.recent, 'shipments_started': self.shipments_started,
                'observed': self.observed, 'phases_visited': self.phases_visited}

    def audit(self) -> None:
        self.inventory.audit()
        self.cash.audit()
        self.transport.audit()
        for line in self.lines.values():
            line.audit()
        if self.inventory.materials:
            virtual = Production.virtual_stock(self.lines.values())
            virtual.update(self.transport.virtual_stock())
            self.inventory.materials.audit_virtual(virtual)
        vehicles = self.transport.vehicles
        for shipment in self.transport.shipments.values():
            if shipment.mode != TransitMode.COMPLETED and vehicles[shipment.vehicle_id].assigned != shipment.id:
                raise InvariantViolation('shipment lost exclusive vehicle reservation')


def supply_chain(seed: int = 1, *, total_ticks: int = 100, decision_ticks: int = 5) -> Kernel:
    catalog = Catalog()
    inventory, cash = Inventory(), CashLedger()
    for good, amount in {'iron_ore': 40000, 'coal': 20000, 'water': 10000, 'fuel': 10000}.items():
        inventory.initialize(Account('A', 'mine', 'available', good), amount)
    inventory.seal()
    inventory.enable_provenance(seed=seed)
    cash.seal()
    line = Line('steel', 'A', 'mine', Recipe.from_catalog(catalog, 'P001'))
    vehicle = Vehicle('truck', 'A', 'mine', 'vehicle', 15000, Fraction(8), Fraction(6))
    corridor = Corridor('road', 'mine', 'port', 16, 30000)
    transport = Transport(inventory, {'road': corridor}, {'truck': vehicle})
    state = SupplyChainState(inventory, cash, {'steel': line}, transport, Commands(), {'steel': 100})
    production = Production(seed, inventory)

    def mark(state, phase):
        state.phases_visited.append(phase.name)
        state.phases_visited = state.phases_visited[-8:]

    def commands(state, ctx):
        mark(state, Phase.COMMANDS)
        inventory.materials.begin_tick(ctx.tick)

        def prepare(order):
            if set(order) - {'id', 'kind', 'op', 'line_id', 'batch_mU', 'replaces_id'} or order.get('kind') != 'industry' or order.get('op') != 'fixture_set_batch':
                raise RuleViolation('ERR_SCHEMA', 'development fixture command only')
            lid, amount = order.get('line_id'), order.get('batch_mU')
            if lid not in state.lines or type(amount) is not int or not 0 <= amount <= 100:
                raise RuleViolation('ERR_SCHEMA')
            return PreparedCommand(lambda: state.batch_limits.__setitem__(lid, amount), persistent=True)

        state.commands.process(list(ctx.orders.get('A', ())), prepare)
        # A trusted standing transport plan, not free market teleportation.
        stock = inventory.balance(Account('A', 'mine', 'available', 'steel'))
        if vehicle.assigned is None and stock >= 3000:
            sid = f'shipment-{state.shipments_started}'
            transport.launch(sid, 'A', 'truck', 'steel', 3000, 'mine', 'port', ('road',), ctx.tick)
            state.shipments_started += 1

    def logistics(state, ctx):
        mark(state, Phase.ENVIRONMENT_LOGISTICS)
        for sid in sorted(transport.shipments):
            transport.advance(ctx.tick, sid, entry_granted=True, free_capacity_mU=300000)

    def power(state, ctx):
        mark(state, Phase.POWER)
        # Explicit fixture boundary: exogenous P3/labor quotas, no fabricated plants.

    def combat(state, ctx):
        mark(state, Phase.COMBAT)  # Fixture contains no military units.

    def markets(state, ctx):
        mark(state, Phase.MARKETS)  # Fixture contains no market orders.

    def produce(state, ctx):
        mark(state, Phase.PRODUCTION)
        occupied = sum(q for a, q in inventory.balances().items() if a.location == 'mine')
        production.advance(ctx.tick, list(state.lines.values()), state.batch_limits,
                           free_capacity_mU={'mine': max(0, 300000 - occupied)})

    def settle(state, ctx):
        mark(state, Phase.SETTLEMENT)
        commit_buffers(inventory)

    def observe(state, ctx):
        mark(state, Phase.OBSERVATION)
        if ctx.clock.is_window_end(ctx.tick):
            state.observed = {'window': ctx.window + 1, 'steel_at_port_mU': inventory.balance(Account('A', 'port', 'available', 'steel'))}

    functions = (commands, logistics, power, combat, markets, produce, settle, observe)
    systems = tuple(System(phase.name.lower(), phase, function) for phase, function in zip(Phase, functions))
    return Kernel(seed, state, systems, clock=ClockConfig(total_ticks, decision_ticks, 10))


def replay_supply_chain(path: Path) -> str:
    """Re-execute all ticks, rather than merely checking stored hash integrity."""
    records = iter(verify_replay(path))
    header = next(records)['payload']
    if header.get('scenario') != 'supply_chain_v1' or header.get('official') is not False:
        raise InfrastructureFault('unsupported replay type')
    if (header.get('rules_hash') != Catalog().fingerprint or
            header.get('engine_hash') != engine_source_fingerprint() or
            header.get('runtime') != runtime_fingerprint()):
        raise InfrastructureFault('replay version/runtime fingerprint mismatch')
    clock = ClockConfig(**header['clock'])
    engine = supply_chain(header['seed'], total_ticks=clock.total_ticks, decision_ticks=clock.decision_ticks)
    final_hash = ''
    for record in records:
        if record['kind'] == 'tick':
            result = engine.step()
            if canonical(result) != canonical(record['payload']):
                raise InfrastructureFault(f'replay diverged at tick {result.tick}')
            final_hash = result.state_hash
        elif record['kind'] == 'end':
            if (engine.tick != clock.total_ticks or record['payload'] != {'ticks': engine.tick, 'state_hash': final_hash}):
                raise InfrastructureFault('replay terminal state mismatch')
        else:
            raise InfrastructureFault('unexpected replay record')
    return final_hash

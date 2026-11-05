"""Physical transit/return state machine with exclusive vehicle reservations.

The admission layer supplies entry grants after ACL/capacity arbitration. Once
entered, a vehicle never consumes the edge's admission quota a second time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from collections.abc import Mapping

from .errors import RuleViolation, InvariantViolation
from .ledger import Account, Inventory, Transfer
from .numeric import Q, ceil, floor
from .provenance import Leg, Use


class TransitMode(str, Enum):
    WAIT_ENTRY = 'WAIT_ENTRY'
    IN_TRANSIT = 'IN_TRANSIT'
    WAIT_UNLOAD = 'WAIT_UNLOAD'
    RETURNING = 'RETURNING'
    STRANDED = 'STRANDED'
    COMPLETED = 'COMPLETED'


@dataclass
class Corridor:
    id: str
    a: str
    b: str
    length_cells: int
    capacity_mU_cycle: int
    health_mU: int = 1000
    blocked: dict[str, Fraction] = field(default_factory=dict)
    # 06 logistics/corridor_policy: per-target access tier (0 closed,
    # 1 commerce, 2 military supplies, 3 troop transit) and per-target toll in
    # basis points of the 06 cargo base.  A nation absent from access_tier has
    # no explicit row: domestic edges stay open to it and foreign-controlled
    # endpoints still require an explicit right (see CargoOperations.permitted).
    # restricted[target] is the highest tier the operator granted before a
    # tightening; while the current tier is below it, 07 hostile-policy events
    # accrue every tick.
    mode: str = 'road'
    access_tier: dict[str, int] = field(default_factory=dict)
    toll_bp: dict[str, int] = field(default_factory=dict)
    restricted: dict[str, int] = field(default_factory=dict)

    def other(self, hub: str) -> str:
        if hub == self.a:
            return self.b
        if hub == self.b:
            return self.a
        raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'disconnected path')

    def capacity_tick(self, owner: str) -> int:
        return floor(Q(self.capacity_mU_cycle, 10) * Q(self.health_mU, 1000) * (1 - self.blocked.get(owner, Q(0))))

    def allowed(self, owner: str) -> bool:
        return self.access_tier.get(owner) != 0 and self.blocked.get(owner, Q(0)) < 1

    def tier(self, owner: str) -> int:
        return self.access_tier.get(owner, 0)


@dataclass
class Vehicle:
    id: str
    owner: str
    hub: str | None
    good: str
    capacity_mU: int
    speed_cells_tick: Fraction
    fuel_mU_cell: Fraction
    health_mU: int = 1000
    assigned: str | None = None
    fuel_credit_mU: Fraction = Q(0)
    physical_account: Account | None = None
    propulsion: str = 'fuel'


@dataclass
class Shipment:
    id: str
    owner: str
    vehicle_id: str
    good: str
    quantity_mU: int
    origin: str
    destination: str
    path: tuple[str, ...]
    departure_tick: int
    fuel_reserved_mU: int
    current_hub: str
    index: int = 0
    progress_cells: Fraction = Q(0)
    mode: TransitMode = TransitMode.WAIT_ENTRY
    returning: bool = False
    fuel_exact_used: Fraction = Q(0)
    fuel_paid_mU: int = 0
    last_tick: int = -1
    entered_tick: int = -1
    initial_fuel_credit_mU: Fraction = Q(0)
    cargo_quantum_mU: int = 1
    decay_remainder: Fraction = Q(0)
    auxiliary_accounts: tuple[Account, ...] = ()


class Transport:
    def __init__(self, inventory: Inventory, corridors: Mapping[str, Corridor],
                 vehicles: Mapping[str, Vehicle]):
        self.inventory = inventory
        self.corridors = corridors
        self.vehicles = vehicles
        self.shipments: dict[str, Shipment] = {}

    def launch(self, shipment_id: str, owner: str, vehicle_id: str, good: str, quantity_mU: int,
               origin: str, destination: str, path: tuple[str, ...], tick: int,
               *, cargo_weight: Fraction = Q(1), indivisible: bool = False,
               cargo_source: Account | None = None, fuel_source: Account | None = None,
               prepare_only: bool = False, fuel_reserved_mU: int | None = None):
        if shipment_id in self.shipments or not path or len(path) > 128 or type(quantity_mU) is not int or quantity_mU <= 0:
            raise RuleViolation('ERR_SCHEMA')
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET', 'unknown vehicle')
        if vehicle.owner != owner or vehicle.hub != origin or vehicle.assigned is not None or vehicle.health_mU == 0:
            raise RuleViolation('ERR_NO_PERMISSION', 'vehicle unavailable')
        if quantity_mU * cargo_weight > vehicle.capacity_mU or (indivisible and quantity_mU % 1000):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        node, length = origin, 0
        for eid in path:
            edge = self.corridors.get(eid)
            if edge is None or edge.health_mU == 0 or edge.blocked.get(owner, Q(0)) >= 1:
                raise RuleViolation('ERR_CORRIDOR_BLOCKED')
            node = edge.other(node)
            length += edge.length_cells
        if node != destination:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        fuel = max(0, ceil(length * vehicle.fuel_mU_cell * Q(3, 2) - vehicle.fuel_credit_mU))
        if fuel_reserved_mU is not None:
            if type(fuel_reserved_mU) is not int or fuel_reserved_mU < 0:
                raise InvariantViolation('invalid prepaid route fuel')
            fuel = fuel_reserved_mU
        cargo_source = cargo_source or Account(owner, origin, 'available', good)
        fuel_source = fuel_source or Account(owner, origin, 'available', 'fuel')
        if (cargo_source.owner != owner or cargo_source.asset != good or fuel_source.owner != owner
                or fuel_source.asset != 'fuel' or cargo_source.location != origin or fuel_source.location != origin):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'loading requires co-located stock')
        transfers = [Transfer(cargo_source,
                              Account(owner, shipment_id, 'in_transit', good), quantity_mU, destination_hub=origin)]
        if fuel:
            transfers.append(Transfer(fuel_source,
                                      Account(owner, shipment_id, 'fuel_tank', 'fuel'), fuel, destination_hub=origin))
        plan = self.inventory.prepare_transfers(transfers)
        shipment = Shipment(shipment_id, owner, vehicle_id, good, quantity_mU, origin,
                            destination, path, tick, fuel, origin, initial_fuel_credit_mU=vehicle.fuel_credit_mU,
                            cargo_quantum_mU=1000 if indivisible else 1)
        def commit():
            plan.commit()
            self.shipments[shipment_id] = shipment
            vehicle.assigned = shipment_id
            return shipment
        return commit if prepare_only else commit()

    def _spend_fuel(self, shipment: Shipment, distance: Fraction, multiplier: Fraction, fuel_rate=None) -> bool:
        vehicle = self.vehicles[shipment.vehicle_id]
        exact = shipment.fuel_exact_used + distance * (vehicle.fuel_mU_cell if fuel_rate is None else fuel_rate) * multiplier
        need = max(0, ceil(exact - shipment.initial_fuel_credit_mU) - shipment.fuel_paid_mU)
        tank = Account(shipment.owner, shipment.id, 'fuel_tank', 'fuel')
        if self.inventory.balance(tank) < need:
            shipment.mode = TransitMode.STRANDED
            return False
        material_plan = None
        if self.inventory.materials:
            draft = self.inventory.materials.draft()
            credit = self._fuel_credit_account(vehicle)
            draft.add(credit, draft.take(tank, Q(need)), virtual=True)
            draft.consume(credit, exact - shipment.fuel_exact_used,
                          use=Use.CIVIL_LOGISTICS, reason='civilian_transport_fuel')
            material_plan = draft.finish()
        self.inventory.transform({tank: need}, {}, reason='civilian_transport_fuel',
                                 use=Use.CIVIL_LOGISTICS, material_plan=material_plan)
        shipment.fuel_exact_used, shipment.fuel_paid_mU = exact, shipment.fuel_paid_mU + need
        vehicle.fuel_credit_mU = shipment.initial_fuel_credit_mU + shipment.fuel_paid_mU - exact
        return True

    @staticmethod
    def _fuel_credit_account(vehicle: Vehicle) -> Account:
        return Account(vehicle.owner, vehicle.id, 'vehicle_fuel_credit', 'fuel')

    def advance(self, tick: int, shipment_id: str, *, entry_granted: bool,
                free_capacity_mU: int, cargo_weight: Fraction = Q(1),
                motion_fraction: Fraction = Q(1), defer_unload: bool = False,
                speed_cells_tick: Fraction | None = None, fuel_mU_cell: Fraction | None = None) -> None:
        if not 0 <= motion_fraction <= 1 or cargo_weight <= 0 or free_capacity_mU < 0:
            raise InvariantViolation('invalid transport allocation')
        shipment = self.shipments[shipment_id]
        vehicle = self.vehicles[shipment.vehicle_id]
        if tick <= shipment.last_tick:
            raise InvariantViolation('shipment advanced twice or backdated')
        shipment.last_tick = tick
        if shipment.mode in (TransitMode.COMPLETED, TransitMode.STRANDED) or tick <= shipment.departure_tick:
            return
        if shipment.mode == TransitMode.WAIT_UNLOAD:
            if not defer_unload:
                self.unload(shipment_id, free_capacity_mU, cargo_weight=cargo_weight)
            return
        edge = self.corridors[shipment.path[shipment.index]]
        if shipment.mode == TransitMode.WAIT_ENTRY:
            if not entry_granted or edge.health_mU == 0 or edge.blocked.get(shipment.owner, Q(0)) >= 1:
                return
            shipment.mode = TransitMode.RETURNING if shipment.returning else TransitMode.IN_TRANSIT
            shipment.entered_tick = tick
            vehicle.hub = None
        # A destroyed edge cannot be crossed. Retreat physically towards entry.
        retreat = edge.health_mU == 0
        remaining = shipment.progress_cells if retreat else edge.length_cells - shipment.progress_cells
        distance = min((vehicle.speed_cells_tick if speed_cells_tick is None else speed_cells_tick) * motion_fraction, remaining)
        if not self._spend_fuel(shipment, distance, Q(1, 2) if shipment.returning else Q(1), fuel_mU_cell):
            return
        shipment.progress_cells += -distance if retreat else distance
        if retreat:
            if shipment.progress_cells == 0:
                shipment.mode = TransitMode.WAIT_ENTRY
                vehicle.hub = shipment.current_hub
            return
        if shipment.progress_cells < edge.length_cells:
            return
        destination = edge.other(shipment.current_hub)
        self.inventory.record_transport((Account(shipment.owner, shipment.id, 'in_transit', shipment.good),
                                         Account(shipment.owner, shipment.id, 'fuel_tank', 'fuel'),
                                         self._fuel_credit_account(vehicle)) +
                                        ((vehicle.physical_account,) if vehicle.physical_account else ()) + shipment.auxiliary_accounts,
                                        Leg(shipment.current_hub, destination, vehicle.owner, edge.length_cells))
        shipment.current_hub = destination
        shipment.progress_cells = Q(0)
        vehicle.hub = shipment.current_hub
        if shipment.returning:
            shipment.index -= 1
            if shipment.index < 0:
                self._complete(shipment)
            else:
                shipment.mode = TransitMode.WAIT_ENTRY
        else:
            shipment.index += 1
            if shipment.index == len(shipment.path):
                shipment.mode = TransitMode.WAIT_UNLOAD
                if not defer_unload:
                    self.unload(shipment_id, free_capacity_mU, cargo_weight=cargo_weight)
            else:
                shipment.mode = TransitMode.WAIT_ENTRY

    def unload(self, shipment_id: str, free_capacity_mU: int, *, cargo_weight: Fraction = Q(1),
               bucket: str = 'available', destination_account: Account | None = None) -> None:
        shipment = self.shipments[shipment_id]
        if shipment.mode != TransitMode.WAIT_UNLOAD:
            raise InvariantViolation('unloading vehicle not at destination')
        if cargo_weight <= 0 or free_capacity_mU < 0 or bucket not in ('available', 'outdoor'):
            raise InvariantViolation('invalid unloading grant')
        amount = min(shipment.quantity_mU, floor(Q(free_capacity_mU) / cargo_weight))
        amount -= amount % shipment.cargo_quantum_mU
        target = destination_account or Account(shipment.owner, shipment.destination, bucket, shipment.good)
        if target.owner != shipment.owner or target.location != shipment.destination or target.asset != shipment.good:
            raise InvariantViolation('unload custody account differs from physical destination')
        self.inventory.transfer(Account(shipment.owner, shipment.id, 'in_transit', shipment.good),
                                target, amount)
        shipment.quantity_mU -= amount
        if shipment.quantity_mU:
            return
        shipment.returning = True
        shipment.index = len(shipment.path) - 1
        shipment.mode = TransitMode.WAIT_ENTRY

    def _complete(self, shipment: Shipment) -> None:
        vehicle = self.vehicles[shipment.vehicle_id]
        if shipment.current_hub != shipment.origin:
            raise InvariantViolation('returning vehicle finished away from origin')
        tank = Account(shipment.owner, shipment.id, 'fuel_tank', 'fuel')
        self.inventory.transfer(tank, Account(shipment.owner, shipment.origin, 'available', 'fuel'), self.inventory.balance(tank))
        vehicle.hub, vehicle.assigned = shipment.origin, None
        shipment.mode = TransitMode.COMPLETED

    def decay(self, shipment_id: str) -> int:
        """One cycle of loaded waiting loss; never a consumption receipt."""
        shipment = self.shipments[shipment_id]
        if shipment.good not in ('food', 'grain') or shipment.mode not in (
                TransitMode.WAIT_ENTRY, TransitMode.WAIT_UNLOAD, TransitMode.STRANDED):
            return 0
        exact = shipment.decay_remainder + Q(shipment.quantity_mU, 20)
        lost = min(shipment.quantity_mU, floor(exact))
        shipment.decay_remainder = exact - lost if shipment.quantity_mU > lost else Q(0)
        self.inventory.transform({Account(shipment.owner, shipment.id, 'in_transit', shipment.good): lost},
                                 {}, reason='vehicle_wait_decay', use=Use.DESTRUCTION)
        shipment.quantity_mU -= lost
        return lost

    def refuel(self, shipment_id: str, delivered_fuel_mU: int, from_account: Account) -> None:
        shipment = self.shipments[shipment_id]
        # Must be a real co-located aid delivery, not a remote capital account.
        if from_account.location != shipment.id:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        self.inventory.transfer_many((Transfer(from_account, Account(shipment.owner, shipment.id, 'fuel_tank', 'fuel'),
                                               delivered_fuel_mU, destination_hub=shipment.current_hub),))
        if shipment.mode == TransitMode.STRANDED:
            shipment.mode = (TransitMode.RETURNING if shipment.returning else TransitMode.IN_TRANSIT)

    def audit(self) -> None:
        occupied = set()
        for shipment in self.shipments.values():
            cargo = Account(shipment.owner, shipment.id, 'in_transit', shipment.good)
            if self.inventory.balance(cargo) != shipment.quantity_mU:
                raise InvariantViolation('shipment cargo differs from actual inventory')
            if shipment.mode == TransitMode.COMPLETED:
                continue
            vehicle = self.vehicles[shipment.vehicle_id]
            if vehicle.id in occupied or vehicle.assigned != shipment.id:
                raise InvariantViolation('vehicle assignment duplicated or lost')
            occupied.add(vehicle.id)
            if vehicle.fuel_credit_mU != shipment.initial_fuel_credit_mU + shipment.fuel_paid_mU - shipment.fuel_exact_used:
                raise InvariantViolation('vehicle fuel credit differs from actual use')
        for vehicle in self.vehicles.values():
            if not 0 <= vehicle.fuel_credit_mU < 1:
                raise InvariantViolation('invalid vehicle fuel credit')
            if vehicle.assigned is not None and vehicle.id not in occupied:
                raise InvariantViolation('vehicle assigned to absent shipment')
            if self.inventory.materials and self.inventory.materials.quantity(self._fuel_credit_account(vehicle)) != vehicle.fuel_credit_mU:
                raise InvariantViolation('fuel provenance differs from prepaid vehicle fuel')

    def virtual_stock(self) -> dict[Account, Fraction]:
        return {self._fuel_credit_account(vehicle): vehicle.fuel_credit_mU
                for vehicle in self.vehicles.values() if vehicle.fuel_credit_mU}

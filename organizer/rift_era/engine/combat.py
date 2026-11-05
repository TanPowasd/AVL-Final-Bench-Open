"""P4 snapshot fire allocation and simultaneous casualties (03/military.json)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction

from .catalog import Catalog
from .diplomacy import Diplomacy, Relation
from .errors import InvariantViolation
from .numeric import Q, clip, units
from .scoring import action_loss


@dataclass
class Unit:
    id: str
    owner: str
    unit_type: str
    region: str
    hp_mU: int
    crew_mU: int
    morale: Fraction = Q(1)
    fatigue: Fraction = Q(0)
    posture: str = "preserve"
    precision: bool = False
    tech_multiplier: Fraction = Q(1)
    book_nmU: dict[str, Fraction] = field(default_factory=dict)
    book_rounding_steps: int = 0
    armor_bonus: int = 0
    speed_factor: Fraction = Q(1)
    fuel_factor: Fraction = Q(1)
    sensor_bonus: Fraction = Q(0)
    asw_factor: Fraction = Q(1)
    electronic_warfare: bool = False


@dataclass(frozen=True)
class TargetContact:
    target: str
    quality: Fraction
    hops: int
    expected_region: str | None = None
    snapshot: Unit | None = None


@dataclass(frozen=True)
class Shot:
    source: str
    target: str
    raw_damage_mU: Fraction


@dataclass(frozen=True)
class CombatResult:
    shots: tuple[Shot, ...]
    damage_mU: Mapping[str, int]
    casualties_mU: Mapping[str, int]
    action_losses: Mapping[str, Fraction]
    consumed_mU: Mapping[str, Mapping[str, int]]
    combat_regions: frozenset[str]


LAND_ATTACK = {'plain': Q(1), 'hill': Q(9, 10), 'mountain': Q(7, 10),
               'forest': Q(4, 5), 'desert': Q(105, 100), 'port': Q(85, 100)}
AIR_ATTACK = {'plain': Q(11, 10), 'hill': Q(95, 100), 'mountain': Q(4, 5),
              'forest': Q(3, 4), 'desert': Q(115, 100), 'port': Q(9, 10)}
ARMOR = {'plain': 0, 'hill': 12, 'mountain': 30, 'forest': 20, 'desert': -5, 'port': 25}
POSTURE = {'preserve': Q(85, 100), 'assault': Q(1), 'bombard': Q(115, 100)}


def precision_multiplier(quality: Fraction) -> Fraction:
    if quality < Q(35, 100):
        return Q(1)
    return min(Q(7, 4), 1 + 3 * (quality - Q(35, 100)))


def resolve(catalog: Catalog, diplomacy: Diplomacy, units_by_id: Mapping[str, Unit],
            contacts: Mapping[str, tuple[TargetContact, ...]],
            allocated_supplies: Mapping[str, Mapping[str, int]],
            terrain: Mapping[str, str], *, moving: frozenset[str] = frozenset(),
            terrain_by_unit: Mapping[str, str] | None = None,
            inactive: frozenset[str] = frozenset(),
            target_only: frozenset[str] = frozenset()) -> CombatResult:
    """Pure calculation; no shot sees casualties from an earlier iteration.

    allocated_supplies must be reserved from each formation's real onboard pool
    before this function. Visibility comes ONLY from the observation subsystem.
    """
    if any(uid != u.id for uid, u in units_by_id.items()):
        raise InvariantViolation("unit ID mismatch")
    shots, damage = [], defaultdict(Fraction)
    losses, consumed, regions = defaultdict(Fraction), {}, set()
    channels = catalog.tables['military']['domain_channel']
    for uid, unit in sorted(units_by_id.items()):
        spec = catalog.units[unit.unit_type]
        if uid in inactive or uid in target_only or unit.hp_mU <= 0 or unit.crew_mU <= 0:
            continue
        targets = {}
        for contact in contacts.get(uid, ()):
            enemy = contact.snapshot if contact.snapshot is not None else units_by_id.get(contact.target)
            if enemy is None or enemy.hp_mU <= 0 or enemy.owner == unit.owner:
                continue
            if contact.quality < Q(1, 5) or contact.hops > spec['range_hops'] or contact.hops < 0:
                continue
            if diplomacy.relation(unit.owner, enemy.owner) != Relation.WAR:
                continue
            channel = channels[catalog.units[enemy.unit_type]['domain']]
            if spec['firepower'][channel] <= 0:
                continue
            if enemy.id in targets:
                raise InvariantViolation("duplicate target contact")
            targets[enemy.id] = (enemy, contact, channel)
        if not targets and uid not in moving:
            continue
        fired = False
        supplies = allocated_supplies.get(uid, {})
        used, ratios = {}, []
        for good, rate in spec['combat_tick'].items():
            need = units(rate * unit.fuel_factor if good == 'fuel' else rate)
            # Moving without shooting uses fuel, not ammunition/battery shots.
            if not targets and good == 'ammunition':
                need = 0
            if unit.precision and good == 'ammunition':
                good = 'precision_ammunition'
            actual = min(need, supplies.get(good, 0))
            if actual < 0:
                raise InvariantViolation("negative combat allocation")
            if actual:
                used[good] = actual
            if need and good not in ('fuel',):
                ratios.append(Q(actual, need))
        if unit.unit_type == 'energy_weapon' and targets:
            power = min(500, supplies.get('electricity', 0))
            used['electricity'] = power
            ratios.append(Q(power, 500))
        supply = min(ratios, default=Q(1))
        if targets:
            domains = {channel for _, _, channel in targets.values()}
            for enemy, contact, channel in targets.values():
                enemy_spec = catalog.units[enemy.unit_type]
                group_hp = sum(e.hp_mU for e, _, c in targets.values() if c == channel)
                ratio = supply
                if spec['can_melee'] and contact.hops == 0 and channel == 'ground':
                    ratio = Q(15, 100) + Q(85, 100) * supply
                t = (terrain_by_unit or {}).get(enemy.id, terrain[enemy.region])
                attack = (LAND_ATTACK.get(t, Q(1)) if spec['domain'] == 'Land' else
                          AIR_ATTACK[t] if spec['domain'] == 'Air' and enemy_spec['domain'] == 'Land' else Q(1))
                morale = Q(1) if unit.unit_type == 'swarm' else Q(1, 2) + unit.morale / 2
                firepower = (spec['firepower'][channel] / Q(len(domains)) * Q(enemy.hp_mU, group_hp) *
                    Q(unit.hp_mU, units(spec['hp'])) * min(Q(1), Q(unit.crew_mU, units(spec['crew']))) *
                    attack * ratio * morale * (1 - Q(2, 5) * unit.fatigue) * POSTURE[unit.posture] * unit.tech_multiplier)
                if unit.precision and used.get('precision_ammunition',0):
                    firepower *= precision_multiplier(contact.quality)
                    if enemy.electronic_warfare and enemy.id not in inactive:firepower*=Q(65,100)
                if unit.unit_type=='swarm' and enemy.electronic_warfare and enemy.id not in inactive:firepower*=Q(2,5)
                if channel=='asw':firepower*=unit.asw_factor
                armor = enemy_spec['armor'] + enemy.armor_bonus + (ARMOR[t] if enemy_spec['domain'] == 'Land' else 0)
                raw = firepower * 100 / (100 + armor) * 1000
                if raw > 0:
                    fired = True
                    shots.append(Shot(uid, enemy.id, raw))
                    physical=units_by_id.get(enemy.id)
                    if physical is not None and (contact.expected_region is None or physical.region==contact.expected_region):
                        damage[enemy.id] += raw
                    regions.update((unit.region, contact.expected_region or enemy.region))
        consumed[uid] = used
        if any(used.values()) or uid in moving or fired:
            losses[unit.owner] += action_loss(used, catalog.weights)
    applied = {uid: (0 if uid in target_only else min(u.hp_mU, round(damage[uid]))) for uid, u in units_by_id.items()}
    casualties = {uid: (u.crew_mU * applied[uid] // u.hp_mU if u.hp_mU else 0) for uid, u in units_by_id.items()}
    return CombatResult(tuple(shots), applied, casualties, dict(losses), consumed, frozenset(regions))


def apply_result(units_by_id: Mapping[str, Unit], result: CombatResult,
                 *, food_water_short: frozenset[str] = frozenset(), moving: frozenset[str] = frozenset(),
                 forced_march: frozenset[str] = frozenset()) -> None:
    participants = {shot.source for shot in result.shots} | {shot.target for shot in result.shots}
    for uid, unit in units_by_id.items():
        old_hp = unit.hp_mU
        unit.hp_mU -= result.damage_mU[uid]
        unit.crew_mU -= result.casualties_mU[uid]
        loss = Q(result.damage_mU[uid], old_hp) if old_hp else Q(0)
        unit.morale = clip(unit.morale - loss / 2 - Q(2, 100) * int(uid in food_water_short) +
                           (Q(1, 100) if uid not in participants and loss == 0 and uid not in food_water_short else 0), Q(0), Q(1))
        # Normal movement is not stationary rest; hungry garrisons cannot
        # recover fatigue merely because nobody shot this tick.
        change = (Q(25, 1000) if uid in participants or uid in forced_march else
                  -Q(5, 100) if uid not in moving and uid not in food_water_short else Q(0))
        unit.fatigue = clip(unit.fatigue + change, Q(0), Q(1))


def blockade_fraction(attack: Fraction, escort: Fraction) -> Fraction:
    if attack <= 0:
        return Q(0)
    return Q(1) if attack >= 2 * escort + 50 else attack / (attack + escort + 50)

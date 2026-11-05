"""Local population/gov rules. Migrants and military never breed in two places."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction

from .numeric import Q, clip, floor, proportional
from .errors import RuleViolation, InvariantViolation

SKILLS = ('general_worker', 'skilled_worker', 'engineer')


@dataclass
class Population:
    hub: str
    skills_mU: dict[str, int]
    support_mU: int = 720
    red_streak: int = 0
    training_mU: dict[str, int] = field(default_factory=dict)

    @property
    def total_mU(self) -> int:
        return sum(self.skills_mU.values()) + sum(self.training_mU.values())

    def reserve_people(self, requested: Mapping[str, int]) -> None:
        if set(requested) - set(SKILLS) or any(type(q) is not int or q < 0 for q in requested.values()):
            raise RuleViolation('ERR_SCHEMA')
        if any(q > self.skills_mU.get(s, 0) for s, q in requested.items()):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        for skill, amount in requested.items():
            self.skills_mU[skill] -= amount

    def receive(self, people: Mapping[str, int]) -> None:
        if set(people) - set(SKILLS) or any(type(q) is not int or q < 0 for q in people.values()):
            raise InvariantViolation('invalid population arrival')
        for skill, amount in people.items():
            self.skills_mU[skill] = self.skills_mU.get(skill, 0) + amount

    @property
    def warning(self) -> str:
        if self.support_mU >= 450:
            return 'GOV_STABLE'
        if self.support_mU >= 300:
            return 'GOV_WARN_DISCONTENT'
        if self.support_mU < 150 and self.red_streak >= 6:
            return 'GOV_CRISIS_PARALYSIS'
        return 'GOV_ALERT_STRIKE'


def housing_factor(population_mU: int, capacity_mU: int) -> Fraction:
    if population_mU == 0:
        return Q(1)
    if capacity_mU == 0:
        return Q(0)
    return clip(1 - 4 * Q(max(0, population_mU - capacity_mU), capacity_mU), Q(0), Q(1))


@dataclass(frozen=True)
class Demography:
    food: Fraction
    housing_mU: int
    development: Fraction
    effective_medical: Fraction
    gene_satisfaction: Fraction
    pollution: Fraction
    inflation_annual: Fraction


def grow(seed: int, cycle: int, population: Population, context: Demography) -> tuple[int, int]:
    initial = population.total_mU
    food = clip((context.food - Q(4, 5)) * 5, Q(0), Q(1))
    housing = housing_factor(initial, context.housing_mU)
    inflation = max(Q(7, 10), 1 - Q(2, 100) * max(Q(0), context.inflation_annual - 5))
    unrest = Q(4, 5) if population.support_mU < 300 else Q(1)
    birth_rate = Q(3, 1000) * food * housing * (1 - Q(3, 5) * context.development) * inflation * unrest
    famine = Q(3, 1000) * max(Q(0), Q(4, 5) - context.food) / Q(4, 5)
    death_rate = Q(1, 1000) * (1 - context.effective_medical / 2) * (1 - Q(3, 10) * context.gene_satisfaction) * (1 + context.pollution / 2) + famine
    births, deaths = max(0, floor(initial * birth_rate)), min(initial, max(0, floor(initial * death_rate)))
    residents = dict(population.skills_mU)
    residents.update({f'training:{job}': q for job, q in population.training_mU.items()})
    removed = proportional(deaths, residents, key=(seed, 'demography', cycle, population.hub))
    for skill, amount in removed.items():
        if skill.startswith('training:'):
            population.training_mU[skill[9:]] -= amount
        else:
            population.skills_mU[skill] -= amount
    population.skills_mU['general_worker'] = population.skills_mU.get('general_worker', 0) + births
    return births, deaths


def support_delta(*, food: Fraction, consumer_goods: Fraction, medicine: Fraction,
                   employment: Fraction, tax: Fraction, fatigue: Fraction,
                   pollution: Fraction, autonomy: Fraction, social_security: Fraction | None,
                   occupied: bool, events_mU: int = 0) -> int:
    food, consumer_goods, medicine = (clip(v, Q(0), Q(6, 5)) for v in (food, consumer_goods, medicine))
    value = (20 * (food - Q(85, 100)) + 12 * (consumer_goods - Q(65, 100)) +
             8 * (medicine - Q(1, 2)) + 8 * (employment - Q(3, 4)) -
             30 * (tax - Q(1, 5)) - 16 * fatigue - 10 * pollution + 12 * autonomy -
             12 * int(occupied) + events_mU)
    if social_security is not None:
        value += 4 * social_security - 8 * (1 - social_security)
    return round(clip(value, Q(-45), Q(25)))


def update_support(population: Population, delta_mU: int) -> None:
    population.support_mU = clip(population.support_mU + delta_mU, 0, 1000)
    population.red_streak = population.red_streak + 1 if population.support_mU < 150 else 0


@dataclass
class Passenger:
    id: str
    origin_hub: str
    destination_hub: str
    skills_mU: dict[str, int]
    depart_tick: int
    arrival_tick: int
    arrived: bool = False

    def __post_init__(self):
        if self.arrival_tick <= self.depart_tick:
            raise ValueError('migration requires physical transit time')

    def arrive(self, tick: int, destination: Population) -> bool:
        if self.arrived or tick < self.arrival_tick:
            return False
        if destination.hub != self.destination_hub:
            raise InvariantViolation('passenger delivered to wrong hub')
        destination.receive(self.skills_mU)
        self.arrived = True
        return True

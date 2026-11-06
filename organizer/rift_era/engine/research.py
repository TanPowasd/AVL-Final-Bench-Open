"""Project-bound research, global multi-hub diminishing returns and validation."""

from __future__ import annotations

from dataclasses import dataclass, replace, field
from fractions import Fraction
from decimal import Decimal, localcontext
from collections.abc import Mapping

from .catalog import Catalog
from .errors import RuleViolation
from .numeric import Q, units
from .production import Line

SECURITY_MULTIPLIER = {'open': Q(11, 10), 'standard': Q(1), 'classified': Q(9, 10)}


@dataclass
class ValidationJob:
    id: str
    lab_id: str
    tech_id: str
    line: Line
    required_ticks: int
    active: bool = True
    interrupted: bool = False


def rational_power(base: Fraction, exponent: Fraction) -> Fraction:
    if base < 0:
        raise ValueError("negative power base")
    if base == 0:
        return Q(0)
    with localcontext() as ctx:
        ctx.prec = 50
        b = Decimal(base.numerator) / Decimal(base.denominator)
        e = Decimal(exponent.numerator) / Decimal(exponent.denominator)
        return Q(ctx.power(b, e))


@dataclass(frozen=True)
class LabOutput:
    line_id: str
    hub_id: str
    research_mU: int
    autonomous: bool = False
    connected: bool = False
    network_id: str | None = None


def combine_labs(outputs: tuple[LabOutput, ...], *, organized: bool) -> int:
    regular = [o for o in outputs if not o.autonomous and o.research_mU > 0]
    independent = sum(o.research_mU for o in outputs if o.autonomous)
    n = len(regular)
    if not n:
        return independent
    alpha = (Q(92, 100) if organized and all(o.connected for o in regular)
             and len({o.network_id for o in regular}) == 1 and len({o.hub_id for o in regular}) <= 4 else Q(3, 4))
    return round(rational_power(Q(n), alpha - 1) * sum(o.research_mU for o in regular)) + independent


@dataclass
class Project:
    tech_id: str
    target_mU: int
    theory_mU: int = 0
    theory_complete: bool = False
    validation_progress_mU: int = 0  # mU-ticks
    verified: bool = False
    cost_modifiers: dict[str, bool] = field(default_factory=dict)

    def advance_theory(self, amount_mU: int) -> None:
        if amount_mU < 0 or self.theory_complete:
            raise ValueError("inactive or negative research")
        self.theory_mU = min(self.target_mU, self.theory_mU + amount_mU)
        self.theory_complete = self.theory_mU == self.target_mU
        # Deliberately no return value containing transferable overflow.

    def interrupt(self, standard_records: bool) -> None:
        if not self.theory_complete and not self.verified:
            self.theory_mU = self.theory_mU * (95 if standard_records else 75) // 100

    def validate_tick(self, required_ticks: int, *, fully_supplied: bool,
                      standard_records: bool) -> bool:
        if not self.theory_complete or self.verified:
            raise ValueError("project cannot validate")
        if not fully_supplied:
            self.validation_progress_mU = self.validation_progress_mU * (90 if standard_records else 50) // 100
            return False
        self.validation_progress_mU = min(required_ticks * 1000, self.validation_progress_mU + 1000)
        self.verified = self.validation_progress_mU == required_ticks * 1000
        return self.verified


class Research:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.known: set[str] = set()
        self.projects: dict[str, Project] = {}
        self.focus: dict[str, str] = {}
        self.security: dict[str, str] = {}
        self.licenses: dict[str, bool] = {}  # source blockade, not an additional technology unlock
        self.backups: dict[str, str] = {}  # R08: primary research Hub -> backup Hub

    def set_focus(self, line_id: str, tech_id: str | None, *, licensed: bool = False,
                  open_spillover: bool = False, stolen: bool = False, source_blockade: bool = False) -> None:
        if tech_id is not None:
            if tech_id in self.known or not self.catalog.researchable(tech_id, self.known):
                raise RuleViolation("ERR_TECH_LOCKED")
            if tech_id not in self.projects:
                modifiers = {'licensed': licensed or tech_id in self.licenses,
                             'open_spillover': open_spillover, 'stolen': stolen,
                             'source_blockade': source_blockade or self.licenses.get(tech_id, False)}
                self.projects[tech_id] = Project(tech_id, self.target_cost(tech_id, modifiers), cost_modifiers=modifiers)
        previous = self.focus.get(line_id)
        if previous == tech_id:
            return
        if previous is not None:
            self.projects[previous].interrupt('R01' in self.known)
        if tech_id is None:
            self.focus.pop(line_id, None)
        else:
            self.focus[line_id] = tech_id

    def target_cost(self, tech_id, modifiers):
        tech = self.catalog.technologies[tech_id]
        tier = next(r for r in self.catalog.tables['technology']['research_tiers'] if r['tier'] == tech['tier'])
        multiplier = ((Q(1, 2) if modifiers.get('licensed') else 1) *
                      (Q(85, 100) if modifiers.get('open_spillover') else 1) *
                      (Q(4, 5) if modifiers.get('stolen') else 1) *
                      (2 if modifiers.get('source_blockade') else 1))
        return round(units(tier['theory_points']) * max(Q(1, 4), multiplier))

    def license(self, tech_id, *, source_blockade):
        self.licenses[tech_id] = source_blockade
        project = self.projects.get(tech_id)
        if project is not None:
            project.cost_modifiers['licensed'] = True
            project.cost_modifiers['source_blockade'] = project.cost_modifiers.get('source_blockade', False) or source_blockade
            project.target_mU = self.target_cost(tech_id, project.cost_modifiers)
            project.theory_mU = min(project.theory_mU, project.target_mU)
            project.theory_complete = project.theory_mU == project.target_mU

    def advance(self, outputs: Mapping[str, LabOutput], *, open_allowed: bool = True) -> None:
        grouped: dict[str, list[LabOutput]] = {}
        for line, output in outputs.items():
            tech = self.focus.get(line)
            if tech is not None and not self.projects[tech].theory_complete:
                mode = self.security.get(line, 'standard')
                if mode == 'open' and not open_allowed:
                    mode = 'standard'
                adjusted = replace(output, research_mU=round(output.research_mU * SECURITY_MULTIPLIER[mode]))
                grouped.setdefault(tech, []).append(adjusted)
        for tech, rows in grouped.items():
            amount = combine_labs(tuple(rows), organized={'R05', 'C02'} <= self.known)
            self.projects[tech].advance_theory(amount)

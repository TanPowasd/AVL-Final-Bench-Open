"""P0-2: per-cycle five-pillar national power, the 2880-cycle trajectory and V.

The collector is read-only towards the game: it reads ledgers, metrics and
authority rows that the P7 settlement already produced, and stores only its own
score history. It runs as the last SETTLEMENT system of each cycle-end tick, so
querying it 0, 1 or 100 times never changes the Game state (07 section 4).

Every input that the engine does not yet produce exactly is listed in
`COVERAGE` with the approximation used, so a later change can replace it and
the trajectory version string changes with it.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .errors import InvariantViolation
from .numeric import Q, clip
from .scoring import (Pillars, Trajectory, control_pillar, industrial_pillar, international_pillar,
                      population_pillar, sustainability, technology_pillar)

VERSION = 'national_power_v1'

# Inputs that are approximations of the r6 text, by pillar. Kept in the
# snapshot and in every finalizer report so no score hides a gap.
COVERAGE = {
    'population.remediation': 'governance remediation_mU >= 1000 counts as 1, else 0',
    'population.advanced_medicine': 'medical_supplies satisfaction',
    'industry.va': 'industry_activity.last_company_va (signed catalog VA x direct share), by issuer',
    'control.connected': 'controlled regions reachable from the capital region over controlled adjacent regions',
    'control.powered_slots': 'non-housing lines with a positive batch in the cycle (power >= 0.8 not checked)',
    'control.chokepoint_phi': 'zero: per-region receipt path flow not retained',
    'control.kinfra_alpha_maint': '1 for assets that are not maintenance_offline, else 0',
    'control.readiness': 'sum over own units of hp_ratio*crew_ratio*installed catalog value*(.5+.5*morale)*(1-.4*fatigue); supply ratio 1',
    'technology.active_scale': 'active_scale.py meters, cycle-end values; online_offshore_mines, active_automated_routes, supplied_field_depots meter zero (no receipt yet)',
    'sustainability.unrest': 'population of controlled national hubs with support_mU < 300',
    'sustainability.shortage': '1 - min(food, electricity) satisfaction, population weighted, per cycle',
}


def _ratio(value, total):
    return Q(value, total) if total else Q(0)


class NationalPower:
    def __init__(self, state):
        self.s = state
        self.nations = tuple(sorted(state.initial.known))
        self.trajectories = {p: Trajectory() for p in self.nations}
        self.pillars = {p: [] for p in self.nations}          # one Pillars row per cycle
        self.shortage = {p: deque(maxlen=20) for p in self.nations}
        self.extracted = defaultdict(lambda: defaultdict(int))  # region -> good -> mU this cycle
        self.initial_cash_book = {p: state.initial.cash.owner_book(p) for p in self.nations}
        self.default_cycles = {p: [] for p in self.nations}
        self.last_tick = -1
        self.cycle = 0

    # -- hooks fed during the cycle -------------------------------------------------
    def record_extraction(self, hub, good, amount):
        if amount:
            self.extracted[self.s.initial.map.hubs[hub].region][good] += amount

    def advance(self, ctx):
        """Last SETTLEMENT system; meters combat every tick, scores at cycle end."""
        if ctx.tick <= self.last_tick:
            raise InvariantViolation('national power metered twice')
        self.last_tick = ctx.tick
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        if ctx.cycle != self.cycle:
            raise InvariantViolation('national power skipped a cycle')
        for p in self.nations:
            row = self.measure(p, ctx.cycle)
            self.pillars[p].append(row)
            if ctx.cycle < 2880:
                self.trajectories[p].commit(ctx.cycle, row.total)
        self.extracted.clear()
        self.cycle += 1

    # -- pillars --------------------------------------------------------------------
    def _controller(self, region_id, hub):
        row = self.s.authority.regions[region_id]
        return row.controller or self.s.initial.map.hubs[hub].owner, row

    def population(self, p, cycle):
        s = self.s
        total = Q(0)
        shortage_num = Q(0)
        shortage_den = 0
        for hub, population in s.initial.populations.items():
            if not population.total_mU:
                continue
            region = s.initial.map.hubs[hub].region
            controller, row = self._controller(region, hub)
            if controller != p:
                continue
            sat = s.last_satisfaction.get(hub, {})
            pollution = Q(s.pollution[hub], s.pollution[hub] + 100000)
            homeland = row.sovereign == p
            hold = 0 if homeland or row.occupied_since_tick is None else max(0, cycle - row.occupied_since_tick // 10)
            remedied = Q(int(s.governance.remediation_mU.get(region, 0) >= 1000))
            total += population_pillar(population.total_mU, population.support_mU, homeland=homeland,
                hold_cycles=hold, food=sat.get('food', Q(0)), synthetic_share=s.consumption.synthetic_share(hub, cycle),
                consumer_goods=sat.get('consumer_goods', Q(0)), medicine=sat.get('medicine', Q(0)),
                advanced_medicine=sat.get('medical_supplies', Q(0)), water=sat.get('water', Q(0)),
                electricity=sat.get('electricity', Q(0)), combat=s.battles.combat_share(region, cycle),
                blockade=s.blockades.region_share(region, p), pollution=pollution, remediation=remedied)
            shortage_num += population.total_mU * (1 - min(Q(1), sat.get('food', Q(0)), sat.get('electricity', Q(0))))
            shortage_den += population.total_mU
        total += sum((row['value'] for row in s.city_economy.population_scoring_rows(p, cycle)), Q(0))
        self.shortage[p].append(shortage_num / shortage_den if shortage_den else Q(0))
        return total

    def nfa_mU(self, p, cycle):
        s = self.s
        cash = s.initial.cash.owner_book(p) - self.initial_cash_book[p]
        longs = (s.bond_market.asset_book(p) + s.stock_market.long_book(p, cycle)
                 + s.contracts.loans.creditor_book(p))
        debts = (s.sovereign_finance.debt_book(p) + s.payables.debt_book(p) + s.contracts.loans.debt_book(p)
                 + s.stock_market.finance.debt_book(p) + s.stock_market.finance.short_liability_book(p, cycle)
                 + s.fx_shorts.debt_book(p))
        issue = s.initial.cash.issue_book.get(p, Q(0))
        equity = s.stock_market.external_equity_book(p, cycle)
        return cash + longs - debts - issue - equity

    def _cycle_table(self, table, cycle, p):
        return table.get(cycle, {}).get(p, Q(0))

    def industry(self, p, cycle):
        s = self.s
        va = sum((v for cid, v in s.industry_activity.last_company_va.items()
                  if s.initial.companies[cid].issuer == p), Q(0))
        metric = s.history[p][-1] if s.history[p] else None
        destroyed = metric.destroyed_value if metric else Q(0)
        actions = (self._cycle_table(s.battles.loss_cycles, cycle, p) + self._cycle_table(s.asat.action_cycles, cycle, p)
                   + self._cycle_table(s.deception.action_cycles, cycle, p) + self._cycle_table(s.climate.action_cycles, cycle, p)
                   + self._cycle_table(s.blockades.action_cycles, cycle, p)
                   + self._cycle_table(s.hypersonics.action_cycles, cycle, p)
                   + self._cycle_table(s.cyber.action_cycles, cycle, p))
        transit = s.consumption.receipt_mean20(p, 'transit')
        return industrial_pillar(va, destroyed, actions, transit, self.nfa_mU(p, cycle) / 1000)

    def connected_regions(self, p):
        s = self.s
        world = s.initial.map
        capital = world.hubs.get(f'{p}-h0')
        if capital is None:
            return set()
        controlled = {rid for rid, row in s.authority.regions.items()
                      if (row.controller or row.sovereign) == p}
        start = capital.region
        if start not in controlled:
            return set()
        seen, queue = {start}, deque([start])
        while queue:
            rid = queue.popleft()
            for n in world.regions[rid].adjacent:
                if n in controlled and n not in seen:
                    seen.add(n)
                    queue.append(n)
        return seen

    def control(self, p):
        s = self.s
        world, catalog = s.initial.map, s.catalog
        connected = self.connected_regions(p)
        metric = s.history[p][-1] if s.history[p] else None
        batches = metric.line_batches if metric else {}
        slots = defaultdict(int)
        for lid, batch in batches.items():
            line = s.initial.lines.get(lid)
            if batch > 0 and line is not None:
                slots[world.hubs[line.hub].region] += 1
        sigma = Q(0)
        for rid in connected:
            extract = sum((catalog.weights[g] * Q(q, 1000) for g, q in self.extracted.get(rid, {}).items()), Q(0))
            sigma += min(Q(5), Q(5, 100) * extract + Q(1, 2) * slots[rid])
        book = Q(0)
        for aid, asset in s.initial.assets.items():
            if asset.under_construction or asset.maintenance_offline or world.hubs.get(asset.hub) is None:
                continue
            if world.hubs[asset.hub].region not in connected:
                continue
            rights = s.authority.assets.get(aid)
            share = Q(rights.owner_shares.get(p, 0), 1000) if rights else Q(int(asset.owner == p))
            if share:
                book += share * sum((Q(q, 1000000) * catalog.weights[g] for g, q in asset.book_nmU.items()), Q(0))
        satellites = sum(1 for sat in s.orbit.satellites.values() if sat.owner == p)
        population = sum(pop.total_mU for hub, pop in s.initial.populations.items() if world.hubs[hub].owner == p)
        readiness = Q(0)
        for unit in s.initial.units.values():
            if unit.owner != p:
                continue
            spec = catalog.units[unit.unit_type]
            installed = sum((Q(q) * catalog.weights[g] for g, q in spec['installed'].items()), Q(0))
            hp, crew = Q(spec['hp']) * 1000, Q(spec['crew']) * 1000
            readiness += (min(Q(1), unit.hp_mU / hp) * min(Q(1), unit.crew_mU / crew) * installed
                          * (Q(1, 2) + Q(1, 2) * unit.morale) * (1 - Q(2, 5) * unit.fatigue))
        return control_pillar(sigma, book, satellites, population, sum(slots.values()), readiness)

    def technology(self, p):
        s = self.s
        catalog = s.catalog
        known = frozenset(t for t in s.research[p].known if t in catalog.technologies or t in catalog.milestones)
        scale = s.active_scale
        if scale is None:
            return technology_pillar(catalog, known, {}, {})
        return technology_pillar(catalog, known, scale.values.get(p, {}), scale.licensed.get(p, {}))

    def international(self, p):
        s = self.s
        inputs = s.city_economy.scoring_inputs(p)
        return international_pillar(s.consumption.receipt_mean20(p, 'export'), s.credit[p],
                                    inputs['city_state_alignment'], inputs['realized_aid_mean20'])

    def measure(self, p, cycle):
        return Pillars(self.population(p, cycle), self.industry(p, cycle), self.control(p),
                       self.technology(p), self.international(p))

    # -- end of match ----------------------------------------------------------------
    def phi(self, p):
        s = self.s
        debts = tuple(a.maintenance_debt for a in s.initial.assets.values() if a.owner == p)
        controlled = unrest = 0
        for hub, pop in s.initial.populations.items():
            region = s.initial.map.hubs[hub].region
            if self._controller(region, hub)[0] != p:
                continue
            controlled += pop.total_mU
            if pop.support_mU < 300:
                unrest += pop.total_mU
        defaults = len(s.sovereign_finance.default_cycles.get(p, ())) if hasattr(s.sovereign_finance, 'default_cycles') else 0
        last = self.cycle - 1
        recent = any(c >= last - 39 for c in s.sovereign_finance.default_cycles.get(p, ())) if defaults else False
        return sustainability(maintenance_debts=debts, shortage20=tuple(self.shortage[p]),
                              unrest_population_mU=unrest, controlled_population_mU=controlled,
                              defaults=defaults, default_last40=recent, peg_break_last40=False)

    def final_values(self):
        """V per nation; only defined once all 2880 cycles were committed."""
        return {p: self.trajectories[p].value(self.phi(p)) for p in self.nations}

    def partial_mean(self, p):
        rows = self.pillars[p]
        return sum((r.total for r in rows), Q(0)) / len(rows) if rows else Q(0)

    def snapshot(self):
        return {'version': VERSION, 'coverage': COVERAGE, 'cycle': self.cycle, 'last_tick': self.last_tick,
                'initial_cash_book': self.initial_cash_book,
                'pillars': {p: [[r.population, r.industry, r.control, r.technology, r.international] for r in rows]
                            for p, rows in self.pillars.items()},
                'shortage': {p: list(v) for p, v in self.shortage.items()},
                'extracted': {r: dict(v) for r, v in sorted(self.extracted.items())},
                'trajectory': {p: {'cycles': t.cycles, 'era_sums': list(t.era_sums), 'last40': list(t.last40)}
                               for p, t in self.trajectories.items()}}

    def audit(self):
        for p in self.nations:
            if len(self.pillars[p]) != self.cycle or self.trajectories[p].cycles != min(self.cycle, 2880):
                raise InvariantViolation('national power trajectory out of step')

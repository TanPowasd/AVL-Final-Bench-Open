"""Actual-flow GDP, population-weighted CPI, monetary policy and simultaneous FX.

Historical cash basis is never revalued by this module. The only monetary source
is CashLedger.issue; intervention is an ordinary finite-pool FX transaction.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from fractions import Fraction
import math

from .command_schema import fields
from .determinism import normal
from .errors import RuleViolation, InvariantViolation
from .ledger import CashAccount
from .numeric import Q, ceil, clip
from .payments import FXRequest
from .protocol import PreparedCommand


@dataclass
class BankPolicy:
    command_id: str
    rate_bp: int
    issue_mU: int
    intervene_ru_mU: int
    peg_mU: int | None
    capital_control_bp: int


@dataclass
class MacroCycle:
    output_native_mU: Fraction = Q(0)
    input_native_mU: Fraction = Q(0)
    output_base_mU: Fraction = Q(0)
    input_base_mU: Fraction = Q(0)
    imports_ru_mU: Fraction = Q(0)
    exports_ru_mU: Fraction = Q(0)
    portfolio_inflow_ru_mU: Fraction = Q(0)
    intervention_ru_mU: int = 0
    defaulted: bool = False
    blocked_essential_import: bool = False


class Macroeconomy:
    def __init__(self, state):
        self.state = state
        self.policies = {}
        self.active_policies = {}
        self.cycles = {p: MacroCycle() for p in state.initial.known}
        self.previous_money = {p: self.money(p) for p in state.initial.known}
        self.previous_output = dict.fromkeys(state.initial.known, Q(100000))
        self.previous_gdp_ru_mU = dict.fromkeys(state.initial.known, Q(100000))
        self.cpi_history = {p: deque([Q(1)] * 20, maxlen=20) for p in state.initial.known}
        self.import_history = {p: deque(maxlen=20) for p in state.initial.known}
        self.crisis_until_cycle = dict.fromkeys(state.initial.known, -1)
        self.peg_cooldown_cycle = dict.fromkeys(state.initial.known, -1)
        self.widen_cycle = dict.fromkeys(state.initial.known, -1)
        self.blackout_ticks = defaultdict(int)
        self.last_issue_cycle = -1
        self.last_policy_cycle = -1
        self.last_settlement_cycle = -1
        self.last_report = {}

    def money(self, currency):
        return self.state.initial.cash.supply(currency)

    def begin_tick(self, ctx):
        """Freeze each cycle's policy before life reservations and new orders.

        Commands replace the next cycle's policy. A mid-cycle replacement never
        spends a reservation made for a different peg or intervention.
        """
        if ctx.tick % ctx.clock.cycle_ticks:
            return
        if ctx.cycle <= self.last_policy_cycle:
            raise InvariantViolation('monetary policy frozen twice')
        self.active_policies = {p: replace(v) for p, v in self.policies.items()
                                if 'F02' in self.state.research[p].known}
        for owner, policy in self.active_policies.items():
            self.state.interest_bp[owner] = policy.rate_bp
        self.state.fx.controls = {p: v.capital_control_bp for p, v in self.active_policies.items()}
        self.last_policy_cycle = ctx.cycle

    def output(self, owner, hub, quantities):
        row = self.cycles[owner]
        quantities = {g: q for g, q in quantities.items() if self.state.catalog.goods[g]['kind'] == 'inventory'}
        row.output_native_mU += sum((q * self.state.price(g, hub) for g, q in quantities.items()), Q(0))
        row.output_base_mU += sum((q * 10 * self.state.catalog.weights[g] for g, q in quantities.items()), Q(0))

    def input(self, owner, hub, good, quantity):
        self.cycles[owner].input_native_mU += quantity * self.state.price(good, hub)
        self.cycles[owner].input_base_mU += quantity * 10 * self.state.catalog.weights[good]

    def electricity(self, owner, seller, amount_ru_mU, *, intermediate):
        """Actual paid RU, not a second valuation of dispatch receipts."""
        state = self.state
        if seller in self.cycles:
            self.cycles[seller].output_native_mU += Q(amount_ru_mU) / state.rates[seller]
        if intermediate and owner in self.cycles:
            self.cycles[owner].input_native_mU += Q(amount_ru_mU) / state.rates[owner]
        self.trade(owner, seller, amount_ru_mU)

    def electricity_quantity(self, owner, seller, quantity_mU, *, intermediate):
        # The opening delivered-electricity price is 2 native units, frozen.
        if seller in self.cycles:
            self.cycles[seller].output_base_mU += 2 * quantity_mU
        if intermediate and owner in self.cycles:
            self.cycles[owner].input_base_mU += 2 * quantity_mU

    def trade(self, buyer, seller, actual_ru_mU):
        if buyer == seller:
            return
        if buyer in self.cycles:
            self.cycles[buyer].imports_ru_mU += actual_ru_mU
        if seller in self.cycles:
            self.cycles[seller].exports_ru_mU += actual_ru_mU

    def trades(self, clearings):
        state = self.state
        for clearing in clearings:
            venue = state.spot.venues[clearing.venue]
            rate = Q(1) if venue.currency == 'RU' else state.rates[venue.currency]
            if sum(amount for _, _, amount in clearing.cash_flows) != clearing.paid_mU:
                raise InvariantViolation('market clearing is missing its actual cash-flow attribution')
            for buyer, seller, amount in clearing.cash_flows:
                self.trade(buyer, seller, amount * rate)

    def bank_lock(self, owner, currency):
        return CashAccount(owner, f'central_bank:{currency}', currency)

    def reserve(self, ctx):
        if ctx.tick % ctx.clock.cycle_ticks:
            return
        if ctx.cycle <= self.last_issue_cycle:
            raise InvariantViolation('monetary policy applied twice')
        self.last_issue_cycle = ctx.cycle
        state = self.state
        for owner, policy in self.active_policies.items():
            if 'F02' not in state.research[owner].known:
                continue
            amount = min(policy.issue_mU, self.money(owner) // 50)
            if amount:
                state.initial.cash.issue(state.native(owner, 'treasury'), amount, rate=state.rates[owner])
            max_intervention = ceil(max(Q(100000), self.money(owner) * state.rates[owner]) * Q(3, 100)) if policy.peg_mU else 0
            reserve_ru = max(0, policy.intervene_ru_mU) + max_intervention
            reserve_native = ceil(Q(max(0, -policy.intervene_ru_mU) + max_intervention) / state.rates[owner])
            for currency, desired, source in (('RU', reserve_ru, CashAccount(owner, 'reserves', 'RU')),
                    (owner, reserve_native, state.native(owner, 'treasury'))):
                lock = self.bank_lock(owner, currency)
                if state.initial.cash.balance(lock).native_mU:
                    raise InvariantViolation('monetary policy reservation not refunded')
                amount = min(state.initial.cash.balance(source).native_mU, ceil(desired * (1 + state.fx.fee)))
                state.initial.cash.transfer(source, lock, amount)

    def record_grid(self):
        for hub, grid in self.state.grids.items():
            self.blackout_ticks[hub] += int(grid.recovering)

    @staticmethod
    def _log_ratio(a, b):
        return math.log(max(float(a), 1e-300) / max(float(b), 1e-300))

    def settle(self, ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        if ctx.cycle <= self.last_settlement_cycle:
            raise InvariantViolation('macroeconomy settled twice')
        state = self.state
        old_rates, old_cpi, old_pi = dict(state.rates), dict(state.cpi), dict(state.inflation)
        old_interest = dict(state.interest_bp)
        weights = self.previous_gdp_ru_mU
        total = sum(weights.values())
        world_cpi = sum(weights[p] * old_cpi[p] for p in weights) / total
        world_pi = sum(weights[p] * old_pi[p] for p in weights) / total
        world_interest = (Q(5) if ctx.cycle == 0 or not any('F02' in r.known for r in state.research.values()) else
                          sum(weights[p] * Q(old_interest[p], 100) for p in weights) / total)
        cpis, inflations, gdps, growths, candidates = {}, {}, {}, {}, {}
        for owner, row in self.cycles.items():
            if 'F02' not in state.research[owner].known:
                state.interest_bp[owner] = round(world_interest * 100)
            populations = {h: p.total_mU for h, p in state.initial.populations.items() if state.initial.map.hubs[h].owner == owner}
            population = sum(populations.values())
            local_cpi = Q(0)
            for hub, residents in populations.items():
                basket = Q(0)
                for good, weight in (('food', Q(4, 10)), ('consumer_goods', Q(3, 10)), ('medicine', Q(15, 100))):
                    base = 10 * state.catalog.weights[good]
                    price = Q(state.spot.prices.get(f'spot:{hub}:{good}', round(1000 * base)), 1000)
                    basket += weight * price / base
                power = state.storage.cycle_cost[hub] / state.storage.cycle_quantity[hub] if state.storage.cycle_quantity[hub] else state.grid_prices_ru[hub]
                basket += Q(15, 100) * power / old_rates[owner] / 2
                local_cpi += residents * basket
            cpis[owner] = max(Q(1, 100), Q(round(1000 * local_cpi / population), 1000)) if population else old_cpi[owner]
            money = self.money(owner)
            g_m = self._log_ratio(max(1, money), max(1, self.previous_money[owner]))
            real_output = max(Q(0), row.output_base_mU - row.input_base_mU)
            g_y = self._log_ratio(max(Q(100000), real_output), max(Q(100000), self.previous_output[owner]))
            inflation_input = (.5 * clip(g_m - g_y, -.02, .02) + .5 * clip(self._log_ratio(cpis[owner], old_cpi[owner]), -.02, .02)
                               - clip(2e-5 * (state.interest_bp[owner] / 100 - 10000 * float(old_pi[owner]) - 2), -.0002, .0002))
            # Fractional rate, not a stock quantity: keep a fixed 10^-9 grid.
            inflations[owner] = Q(round((.9 * float(old_pi[owner]) + .1 * inflation_input) * 10**9), 10**9)
            gdps[owner] = max(Q(100000), (row.output_native_mU - row.input_native_mU) * old_rates[owner])
            blackout_population = sum(residents for hub, residents in populations.items() if self.blackout_ticks[hub] >= 3)
            war = bool(state.initial.diplomacy.active_war_interest_pp(owner))
            policy = self.active_policies.get(owner)
            low_reserves = (policy is not None and policy.peg_mU is not None and
                state.initial.cash.balance(CashAccount(owner, 'reserves', 'RU')).native_mU +
                state.initial.cash.balance(self.bank_lock(owner, 'RU')).native_mU < Q(sum(self.import_history[owner]), 200))
            if (war or row.defaulted or row.blocked_essential_import or low_reserves
                    or population and blackout_population * 10 >= population * 3):
                self.crisis_until_cycle[owner] = ctx.cycle + 10
            mobility = Q(3, 10) if policy and policy.capital_control_bp else Q(1)
            real_interest = Q(state.interest_bp[owner], 100) - 10000 * old_pi[owner]
            trade = .08 * float((row.exports_ru_mU - row.imports_ru_mU) / gdps[owner])
            ppp = -.15 * clip(self._log_ratio(old_rates[owner] * old_cpi[owner], world_cpi), -.2, .2)
            carry = float(Q(1, 2) * mobility * state.credit[owner] * (real_interest - world_interest + 10000 * world_pi) / 10000)
            flow = float(mobility) * clip(.01 * float(row.portfolio_inflow_ru_mU / gdps[owner]), -.01, .01)
            crisis = -.01 if ctx.cycle < self.crisis_until_cycle[owner] else 0
            value = trade + ppp + carry + flow + crisis - .05 * row.defaulted + .002 * normal(ctx.seed, 'fx', ctx.cycle, owner)
            candidates[owner] = value
            growths[owner] = (money, real_output)
        requests, targets = [], {}
        for owner, policy in self.active_policies.items():
            requested = Q(policy.intervene_ru_mU)
            if policy.peg_mU is not None:
                distance = self._log_ratio(old_rates[owner], Q(policy.peg_mU, 1000)) + candidates[owner]
                correction = -math.copysign(max(0, abs(distance) - .01), distance)
                requested += round(max(Q(100000), self.money(owner) * old_rates[owner]) * Q.from_float(clip(correction, -.03, .03)))
            if requested:
                selling = requested > 0
                source = self.bank_lock(owner, 'RU' if selling else owner)
                target = state.native(owner, 'treasury') if selling else CashAccount(owner, 'reserves', 'RU')
                rate = 1 / old_rates[owner] if selling else old_rates[owner]
                offered = ceil(abs(requested) * (1 + state.fx.fee) / (1 if selling else old_rates[owner]))
                oid = f'bank:{owner}'
                principal = ceil(abs(requested) / (1 if selling else old_rates[owner]))
                requests.append(FXRequest(oid, source, target, offered, rate, monetary_intervention=True,
                                          principal_limit_mU=principal))
                targets[oid] = owner, selling
        state.fx.execute(ctx.seed, ctx.tick, tuple(requests))
        for oid, (owner, selling) in targets.items():
            # Intervention uses actual principal. Fees are ordinary cash costs.
            row = self.cycles[owner]
            row.intervention_ru_mU = state.fx.last_fills[oid].executed_ru_mU * (1 if selling else -1)
        for owner, candidate in candidates.items():
            row = self.cycles[owner]
            effect = clip(float(Q(row.intervention_ru_mU) / max(Q(100000), self.money(owner) * old_rates[owner])), -.03, .03)
            bound = .15 if self.widen_cycle[owner] == ctx.cycle else .05
            value = clip(float(old_rates[owner]) * math.exp(clip(candidate + effect, -bound, bound)), .05, 20)
            state.rates[owner] = Q(round(value * 1000), 1000)
            policy = self.active_policies.get(owner)
            if policy and policy.peg_mU is not None and abs(self._log_ratio(state.rates[owner], Q(policy.peg_mU, 1000))) > .01:
                policy.peg_mU = None
                # Breakage invalidates any queued target as well; a command
                # accepted earlier this cycle cannot evade the cooldown.
                if owner in self.policies:
                    self.policies[owner].peg_mU = None
                self.peg_cooldown_cycle[owner] = ctx.cycle + 20
                self.widen_cycle[owner] = ctx.cycle + 1
            for currency, source in (('RU', CashAccount(owner, 'reserves', 'RU')), (owner, state.native(owner, 'treasury'))):
                state.payments.release(self.bank_lock(owner, currency), source)
            state.cpi[owner], state.inflation[owner] = cpis[owner], inflations[owner]
            self.cpi_history[owner].append(cpis[owner])
            state.wage_indices[owner] = self.cpi_history[owner][0]
            self.import_history[owner].append(row.imports_ru_mU)
        self.last_report = {p: {'gdp_ru_mU': round(gdps[p]), 'cpi_mU': round(cpis[p] * 1000),
            'inflation_per_billion': round(inflations[p] * 10**9), 'exchange_ru_mU': round(state.rates[p] * 1000),
            'imports_ru_mU': round(r.imports_ru_mU), 'exports_ru_mU': round(r.exports_ru_mU),
            'intervention_ru_mU': r.intervention_ru_mU, 'crisis_until_cycle': self.crisis_until_cycle[p]}
            for p, r in self.cycles.items()}
        self.previous_money = {p: v[0] for p, v in growths.items()}
        self.previous_output = {p: v[1] for p, v in growths.items()}
        self.previous_gdp_ru_mU = gdps
        self.cycles = {p: MacroCycle() for p in state.initial.known}
        self.blackout_ticks.clear()
        self.last_settlement_cycle = ctx.cycle

    def visible(self, owner):
        policy = self.policies.get(owner)
        return {'money_supply_mU': self.money(owner), 'interest_bp': self.state.interest_bp[owner],
                'exchange_ru_mU': round(1000 * self.state.rates[owner]),
                'cpi_mU': round(1000 * self.state.cpi[owner]),
                'inflation_per_billion': round(10**9 * self.state.inflation[owner]),
                'wage_index_mU': round(1000 * self.state.wage_indices[owner]),
                'last_cycle': dict(self.last_report.get(owner, {})),
                'peg_cooldown_until_cycle': self.peg_cooldown_cycle[owner],
                'next_policy': None if policy is None else {'command_id': policy.command_id,
                    'rate_bp': policy.rate_bp, 'issue_mU': policy.issue_mU,
                    'intervene_ru_mU': policy.intervene_ru_mU, 'peg': policy.peg_mU,
                    'capital_control_bp': policy.capital_control_bp}}

    def snapshot(self):
        return {'policies': self.policies, 'active_policies': self.active_policies,
                'cycles': self.cycles, 'previous_money': self.previous_money,
                'previous_output': self.previous_output, 'previous_gdp_ru_mU': self.previous_gdp_ru_mU,
                'cpi_history': {p: tuple(v) for p, v in self.cpi_history.items()}, 'crisis_until_cycle': self.crisis_until_cycle,
                'import_history': {p: tuple(v) for p, v in self.import_history.items()},
                'peg_cooldown_cycle': self.peg_cooldown_cycle, 'widen_cycle': self.widen_cycle,
                'blackout_ticks': dict(self.blackout_ticks), 'last_issue_cycle': self.last_issue_cycle,
                'last_policy_cycle': self.last_policy_cycle,
                'last_settlement_cycle': self.last_settlement_cycle, 'last_report': self.last_report}


class MonetaryCommands:
    OPERATIONS = frozenset({('finance', 'central_bank')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        fields(order, {'rate_bp': int, 'issue_mU': int, 'intervene_ru_mU': int, 'peg': (int, type(None)),
                       'capital_control_bp': int}, {'replaces_id': str})
        state = self.state
        known = state.research[owner].known
        if 'F02' not in known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if (not (-100 if 'F06' in known else 0) <= order['rate_bp'] <= 2000
                or not 0 <= order['issue_mU'] <= state.macro.money(owner) // 50
                or order['peg'] is not None and order['peg'] <= 0
                or order['capital_control_bp'] != 0 and not 100 <= order['capital_control_bp'] <= 500):
            raise RuleViolation('ERR_SCHEMA')
        if order['peg'] is not None and ctx.cycle < state.macro.peg_cooldown_cycle[owner]:
            raise RuleViolation('ERR_NO_PERMISSION', 'peg is in its twenty-cycle cooldown')
        old = state.macro.policies.get(owner)
        if old is not None and order.get('replaces_id') != old.command_id:
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID', 'replace the existing monetary plan')
        if order.get('replaces_id') and old is None:
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        policy = BankPolicy(str(int(order['id'])), order['rate_bp'], order['issue_mU'], order['intervene_ru_mU'],
                            order['peg'], order['capital_control_bp'])
        def commit():
            state.macro.policies[owner] = policy
        return PreparedCommand(commit, persistent=True)

import json
import math
import unittest
from fractions import Fraction as Q
from unittest.mock import patch

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.commands import EngineCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import InvariantViolation, RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.kernel import TickContext
from organizer.rift_era.engine.ledger import CashAccount, CashLedger
from organizer.rift_era.engine.macroeconomy import BankPolicy, MonetaryCommands
from organizer.rift_era.engine.payments import FXRequest, ForeignExchange, Payments, Obligation
from organizer.rift_era.engine.world import generate_candidate


class MacroeconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def fixture(self):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=30,
                               commerce=True, debug=False)
        return game, game.state

    @staticmethod
    def ctx(tick):
        return TickContext(1000, tick, ClockConfig(), {})

    @staticmethod
    def order(**fields):
        return {'id': 1, 'kind': 'finance', 'op': 'central_bank', 'rate_bp': 700, 'issue_mU': 1000,
                'intervene_ru_mU': 0, 'peg': None, 'capital_control_bp': 0} | fields

    def test_policy_waits_for_cycle_boundary_and_issue_matches_historical_liability(self):
        _, state = self.fixture()
        state.research['n0'].known.add('F02')
        state.macro.begin_tick(self.ctx(0))
        commands = MonetaryCommands(state)
        commands.prepare('n0', self.order(), self.ctx(0)).commit()
        state.macro.reserve(self.ctx(0))
        self.assertEqual(state.interest_bp['n0'], 500)
        self.assertEqual(state.initial.cash.supply('n0'), 650000000)
        state.macro.settle(self.ctx(9))
        before = state.initial.cash.owner_book('n0')
        state.macro.begin_tick(self.ctx(10))
        rate = state.rates['n0']
        state.macro.reserve(self.ctx(10))
        self.assertEqual(state.interest_bp['n0'], 700)
        self.assertEqual(state.initial.cash.supply('n0'), 650001000)
        self.assertEqual(state.initial.cash.issue_book['n0'], 1000 * rate)
        self.assertEqual(state.initial.cash.owner_book('n0') - before, 1000 * rate)
        with self.assertRaises(InvariantViolation):
            state.macro.reserve(self.ctx(10))
        state.initial.cash.audit()

    def test_invalid_policy_is_pure_and_cancel_stops_future_issuance(self):
        _, state = self.fixture()
        commands = EngineCommands(state)
        before = fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            commands.prepare('n0', self.order(), self.ctx(0))
        self.assertEqual(fingerprint(state.snapshot()), before)
        state.research['n0'].known.add('F02')
        stream = state.command_streams['n0']
        stream.process([self.order()], lambda o: commands.prepare('n0', o, self.ctx(0)))
        stream.process([{'id': 2, 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '1'}],
                       lambda o: commands.prepare('n0', o, self.ctx(5)))
        self.assertFalse(state.macro.policies)
        state.macro.begin_tick(self.ctx(10))
        state.macro.reserve(self.ctx(10))
        self.assertFalse(state.initial.cash.issue_book)

    def test_intervention_uses_actual_ru_leg_not_reconstructed_native_value(self):
        _, state = self.fixture()
        state.research['n0'].known.add('F02')
        state.rates['n0'] = Q(137, 100)
        state.initial.cash.transfer(CashAccount('world', 'pool', 'RU'), CashAccount('n0', 'reserves', 'RU'), 47)
        state.macro.policies['n0'] = BankPolicy('1', 500, 0, 46, None, 0)
        state.macro.begin_tick(self.ctx(0))
        state.macro.reserve(self.ctx(0))
        state.macro.settle(self.ctx(9))
        fill = state.fx.last_fills['bank:n0']
        self.assertEqual((fill.given_mU, fill.received_mU), (46, 33))
        self.assertEqual(state.macro.last_report['n0']['intervention_ru_mU'], 46)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0', 'reserves', 'RU')).native_mU, 1)

    def test_clip_happens_after_intervention_and_peg_failure_has_cooldown(self):
        _, state = self.fixture()
        state.research['n0'].known.add('F02')
        state.macro.policies['n0'] = BankPolicy('1', 500, 0, 0, 500, 0)
        state.macro.begin_tick(self.ctx(0))
        state.macro.reserve(self.ctx(0))
        # A very large export shock remains above +.05 even after -.03 of
        # actually funded intervention. Pre-clipping would incorrectly give +.02.
        state.macro.cycles['n0'].exports_ru_mU = 10000000
        with patch('organizer.rift_era.engine.macroeconomy.normal', return_value=0):
            state.macro.settle(self.ctx(9))
        self.assertEqual(state.rates['n0'], Q(round(math.exp(.05) * 1000), 1000))
        self.assertIsNone(state.macro.policies['n0'].peg_mU)
        self.assertEqual(state.macro.widen_cycle['n0'], 1)
        with self.assertRaises(RuleViolation):
            MonetaryCommands(state).prepare('n0', self.order(id=2, peg=1000, replaces_id='1'), self.ctx(10))

    def test_nation_insertion_order_does_not_change_macro_results(self):
        results = []
        for reverse in (False, True):
            _, state = self.fixture()
            state.research['n0'].known.add('F02')
            state.interest_bp['n0'] = 1300
            if reverse:
                state.macro.cycles = dict(reversed(tuple(state.macro.cycles.items())))
            state.macro.begin_tick(self.ctx(10))
            state.macro.reserve(self.ctx(10))
            state.macro.settle(self.ctx(19))
            results.append((state.rates, state.inflation, state.interest_bp, state.macro.last_report))
        self.assertEqual(*results)

    def test_gdp_and_real_growth_subtract_actual_intermediates(self):
        _, state = self.fixture()
        row = state.macro.cycles['n0']
        row.output_native_mU, row.input_native_mU = Q(900000), Q(200000)
        row.output_base_mU, row.input_base_mU = Q(800000), Q(300000)
        state.macro.begin_tick(self.ctx(0))
        state.macro.reserve(self.ctx(0))
        state.macro.settle(self.ctx(9))
        self.assertEqual(state.macro.last_report['n0']['gdp_ru_mU'], 700000)
        self.assertEqual(state.macro.previous_output['n0'], 500000)

    def test_p7_macro_precedes_demography_and_observations_are_plain_json(self):
        game, state = self.fixture()
        systems = [s.id for s in game.systems]
        self.assertLess(systems.index('settlement'), systems.index('macroeconomy'))
        self.assertLess(systems.index('macroeconomy'), systems.index('population'))
        game.step({'n0': (self.order(),)})  # rejected command must still serialize
        for _ in range(9):
            game.step()
        self.assertEqual(state.completed_cycles, 1)
        self.assertTrue(state.macro.last_report)
        json.dumps(state.observations['n0'], allow_nan=False)
        state.audit()

    def test_capital_control_is_an_actual_fee_and_cannot_overdraw_budget(self):
        cash = CashLedger()
        payer, recipient = CashAccount('p', 'fx', 'q'), CashAccount('p', 'reserve', 'RU')
        pool, treasury = CashAccount('world', 'pool', 'RU'), CashAccount('q', 'treasury', 'q')
        cash.initialize(payer, 10520)
        cash.initialize(pool, 9000)
        cash.seal()
        fx = ForeignExchange(cash, Payments(cash))
        fx.controls['q'] = 500
        fx.execute(1, 0, (FXRequest('a', payer, recipient, 10520, Q(1)),))
        fill = fx.last_fills['a']
        self.assertEqual((fill.received_mU, fill.control_mU, fill.fee_mU), (9000, 450, 18))
        self.assertEqual(cash.balance(treasury).native_mU, 450)
        self.assertEqual(cash.balance(payer).native_mU, 10520 - 9000 - 450 - 18)
        cash.audit()

    def test_multi_party_bill_rounds_one_debit_and_keeps_shared_fraction(self):
        cash = CashLedger()
        payer = CashAccount('p', 'power', 'RU')
        recipients = (CashAccount('a', 'generation', 'RU'), CashAccount('b', 'grid', 'RU'))
        cash.initialize(payer, 10)
        cash.seal()
        payments = Payments(cash)
        rows = tuple(Obligation(payer, target, Q(3, 5), 'electricity') for target in recipients)
        for tick in range(5):
            plan = payments.prepare_invoice(rows, kind='power', key=(1, 'invoice', tick))
            plan.commit()
            self.assertEqual(sum(plan.amounts), 2 if tick == 4 else 1)
        self.assertEqual(cash.balance(payer).native_mU, 4)
        self.assertEqual(sum(cash.balance(a).native_mU for a in recipients), 6)
        self.assertFalse(payments.residuals)
        cash.audit()


if __name__ == '__main__':
    unittest.main()

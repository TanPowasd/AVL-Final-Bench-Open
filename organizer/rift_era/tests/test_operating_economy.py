import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from fractions import Fraction as Q

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.ledger import Account, CashAccount, CashLedger
from organizer.rift_era.engine.payments import Payments, ForeignExchange, FXRequest
from organizer.rift_era.engine.power import Supply, Load, dispatch, dispatch_reference, PlantMode, Plant, PLANTS
from organizer.rift_era.engine.production import Line, Recipe, Production, commit_buffers
from organizer.rift_era.engine.errors import InfrastructureFault, RuleViolation
from organizer.rift_era.engine.sandbox import Sandbox


class OperatingEconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def game(self, ticks=10):
        return opening_economy(self.catalog, initialize(self.catalog, generate_candidate(1000)),
                               total_ticks=ticks, debug=False)

    def test_fast_dispatch_matches_reference_with_caps_and_prior_service(self):
        randomizer = random.Random(771)
        for seed in range(100):
            supplies = tuple(Supply(str(i), str(randomizer.randrange(2)), 'A', randomizer.randrange(30),
                                    Q(randomizer.randrange(5))) for i in range(4))
            loads = tuple(Load(str(i), str(randomizer.randrange(2)), 'A', randomizer.randrange(1, 30),
                              randomizer.randrange(6), Q(randomizer.randrange(6)), Q(randomizer.randrange(50)))
                          for i in range(5))
            fast, slow = dispatch(seed, 0, supplies, loads, ()), dispatch_reference(seed, 0, supplies, loads, ())
            self.assertEqual((fast.received, fast.spent, fast.source_remaining),
                             (slow.received, slow.spent, slow.source_remaining))

    def test_fx_pool_is_finite_and_batch_order_independent(self):
        def run(reverse):
            ledger = CashLedger()
            ledger.initialize(CashAccount('world', 'pool', 'RU'), 1001)
            requests = []
            for owner in ('A', 'B'):
                source, target = CashAccount(owner, 'treasury', owner), CashAccount(owner, 'reserve', 'RU')
                ledger.initialize(source, 10000)
                requests.append(FXRequest(owner, source, target, 1000, Q(1)))
            ledger.seal()
            fx = ForeignExchange(ledger, Payments(ledger))
            amounts = fx.execute(3, 0, tuple(reversed(requests) if reverse else requests))
            self.assertEqual(sum(amounts.values()), 1001)
            self.assertLessEqual(abs(amounts['A'] - amounts['B']), 1)
            self.assertEqual(ledger.balance(CashAccount('world', 'pool', 'RU')).native_mU, 0)
            ledger.audit()
            return ledger.snapshot()
        self.assertEqual(run(False), run(True))

    def test_unpaid_obligations_do_not_mint_cash(self):
        ledger = CashLedger()
        payer, worker = CashAccount('A', 'corp', 'A'), CashAccount('A', 'households', 'A')
        ledger.initialize(payer, 3)
        ledger.seal()
        payments = Payments(ledger)
        result = payments.pay(payer, worker, Q(10), kind='wage', allow_arrears=True)
        self.assertEqual((result.paid_mU, result.unpaid_mU), (3, 7))
        self.assertEqual(ledger.balance(worker).native_mU, 3)
        self.assertEqual(ledger.balance(payer).native_mU, 0)
        ledger.audit()
        before = ledger.snapshot()
        with self.assertRaises(RuleViolation):
            payments.pay(payer, worker, Q(1), kind='wage')
        self.assertEqual(before, ledger.snapshot())

    def test_shared_storage_and_buffer_for_mining_and_manufacturing(self):
        from organizer.rift_era.engine.ledger import Inventory
        inventory = Inventory()
        inventory.initialize(Account('nature', 'd', 'deposit', 'ore'), 100)
        mine = Line('mine', 'A', 'h', Recipe('extract', {'ore': 1000}, {'ore': 1000}, frozenset(), frozenset()))
        factory = Line('factory', 'A', 'h', Recipe('steel', {'ore': 1000}, {'steel': 1000}, frozenset(), frozenset()))
        production = Production(1, inventory)
        result = production.advance(0, [mine, factory], {'mine': 100, 'factory': 100},
            free_capacity_mU={'h': 50}, input_sources={'mine': ('nature', 'd', 'deposit')})
        self.assertEqual({r.line_id: r.batch_mU for r in result}, {'mine': 50, 'factory': 0})
        self.assertEqual(inventory.balance(Account('nature', 'd', 'deposit', 'ore')), 50)
        commit_buffers(inventory)
        result = production.advance(1, [factory], {'factory': 100}, free_capacity_mU={'h': 100})
        self.assertEqual(result[0].batch_mU, 50)
        inventory.audit()

    def test_actual_cycle_reservations_wages_and_population(self):
        engine = self.game()
        for _ in range(10):
            engine.step()
            engine.state.audit()
        state = engine.state
        self.assertEqual(state.completed_cycles, 1)
        for owner in state.initial.known:
            cycle = state.history[owner][-1]
            self.assertGreater(cycle.wages_due_mU, 0)
            self.assertEqual(cycle.unpaid_wages_mU, 0)
            self.assertEqual(state.last_satisfaction[f'{owner}-h0']['food'], 1)
            self.assertGreater(state.initial.populations[f'{owner}-h0'].total_mU, 194000)
            self.assertGreater(state.research[owner].projects['I01'].theory_mU, 0)
        self.assertFalse(any(a.bucket.startswith('locked_subsistence:') for a in state.initial.inventory.balances()))
        for reservation, _ in state.frame.reservations:
            self.assertEqual(state.initial.cash.balance(reservation).native_mU, 0)

    def test_no_money_means_no_free_power(self):
        engine = self.game()
        state = engine.state
        for name in ('treasury', 'household:n0-h0'):
            source = state.native('n0', name)
            state.initial.cash.transfer(source, CashAccount('world', 'pool', 'n0'), state.initial.cash.balance(source).native_mU)
        coal = state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'coal'))
        engine.step()
        self.assertEqual(sum(q for lid, q in state.frame.generation.items() if lid.startswith('n0/')), 0)
        self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'coal')), coal)
        self.assertGreater(state.cycles['n0'].unpaid_wages_mU, 0)
        state.audit()

    def test_grain_emergency_is_bought_and_capped(self):
        engine = self.game()
        state = engine.state
        food = Account('n0', 'n0-h0', 'available', 'food')
        state.initial.inventory.transform({food: state.initial.inventory.balance(food)}, {}, reason='test_food_loss')
        engine.step()
        metric = state.cycles['n0']
        self.assertGreater(metric.household_consumed['grain'], 0)
        self.assertLessEqual(metric.household_consumed['food'] / metric.needed['food'], Q(3, 4))
        self.assertEqual(metric.household_consumed['food'], metric.household_consumed['grain'] * Q(20, 27))
        state.audit()

    def test_cash_shortage_shares_food_and_water_budget(self):
        engine = self.game()
        state = engine.state
        household = state.native('n0', 'household:n0-h0')
        state.initial.cash.transfer(household, state.native('n0', 'treasury'),
                                   state.initial.cash.balance(household).native_mU - 25000)
        engine.step()
        metric = state.cycles['n0']
        food = metric.household_consumed['food'] / metric.needed['food']
        water = metric.household_consumed['water'] / metric.needed['water']
        self.assertGreater(food, 0)
        self.assertGreater(water, 0)
        self.assertLess(food, 1)
        self.assertLess(abs(food - water), Q(1, 500))
        state.audit()

    def test_starting_generator_pays_fuel_before_countdown_progress(self):
        engine = self.game()
        state = engine.state
        lid = 'n0/P070/0/line'
        state.initial.plants[lid].mode = PlantMode.STARTING
        state.initial.plants[lid].timer = 5
        engine.step()
        self.assertEqual(state.initial.plants[lid].timer, 4)
        self.assertEqual(state.frame.generation[lid], 0)
        self.assertGreater(sum(state.frame.worked[lid].values()), 0)
        self.assertGreater(state.cycles['n0'].consumed['coal'], 0)

    def test_first_online_tick_reserves_minimum_stable_output(self):
        engine = self.game()
        plant = engine.state.initial.plants['n0/P070/0/line']
        plant.mode, plant.timer = PlantMode.STARTING, 1
        engine.step()
        self.assertEqual(plant.timer, 0)
        engine.step()
        self.assertEqual(plant.mode, PlantMode.ON)
        self.assertGreaterEqual(plant.output_fraction, Q(2, 5))
        hydrogen = Plant('h', PLANTS['P074'], online=True)
        self.assertEqual(hydrogen.prepare(Q(1), Q(1), Q(1)), (Q(0), Q(1)))

    def test_cgroup_preflight_does_not_soft_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(InfrastructureFault, 'cgroup v2'):
                Sandbox(Path(root)).preflight()
            (Path(root) / 'cgroup.controllers').write_text('cpu memory pids')
            (Path(root) / 'cgroup.subtree_control').write_text('cpu memory pids')
            with self.assertRaisesRegex(InfrastructureFault, 'kernel cgroup v2 filesystem'):
                Sandbox(Path(root)).preflight()

    def test_shut_down_generators_do_not_supply_or_consume_fuel(self):
        engine = self.game()
        state = engine.state
        for lid, plant in state.initial.plants.items():
            if lid.startswith('n0/'):
                plant.online = False
        initial_coal = state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'coal'))
        engine.step()
        self.assertEqual(sum(q for lid, q in state.frame.generation.items() if lid.startswith('n0/')), 0)
        self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'coal')), initial_coal)
        self.assertEqual(state.cycles['n0'].generation_mU, 0)
        state.audit()

    @unittest.skipUnless(sys.platform == 'linux' and Path('/lib/x86_64-linux-gnu/libseccomp.so.2').exists(), 'Linux x86_64 seccomp component test')
    def test_post_exec_filter_blocks_fork_network_and_exec_but_allows_threads(self):
        program = '''
import os,socket,threading,errno
from organizer.rift_era.engine.seccomp_filter import install
install('/lib/x86_64-linux-gnu/libseccomp.so.2')
for operation in (os.fork,socket.socket,lambda:os.execv('/nonexistent',['x'])):
    try:operation()
    except OSError as e:assert e.errno==errno.EPERM,e
    else:raise AssertionError('forbidden syscall allowed')
result=[]
t=threading.Thread(target=lambda:result.append(1));t.start();t.join()
assert result==[1]
'''
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

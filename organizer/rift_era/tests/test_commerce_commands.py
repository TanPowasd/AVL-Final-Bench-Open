import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.commerce_commands import CommerceCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import InfrastructureFault
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.kernel import TickContext
from organizer.rift_era.engine.ledger import Account, CashAccount
from organizer.rift_era.engine.world import generate_candidate


class CommerceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.world = generate_candidate(1000)

    def game(self, commerce=True):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=10,
                               debug=False, commerce=commerce)

    def fund(self, game, owner, amount, currency='RU'):
        # Explicit finite fixture funding; neither the command handler nor the
        # economy is allowed to make this transfer on the player's behalf.
        account = CashAccount(owner, 'reserves' if currency == 'RU' else 'treasury', currency)
        game.state.initial.cash.transfer(CashAccount('world', 'pool', currency), account, amount)
        return account

    def test_commands_live_in_eight_stage_economy_and_do_not_teleport_goods(self):
        game = self.game()
        state = game.state
        payer = self.fund(game, 'n1', 50000, 'n0')
        orders = {'n0': ({'id': '1', 'kind': 'logistics', 'op': 'market_ask', 'hub_id': 'n0-h0',
                   'good': 'steel', 'qty_mU': 1000, 'limit_price_mU': 10000, 'expire_cycles': 1},),
                  'n1': ({'id': '1', 'kind': 'logistics', 'op': 'market_bid', 'region_market': 'n0-h0',
                   'good': 'steel', 'qty_mU': 1000, 'limit_price_mU': 10000, 'expire_cycles': 1},)}
        game.step(orders)
        self.assertEqual(state.command_streams['n0'].recent[-1].status, 'ACCEPTED')
        self.assertEqual(state.command_streams['n1'].recent[-1].status, 'ACCEPTED')
        self.assertEqual(state.initial.cash.balance(payer).native_mU, 40000)
        self.assertEqual(state.initial.inventory.balance(Account('n1', 'n0-h0', 'available', 'steel')), 0)
        for _ in range(4):
            game.step()
            state.audit()
        self.assertEqual(state.initial.inventory.balance(Account('n1', 'n0-h0', 'available', 'steel')), 1000)
        self.assertEqual(state.initial.inventory.balance(Account('n1', 'n1-h4', 'available', 'steel')), 0)
        clearings=[row for row in state.market_events if row.venue=='spot:n0-h0:steel']
        self.assertEqual(len(clearings),1)
        self.assertEqual(clearings[0].quantity_mU, 1000)
        self.assertFalse([o for o in state.spot.orders.values() if o.owner in state.initial.known])
        # Autonomous cities have independent, genuinely funded resting bids.
        # Their presence cannot be mistaken for an unfilled player order.
        self.assertTrue(any(o.owner in state.city_economy.cities for o in state.spot.orders.values()))
        self.assertIn('command_receipts', state.observations['n1'])

    def test_same_p1_export_competition_is_proportional_and_cancel_wins(self):
        for cancel in (False, True):
            game = self.game()
            state = game.state
            for p in ('n1', 'n2'):
                self.fund(game, p, 50000)
            game.step({'n0': ({'id': 1, 'kind': 'trade', 'op': 'export_offer', 'hub_id': 'n0-h0',
                'good': 'steel', 'qty_mU': 1001, 'price_ru_mU': 1000},)})
            for _ in range(4):
                game.step()
            orders = {p: ({'id': 1, 'kind': 'trade', 'op': 'buy_from', 'offer_id': 'offer-1', 'seller': 'n0',
                            'good': 'steel', 'qty_mU': 1000, 'max_price_ru_mU': 1000},) for p in ('n1', 'n2')}
            if cancel:
                orders['n0'] = ({'id': 2, 'kind': 'cancel', 'op': 'cancel_by_id', 'target_id': '1'},)
            game.step(orders)
            quantities = [state.initial.inventory.balance(Account(p, 'n0-h0', 'available', 'steel')) for p in ('n1', 'n2')]
            self.assertEqual(sorted(quantities), [0, 0] if cancel else [500, 501])
            self.assertFalse([o for o in state.spot.orders.values() if o.owner in state.initial.known])
            state.audit()

    def test_schema_rejection_advances_watermark_without_business_mutation(self):
        game = self.game()
        state = game.state
        adapter = CommerceCommands(state)
        ctx = TickContext(1000, 0, ClockConfig(total_ticks=10), {})
        original = fingerprint((state.spot.snapshot(), state.initial.cash.snapshot(), state.initial.inventory.snapshot()))
        command = {'id': 1, 'kind': 'logistics', 'op': 'market_bid', 'region_market': 'n0-h0',
                   'good': 'steel', 'qty_mU': True, 'limit_price_mU': 1000, 'expire_cycles': 1}
        receipts = state.command_streams['n0'].process([command], lambda o: adapter.prepare('n0', o, ctx))
        self.assertEqual(receipts[0].code, 'ERR_SCHEMA')
        self.assertEqual(state.command_streams['n0'].watermark, 1)
        command['qty_mU'] = 1000
        receipts = state.command_streams['n0'].process([command], lambda o: adapter.prepare('n0', o, ctx))
        self.assertEqual(receipts[0].code, 'ERR_DUPLICATE_ID')
        self.assertEqual(original, fingerprint((state.spot.snapshot(), state.initial.cash.snapshot(), state.initial.inventory.snapshot())))

    def test_life_reservations_cannot_be_sold_out_from_under_residents(self):
        game = self.game()
        state = game.state
        available = state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'food'))
        game.step({'n0': ({'id': 1, 'kind': 'trade', 'op': 'export_offer', 'hub_id': 'n0-h0',
                          'good': 'food', 'qty_mU': available, 'price_ru_mU': 1000},)})
        self.assertEqual(state.command_streams['n0'].recent[-1].code, 'ERR_INSUFFICIENT_STOCK')
        self.assertEqual(state.cycles['n0'].household_consumed['food'], state.cycles['n0'].needed['food'])
        state.audit()

    def test_new_market_quote_does_not_reprice_already_reserved_household_bill(self):
        game = self.game()
        state = game.state
        self.fund(game, 'n1', 200000, 'n0')
        game.step({'n0': ({'id': 1, 'kind': 'logistics', 'op': 'market_ask', 'hub_id': 'n0-h0',
                           'good': 'food', 'qty_mU': 1, 'limit_price_mU': 100000000, 'expire_cycles': 1},),
                   'n1': ({'id': 1, 'kind': 'logistics', 'op': 'market_bid', 'region_market': 'n0-h0',
                           'good': 'food', 'qty_mU': 1, 'limit_price_mU': 100000000, 'expire_cycles': 1},)})
        old = state.price('food', 'n0-h0')
        for _ in range(4):
            game.step()
        self.assertEqual(state.spot.prices['spot:n0-h0:food'], 100000000)
        self.assertEqual(state.price('food', 'n0-h0'), old)
        game.step()
        self.assertEqual(state.price('food', 'n0-h0'), 100000)
        state.audit()

    def test_bootstrap_remains_fixed_and_rejects_orders_as_assembly_error(self):
        game = self.game(commerce=False)
        with self.assertRaises(InfrastructureFault):
            game.step({'n0': ({'id': 1},)})

    def test_export_purchase_funded_by_actual_player_fx_not_fixture_cash(self):
        game = self.game()
        state = game.state
        game.step({'n0': ({'id': 1, 'kind': 'trade', 'op': 'export_offer', 'hub_id': 'n0-h0',
                           'good': 'steel', 'qty_mU': 1000, 'price_ru_mU': 1000},),
                   'n1': ({'id': 1, 'kind': 'finance', 'op': 'fx', 'side': 'buy_ru',
                           'currency': 'n1', 'amount_mU': 10020},)})
        self.assertEqual(state.command_streams['n1'].recent[-1].status, 'ACCEPTED')
        reserves = CashAccount('n1', 'reserves', 'RU')
        self.assertEqual(state.initial.cash.balance(reserves).native_mU, 10000)
        for _ in range(4):
            game.step()
        game.step({'n1': ({'id': 2, 'kind': 'trade', 'op': 'buy_from', 'offer_id': 'offer-1', 'seller': 'n0',
                           'good': 'steel', 'qty_mU': 1000, 'max_price_ru_mU': 1000},)})
        self.assertEqual(state.initial.cash.balance(reserves).native_mU, 9000)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0', 'reserves', 'RU')).native_mU, 1000)
        self.assertEqual(state.initial.inventory.balance(Account('n1', 'n0-h0', 'available', 'steel')), 1000)
        state.audit()

    def test_fx_commands_share_frozen_world_cash_and_retain_fractional_fees(self):
        game = self.game()
        state = game.state
        adapter = CommerceCommands(state)
        pool = CashAccount('world', 'pool', 'RU')
        state.initial.cash.transfer(pool, CashAccount('world', 'sequestered_fixture', 'RU'),
                                   state.initial.cash.balance(pool).native_mU - 1001)
        orders = {p: ({'id': 1, 'kind': 'finance', 'op': 'fx', 'side': 'buy_ru',
                       'currency': p, 'amount_mU': 2004},) for p in ('n0', 'n1')}
        adapter.commands(state, TickContext(1000, 0, ClockConfig(total_ticks=10), orders))
        self.assertEqual(sorted(state.fx_fills.values()), [500, 501])
        self.assertEqual(state.initial.cash.balance(pool).native_mU, 0)
        self.assertFalse(state.fx_commands)
        self.assertFalse(any(a.name.startswith('fx_command:') for a in state.initial.cash.balances()))
        state.audit()

    def test_foreign_currency_requires_reserve_currency_technology(self):
        game = self.game()
        game.step({'n0': ({'id': 1, 'kind': 'finance', 'op': 'fx', 'side': 'sell_ru',
                           'currency': 'n1', 'amount_mU': 1000},)})
        self.assertEqual(game.state.command_streams['n0'].recent[-1].code, 'ERR_TECH_LOCKED')

    def test_round_trip_export_never_relabels_origin_or_earns_trade_credit(self):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=20,
                               debug=False, commerce=True)
        state = game.state
        for tick in range(20):
            orders = {}
            if tick == 0:
                orders = {'n0': ({'id': 1, 'kind': 'trade', 'op': 'export_offer', 'hub_id': 'n0-h0',
                                   'good': 'food', 'qty_mU': 1000, 'price_ru_mU': 1000},),
                          'n1': ({'id': 1, 'kind': 'finance', 'op': 'fx', 'side': 'buy_ru',
                                   'currency': 'n1', 'amount_mU': 10020},)}
            elif tick == 5:
                offer = state.observations['n1']['markets']['export_offers'][0]
                orders = {'n1': ({'id': 2, 'kind': 'trade', 'op': 'buy_from', 'offer_id': offer['id'],
                                   'seller': 'n0', 'good': 'food', 'qty_mU': 1000, 'max_price_ru_mU': 1000},),
                          'n0': ({'id': 2, 'kind': 'finance', 'op': 'fx', 'side': 'buy_ru',
                                   'currency': 'n0', 'amount_mU': 10020},)}
            elif tick == 10:
                orders = {'n1': ({'id': 3, 'kind': 'trade', 'op': 'export_offer', 'hub_id': 'n0-h0',
                                   'good': 'food', 'qty_mU': 1000, 'price_ru_mU': 1000},)}
            elif tick == 15:
                offer = state.observations['n0']['markets']['export_offers'][0]
                orders = {'n0': ({'id': 3, 'kind': 'trade', 'op': 'buy_from', 'offer_id': offer['id'],
                                   'seller': 'n1', 'good': 'food', 'qty_mU': 1000, 'max_price_ru_mU': 1000},)}
            game.step(orders)
            state.audit()
        for p in ('n0', 'n1'):
            self.assertTrue(all(r.status == 'ACCEPTED' for r in state.command_streams[p].recent))
            self.assertEqual(state.consumption.receipt_mean20(p, 'export'), 0)
        for origin in state.initial.inventory.materials.composition(Account('n0', 'n0-h0', 'available', 'food')):
            self.assertEqual(origin.nation, 'n0')
        civilian_food = sum(c.household_consumed['food'] for c in state.history['n0'])
        self.assertEqual(state.consumption.consumption20('n0', 'food'), civilian_food)
        self.assertEqual(state.consumption.sink_debt[('n0', 'food')], 720)

    def test_full_phase_trace_replays_commands_fx_and_bilateral_settlement(self):
        traces = []
        for reverse in (False, True):
            game = self.game()
            game.debug = True
            trace = []
            for tick in range(10):
                orders = {}
                if tick == 0:
                    orders = {'n0': ({'id': 31, 'kind': 'trade', 'op': 'export_offer', 'hub_id': 'n0-h0',
                                       'good': 'steel', 'qty_mU': 1000, 'price_ru_mU': 1000},),
                              'n1': ({'id': 7, 'kind': 'finance', 'op': 'fx', 'side': 'buy_ru',
                                       'currency': 'n1', 'amount_mU': 10020},)}
                if tick == 5:
                    offer = game.state.observations['n1']['markets']['export_offers'][0]
                    self.assertEqual(offer['id'], 'offer-1')
                    orders = {'n1': ({'id': 8, 'kind': 'trade', 'op': 'buy_from', 'offer_id': offer['id'],
                                      'seller': 'n0', 'good': 'steel', 'qty_mU': 1000, 'max_price_ru_mU': 1000},)}
                trace.append(game.step(dict(reversed(tuple(orders.items()))) if reverse else orders).stage_hashes)
            self.assertEqual(game.state.observations['n1']['market_fills'][0]['id'], '8')
            self.assertEqual(game.state.observations['n1']['market_fills'][0]['cash_mU'], 1000)
            self.assertEqual(game.state.observations['n0']['market_fills'][0]['id'], '31')
            traces.append(trace)
        self.assertEqual(*traces)


if __name__ == '__main__':
    unittest.main()

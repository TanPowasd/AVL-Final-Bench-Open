import unittest
from types import SimpleNamespace

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.city_state_economy import CityStateEconomy
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account, CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.power import Supply, dispatch
from organizer.rift_era.engine.production import commit_buffers
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.errors import RuleViolation, InvariantViolation


class CityStateEconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.catalog = Catalog()

    def game(self):
        engine = opening_economy(self.catalog, initialize(self.catalog, generate_candidate(1000)), debug=False)
        state = engine.state
        city = state.city_economy
        return state, city

    def ctx(self, tick):
        return SimpleNamespace(tick=tick, cycle=tick//10, seed=1000, clock=ClockConfig(total_ticks=150))

    def tick(self, state, city, tick, *, power=False):
        ctx = self.ctx(tick)
        state.initial.inventory.materials.begin_tick(tick)
        city.begin_tick(ctx)
        if power:
            supplies = tuple(Supply('grid:'+c.id, c.hub, 'n0', 10000, Q(1)) for c in city.cities.values())
            result = dispatch(1000,tick,supplies,city.power_loads(),())
            city.record_power(ctx,result,{s.id:CashAccount('n0','corp:energy','RU') for s in supplies})
        city.produce(ctx)
        city.after_markets(ctx)
        commit_buffers(state.initial.inventory)
        city.settle(ctx)
        state.audit(); city.audit()

    def test_seeded_genesis_is_real_finite_and_not_a_player_seat(self):
        a, city = self.game()
        self.assertEqual(set(a.initial.known), {f'n{i}' for i in range(8)})
        self.assertTrue(all(10000 <= c.population.total_mU <= 20000 for c in city.cities.values()))
        self.assertTrue(all(len(c.sites) == 3 for c in city.cities.values()))
        a.audit(); city.audit()
        b, other = self.game()
        self.assertEqual(fingerprint(city.snapshot()), fingerprint(other.snapshot()))
        self.assertEqual(fingerprint(a.initial.inventory.snapshot()), fingerprint(b.initial.inventory.snapshot()))

    def test_no_actual_grid_power_means_no_mine_output_or_wages(self):
        state, city = self.game()
        c = city.cities['city-0']; treasury = state.initial.cash.balance(c.treasury).native_mU
        household = state.initial.cash.balance(c.household).native_mU
        for t in range(10): self.tick(state, city, t)
        self.assertEqual(sum(s.elapsed_output_mU for s in city.sites.values()), 0)
        self.assertLessEqual(state.initial.cash.balance(c.treasury).native_mU, treasury)
        self.assertGreaterEqual(state.initial.cash.balance(c.household).native_mU, household)
        self.assertGreater(c.last_report['deaths_mU'], 0)

    def test_actual_power_pays_suppliers_and_preserves_finite_deposits(self):
        state, city = self.game()
        money = state.initial.cash.supply('RU')
        for t in range(10): self.tick(state, city, t, power=True)
        self.assertEqual(state.initial.cash.supply('RU'), money)
        self.assertGreater(state.initial.cash.balance(CashAccount('n0','corp:energy','RU')).native_mU, 0)
        for site in city.sites.values():
            self.assertGreater(site.elapsed_output_mU, 0)
            if site.initial_mU:
                self.assertEqual(state.initial.inventory.balance(site.source)+site.elapsed_output_mU,site.initial_mU)
        self.assertGreater(city.cities['city-0'].last_report['consumed_mU'].get('water',0),0)
        self.assertEqual(state.consumption.receipt_mean20('n0','export'),0)

    def test_no_cash_stops_mining_even_with_grid(self):
        state, city = self.game()
        c = city.cities['city-0']
        state.initial.cash.transfer(c.treasury,CashAccount('world','pool','RU'),state.initial.cash.balance(c.treasury).native_mU)
        self.tick(state,city,0,power=True)
        self.assertEqual(sum(city.sites[s].elapsed_output_mU for s in c.sites),0)

    def test_real_local_purchase_consumes_foreign_origin_once(self):
        state, city = self.game()
        c = city.cities['city-0']; source=Account('n0','n0-h0','available','food')
        destination=Account('n0',c.hub,'available','food')
        # Fixture explicitly records completed physical travel before offering.
        from organizer.rift_era.engine.ledger import Transfer
        from organizer.rift_era.engine.provenance import Leg
        state.initial.inventory.transfer_many((Transfer(source,destination,1000,(Leg('n0-h0',c.hub,'n0',10),)),))
        from organizer.rift_era.engine.spot import RestingOrder
        state.initial.inventory.materials.begin_tick(0); ctx=self.ctx(0);city.begin_tick(ctx)
        venue=city._venue(c,'food')
        ask=RestingOrder('fixture','fixture','n0',venue.id,'sell',1000,venue.initial_price_mU,10,
            CashAccount('n0','reserves','RU'),destination)
        state.spot.prepare_order(ask,tick=0).commit()
        clearing=state.spot.clear(1000,0,venue.id)
        state.market_events=[clearing]
        city.after_markets(ctx);city.produce(ctx);commit_buffers(state.initial.inventory);city.settle(ctx)
        used=c.cycle_used['food']; self.assertGreater(used,0)
        self.assertGreater(c.cycle_external['n0']['vital'],0)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,clearing.paid_mU)
        self.assertEqual(state.initial.inventory.balance(Account(c.id,c.hub,'household','food')),0)
        city.audit();state.audit()

    def test_treaty_rejects_missing_local_materials_without_effects(self):
        state, city = self.game(); c=city.cities['city-0'];c.affinity['n0'].j=Q(1)
        order={'id':'1','kind':'diplomacy','op':'city_state_treaty','city_state_id':c.id,
               'treaty_kind':'resource_jv','aid_ru_mU':0}
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation): city.prepare_treaty('n0',order,self.ctx(0))
        self.assertEqual(before,fingerprint(state.snapshot()))
        self.assertFalse(city.proposals)

    def test_jv_locks_real_inputs_awards_twenty_percent_and_keeps_it_after_expiry(self):
        state,city=self.game(); c=city.cities['city-0'];c.affinity['n0'].j=Q(1)
        from organizer.rift_era.engine.ledger import Transfer
        from organizer.rift_era.engine.provenance import Leg
        for good,quantity in {'steel':800,'cement':800,'basic_parts':200}.items():
            state.initial.inventory.transfer_many((Transfer(Account('n0','n0-h0','available',good),
                Account('n0',c.hub,'available',good),quantity,(Leg('n0-h0',c.hub,'n0',10),)),))
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n0','reserves','RU'),200000)
        order={'id':'1','kind':'diplomacy','op':'city_state_treaty','city_state_id':c.id,
               'treaty_kind':'resource_jv','aid_ru_mU':0}
        city.prepare_treaty('n0',order,self.ctx(0)).commit()
        proposal=city.proposals[c.id,'n0'];sid=proposal.site
        self.assertEqual(state.authority.assets[f'{sid}/asset'].owner_shares,{c.id:1000})
        city._choose(c,self.ctx(39))
        self.assertEqual(state.authority.assets[f'{sid}/asset'].owner_shares,{c.id:800,'n0':200})
        self.assertEqual(c.expiry_cycle,44)
        self.assertEqual(state.initial.cash.balance(proposal.cash_lock).native_mU,0)
        self.assertEqual(state.initial.inventory.balance(Account('n0',c.hub,proposal.material_lock,'steel')),0)
        c.cycle_demand={g:0 for g in city.GOODS};c.cycle_used={};c.cycle_external={}
        city._end_cycle(c,self.ctx(439))
        self.assertIsNone(c.partner)
        self.assertEqual(state.authority.assets[f'{sid}/asset'].owner_shares['n0'],200)
        state.audit();city.audit()

    def test_only_supplier_gets_vital_affinity_and_cash_aid_is_not_use(self):
        state,city=self.game();c=city.cities['city-0']
        c.cycle_demand={g:1000 for g in city.GOODS};c.cycle_used={'food':1000}
        c.cycle_external={'n0':{'trade':Q(100),'vital':Q(100)}}
        city._end_cycle(c,self.ctx(9))
        self.assertGreater(c.affinity['n0'].vital,0)
        self.assertEqual(c.affinity['n1'].vital,0)
        self.assertGreater(c.affinity['n0'].j,c.affinity['n1'].j)

    def test_local_resource_sale_is_bounded_by_one_cycle_output(self):
        state,city=self.game()
        for t in range(10):self.tick(state,city,t,power=True)
        state.initial.inventory.materials.begin_tick(10);city.begin_tick(self.ctx(10))
        for c in city.cities.values():
            for sid in c.sites:
                site=city.sites[sid]
                if site.good=='water':continue
                order=state.spot.orders.get(city._order_id(c,site.good,'sell'))
                if order is not None:
                    self.assertLessEqual(order.remaining_mU,1000*self.catalog.runtime['extraction'][site.good]['output'])
        state.audit();city.audit()

    def test_local_baseline_generator_consumes_real_fuel_pays_workers_and_powers_mines(self):
        state,city=self.game();c=city.cities['city-0']
        coal=state.initial.inventory.balance(city.stock(c,'coal'))
        for t in range(10):
            ctx=self.ctx(t);state.initial.inventory.materials.begin_tick(t);city.begin_tick(ctx)
            supplies=city.power.supplies(ctx)
            result=dispatch(1000,t,supplies,city.power_loads(),())
            city.record_power(ctx,result,city.power.payees())
            city.power.complete(ctx,result)
            city.after_markets(ctx);city.produce(ctx);commit_buffers(state.initial.inventory);city.settle(ctx)
            city.audit();state.audit()
        self.assertGreater(city.power.generators[c.id].lifetime_generated_mU,0)
        self.assertGreater(sum(city.sites[s].elapsed_output_mU for s in c.sites),0)
        self.assertLess(state.initial.inventory.balance(city.stock(c,'coal')),coal)
        self.assertGreater(c.last_report['worked_mU'],0)

    def test_city_mineral_types_follow_actual_terrain_and_maintenance_has_real_pledges(self):
        state,city=self.game()
        for c in city.cities.values():
            terrain=state.initial.map.regions[state.initial.map.hubs[c.hub].region].terrain
            self.assertEqual([city.sites[s].good for s in c.sites[1:]],city.config['terrain_minerals'][terrain])
            self.assertIn(c.housing_asset,city.maintenance.targets)
            self.assertIn(city.power.generators[c.id].id,city.maintenance.targets)

    def test_aid_requires_local_stock_and_counts_only_real_end_use(self):
        state,city=self.game();c=city.cities['city-0']
        before=fingerprint(state.initial.inventory.snapshot())
        with self.assertRaises(RuleViolation):city.relations.grant_aid('n0',c.id,'food',1000,tick=0)
        self.assertEqual(before,fingerprint(state.initial.inventory.snapshot()))
        from organizer.rift_era.engine.ledger import Transfer
        from organizer.rift_era.engine.provenance import Leg
        state.initial.inventory.transfer_many((Transfer(Account('n0','n0-h0','available','food'),
            Account('n0',c.hub,'available','food'),1000,(Leg('n0-h0',c.hub,'n0',10),)),))
        city.relations.grant_aid('n0',c.id,'food',1000,tick=0)
        self.assertEqual(city.relations.aid_mean20('n0'),0)
        self.tick(state,city,0)
        self.assertGreater(city.relations.aid_mean20('n0'),0)
        self.assertEqual(state.consumption.receipt_mean20('n0','export'),0)
        city.audit();state.audit()

    def test_controlled_reexport_quota_keeps_actual_origin_and_cannot_reset_by_split(self):
        state,city=self.game();c=city.cities['city-0'];good='advanced_chip'
        from organizer.rift_era.engine.export_controls import ExportPolicy
        from organizer.rift_era.engine.materials import Origin
        account=city.stock(c,good)
        state.initial.inventory.transform({}, {account:1000},reason='component_fixture',origins={account:Origin('n0',c.hub)})
        state.export_controls.policies['n0']=ExportPolicy(frozenset({good}),frozenset({'n1','n2'}))
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n1','reserves','RU'),1000000)
        self.assertEqual(city.relations.reexport_limit(c.id,'n1',good,0),100)
        city.relations.reexport(c.id,'n1',good,60,1000,self.ctx(0))
        self.assertEqual(city.relations.reexport_limit(c.id,'n1',good,0),40)
        city.relations.reexport(c.id,'n1',good,40,1000,self.ctx(0))
        with self.assertRaises(RuleViolation):city.relations.reexport(c.id,'n1',good,1,1000,self.ctx(0))
        self.assertEqual(city.relations.reexport_limit(c.id,'n2',good,0),0)
        received=Account('n1',c.hub,'next_available',good)
        self.assertEqual(state.initial.inventory.balance(received),100)
        self.assertTrue(all(o.nation=='n0' for o in state.initial.inventory.materials.composition(received)))
        city.audit();state.audit()

    def test_local_generator_no_fuel_means_no_free_generation(self):
        state,city=self.game();c=city.cities['city-0']
        state.initial.inventory.transfer(city.stock(c,'coal'),Account('world',c.hub,'available','coal'),
            state.initial.inventory.balance(city.stock(c,'coal')))
        ctx=self.ctx(0);state.initial.inventory.materials.begin_tick(0);city.begin_tick(ctx)
        supplies=city.power.supplies(ctx)
        self.assertFalse(any(s.owner==c.id for s in supplies))
        result=dispatch(1000,0,supplies,city.power_loads(),())
        city.record_power(ctx,result,city.power.payees());city.power.complete(ctx,result)
        city.after_markets(ctx);city.produce(ctx);commit_buffers(state.initial.inventory);city.settle(ctx)
        self.assertEqual(city.power.generators[c.id].generated_mU,0)
        city.audit();state.audit()

    def test_city_maintenance_consumes_real_parts_and_missing_parts_create_arrears(self):
        state,city=self.game();c=city.cities['city-0']
        aid=f'{c.sites[0]}/asset';city.maintenance.status[aid].next_due_tick=0
        before=state.initial.inventory.balance(city.stock(c,'basic_parts'))
        self.tick(state,city,0)
        self.assertEqual(state.initial.inventory.balance(city.stock(c,'basic_parts')),before-100)
        state2,city2=self.game();c2=city2.cities['city-0'];aid2=f'{c2.sites[0]}/asset'
        city2.maintenance.status[aid2].next_due_tick=0
        account=city2.stock(c2,'basic_parts')
        state2.initial.inventory.transfer(account,Account('world',c2.hub,'available','basic_parts'),state2.initial.inventory.balance(account))
        self.tick(state2,city2,0)
        self.assertEqual(state2.initial.assets[aid2].maintenance_debt,1)
        self.assertEqual(city2.maintenance.status[aid2].arrears_mU,{'basic_parts':100})

    def test_protectorate_creates_existing_defense_obligation_without_changing_sovereignty(self):
        state,city=self.game();c=city.cities['city-0'];c.affinity['n0'].j=Q(1)
        order={'id':'1','kind':'diplomacy','op':'city_state_treaty','city_state_id':c.id,
            'treaty_kind':'protectorate','aid_ru_mU':0}
        city.prepare_treaty('n0',order,self.ctx(0)).commit();city._choose(c,self.ctx(39))
        self.assertTrue(any(set(t.parties)=={c.id,'n0'} for t in state.contracts.defense))
        self.assertEqual(state.authority.regions[state.initial.map.hubs[c.hub].region].sovereign,c.id)

    def test_port_lease_uses_ru_fee_and_does_not_authorize_other_foreign_endpoint(self):
        state,city=self.game();c=city.cities['city-0']
        edge=next(e for e in state.cargo.corridors.values() if c.hub in (e.a,e.b))
        other=edge.b if edge.a==c.hub else edge.a
        other_region=state.initial.map.hubs[other].region
        state.authority.regions[other_region].controller='n0'
        c.partner='n0';c.treaty_kind='port_lease';c.expiry_cycle=40
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n0','reserves','RU'),10000)
        before=state.initial.cash.balance(c.treasury).native_mU
        self.assertTrue(city.relations.pay_entry('n0',edge.id,1000,0))
        self.assertEqual(state.initial.cash.balance(c.treasury).native_mU-before,100)
        state.authority.regions[other_region].controller='n1'
        self.assertIsNone(city.relations.transit_right('n0',edge.id,0))

    def test_reexport_player_commands_lock_and_compete_for_one_actual_spot_sale(self):
        state,city=self.game();c=city.cities['city-0'];good='advanced_chip'
        from organizer.rift_era.engine.materials import Origin
        from organizer.rift_era.engine.city_state_economy import CityStateCommands
        stock=city.stock(c,good)
        state.initial.inventory.transform({}, {stock:1000},reason='component_fixture',origins={stock:Origin('n0',c.hub)})
        for nation in ('n1','n2'):
            state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount(nation,'reserves','RU'),1000000)
        ctx=self.ctx(0);state.initial.inventory.materials.begin_tick(0);city.begin_tick(ctx)
        commands=CityStateCommands(state)
        price=1000*10*self.catalog.weights[good]
        for nation in ('n1','n2'):
            commands.prepare(nation,{'id':'1','kind':'trade','op':'city_reexport','city_state_id':c.id,
                'good':good,'qty_mU':100,'price_ru_mU':int(price)},ctx).commit()
        fills=city.relations.clear(self.ctx(4))
        self.assertEqual(sum(row.quantity_mU for row in fills),100)
        for nation in ('n1','n2'):
            self.assertEqual(state.initial.inventory.balance(Account(nation,c.hub,'next_available',good)),50)
        city.audit();state.audit()

    def test_integrated_kernel_runs_ten_actual_ticks_with_one_genesis(self):
        engine=opening_economy(self.catalog,initialize(self.catalog,generate_candidate(1000)),total_ticks=10,debug=False,commerce=True)
        state=engine.state;city=state.city_economy;c=city.cities['city-0']
        supply=state.initial.cash.supply('RU')
        self.assertEqual(state.initial.inventory.balance(city.stock(c,'coal')),30000)
        self.assertEqual(len(city.power.generators),len(state.initial.map.city_states))
        for _ in range(10):engine.step();state.audit()
        self.assertEqual(state.initial.cash.supply('RU'),supply)
        self.assertEqual(city.last_settled_tick,9)
        self.assertGreater(city.power.generators[c.id].lifetime_generated_mU,0)
        self.assertGreater(sum(city.sites[s].elapsed_output_mU for s in c.sites),0)
        self.assertEqual(state.consumption.tick,9)
        self.assertTrue(state.observations['n0']['city_states']['cities'])
        import json
        json.dumps(state.observations)
        self.assertIsInstance(state.observations['n0']['city_states']['cities'][0]['affinity_mU']['security'],int)

    def test_occupation_uses_actual_authority_and_direct_equity_activity(self):
        state,city=self.game();c=city.cities['city-0'];region=state.initial.map.hubs[c.hub].region
        state.initial.diplomacy.phase1(0,declarations=(('n0',c.id),))
        authority=state.authority.regions[region];authority.controller='n0';authority.occupied_since_tick=0;authority.support_mU=280
        self.assertEqual(city.operation_factor(c),Q(3,5))
        sid=c.sites[1];rights=state.authority.assets[f'{sid}/asset'];rights.owner_shares={c.id:800,'n1':200}
        for t in range(10):self.tick(state,city,t,power=True)
        self.assertLessEqual(c.population.support_mU,305)
        self.assertEqual(len(city.population_scoring_rows('n0',1)),1)
        self.assertEqual(city.population_scoring_rows('n1',1),[])
        row=state.industry_activity.rows[f'{sid}/asset','n1-mining']
        self.assertEqual(row.corporate_share,Q(1,5))
        self.assertEqual(row.outputs_mU,{})

    def test_annexation_hands_population_to_controller_without_copying_cash(self):
        state,city=self.game();c=city.cities['city-0'];region=state.initial.map.hubs[c.hub].region
        authority=state.authority.regions[region];authority.controller='n0';authority.occupied_since_tick=0;authority.support_mU=650
        cash=state.initial.cash.supply('RU');population=c.population
        self.assertFalse(city.annex_if_eligible(c.id,'n0',239))
        self.assertTrue(city.annex_if_eligible(c.id,'n0',240))
        # P7 closes autonomy now; ordinary population/industry joins next P1,
        # so this cycle cannot apply demographic growth twice.
        city._handoff_annexed(c,'n0');del city.pending_handoffs[c.id]
        self.assertIs(state.initial.populations[c.hub],population)
        self.assertEqual(state.initial.map.hubs[c.hub].owner,'n0')
        self.assertEqual(state.initial.cash.supply('RU'),cash)
        self.assertEqual(state.initial.cash.balance(c.treasury).native_mU,0)
        self.assertFalse(city.annex_if_eligible(c.id,'n0',240))
        self.assertIn(c.housing_asset,state.initial.facilities)
        self.assertIn(f'{city.power.generators[c.id].id}/line',state.initial.plants)
        self.assertIn(f'{c.sites[0]}/asset',state.initial.mines)
        state.audit()

    def test_annexed_assets_continue_through_normal_kernel_without_second_population(self):
        engine=opening_economy(self.catalog,initialize(self.catalog,generate_candidate(1000)),total_ticks=10,debug=False,commerce=True)
        state=engine.state;city=state.city_economy;c=city.cities['city-0'];region=state.initial.map.hubs[c.hub].region
        authority=state.authority.regions[region];authority.controller='n0';authority.occupied_since_tick=0;authority.support_mU=650
        self.assertTrue(city.annex_if_eligible(c.id,'n0',240))
        engine.step();engine.step();state.audit()
        self.assertIs(state.initial.populations[c.hub],c.population)

    def test_repeated_phase_calls_are_rejected_and_snapshot_has_no_unbounded_log(self):
        state, city=self.game(); self.tick(state,city,0)
        with self.assertRaises(InvariantViolation): city.begin_tick(self.ctx(0))
        with self.assertRaises(InvariantViolation): city.produce(self.ctx(0))
        with self.assertRaises(InvariantViolation): city.settle(self.ctx(0))
        fingerprint(city.snapshot())


if __name__ == '__main__': unittest.main()

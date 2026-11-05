import unittest
from unittest.mock import patch

from organizer.rift_era.tests import test_orbital_operations as fixtures
from organizer.rift_era.engine.asat_operations import ASATCommands
from organizer.rift_era.engine.orbital_operations import Satellite
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation


class ASATTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.OrbitalTests.setUpClass()
        cls.helper = fixtures.OrbitalTests()

    def fixture(self):
        game = self.helper.fixture(('n0', 'n1'))
        state = game.state
        state.research['n0'].known.add('N12')
        outputs = {}
        # Explicit pre-existing satellite genesis; launch timing has its own
        # real 20-tick integration test. No full-game acceptance is implied.
        for i, berth in enumerate(((0, 0, 0), (0, 1, 0), (1, 0, 0))):
            satellite = Satellite(f'test-orbit-{i}', 'n1', 'n1-h0', berth, -100, 1000)
            state.orbit.satellites[satellite.id] = satellite
            outputs[state.orbit.satellite_account(satellite)] = 1000
        for good in state.asat.MATERIALS:
            outputs[Account('n0', 'n0-h0', 'available', good)] = 5000
        state.initial.inventory.transform({}, outputs, reason='test_initial_asat_assets',
            origins={a: Origin(a.owner, a.location) for a in outputs})
        state.orbital_observation.publish(0)
        return game

    def order(self, state, seq=1):
        target = next(r['id'] for r in state.orbital_observation.visible('n0')['contacts']
                      if r['orbit_plane'] == 0 and r['slot'] == 0)
        return {'id': str(seq), 'kind': 'military', 'op': 'asat_strike',
                'origin_hub': 'n0-h0', 'target_sat_id': target}

    def test_real_command_cost_flight_and_duplicate_hits_create_one_debris_event(self):
        game = self.fixture(); state = game.state
        credit = state.credit['n0']
        with patch('organizer.rift_era.engine.asat_operations.uniform', return_value=0):
            game.step({'n0': [self.order(state, 1), self.order(state, 2)]})
            self.assertIn('test-orbit-0', state.orbit.satellites)
            self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','hypersonic_missile')), 3000)
            self.assertEqual(state.credit['n0'], credit-Q(3,10))
            self.assertTrue(all(s.launched for s in state.asat.strikes.values()))
            game.step()
        self.assertNotIn('test-orbit-0', state.orbit.satellites)
        self.assertEqual(state.orbit.satellites['test-orbit-1'].expires_tick, 1+999*7//10)
        self.assertEqual(state.orbit.satellites['test-orbit-2'].expires_tick, 1000)
        self.assertEqual(state.asat.debris_until[0], 201)
        self.assertEqual(state.cycles['n1'].destroyed_value, state.catalog.weights['satellite'])
        self.assertEqual(state.asat.action_cycles[0]['n0'], 2*sum(state.catalog.weights[g] for g in state.asat.MATERIALS))
        self.assertTrue(all(set(row)=={'command_id','status','target_id','arrival_tick'}
                            for row in state.asat.visible('n0')['recent']))
        state.audit()

    def test_stale_contact_still_costs_ammunition_and_does_not_report_target_truth(self):
        game = self.fixture(); state = game.state
        order = self.order(state)
        # Target has legitimately left orbit after the last received snapshot.
        satellite = state.orbit.satellites.pop('test-orbit-0')
        state.initial.inventory.transform({state.orbit.satellite_account(satellite):1000},{},reason='test_target_retirement')
        ASATCommands(state).prepare('n0', order, self.helper.ctx(0)).commit()
        with patch('organizer.rift_era.engine.asat_operations.uniform', return_value=0):
            state.asat.advance(self.helper.ctx(0))
            state.asat.advance(self.helper.ctx(1))
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','hypersonic_missile')),4000)
        self.assertEqual(state.asat.debris_until[0],0)
        self.assertEqual(state.asat.visible('n0')['recent'][0]['status'],'ARRIVED')
        state.audit()

    def test_hidden_id_and_unfunded_shot_reject_without_mutation(self):
        state = self.fixture().state; handler=ASATCommands(state)
        for order in (self.order(state)|{'target_sat_id':'test-orbit-0'},
                      self.order(state)|{'origin_hub':'n0-h1'}):
            before=fingerprint(state.snapshot())
            with self.assertRaises(RuleViolation):handler.prepare('n0',order,self.helper.ctx(0))
            self.assertEqual(before,fingerprint(state.snapshot()))

    def test_declared_war_avoids_nonwar_penalty_and_miss_does_not_make_debris(self):
        game=self.fixture();state=game.state;credit=state.credit['n0']
        with patch('organizer.rift_era.engine.asat_operations.uniform',return_value=.9):
            game.step({'n0':[{'id':'1','kind':'diplomacy','op':'declare_war','target_nation':'n1'}, self.order(state,2)]})
            game.step()
        self.assertEqual(state.credit['n0'],credit)
        self.assertIn('test-orbit-0',state.orbit.satellites)
        self.assertEqual(state.asat.debris_until[0],0)
        self.assertEqual(state.asat.new_lifetime(0,20,1800),1800)
        state.asat.debris_until[0]=201
        self.assertEqual(state.asat.new_lifetime(0,200,1800),1530)
        self.assertEqual(state.asat.new_lifetime(0,201,1800),1800)
        self.assertEqual(state.asat.new_lifetime(1,20,1800),1800)
        state.audit()

    def test_new_launch_uses_debris_lifetime_in_real_phase_chain(self):
        game=self.fixture();state=game.state
        with patch('organizer.rift_era.engine.asat_operations.uniform',return_value=0):
            game.step({'n0':[self.order(state)]})
            for _ in range(4):game.step()
        command=self.helper.order('n1',seq=1,slot=3)
        game.step({'n1':[command]})
        for _ in range(19):game.step()
        fresh=next(s for s in state.orbit.satellites.values() if s.berth==(0,3,0))
        self.assertEqual(fresh.commissioned_tick,25)
        self.assertEqual(fresh.expires_tick,25+1530)
        state.audit()

    def test_two_distinct_destroyed_targets_apply_two_simultaneous_debris_events(self):
        game=self.fixture();state=game.state
        survivor=Satellite('extra-survivor','n1','n1-h0',(0,2,0),-100,1000)
        account=state.orbit.satellite_account(survivor)
        state.initial.inventory.transform({}, {account:1000},reason='test_initial_extra_satellite',
                                          origins={account:Origin('n1',survivor.id)})
        state.orbit.satellites[survivor.id]=survivor
        state.orbital_observation.publish(0)
        alias=next(r['id'] for r in state.orbital_observation.visible('n0')['contacts']
                   if r['orbit_plane']==0 and r['slot']==1)
        with patch('organizer.rift_era.engine.asat_operations.uniform',return_value=0):
            game.step({'n0':[self.order(state),self.order(state,2)|{'target_sat_id':alias}]})
            game.step()
        self.assertEqual(survivor.expires_tick,1+999*49//100)
        self.assertEqual(state.cycles['n1'].destroyed_value,2*state.catalog.weights['satellite'])
        state.audit()

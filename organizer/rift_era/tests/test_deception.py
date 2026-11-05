import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.deception import IntelligenceCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.world import generate_candidate


class DeceptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,debug=False,commerce=True)
        state=game.state
        state.research['n1'].known.add('S06')
        account=Account('n1','n1-h0','available','cyber_suite')
        state.initial.inventory.transform({}, {account:500},reason='test_initial_deception_stock',
            origins={account:Origin('n1','n1-h0')})
        return game,account

    def order(self,seq=1):
        return {'id':str(seq),'kind':'intelligence','op':'intel_op','op_kind':'S06_decoy',
                'target_region':self.world.hubs['n0-h0'].region,'reported_strength_mU':100000,'reported_type':'motorized'}

    def deploy(self,state,seq=1):
        ctx=NS(seed=1000,tick=0,window=0,cycle=0,clock=ClockConfig())
        before=set(state.deception.rows)
        IntelligenceCommands(state).prepare('n1',self.order(seq),ctx).commit()
        created,=set(state.deception.rows)-before
        return state.deception.rows[created]

    def test_cap_cost_pure_rejection_lifetime_and_no_free_credit(self):
        game,account=self.fixture();state=game.state
        for seq in range(1,5):self.deploy(state,seq)
        self.assertEqual(state.initial.inventory.balance(account),100)
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):self.deploy(state,5)
        self.assertEqual(before,fingerprint(state.snapshot()))
        self.assertEqual(state.deception.action_cycles[0]['n1'],Q(400,1000)*state.catalog.weights['cyber_suite'])
        state.deception.begin_tick(NS(window=9,cycle=4))
        self.assertEqual(len(state.deception.rows),4)
        state.deception.begin_tick(NS(window=10,cycle=5))
        self.assertFalse(state.deception.rows)
        state.audit()

    def test_once_per_window_exposure_and_s04_credit_penalty_once(self):
        game,_=self.fixture();state=game.state;row=self.deploy(state)
        state.research['n0'].known.add('S04')
        credit=state.credit['n1']
        with patch('organizer.rift_era.engine.deception.uniform',return_value=.9) as draw:
            state.vision._observe(1,0)
            alias=next(r['id'] for r in state.vision.visible('n0')['tracks'] if r['owner']=='n1')
            calls=draw.call_count
            for tick in range(2,5):state.vision._observe(tick,0)
            self.assertEqual(draw.call_count,calls)
        self.assertEqual(state.credit['n1'],credit)
        with patch('organizer.rift_era.engine.deception.uniform',return_value=.5):
            state.vision._observe(6,1)
        self.assertIn(alias,state.vision.visible('n0')['decoy_exposed'])
        self.assertFalse(any(r['id']==alias for r in state.vision.visible('n0')['tracks']))
        self.assertNotIn(alias,state.vision.visible('n0')['deleted_ids'])
        self.assertEqual(state.credit['n1'],max(Q(1,10),credit-Q(3,100)))
        with patch('organizer.rift_era.engine.deception.uniform',return_value=0):
            state.vision._observe(7,1)
            # Repeated exposures by another observer cannot multiply the penalty.
            state.research['n2'].known.add('S04')
            state.deception.investigate('n2',row.id,1,'contact-test')
        self.assertEqual(state.credit['n1'],max(Q(1,10),credit-Q(3,100)))
        self.assertFalse(state.battles.contacts()[next(u for u in state.initial.units if state.initial.units[u].owner=='n0')])

    def test_decoy_draws_real_fire_without_physical_damage_or_rewards(self):
        game,account=self.fixture();state=game.state
        state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        before_hp={u:v.hp_mU for u,v in state.initial.units.items()}
        ammo=Account('n0','n0-h0','available','ammunition')
        before_ammo=state.initial.inventory.balance(ammo)
        with patch('organizer.rift_era.engine.deception.uniform',return_value=.99):
            game.step({'n1':[self.order()]})
        result=state.battles.last_result
        self.assertTrue(result.shots)
        target,=state.deception.rows
        self.assertTrue(all(s.target==target for s in result.shots))
        self.assertLess(state.initial.inventory.balance(ammo),before_ammo)
        self.assertEqual({u:v.hp_mU for u,v in state.initial.units.items()},before_hp)
        self.assertEqual(result.damage_mU[target],0)
        self.assertEqual(result.casualties_mU[target],0)
        self.assertNotIn(target,state.initial.units)
        self.assertFalse(state.battles.destroyed_cycles[0])
        self.assertGreater(state.battles.loss_cycles[0]['n0'],0)
        self.assertEqual(state.initial.inventory.balance(account),400)
        row=next(r for r in state.vision.visible('n0')['tracks'] if r['owner']=='n1')
        self.assertNotIn('decoy',str(row))
        self.assertNotIn('hiddenflag',row)
        state.audit()

    def test_first_contact_order_does_not_sort_false_signals_before_real_units(self):
        from dataclasses import replace
        from organizer.rift_era.engine.military_vision import MilitaryVision
        game,_=self.fixture();state=game.state
        real=next(f for f in state.formations.rows.values() if f.owner=='n1')
        real.hub='n0-h0';real.region=self.world.hubs[real.hub].region
        decoy=self.deploy(state)
        old_map=state.initial.map
        orders=set()
        try:
            with patch('organizer.rift_era.engine.deception.uniform',return_value=.99):
                for seed in range(1000,1016):
                    state.initial.map=replace(old_map,seed=seed)
                    vision=MilitaryVision(state)
                    aliases=vision.observers['n0']._aliases
                    real_alias=aliases[vision.current['n0'][real.id]]
                    false_alias=aliases[vision.current['n0'][decoy.id]]
                    orders.add(int(real_alias.split('-')[1])<int(false_alias.split('-')[1]))
        finally:state.initial.map=old_map
        self.assertEqual(orders,{False,True})

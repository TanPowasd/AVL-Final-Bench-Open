import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.military_refits import RefitCommands
from organizer.rift_era.engine.formations import FormationCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.numeric import Q


class MilitaryRefitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)
        state=game.state
        formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
        state.research['n0'].known.update({'G01','I04'})
        stock={Account('n0','n0-h0','available',g):q for g,q in {'communication_equipment':1000,'composite':2000}.items()}
        state.initial.inventory.transform({},stock,reason='test_initial_refit_parts',origins={a:Origin('n0','n0-h0') for a in stock})
        return game,formation

    def order(self,formation):
        return {'id':'1','kind':'military','op':'refit_formation','formation_id':formation.id,'slot_a':'radio','slot_b':'composite'}

    def test_two_cycle_shutdown_and_real_modules_then_disband_does_not_duplicate_buffs(self):
        game,formation=self.fixture();state=game.state
        game.step({'n0':[self.order(formation)]})
        self.assertTrue(formation.refitting)
        self.assertIsNone(formation.slot_a)
        self.assertTrue(all(state.initial.units[u].armor_bonus==0 for u in formation.unit_ids))
        for _ in range(18):game.step()
        self.assertTrue(formation.refitting)
        game.step()
        self.assertFalse(formation.refitting)
        self.assertEqual((formation.slot_a,formation.slot_b),('radio','composite'))
        for uid in formation.unit_ids:
            self.assertEqual(state.initial.units[uid].armor_bonus,8)
            self.assertEqual(state.initial.units[uid].speed_factor,Q(23,20))
            self.assertEqual(state.initial.units[uid].fuel_factor,Q(17,20))
        self.assertEqual(state.initial.inventory.balance(Account('n0',formation.id,'refit:A','communication_equipment')),1000)
        commands=FormationCommands(state);ctx=NS(tick=20,clock=ClockConfig())
        commands.prepare('n0',{'id':'2','kind':'military','op':'disband','formation_id':formation.id},ctx).commit()
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','communication_equipment')),1000)
        commands.prepare('n0',{'id':'3','kind':'military','op':'deploy_formation','hub_id':'n0-h0',
                              'domain':'Land','units':{'motorized':1}},ctx).commit()
        fresh=next(f for f in state.formations.rows.values() if f.id.startswith('formation:'))
        self.assertIsNone(fresh.slot_b)
        self.assertEqual(state.initial.units[fresh.unit_ids[0]].armor_bonus,0)
        state.audit()

    def test_rejected_refit_is_pure_and_cancel_refunds_uninstalled_hardware(self):
        game,formation=self.fixture();state=game.state;handler=RefitCommands(state)
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            handler.prepare('n0',dict(self.order(formation),slot_a='ew'),NS(tick=0,clock=ClockConfig()))
        self.assertEqual(before,fingerprint(state.snapshot()))
        handler.prepare('n0',self.order(formation),NS(tick=0,clock=ClockConfig())).commit()
        state.refits.prepare_cancel('n0','1').commit()
        self.assertFalse(formation.refitting)
        self.assertIsNone(formation.slot_a)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','communication_equipment')),1000)
        self.assertFalse(state.refits.jobs)
        state.audit()

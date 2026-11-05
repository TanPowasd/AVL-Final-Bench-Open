import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.formations import FormationCommands
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.errors import RuleViolation


class FormationDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog, initialize(self.catalog,self.world), total_ticks=10, commerce=True, debug=False)

    def order(self, **changes):
        return dict({'id':'1','kind':'military','op':'deploy_formation','hub_id':'n0-h0',
                     'domain':'Land','units':{'motorized':1}}, **changes)

    def test_real_equipment_and_people_leave_civilian_pools_atomically(self):
        game = self.game(); state = game.state
        population = state.initial.populations['n0-h0']
        before_people = population.total_mU
        inv = state.initial.inventory
        before_vehicle = inv.balance(Account('n0','n0-h0','available','vehicle'))
        count = len(state.initial.units)
        game.step({'n0':[self.order()]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status, 'ACCEPTED')
        self.assertEqual(population.total_mU, before_people-2000)
        self.assertEqual(inv.balance(Account('n0','n0-h0','available','vehicle')),before_vehicle-1000)
        self.assertEqual(len(state.initial.units),count+1)
        formation = next(f for f in state.formations.rows.values() if f.id.startswith('formation:'))
        unit = state.initial.units[formation.unit_ids[0]]
        self.assertEqual(unit.hp_mU,120000)
        self.assertEqual(unit.morale*1000,860)  # P4 full-fed peaceful recovery follows deployment
        self.assertEqual(formation.crew_skills[unit.id]['general_worker'],2000)
        state.audit()

    def test_prepared_deployment_starts_at_exact_850_morale(self):
        state=self.game().state
        ready=FormationCommands(state).prepare('n0',self.order(),NS(tick=0,clock=ClockConfig()))
        ready.commit()
        formation=next(f for f in state.formations.rows.values() if f.id.startswith('formation:'))
        self.assertEqual(state.initial.units[formation.unit_ids[0]].morale*1000,850)
        state.audit()

    def test_invalid_domain_stock_and_crew_assertions_have_no_side_effects(self):
        state = self.game().state
        handler = FormationCommands(state)
        ctx = NS(tick=0,clock=ClockConfig())
        for order in (self.order(domain='Air'), self.order(units={'motorized':4}),
                      self.order(crew_mU=1), self.order(units={'not-a-unit':1})):
            before = fingerprint(state.snapshot())
            with self.assertRaises(RuleViolation):
                handler.prepare('n0',order,ctx)
            self.assertEqual(before,fingerprint(state.snapshot()))

    def test_remote_garrison_cannot_eat_capital_food(self):
        game = self.game(); state = game.state
        formation = next(f for f in state.formations.rows.values() if f.owner=='n0')
        formation.hub='n0-h1'; formation.region=state.initial.map.hubs['n0-h1'].region
        for uid in formation.unit_ids:
            state.initial.units[uid].region=formation.region
        game.step()
        self.assertEqual(state.frame.military[formation.id], {'food':0,'water':0})
        self.assertEqual(state.frame.military_context[formation.id][1], 'n0-h1')
        state.audit()

    def test_initial_equipment_origin_remains_actual_starting_hub(self):
        state = self.game().state
        unit=next(iter(state.initial.units.values()))
        good=next(iter(self.catalog.units[unit.unit_type]['installed']))
        account=state.formations.equipment_account(unit.owner,unit.id,good)
        origins=state.initial.inventory.materials.composition(account)
        self.assertTrue(origins)
        self.assertTrue(all(o.hub==f'{unit.owner}-h0' for o in origins))

    def test_disband_and_redeploy_preserve_equipment_health_and_return_exact_skills(self):
        from fractions import Fraction as Q
        state=self.game().state
        handler=FormationCommands(state)
        ctx=NS(tick=0,clock=ClockConfig())
        pop=state.initial.populations['n0-h0']
        before=dict(pop.skills_mU)
        handler.prepare('n0',self.order(),ctx).commit()
        formation=next(f for f in state.formations.rows.values() if f.id.startswith('formation:'))
        uid=formation.unit_ids[0]
        unit=state.initial.units[uid]
        unit.hp_mU=55000
        unit.fatigue=Q(1,2)
        available=state.initial.inventory.balance(Account('n0','n0-h0','available','vehicle'))
        cmd={'id':'2','kind':'military','op':'disband','formation_id':formation.id}
        snapshot=fingerprint(state.snapshot())
        ready=handler.prepare('n0',cmd,ctx)
        self.assertEqual(snapshot,fingerprint(state.snapshot()))
        ready.commit()
        self.assertEqual(pop.skills_mU,before)
        self.assertNotIn(uid,state.initial.units)
        self.assertEqual(state.formations.stored_units[uid].unit.hp_mU,55000)
        handler.prepare('n0',self.order(id='3'),ctx).commit()
        self.assertEqual(state.initial.units[uid].hp_mU,55000)
        self.assertEqual(state.initial.units[uid].fatigue,Q(1,2))
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','vehicle')),available)
        self.assertNotIn(uid,state.formations.stored_units)
        state.audit()

    def test_stale_or_foreign_disband_cannot_release_people_or_equipment(self):
        state=self.game().state
        formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
        handler=FormationCommands(state)
        for owner,extra in (('n1',{}),('n0',{'expected_version':formation.version+1})):
            before=fingerprint(state.snapshot())
            with self.assertRaises(RuleViolation):
                handler.prepare(owner,{'id':'1','kind':'military','op':'disband',
                                      'formation_id':formation.id,**extra},NS(tick=0,clock=ClockConfig()))
            self.assertEqual(before,fingerprint(state.snapshot()))

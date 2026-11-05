import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.combat import Unit,CombatResult,apply_result
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.military_movement import MarchPlan
from organizer.rift_era.engine.ledger import Account,Transfer
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.errors import InvariantViolation


class RetreatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)
        state=game.state
        formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
        state.movement.plans[formation.id]=MarchPlan('n0','1',formation.id,'retreat',formation.region,'n0-h0',500,
            sum(state.initial.units[u].hp_mU for u in formation.unit_ids),(),(),auto_retreat=True)
        return state,formation

    def load(self,state,formation,good,quantity,bucket='available'):
        account=Account('n0',formation.id,bucket,good)
        state.initial.inventory.transform({}, {account:quantity},reason='test_initial_retreat_stock',
                                          origins={account:Origin('n0','n0-h0')})
        return account

    def test_only_available_supplies_are_lost_and_radio_requires_actual_installation(self):
        for installed,expected in ((False,200),(True,80)):
            state,formation=self.fixture()
            formation.slot_a='radio'
            if installed:self.load(state,formation,'communication_equipment',1000,'refit:A')
            food=self.load(state,formation,'food',1000)
            protected=self.load(state,formation,'food',100,'locked_military:test')
            spare_vehicle=self.load(state,formation,'vehicle',1000)
            crew={u:state.initial.units[u].crew_mU for u in formation.unit_ids}
            hp={u:state.initial.units[u].hp_mU for u in formation.unit_ids}
            used=state.movement.abandon_supplies(NS(seed=1,tick=0),[formation])
            self.assertEqual(state.initial.inventory.balance(food),1000-expected)
            self.assertEqual(state.initial.inventory.balance(protected),100)
            self.assertEqual(state.initial.inventory.balance(spare_vehicle),1000)
            self.assertEqual(sum(row.get('food',0) for row in used.values()),expected)
            self.assertEqual({u:state.initial.units[u].crew_mU for u in formation.unit_ids},crew)
            self.assertEqual({u:state.initial.units[u].hp_mU for u in formation.unit_ids},hp)
            with self.assertRaises(InvariantViolation):
                state.movement.abandon_supplies(NS(seed=1,tick=0),[formation])
            if not installed:formation.slot_a=None  # restore the intentionally forged fixture flag
            state.audit()

    def test_whole_supply_loss_keeps_fractional_remainder_between_retreats(self):
        state,formation=self.fixture()
        spare=self.load(state,formation,'hypersonic_missile',1000)
        for tick in range(5):
            state.movement.plans[formation.id].abandoned=False
            state.movement.abandon_supplies(NS(seed=1,tick=tick),[formation])
            self.assertEqual(state.initial.inventory.balance(spare),1000 if tick<4 else 0)
        state.audit()

    def test_normal_marching_or_hunger_does_not_recover_fatigue(self):
        troops={name:Unit(name,'A','infantry','r',100000,1000,fatigue=Q(1,2)) for name in ('moving','hungry','resting')}
        result=CombatResult((),dict.fromkeys(troops,0),dict.fromkeys(troops,0),{}, {},frozenset())
        apply_result(troops,result,moving=frozenset({'moving'}),food_water_short=frozenset({'hungry'}))
        self.assertEqual(troops['moving'].fatigue,Q(1,2))
        self.assertEqual(troops['hungry'].fatigue,Q(1,2))
        self.assertEqual(troops['resting'].fatigue,Q(45,100))

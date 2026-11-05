import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economic_ownership import EconomicOwner
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.formations import FormationCommands
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account,CashAccount
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.military_refits import RefitCommands
from organizer.rift_era.engine.numeric import Q,floor
from organizer.rift_era.engine.sovereign_finance import SovereignBond


class MilitaryProcurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from organizer.rift_era.engine.world import generate_candidate
        cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)

    def supply(self,state,good,quantity,sector):
        source=Account('n0','n0-h0','available',good);stock=state.initial.inventory
        stock.transfer(source,Account('n0','n0-h0','test_sequestered',good),stock.balance(source))
        stock.transform({}, {source:quantity},reason='explicit_military_supplier_fixture',
            origins={source:Origin('n0','n0-h0',economic_owner=EconomicOwner('n0','corp:'+sector))})
        return source

    def context(self,tick=0):return NS(seed=1000,tick=tick,cycle=tick//10,clock=ClockConfig())

    def deploy(self):
        return {'id':'1','kind':'military','op':'deploy_formation','hub_id':'n0-h0','domain':'Land','units':{'motorized':1}}

    def test_deployment_pays_actual_company_and_government_owns_installed_equipment(self):
        state=self.game().state;source=self.supply(state,'vehicle',1000,'manufacturing')
        treasury=state.native('n0','treasury');before=state.initial.cash.balance(treasury).native_mU
        prepared=FormationCommands(state).prepare('n0',self.deploy(),self.context())
        self.assertEqual(state.initial.inventory.balance(source),1000)
        self.assertEqual(state.initial.cash.balance(state.native('n0','corp:manufacturing')).native_mU,0)
        prepared.commit()
        price=floor(1000*state.price('vehicle','n0-h0'))
        self.assertEqual(state.initial.cash.balance(treasury).native_mU,before-price)
        self.assertEqual(state.initial.cash.balance(state.native('n0','corp:manufacturing')).native_mU,price)
        new=next(f for f in state.formations.rows.values() if f.owner=='n0' and f.id.startswith('formation:'))
        unit=state.initial.units[new.unit_ids[0]]
        stock=state.initial.inventory.materials.composition(state.formations.equipment_account('n0',unit.id,'vehicle'))
        self.assertTrue(all(o.economic_owner is None for o in stock))
        state.audit()

    def test_unfunded_company_equipment_does_not_take_people_or_stock(self):
        state=self.game().state;self.supply(state,'vehicle',1000,'manufacturing')
        treasury=state.native('n0','treasury')
        state.initial.cash.transfer(treasury,CashAccount('world','test_sequestered','n0'),state.initial.cash.balance(treasury).native_mU)
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):FormationCommands(state).prepare('n0',self.deploy(),self.context())
        self.assertEqual(before,fingerprint(state.snapshot()))

    def test_refit_escrow_pays_only_at_install_and_cancel_restores_original_title(self):
        for finish in (False,True):
            state=self.game().state;source=self.supply(state,'communication_equipment',1000,'tech')
            formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
            state.research['n0'].known.add('G01')
            treasury=state.native('n0','treasury');before=state.initial.cash.balance(treasury).native_mU
            order={'id':'1','kind':'military','op':'refit_formation','formation_id':formation.id,'slot_a':'radio','slot_b':None}
            RefitCommands(state).prepare('n0',order,self.context()).commit()
            self.assertEqual(state.initial.cash.balance(state.native('n0','corp:tech')).native_mU,0)
            job,=state.refits.jobs.values()
            self.assertGreater(state.initial.cash.balance(job.cash_lock()).native_mU,0)
            if finish:
                state.refits.advance(self.context(19))
                self.assertEqual(formation.slot_a,'radio')
                self.assertGreater(state.initial.cash.balance(state.native('n0','corp:tech')).native_mU,0)
                installed=state.initial.inventory.materials.composition(state.refits.slot_account(formation,'A','communication_equipment'))
                self.assertTrue(all(o.economic_owner is None for o in installed))
            else:
                state.refits.prepare_cancel('n0','1').commit()
                self.assertEqual(state.initial.cash.balance(treasury).native_mU,before)
                self.assertEqual(state.initial.inventory.balance(source),1000)
                self.assertTrue(all(o.economic_owner==EconomicOwner('n0','corp:tech') for o in state.initial.inventory.materials.composition(source)))
            state.audit()

    def test_mature_debt_reservation_precedes_current_military_wage_reservation(self):
        game=self.game();state=game.state
        for _ in range(9):game.step()
        bond=SovereignBond('priority-claim','n0',-1,0,0,{'world':1000},Q(1000))
        state.sovereign_finance.bonds[bond.id]=bond;state.bond_market.register(bond)
        state.sovereign_finance.publish()
        treasury=state.native('n0','treasury');cash=state.initial.cash
        cash.transfer(treasury,CashAccount('world','test_priority','n0'),cash.balance(treasury).native_mU-1000)
        reserves=CashAccount('n0','reserves','RU')
        cash.transfer(reserves,CashAccount('world','test_priority','RU'),cash.balance(reserves).native_mU)
        game.step()
        self.assertEqual(state.sovereign_finance.reports['n0']['paid_mU'],1000)
        self.assertFalse(state.sovereign_finance.reports['n0']['defaulted'])
        self.assertGreaterEqual(state.history['n0'][-1].unpaid_wages_mU,9000)
        state.audit()

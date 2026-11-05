import unittest
from unittest.mock import patch

from organizer.rift_era.engine.ai import Model
from organizer.rift_era.engine.ai_licenses import AILicenseCommands
from organizer.rift_era.engine.ai_operations import AICommands
from organizer.rift_era.engine.commands import EngineCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.export_controls import ExportPolicy
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class AILicenseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compute_fixture.ComputeOperationsTests.setUpClass()
        cls.helper=compute_fixture.ComputeOperationsTests();cls.catalog=cls.helper.catalog

    def fixture(self):
        state,_=self.helper.fixture()
        for owner in ('n0','n1','n7'):state.research[owner].known.update({'AI01','AI02'})
        state.ai.models['n7']['source']=Model('source','n7',0,Q(10000),Q(10000))
        state.ai.active['n7']='source'
        state.ai.data['n7'].curated_human=Q(20)
        state.ai.data['n7'].synthetic=Q(5000)
        state.ai.data['n7'].licensed_from['n2']=Q(10000)
        return state

    def offer(self,seq=1,kind='weights',price=1000,target=None):
        return {'id':str(seq),'kind':'ai','op':'license_offer','license_kind':kind,
                'price_ru_mU':price,'target':target}

    def accept(self,seq=1,kind='weights'):
        return {'id':str(seq),'kind':'ai','op':'license_accept','license_kind':kind,
                'seller':'n7','max_price_ru_mU':1000}

    def publish(self,state,kind='weights',price=1000):
        AILicenseCommands(state).prepare('n7',self.offer(kind=kind,price=price),self.helper.ctx(0)).commit()
        state.ai_licenses.publish()

    def fund(self,state,owner,amount):
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount(owner,'reserves','RU'),amount)

    def test_weights_copy_uses_source_effective_compute_and_actual_ru_payment(self):
        state=self.fixture();self.publish(state);self.fund(state,'n0',1000)
        handler=AILicenseCommands(state);before=fingerprint(state.snapshot())
        prepared=handler.prepare('n0',self.accept(),self.helper.ctx(0))
        self.assertEqual(before,fingerprint(state.snapshot()))
        prepared.commit()
        self.assertFalse(state.ai.models['n0'])
        self.assertEqual(state.initial.cash.balance(CashAccount('n7','reserves','RU')).native_mU,0)
        state.ai_licenses.settle(self.helper.ctx(0))
        model,=state.ai.models['n0'].values()
        self.assertAlmostEqual(float(model.effective_compute),3215.369133,places=5)
        self.assertEqual(model.raw_compute,model.effective_compute)
        self.assertEqual(state.initial.cash.balance(CashAccount('n7','reserves','RU')).native_mU,1000)
        self.assertIsNone(state.ai.active['n0'])
        state.audit()

    def test_data_is_source_human_only_deduplicated_with_100_cycle_cooldown(self):
        state=self.fixture();self.publish(state,kind='data',price=0)
        handler=AILicenseCommands(state)
        handler.prepare('n0',self.accept(kind='data'),self.helper.ctx(0)).commit()
        with self.assertRaises(RuleViolation):handler.prepare('n0',self.accept(seq=2,kind='data'),self.helper.ctx(0))
        state.ai_licenses.settle(self.helper.ctx(0))
        self.assertEqual(state.ai.data['n0'].licensed_from,{'n7':10})
        with self.assertRaises(RuleViolation):handler.prepare('n0',self.accept(seq=3,kind='data'),self.helper.ctx(99))
        handler.prepare('n0',self.accept(seq=4,kind='data'),self.helper.ctx(100)).commit()
        state.ai_licenses.settle(self.helper.ctx(100))
        self.assertEqual(state.ai.data['n0'].licensed_from,{'n7':10})
        state.ai.data['n7'].curated_human=Q(40);state.ai_licenses.publish()
        handler.prepare('n0',self.accept(seq=5,kind='data'),self.helper.ctx(200)).commit()
        state.ai_licenses.settle(self.helper.ctx(200))
        self.assertEqual(state.ai.data['n0'].licensed_from,{'n7':20})
        state.audit()

    def test_unpublished_offer_and_target_privacy_and_pure_queries(self):
        state=self.fixture();handler=AILicenseCommands(state)
        handler.prepare('n7',self.offer(target='n0'),self.helper.ctx(0)).commit()
        with self.assertRaises(RuleViolation):handler.prepare('n0',self.accept(),self.helper.ctx(0))
        state.ai_licenses.publish()
        self.assertTrue(state.ai_licenses.visible('n0')['offers'])
        self.assertFalse(state.ai_licenses.visible('n1')['offers'])
        before=fingerprint(state.snapshot())
        for _ in range(5):state.ai_licenses.visible('n0')
        self.assertEqual(before,fingerprint(state.snapshot()))
        with self.assertRaises(RuleViolation):handler.prepare('n1',self.accept(),self.helper.ctx(0))
        self.assertNotIn('source',repr(state.ai_licenses.visible('n0')))

    def test_later_export_policy_refunds_escrow_without_granting_model(self):
        state=self.fixture();self.publish(state);self.fund(state,'n0',1000)
        AILicenseCommands(state).prepare('n0',self.accept(),self.helper.ctx(0)).commit()
        state.export_controls.policies['n7']=ExportPolicy(frozenset({'model_weights'}),frozenset({'n0'}))
        state.ai_licenses.settle(self.helper.ctx(0))
        self.assertFalse(state.ai.models['n0'])
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,1000)
        self.assertEqual(state.initial.cash.balance(CashAccount('n7','reserves','RU')).native_mU,0)
        self.assertEqual(state.ai_licenses.recent['n0'][-1]['status'],'BLOCKED_REFUNDED')
        state.audit()

    def test_pending_copy_reserves_model_slot_before_training_can_start(self):
        state=self.fixture();self.publish(state,price=0)
        state.ai.models['n0']={str(i):Model(str(i),'n0',0,Q(0),Q(0)) for i in range(15)}
        AILicenseCommands(state).prepare('n0',self.accept(),self.helper.ctx(0)).commit()
        self.assertEqual(state.ai.model_slots_used('n0'),16)
        train={'id':'2','kind':'ai','op':'train','hub_id':'n0-h0','alloc_compute_mU':1000,
               'cycles':5,'base_model':None,'max_price_mU':1000}
        with self.assertRaises(RuleViolation):AICommands(state).prepare('n0',train,self.helper.ctx(0))
        with self.assertRaises(RuleViolation):AILicenseCommands(state).prepare('n0',self.accept(seq=3),self.helper.ctx(0))
        state.ai_licenses.settle(self.helper.ctx(0))
        self.assertEqual(len(state.ai.models['n0']),16)
        state.audit()

    def test_licensor_proceeds_cannot_finance_its_own_same_batch_purchase(self):
        state=self.fixture();self.publish(state);self.fund(state,'n0',1000)
        state.ai.models['n1']['other']=Model('other','n1',0,Q(1),Q(1));state.ai.active['n1']='other'
        handler=AILicenseCommands(state)
        handler.prepare('n1',self.offer(),self.helper.ctx(0)).commit();state.ai_licenses.publish()
        handler.prepare('n0',self.accept(),self.helper.ctx(0)).commit()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            handler.prepare('n7',self.accept()|{'seller':'n1'},self.helper.ctx(0))
        self.assertEqual(before,fingerprint(state.snapshot()))
        state.ai_licenses.settle(self.helper.ctx(0));state.audit()

    def test_real_window_acceptance_is_paid_and_cancellable_offer_is_removed(self):
        game=opening_economy(self.catalog,self.helper.initial(),total_ticks=10,commerce=True,debug=False)
        state=game.state
        for p in ('n0','n7'):state.research[p].known.add('AI01')
        state.ai.data['n7'].curated_human=Q(20);self.fund(state,'n0',1000)
        with patch('organizer.rift_era.engine.ai_operations.uniform',return_value=.999):
            game.step({'n7':[self.offer(kind='data')]})
            for _ in range(4):game.step()
            game.step({'n0':[self.accept(kind='data')]})
            self.assertEqual(state.ai.data['n0'].licensed_from['n7'],10)
            self.assertEqual(state.initial.cash.balance(CashAccount('n7','reserves','RU')).native_mU,1000)
        commands=EngineCommands(state)
        commands.prepare('n7',{'id':'2','kind':'cancel','op':'cancel_by_id','target_id':'1'},self.helper.ctx(0)).commit()
        self.assertFalse(state.ai_licenses.offers)
        state.ai_licenses.publish()
        self.assertFalse(state.ai_licenses.visible('n0')['offers'])
        state.audit()

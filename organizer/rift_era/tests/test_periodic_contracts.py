import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests import test_contracts as license_tests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.determinism import fingerprint


class PeriodicContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):license_tests.LicenseContractTests.setUpClass()

    def fixture(self):
        state=license_tests.LicenseContractTests().fixture().state
        source=Account('n1','n0-h0','available','food')
        own=Account('n0','n0-h0','available','food')
        state.initial.inventory.transfer(own,source,1000)
        return state,ContractCommands(state)

    def offer(self):
        return {'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':1,'duration_cycles':10,
            'params':{'hub_a':'n0-h0','hub_b':'n1-h0','good_a':'food','qty_a_mU':1000,'good_b':'water','qty_b_mU':1000,'period_cycles':2}}

    def test_escrowed_first_period_exchanges_at_actual_depots_without_transport(self):
        state,handler=self.fixture();ctx=NS(tick=0,cycle=0)
        handler.prepare('n0',self.offer(),ctx).commit();state.contracts.publish()
        alias=state.contracts.visible('n1')['proposals'][0]['id']
        before_a=state.initial.inventory.balance(Account('n1','n0-h0','available','food'))
        before_b=state.initial.inventory.balance(Account('n0','n1-h0','available','water'))
        handler.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        self.assertEqual(state.initial.inventory.balance(Account('n1','n0-h0','available','food')),before_a)
        state.contracts.settle(ctx)
        self.assertEqual(state.initial.inventory.balance(Account('n1','n0-h0','available','food')),before_a+1000)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n1-h0','available','water')),before_b+1000)
        row=state.contracts.swaps.visible('n0')[0]
        self.assertEqual(row.delivered_periods,1)
        self.assertEqual(row.next_cycle,2)
        state.audit()

    def test_missing_one_obligation_refuses_whole_signature_without_debit(self):
        state,handler=self.fixture();ctx=NS(tick=0,cycle=0)
        water=Account('n1','n1-h0','available','water')
        state.initial.inventory.transfer(water,Account('n1','n1-h0','fixture','water'),state.initial.inventory.balance(water))
        handler.prepare('n0',self.offer(),ctx).commit();state.contracts.publish()
        before=fingerprint(state.snapshot());alias=state.contracts.visible('n1')['proposals'][0]['id']
        with self.assertRaises(RuleViolation):handler.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))

    def test_cash_leg_is_locked_before_receipt_and_pays_only_against_real_goods(self):
        from organizer.rift_era.engine.ledger import CashAccount
        state,h=self.fixture();ctx=NS(tick=0,cycle=0)
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n0','reserves','RU'),1000)
        order=self.offer();order['params']={'hub_a':'n0-h0','hub_b':'n1-h0','cash_side':'a','currency':'RU','amount_mU':1000,
                                           'good_b':'water','qty_b_mU':1000,'period_cycles':2}
        h.prepare('n0',order,ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id']
        before=state.initial.cash.balance(CashAccount('n1','reserves','RU')).native_mU
        h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,0)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','reserves','RU')).native_mU,before)
        state.contracts.settle(ctx)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','reserves','RU')).native_mU,before+1000)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n1-h0','available','water')),1000)
        state.audit()


    def test_same_batch_new_embargo_refunds_both_escrows_without_claiming_delivery(self):
        state,h=self.fixture();ctx=NS(tick=0,cycle=0)
        h.prepare('n0',self.offer(),ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id']
        h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        from organizer.rift_era.engine.contracts import EmbargoTreaty
        state.contracts.embargoes.append(EmbargoTreaty(('n0','n2'),'n1',('food',),0,10))
        before=state.initial.inventory.balance(Account('n1','n0-h0','available','food'))
        state.contracts.settle(ctx)
        row=state.contracts.swaps.visible('n0')[0]
        self.assertEqual(row.status,'SUSPENDED')
        self.assertEqual(row.delivered_periods,0)
        self.assertEqual(state.initial.inventory.balance(Account('n1','n0-h0','available','food')),before)

import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.world import generate_candidate


class LicenseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)
        state=game.state;state.research['n0'].known.update({'R06','R01'})
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n1','reserves','RU'),10000)
        return game

    @staticmethod
    def offer(seq=1,other='n1',tech='R01',fee=5000):
        return {'id':str(seq),'kind':'diplomacy','op':'propose_contract','counterparty':other,
                'clause_type':5,'duration_cycles':10,'params':{'tech_id':tech,'fee_ru_mU':fee}}

    @staticmethod
    def accept(alias,seq=2):return {'id':str(seq),'kind':'diplomacy','op':'accept_contract','proposal_id':alias}

    def alias(self,state,owner,tech):
        return next(p['id'] for p in state.contracts.visible(owner)['proposals'] if p['params']['tech_id']==tech and p['counterparty']==owner)

    def test_real_commands_lock_and_pay_fee_without_unlocking_verified_technology(self):
        game=self.fixture();state=game.state
        recipient=state.research['n1']
        lab=next(lid for lid,line in state.initial.lines.items() if line.owner=='n1' and line.recipe.id=='P075')
        recipient.set_focus(lab,'R01');before=recipient.projects['R01'].target_mU
        source=CashAccount('n1','reserves','RU');target=CashAccount('n0','reserves','RU')
        target_before=state.initial.cash.balance(target).native_mU
        game.step({'n0':[self.offer()]})
        for _ in range(4):game.step()
        self.assertFalse(state.contracts.visible('n2')['proposals'])
        alias=self.alias(state,'n1','R01')
        game.step({'n1':[self.accept(alias)]})
        self.assertEqual(state.command_streams['n1'].recent[-1].status,'ACCEPTED')
        self.assertEqual(recipient.projects['R01'].target_mU,before//2)
        self.assertNotIn('R01',recipient.known)
        self.assertEqual(state.initial.cash.balance(target).native_mU,target_before+5000)
        self.assertEqual(state.initial.cash.balance(source).native_mU,5000)
        self.assertFalse(state.contracts.pending)
        self.assertFalse(state.contracts.proposals)
        state.audit()

    def test_same_window_guess_cannot_accept_new_foreign_proposal(self):
        game=self.fixture();state=game.state
        game.step({'n0':[self.offer()],'n1':[self.accept('proposal-1')]})
        self.assertEqual(state.command_streams['n1'].recent[-1].code,'ERR_NO_VISIBLE_TARGET')
        self.assertNotIn('R01',state.research['n1'].licenses)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','reserves','RU')).native_mU,10000)
        state.audit()

    def test_received_fee_cannot_finance_another_seat_during_same_p1_batch(self):
        state=self.fixture().state;handler=ContractCommands(state);ctx=NS(cycle=0)
        state.research['n2'].known.update({'R06','I01'})
        handler.prepare('n0',self.offer(fee=1000),ctx).commit()
        handler.prepare('n2',self.offer(other='n0',tech='I01',fee=1000),ctx).commit()
        state.contracts.publish()
        handler.prepare('n1',self.accept(self.alias(state,'n1','R01')),ctx).commit()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation) as raised:
            handler.prepare('n0',self.accept(self.alias(state,'n0','I01')),ctx)
        self.assertEqual(raised.exception.code,'ERR_INSUFFICIENT_CASH')
        self.assertEqual(before,fingerprint(state.snapshot()))
        state.contracts.settle(ctx)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,1000)
        state.audit()

    def test_license_is_once_and_r12_cost_factor_is_not_silently_ignored(self):
        state=self.fixture().state;handler=ContractCommands(state);ctx=NS(cycle=0)
        state.research['n0'].known.add('R12')
        state.governance.policies['n0'].regimes['science_doctrine']='R12'
        handler.prepare('n0',self.offer(fee=1000),ctx).commit();state.contracts.publish()
        handler.prepare('n1',self.accept(self.alias(state,'n1','R01')),ctx).commit();state.contracts.settle(ctx)
        research=state.research['n1'];research.set_focus('test-lab','R01')
        self.assertEqual(research.projects['R01'].target_mU,research.target_cost('R01',{}))
        handler.prepare('n0',self.offer(seq=3,fee=1000),ctx).commit();state.contracts.publish()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):handler.prepare('n1',self.accept(self.alias(state,'n1','R01'),seq=4),ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))
        state.audit()

    def test_expiry_and_rejection_have_no_payment_or_credit_penalty(self):
        state=self.fixture().state;handler=ContractCommands(state);ctx=NS(cycle=0)
        handler.prepare('n0',self.offer(),ctx).commit();state.contracts.publish()
        before_cash=fingerprint(state.initial.cash.snapshot());before_credit=dict(state.credit)
        handler.prepare('n1',{'id':'2','kind':'diplomacy','op':'reject_contract',
                             'proposal_id':self.alias(state,'n1','R01')},ctx).commit()
        handler.prepare('n0',self.offer(seq=3),ctx).commit();state.contracts.publish()
        state.contracts.begin_tick(NS(cycle=10))
        self.assertFalse(state.contracts.proposals)
        self.assertEqual(before_cash,fingerprint(state.initial.cash.snapshot()))
        self.assertEqual(before_credit,state.credit)
        state.audit()

    def test_known_but_inactive_r12_does_not_apply_its_policy_penalty(self):
        state=self.fixture().state;handler=ContractCommands(state);ctx=NS(cycle=0)
        state.research['n0'].known.add('R12')
        handler.prepare('n0',self.offer(fee=1000),ctx).commit();state.contracts.publish()
        handler.prepare('n1',self.accept(self.alias(state,'n1','R01')),ctx).commit();state.contracts.settle(ctx)
        research=state.research['n1'];research.set_focus('test-lab','R01')
        self.assertEqual(research.projects['R01'].target_mU,research.target_cost('R01',{})//2)

    def test_active_r12_disables_open_research_bonus(self):
        from organizer.rift_era.engine.research import Research, LabOutput
        amounts=[]
        for allowed in (False,True):
            research=Research(self.catalog);research.set_focus('lab','R01');research.security['lab']='open'
            research.advance({'lab':LabOutput('lab','hub',1000)},open_allowed=allowed)
            amounts.append(research.projects['R01'].theory_mU)
        self.assertEqual(amounts,[1000,1100])

    def test_license_uses_r12_effective_at_acceptance_not_stale_offer(self):
        state=self.fixture().state;handler=ContractCommands(state);ctx=NS(cycle=0)
        handler.prepare('n0',self.offer(fee=1000),ctx).commit();state.contracts.publish()
        state.research['n0'].known.add('R12')
        state.governance.policies['n0'].regimes['science_doctrine']='R12'
        handler.prepare('n1',self.accept(self.alias(state,'n1','R01')),ctx).commit();state.contracts.settle(ctx)
        research=state.research['n1'];research.set_focus('test-lab','R01')
        self.assertEqual(research.projects['R01'].target_mU,research.target_cost('R01',{}))

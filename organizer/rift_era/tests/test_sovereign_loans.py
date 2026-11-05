import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests import test_contracts as base
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.authority import AssetRights
from organizer.rift_era.engine.ledger import CashAccount,Account
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.numeric import Q


class SovereignLoanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):base.LicenseContractTests.setUpClass()

    def fixture(self):
        state=base.LicenseContractTests().fixture().state;state.initial.assets['collateral']=Asset('collateral','n1','n1-h0',{})
        state.authority.sync_assets(state.initial.assets)
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n0','reserves','RU'),1000)
        return state,ContractCommands(state)

    def offer(self):return {'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':2,'duration_cycles':10,
        'params':{'currency':'RU','principal_mU':1000,'annual_rate_bp':1000,'due_cycle':2,'collateral':[{'asset_id':'collateral','shares_mU':1000}]}}

    def test_principal_dvp_locks_real_lender_cash_and_pledges_shares(self):
        state,h=self.fixture();ctx=NS(tick=0,cycle=0)
        before=state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU
        h.prepare('n0',self.offer(),ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id']
        h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,before-1000)
        state.contracts.settle(ctx)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','reserves','RU')).native_mU,11000)
        self.assertEqual(state.authority.assets['collateral'].free_shares('n1'),0)
        self.assertTrue(state.contracts.loans.rows)
        state.audit()

    def test_invalid_double_pledge_has_no_mutation(self):
        state,h=self.fixture();ctx=NS(tick=0,cycle=0);h.prepare('n0',self.offer(),ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id'];h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit();state.contracts.settle(ctx)
        before=fingerprint(state.snapshot())
        with self.assertRaises(Exception):h.prepare('n0',self.offer()|{'id':'2'},ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))

    def test_debt_book_exposes_provider_claims_and_default_preserves_collateral_until_foreclosure(self):
        state,h=self.fixture();ctx=NS(tick=0,cycle=0);h.prepare('n0',self.offer(),ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id'];h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit();state.contracts.settle(ctx)
        claims=state.contracts.loans.funding_claims(NS(tick=19,cycle=1,seed=1));self.assertTrue(claims);self.assertEqual(claims[0].currency,'RU')
        state.contracts.loans.settle(NS(tick=19,cycle=1,seed=1,clock=NS(cycle_ticks=10,is_cycle_end=lambda t:True)))
        self.assertEqual(state.authority.assets['collateral'].free_shares('n1'),0)

    def originate(self,state,h):
        ctx=NS(tick=0,cycle=0);h.prepare('n0',self.offer(),ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id'];h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit();state.contracts.settle(ctx)
        return next(iter(state.contracts.loans.rows.values()))

    def test_shared_funding_provider_pays_lender_and_releases_repaid_collateral(self):
        from organizer.rift_era.engine.debt_funding import DebtFunding
        state,h=self.fixture();row=self.originate(state,h);funding=DebtFunding(state);funding.add_provider(state.contracts.loans.funding_claims)
        ctx=NS(tick=29,cycle=2,seed=1000,clock=NS(cycle_ticks=10,is_cycle_end=lambda t:True))
        funding.fund(ctx);state.contracts.loans.settle(ctx)
        self.assertEqual(row.face_mU,0)
        self.assertEqual(state.contracts.loans.debt_book('n1'),0)
        self.assertEqual(state.authority.assets['collateral'].free_shares('n1'),1000)
        self.assertGreaterEqual(state.initial.cash.balance(CashAccount('n0','reserves','RU')).native_mU,1000)
        state.audit()

    def test_foreclosure_transfers_only_residual_value_below_unpaid_debt_and_returns_rest(self):
        state,h=self.fixture();row=self.originate(state,h)
        asset=state.initial.assets['collateral'];asset.book_nmU={'steel':Q(1000000)};asset.original_nmU={'steel':1000000}
        state.contracts.loans.foreclose(row)
        rights=state.authority.assets['collateral']
        self.assertGreater(rights.owner_shares.get('n0',0),0)
        self.assertEqual(sum(rights.owner_shares.values()),1000)
        self.assertGreater(rights.free_shares('n1'),0)
        self.assertFalse(rights.pledged)
        self.assertGreaterEqual(row.face_mU,0)
        state.authority.audit()

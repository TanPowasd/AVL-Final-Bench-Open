import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests import test_contracts as base
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.determinism import fingerprint


class JointVentureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):base.LicenseContractTests.setUpClass()

    def fixture(self):
        state=base.LicenseContractTests().fixture().state
        for good,q in {'steel':3000,'cement':3000,'basic_parts':1000}.items():
            state.initial.inventory.transfer(Account('n0','n0-h0','available',good),Account('n1','n0-h0','available',good),q)
        return state,ContractCommands(state)

    def offer(self):return {'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':4,'duration_cycles':10,
        'params':{'hub_id':'n0-h0','profile_id':'S1','recipe_id':'P001','shares_mU':[600,400],
                  'inputs':[{'steel':3000,'cement':3000,'basic_parts':1000},{'steel':3000,'cement':3000,'basic_parts':1000}],'mode':'industry'}}

    def test_paid_construction_builds_real_shared_capital_only_after_catalog_time(self):
        state,h=self.fixture();ctx=NS(tick=0,cycle=0,clock=ClockConfig());h.prepare('n0',self.offer(),ctx).commit();state.contracts.publish();alias=state.contracts.visible('n1')['proposals'][0]['id']
        h.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        row=next(iter(state.contracts.joint_ventures.rows.values()));self.assertTrue(state.initial.assets[row.asset].under_construction)
        for tick in range(80):state.contracts.joint_ventures.advance(NS(tick=tick,cycle=tick//10,clock=ClockConfig()))
        self.assertEqual(row.status,'COMMISSIONED')
        self.assertEqual(state.authority.assets[row.asset].owner_shares,{'n0':600,'n1':400})
        self.assertTrue(state.initial.facilities[row.asset].commissioned)
        self.assertEqual(len(state.initial.facilities[row.asset].line_ids),1)
        state.audit()

    def test_wrong_material_total_refuses_without_side_effect(self):
        state,h=self.fixture();order=self.offer();order['params']['inputs'][1]['steel']=1;before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):h.prepare('n0',order,NS(tick=0,cycle=0,clock=ClockConfig()))
        self.assertEqual(before,fingerprint(state.snapshot()))

    def test_shared_research_spends_each_real_output_once_and_keeps_validation_domestic(self):
        from organizer.rift_era.engine.joint_ventures import IndustrialVenture,JointVentureProposal
        from organizer.rift_era.engine.research import LabOutput
        state,h=self.fixture();tech='I09'
        prerequisites=set(state.catalog.technologies)-{tech}
        for owner in ('n0','n1'):
            state.research[owner].known.update(prerequisites)
            state.research[owner].set_focus('lab:'+owner,tech)
        p=JointVentureProposal('joint','n0','n1','1','n0-h0','S1','P075',(500,500),({},{}),'research',10,10,tech)
        row=IndustrialVenture(p,'fixture',1,status='COMMISSIONED',active_members=('n0','n1'),contribution={'n0':0,'n1':0})
        state.contracts.joint_ventures.rows[p.id]=row
        outputs={o:{'lab:'+o:LabOutput('lab:'+o,o+'-h0',100000000)} for o in ('n0','n1')}
        left=state.contracts.joint_ventures.route_research(outputs,NS(tick=1,cycle=0))
        self.assertTrue(all(not rows for rows in left.values()))
        for owner in ('n0','n1'):
            self.assertTrue(state.research[owner].projects[tech].theory_complete)
            self.assertFalse(state.research[owner].projects[tech].verified)
            self.assertNotIn(tech,state.research[owner].known)
        self.assertEqual(row.joint_rp,200000000)
        self.assertEqual(row.status,'THEORY_COMPLETE')

    def test_r12_exit_cannot_take_joint_progress_or_continue_contributing(self):
        from organizer.rift_era.engine.joint_ventures import IndustrialVenture,JointVentureProposal
        from organizer.rift_era.engine.research import LabOutput
        state,h=self.fixture();tech='I09'
        for owner in ('n0','n1'):
            state.research[owner].known.update(set(state.catalog.technologies)-{tech})
            state.research[owner].set_focus('lab:'+owner,tech)
        p=JointVentureProposal('joint','n0','n1','1','n0-h0','S1','P075',(500,500),({},{}),'research',10,10,tech)
        row=IndustrialVenture(p,'fixture',1,status='COMMISSIONED',active_members=('n0','n1'),contribution={'n0':100,'n1':100},joint_rp=200)
        state.contracts.joint_ventures.rows[p.id]=row
        state.governance.policies['n1'].regimes['science_doctrine']='R12'
        out={o:{'lab:'+o:LabOutput('lab:'+o,o+'-h0',100)} for o in ('n0','n1')}
        left=state.contracts.joint_ventures.route_research(out,NS(tick=1,cycle=0))
        self.assertEqual(row.active_members,('n0',))
        self.assertEqual(row.joint_rp,300)
        self.assertTrue(left['n1'])
        self.assertFalse(left['n0'])


    def test_facility_ownership_splits_one_origin_lot_without_quantity_creation(self):
        from organizer.rift_era.engine.facility_ownership import FacilityOwnership
        from organizer.rift_era.engine.materials import Origin
        from organizer.rift_era.engine.economic_ownership import EconomicOwner
        state,h=self.fixture();owner=FacilityOwnership(state)
        state.authority.assets['x']=__import__('organizer.rift_era.engine.authority',fromlist=['AssetRights']).AssetRights('n0',{'n0':600,'n1':400})
        owner.register('line','x');origin=Origin('n0','n0-h0');parts=owner.split_output('line',{origin:Q(1000)})
        self.assertEqual(sum(parts.values()),Q(1000));self.assertEqual(sum(q for o,q in parts.items() if o.economic_owner==EconomicOwner('n0','treasury')),Q(600))

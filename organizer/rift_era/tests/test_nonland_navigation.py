import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.nonland_navigation import base_online,legal_edge
from organizer.rift_era.engine.military_movement import MilitaryMovement
from organizer.rift_era.engine.numeric import Q


class NonlandNavigationTests(unittest.TestCase):
    def state(self):
        return NS(cargo=NS(corridors={'r':NS(mode='road'),'s':NS(mode='sea'),'a':NS(mode='air')},
                           terminal_online=lambda hub,mode,owner=None: (hub,mode,owner)==('base','air','n0')),
                  authority=NS(controls_hub=lambda owner,hub:owner=='n0' and hub in ('base','depot')))

    def test_air_supplies_require_completed_controlled_airport(self):
        state=self.state()
        self.assertTrue(base_online(state,'n0','base','Air'))
        self.assertFalse(base_online(state,'n0','depot','Air'))
        self.assertFalse(base_online(state,'n1','base','Air'))
        self.assertFalse(base_online(state,'n0',None,'Air'))
        self.assertTrue(base_online(state,'n0','depot','Land'))

    def test_each_domain_uses_only_its_physical_corridor_mode(self):
        state=self.state()
        for domain,edge in (('Land','r'),('Sea-Surface','s'),('Sea-Sub','s'),('Air','a')):
            for candidate in ('r','s','a'):
                self.assertEqual(legal_edge(state,NS(domain=domain),candidate),candidate==edge)

    def test_missing_mode_is_a_legacy_road_never_a_free_sea_or_air_route(self):
        state=self.state();state.cargo.corridors['x']=NS()
        self.assertTrue(legal_edge(state,NS(domain='Land'),'x'))
        self.assertFalse(legal_edge(state,NS(domain='Air'),'x'))
        self.assertFalse(legal_edge(state,NS(domain='Sea-Surface'),'x'))

    def test_air_and_sea_edge_costs_ignore_land_terrain(self):
        movement=MilitaryMovement.__new__(MilitaryMovement)
        for domain in ('Air','Sea-Surface','Sea-Sub'):
            self.assertEqual(movement.profile('edge','origin',NS(domain=domain)),((Q(1000),Q(1)),))


class NonlandPhysicalMovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from organizer.rift_era.engine.catalog import Catalog
        from organizer.rift_era.engine.world import generate_candidate
        cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self,domain,kind,mode):
        from organizer.rift_era.engine.economy import opening_economy
        from organizer.rift_era.engine.initialization import initialize
        from organizer.rift_era.engine.assets import Asset
        from organizer.rift_era.engine.ledger import Account
        from organizer.rift_era.engine.formations import FormationCommands
        from organizer.rift_era.engine.clock import ClockConfig
        from organizer.rift_era.engine.logistics import Corridor
        from organizer.rift_era.engine.materials import Origin
        state=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False).state
        for hub in ('n0-h0','n0-h1'):
            aid='test_base:'+hub
            state.initial.assets[aid]=Asset(aid,'n0',hub,{})
            state.cargo.terminals[hub,mode]=aid
        equipment=next(iter(state.catalog.units[kind]['installed']))
        account=Account('n0','n0-h0','available',equipment)
        if not state.initial.inventory.balance(account):
            state.initial.inventory.transform({}, {account:1000},reason='explicit_nonland_test_fixture',origins={account:Origin('n0','n0-h0')})
        ctx=NS(seed=1000,tick=0,cycle=0,clock=ClockConfig())
        FormationCommands(state).prepare('n0',{'id':'1','kind':'military','op':'deploy_formation',
            'hub_id':'n0-h0','domain':domain,'units':{kind:1}},ctx).commit()
        formation=next(f for f in state.formations.rows.values() if f.id.startswith('formation:'))
        road=next(e for e,row in state.cargo.corridors.items() if {row.a,row.b}=={'n0-h0','n0-h1'})
        source=state.cargo.corridors[road]
        state.cargo.corridors['test_nav']=Corridor('test_nav',source.a,source.b,source.length_cells,60000,mode=mode)
        state.cargo.paths['test_nav']=state.cargo.paths[road]
        return state,formation,ctx

    def test_air_and_navy_deploy_actual_units_and_advance_only_correct_domain_edge(self):
        from organizer.rift_era.engine.military_movement import MarchCommands
        from organizer.rift_era.engine.ledger import Account
        for domain,kind,mode in (('Air','aircraft','air'),('Sea-Surface','escort','sea'),('Sea-Sub','submarine','sea')):
            with self.subTest(domain=domain):
                state,formation,ctx=self.fixture(domain,kind,mode)
                MarchCommands(state).prepare('n0',{'id':'2','kind':'military','op':'formation_task','formation_id':formation.id,
                    'task':'move','target_region':state.initial.map.hubs['n0-h1'].region,'posture':'preserve',
                    'supply_hub':'n0-h0','retreat_loss_mU':500},ctx).commit()
                self.assertEqual(state.movement.plans[formation.id].edges,('test_nav',))
                uid=formation.unit_ids[0]
                fuel=int(state.catalog.units[kind]['combat_tick']['fuel']*1000)
                moved,_=state.movement.advance(ctx,{uid:{'fuel':fuel}})
                self.assertIn(uid,moved)
                self.assertEqual(state.movement.position(formation).edge,'test_nav')
                self.assertEqual(state.movement.position(formation).progress,state.catalog.units[kind]['hops_per_cycle']*100)
                self.assertIsNone(formation.hub)
                state.authority.sync_assets(state.initial.assets)
                state.audit()

    def test_no_fuel_no_nonland_motion(self):
        from organizer.rift_era.engine.military_movement import MarchCommands
        state,formation,ctx=self.fixture('Air','aircraft','air')
        MarchCommands(state).prepare('n0',{'id':'2','kind':'military','op':'formation_task','formation_id':formation.id,
            'task':'move','target_region':state.initial.map.hubs['n0-h1'].region,'posture':'preserve',
            'supply_hub':'n0-h0','retreat_loss_mU':500},ctx).commit()
        moved,_=state.movement.advance(ctx,{})
        self.assertFalse(moved)
        self.assertEqual(state.movement.position(formation).progress,0)

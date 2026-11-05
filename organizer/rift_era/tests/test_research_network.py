import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.public_systems import PublicSystems, InstalledSystem
from organizer.rift_era.engine.logistics import Corridor
from organizer.rift_era.engine.research import LabOutput, combine_labs
from organizer.rift_era.engine.numeric import Q


class ResearchNetworkTests(unittest.TestCase):
    def fixture(self):
        assets={key:Asset(key,'n0',key[0],{}) for key in ('a','b','c','a-radio','b-radio','c-radio')}
        state=NS(initial=NS(assets=assets),research={'n0':NS(known={'C02','R05'})},
                 orbit=NS(coverage=lambda p:None),cargo=NS(corridors={
                     'ab':Corridor('ab','a','b',1,1000),'bc':Corridor('bc','b','c',1,1000)},road_assets={}))
        systems=PublicSystems(state)
        for hub in ('a','b','c'):
            sid=hub+'-radio'
            systems.instances[sid]=InstalledSystem(sid,'n0',hub,hub,'radio_backbone',online=hub!='b')
        return systems

    def test_powered_disconnected_radios_do_not_enable_organized_research(self):
        systems=self.fixture();components=systems.communication_components('n0')
        self.assertNotEqual(components['a'],components['c'])
        outputs=tuple(LabOutput(h,h,1000,connected=True,network_id=components[h]) for h in ('a','c'))
        baseline=combine_labs(outputs,organized=False)
        self.assertEqual(combine_labs(outputs,organized=True),baseline)
        systems.instances['b-radio'].online=True
        components=systems.communication_components('n0')
        outputs=tuple(LabOutput(h,h,1000,connected=True,network_id=components[h]) for h in ('a','c'))
        self.assertGreater(combine_labs(outputs,organized=True),baseline)

    def test_road_or_radio_destruction_breaks_real_connectivity(self):
        systems=self.fixture();systems.instances['b-radio'].online=True
        self.assertEqual(len(set(systems.communication_components('n0').values())),1)
        systems.state.cargo.corridors['bc'].health_mU=0
        components=systems.communication_components('n0')
        self.assertNotEqual(components['a'],components['c'])
        systems.state.cargo.corridors['bc'].health_mU=1000
        systems.state.initial.assets['b-radio'].health_mU=0
        components=systems.communication_components('n0')
        self.assertNotIn('b',components)
        self.assertNotEqual(components['a'],components['c'])

    def test_actual_orbital_service_can_bridge_islands_but_not_unpowered_endpoint(self):
        systems=self.fixture();systems.state.research['n0'].known.add('N08')
        self.assertEqual(len(set(systems.communication_components('n0').values())),2)
        systems.state.orbit.coverage=lambda p:('sea',Q(4,5))
        self.assertEqual(len(set(systems.communication_components('n0').values())),1)
        systems.instances['c-radio'].online=False
        self.assertEqual(set(systems.communication_components('n0')),{'a'})

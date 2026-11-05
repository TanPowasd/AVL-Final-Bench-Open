import unittest

from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.determinism import fingerprint, normal
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.public_systems import InstalledSystem
from organizer.rift_era.engine.weather import rain_step
from organizer.rift_era.engine.world import generate_candidate


class ForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)
        state=game.state
        state.research['n0'].known.add('V06')
        host=next(f.id for f in state.initial.facilities.values() if f.owner=='n0')
        slot=host+'/test-climate'
        state.initial.assets[slot]=Asset(slot,'n0','n0-h0',{})
        state.slot_use[slot]='system:'+slot
        state.systems.instances[slot]=InstalledSystem(slot,'n0','n0-h0',host,'climate_model',online=True)
        state.authority.sync_assets(state.initial.assets)
        return game,slot

    def test_forecast_matches_actual_weather_until_emissions_change(self):
        game,_=self.fixture();state=game.state
        state.forecasts.build(0)
        view=state.forecasts.visible('n0')
        self.assertTrue(view['regions'])
        self.assertTrue(all(len(rows)==60 for rows in view['regions'].values()))
        self.assertFalse(state.forecasts.visible('n1')['regions'])
        for tick in range(10):
            game.step()
            for rid,rows in view['regions'].items():
                self.assertEqual(rows[tick],[state.wind_mU[rid],state.cloud_mU[rid],state.sun_mU[rid],
                                            round(1000*state.hydrology.rain[rid])])
        state.audit()

    def test_frozen_forecast_is_pure_and_online_installation_is_required(self):
        game,slot=self.fixture();state=game.state
        state.systems.instances[slot].online=False
        state.forecasts.build(0)
        self.assertFalse(state.forecasts.visible('n0')['regions'])
        state.systems.instances[slot].online=True
        state.forecasts.build(0)
        frozen=state.forecasts.visible('n0');before=fingerprint(state.forecasts.snapshot())
        state.global_emissions_mU=50000000
        state.wind_mU={rid:0 for rid in state.wind_mU}
        state.cloud_mU={rid:0 for rid in state.cloud_mU}
        for _ in range(10):
            result=state.forecasts.visible('n0')
            self.assertEqual(result,frozen)
            result['regions'].clear()
        self.assertEqual(before,fingerprint(state.forecasts.snapshot()))
        state.forecasts.build(5)
        self.assertNotEqual(frozen,state.forecasts.visible('n0'))

    def test_integer_commit_matches_independent_rain_equation(self):
        previous=Q(1)
        for cycle in range(30):
            e=cycle*1000000
            expected=round(1000*max(0,min(1.5,1+.8*(float(previous)-1)+.1*(1+.5*min(1,e/50000000))*
                normal(1000,'rain',cycle,'0/test'))))
            actual=rain_step(1000,0,cycle,'test',previous,e)
            self.assertEqual(actual,Q(expected,1000))
            previous=actual

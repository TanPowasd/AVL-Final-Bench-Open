"""Shared realized/conditional weather equations; forecasts freeze emissions."""
import math

from .environment import weather_ar
from .numeric import Q


def weather_step(seed, attempt, tick, region, wind_mU, cloud_mU, emissions_mU):
    key = f'{attempt}/{region.id}'
    wind = weather_ar(seed, tick, key, 'weather_wind', wind_mU / 1000, region.wind_mean_mU / 1000,
                      .8, .12, 0, 1, emissions_mU / 1000)
    cloud = weather_ar(seed, tick, key, 'weather_cloud', cloud_mU / 1000, region.cloud_mean_mU / 1000,
                       .7, .15, 0, 1, emissions_mU / 1000)
    day = max(0.0, math.sin(math.pi * (tick % 10 - 1) / 6))
    return round(1000 * wind), round(1000 * cloud), round(day * (1 - .7 * cloud) * region.latitude_mU)


def rain_step(seed, attempt, cycle, region_id, previous, emissions_mU):
    return Q(round(1000 * weather_ar(seed, cycle, f'{attempt}/{region_id}', 'rain',
                                    float(previous), 1, .8, .1, 0, 1.5, emissions_mU / 1000)), 1000)


class WeatherForecasts:
    def __init__(self, state):
        self.s = state
        self.views = {}
        self.build(0)

    def advance(self, ctx):
        if ctx.clock.is_window_end(ctx.tick):
            self.build(ctx.tick + 1)

    def build(self, tick):
        state = self.s
        world = state.initial.map
        requests = {}
        limits = {}
        for owner in state.initial.known:
            requested = {}
            if state.orbit.coverage(owner) is not None and 'N08' in state.research[owner].known:
                requested.update({rid: 30 for rid, r in world.regions.items() if r.terrain == 'ocean'})
            climate = 'V06' in state.research[owner].known and any(
                system.owner == owner and system.kind == 'climate_model' and system.online
                and state.systems.availability(system) > 0 for system in state.systems.instances.values())
            if climate:
                requested.update({rid: 60 for rid, r in state.authority.regions.items() if r.controller == owner})
            requests[owner] = requested
            for rid, horizon in requested.items():
                limits[rid] = max(limits.get(rid, 0), horizon)
        predictions = {}
        # Evaluate a shared region forecast once, independent of seat order.
        # Future commands, future E_cum and future endogenous reservoirs are
        # intentionally absent from this equation's inputs.
        emissions = state.global_emissions_mU
        for rid, horizon in sorted(limits.items()):
            region = world.regions[rid]
            wind = state.wind_mU.get(rid, region.wind_mean_mU)
            cloud = state.cloud_mU.get(rid, region.cloud_mean_mU)
            rain = state.hydrology.rain.get(rid)
            rows = []
            for future in range(tick, tick + horizon):
                wind, cloud, sun = weather_step(world.seed, world.attempt, future, region, wind, cloud, emissions)
                if rain is not None and future % 10 == 0:
                    rain = rain_step(world.seed, world.attempt, future // 10, rid, rain, emissions)
                rows.append([wind, cloud, sun, None if rain is None else round(rain * 1000)])
            predictions[rid] = rows
        self.views = {owner: {'issued_tick': tick, 'first_tick': tick,
                              'fields': ['wind_mU', 'cloud_mU', 'sun_mU', 'rain_mU'],
                              'regions': {rid: predictions[rid][:horizon] for rid, horizon in sorted(wanted.items())}}
                      for owner, wanted in requests.items()}

    def visible(self, owner):
        from copy import deepcopy
        return deepcopy(self.views[owner])

    def snapshot(self):
        return self.views

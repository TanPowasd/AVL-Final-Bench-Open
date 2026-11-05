"""02 P7 support events and direct support shocks (C04, E26, capture reset, 03 bombard)."""

from .numeric import Q, clip


def support_events_mU(metric):
    """Delta_event of 02 P7 for one hub-cycle, before the shared clip(-45, +25).

    C04: -20 in every cycle the hub's grid spent at least one tick blacked out.
    E26: -10 * delta_B1, the unserved share of the hub's B1 power demand.
    """
    events = Q(0)
    if metric.blackout_ticks:
        events -= 20
    if metric.b1_needed_mU:
        events -= 10 * (1 - Q(metric.b1_received_mU, metric.b1_needed_mU))
    return events


def region_hubs(state, region):
    return [h for h in state.initial.populations if state.initial.map.hubs[h].region == region]


def reset_captured_support(state, changes):
    """02 P7: a captured region starts at the authority's 280/550 mU for its new controller."""
    for region, _, _ in changes:
        row = state.authority.regions[region]
        for hub in region_hubs(state, region):
            state.initial.populations[hub].support_mU = row.support_mU
            state.initial.populations[hub].red_streak = 0


def shock_region_support(state, region, delta_mU):
    """A direct support shock outside the per-cycle clip (03 bombard -300 mU)."""
    row = state.authority.regions[region]
    row.support_mU = clip(row.support_mU + delta_mU, 0, 1000)
    for hub in region_hubs(state, region):
        population = state.initial.populations[hub]
        population.support_mU = clip(population.support_mU + delta_mU, 0, 1000)
    for city in state.city_economy.cities.values():
        if initial_region(state, city.hub) == region:
            city.population.support_mU = clip(city.population.support_mU + delta_mU, 0, 1000)


def initial_region(state, hub):
    return state.initial.map.hubs[hub].region

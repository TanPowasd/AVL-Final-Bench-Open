"""P7 resident outcomes use the goods, work and pollution of their own Hub."""

from .economy_state import BASKET
from .environment import pollution_next
from .numeric import Q, floor
from .population import Demography, grow, support_delta, update_support
from .support_events import support_events_mU


def settle_residents(state, ctx):
    initial = state.initial
    regional = {}
    for hub, metric in state.local_cycles.items():
        region = initial.map.hubs[hub].region
        row = state.authority.regions[region]
        # Support is held towards the current controller p; the -12 term applies
        # whenever p is not the region's sovereign (02 P7).
        owner = initial.map.hubs[hub].owner
        controller = row.controller if row.controller in state.governance.policies else owner
        occupied = row.sovereign is not None and controller != row.sovereign
        remedied = state.governance.remediation_mU.get(region, 0) >= 1000
        state.pollution[hub] = pollution_next(state.pollution[hub], metric.emitted_mU, Q(int(remedied)))
        population = initial.populations[hub]
        if not population.total_mU:
            state.last_satisfaction[hub] = {}
            state.local_history[hub].append(metric)
            continue
        satisfaction = {g: min(Q(2), q / max(Q(1), metric.needed.get(g, Q(1))))
                        for g, q in metric.household_consumed.items()}
        for good in (*BASKET, 'electricity'):
            satisfaction.setdefault(good, Q(0))
        pollution = Q(state.pollution[hub], state.pollution[hub] + 100000)
        known = state.research[owner].known
        development = (Q(1, 2) * min(Q(1), Q(sum(t.startswith(('A', 'F')) for t in known), 20)) +
                       Q(1, 2) * min(Q(1), metric.household_consumed.get('consumer_goods', Q(0)) /
                                     max(Q(1), population.total_mU * Q(3, 100))))
        medical = min(Q(1), Q(len(known & {'A04', 'A08', 'A11'}), 3) *
                      (min(Q(1), satisfaction['medicine']) + min(Q(1), satisfaction['medical_supplies'])))
        # C05: M_eff = min(1, M) * (1 - 0.5 * delta_B1), delta_B1 the unserved
        # share of this hub's B1 power demand (same measure E26 uses).
        delta_b1 = (1 - min(Q(1), Q(metric.b1_received_mU, metric.b1_needed_mU))
                    if metric.b1_needed_mU else Q(0))
        medical *= 1 - Q(1, 2) * delta_b1
        gene = min(Q(1), metric.household_consumed.get('gene_therapy', Q(0)) /
                   max(Q(1), population.total_mU * Q(1, 1000))) if 'A11' in known else Q(0)
        satisfaction['gene_therapy'] = gene
        state.last_satisfaction[hub] = satisfaction
        # Employment's denominator is the population that actually worked this
        # cycle; births at its end cannot retroactively lower employment.
        participants = floor(sum(population.skills_mU.values()) * state.governance.participant_fraction(owner, hub))
        employment = min(Q(1), Q(sum(metric.labor.values()), max(1, participants)))
        grow(ctx.seed, ctx.cycle, population, Demography(satisfaction['food'], initial.housing_mU[hub],
            development, medical, gene, pollution, state.inflation[owner] * 10000))
        policy = state.governance.policies[controller]
        delta = support_delta(food=satisfaction['food'], consumer_goods=satisfaction['consumer_goods'],
            medicine=satisfaction['medicine'], employment=employment,
            tax=policy.nominal_tax, fatigue=policy.fatigue, pollution=pollution,
            autonomy=state.governance.autonomy.get(region, Q(0)), social_security=state.governance.social_ratio(controller),
            occupied=occupied,
            events_mU=-5 * int('economic_doctrine' in policy.transitions) + support_events_mU(metric))
        update_support(population, delta)
        regional.setdefault(region, []).append(population)
        if state.governance.active(controller, 'F07'):
            population.support_mU = min(850, population.support_mU)
        state.local_history[hub].append(metric)
    # Keep the authority row (the value controllers observe and annexation reads)
    # equal to the population-weighted support of the region's national hubs.
    for region, rows in regional.items():
        total = sum(p.total_mU for p in rows)
        if total:
            state.authority.regions[region].support_mU = sum(p.support_mU * p.total_mU for p in rows) // total

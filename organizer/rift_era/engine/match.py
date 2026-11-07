"""P0-1: complete-match assembly helpers, the end-of-match finalizer and seat score.

`full_match` (economy.py) builds the kernel; `finalize_match` is the driver's
finalizer. It never invents a V for an incomplete game: a run shorter than the
official 28,800 ticks reports only its partial pillar means, and
`full_rules_accepted` stays False while any release gate is pending.
"""
from __future__ import annotations

from .errors import InfrastructureFault
from .numeric import Q
from .release import PENDING_RELEASE_GATES
from .scoring import relative_ratio, score_interval


def _q(value):
    value = Q(value)
    return [value.numerator, value.denominator]


def finalize_match(kernel) -> dict:
    state = kernel.state
    power = state.national_power
    if power is None:
        raise InfrastructureFault('finalizer requires a full_match kernel with national power')
    if kernel.tick != kernel.clock.total_ticks:
        raise InfrastructureFault('finalizer called before the last tick')
    state.audit()
    complete = kernel.clock.official and power.cycle == 2880
    result = {'scope': 'full_match', 'national_power_version': power.snapshot()['version'],
              'cycles': power.cycle, 'complete_trajectory': complete,
              'coverage_gaps': dict(power.snapshot()['coverage']),
              'pending_release_gates': list(PENDING_RELEASE_GATES),
              # Certification needs the official clock, all 2880 cycles, no
              # pending release gate and no approximated input.
              'full_rules_accepted': bool(complete and not PENDING_RELEASE_GATES and not power.snapshot()['coverage'])}
    last = {p: rows[-1] for p, rows in power.pillars.items() if rows}
    result['last_pillars'] = {p: {'population': _q(r.population), 'industry': _q(r.industry), 'control': _q(r.control),
                                  'technology': _q(r.technology), 'international': _q(r.international),
                                  'U': _q(r.total)} for p, r in last.items()}
    result['mean_U'] = {p: _q(power.partial_mean(p)) for p in power.nations}
    if complete:
        values = power.final_values()
        result['V'] = {p: _q(v) for p, v in values.items()}
        result['phi'] = {p: _q(power.phi(p)) for p in power.nations}
    return result


def seat_score(v_player, v_shadow, precision: int = 50) -> dict:
    """07 section 3: score = 50 + 50*tanh(.6*log R) = 100/(1+R^-1.2), as an interval."""
    ratio = relative_ratio(Q(v_player), Q(v_shadow))
    interval = score_interval(ratio, precision)
    return {'R': _q(ratio), 'score_lower': str(interval.lower), 'score_upper': str(interval.upper)}

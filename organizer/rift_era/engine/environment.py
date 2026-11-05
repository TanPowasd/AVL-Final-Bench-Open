"""Finite extraction, directed river conservation and lagged pollution/weather."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from fractions import Fraction

from .determinism import normal
from .errors import InvariantViolation
from .numeric import Q, clip, floor, proportional


def finite_extraction(reserve_mU: int, initial_mU: int, nominal_mU: int,
                       availability: Fraction) -> int:
    if reserve_mU <= 0 or initial_mU <= 0:
        return 0
    ratio = Q(reserve_mU, initial_mU)
    tail = Q(1) if ratio >= Q(3, 10) else Q(1, 4) + Q(3, 4) * ratio / Q(3, 10)
    return min(reserve_mU, floor(nominal_mU * clip(availability, Q(0), Q(1)) * tail))


@dataclass(frozen=True)
class RiverNode:
    id: str
    natural_mU: int
    downstream: Mapping[str, Fraction]
    withdrawal_requests: Mapping[str, int]


@dataclass(frozen=True)
class RiverResult:
    withdrawn: Mapping[str, int]
    outflow: Mapping[str, int]
    ocean_mU: int


def rivers(seed: int, cycle: int, nodes: tuple[RiverNode, ...]) -> RiverResult:
    by_id = {n.id: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('duplicate river node')
    indegree = {n.id: 0 for n in nodes}
    for n in nodes:
        if n.natural_mU < 0 or (n.downstream and sum(n.downstream.values()) != 1) or any(w < 0 for w in n.downstream.values()):
            raise ValueError('invalid water source or branch weights')
        for dest in n.downstream:
            if dest not in by_id:
                raise ValueError('unknown downstream node')
            indegree[dest] += 1
    incoming = {n.id: n.natural_mU for n in nodes}
    ready, visited = sorted(k for k, d in indegree.items() if d == 0), 0
    withdrawn, outflows, ocean = {}, {}, 0
    while ready:
        nid = ready.pop(0)
        node, flow = by_id[nid], incoming[nid]
        allocations = proportional(flow * 4 // 5, node.withdrawal_requests,
                                   key=(seed, 'river_withdrawal', cycle, nid))
        if withdrawn.keys() & allocations.keys():
            raise ValueError('withdrawal IDs must be unique across river network')
        withdrawn.update(allocations)
        outflow = flow - sum(allocations.values())
        outflows[nid] = outflow
        if not node.downstream:
            ocean += outflow
        else:
            from .numeric import split_payment
            branches = split_payment(outflow, node.downstream, key=(seed, 'river_branch', cycle, nid))
            for dest, quantity in branches.items():
                incoming[dest] += quantity
                indegree[dest] -= 1
                if indegree[dest] == 0:
                    ready.append(dest)
            ready.sort()
        visited += 1
    if visited != len(nodes):
        raise InvariantViolation('river topology is not a DAG')
    if sum(n.natural_mU for n in nodes) != ocean + sum(withdrawn.values()):
        raise InvariantViolation('water created or lost at a branch')
    return RiverResult(withdrawn, outflows, ocean)


def pollution_next(old_mU: int, emitted_mU: int, remediation: Fraction) -> int:
    return max(0, round((Q(98, 100) - Q(2, 100) * clip(remediation, Q(0), Q(1))) * old_mU + emitted_mU))


def weather_ar(seed: int, tick: int, region: str, stream: str,
               previous: float, mean: float, persistence: float,
               sigma: float, low: float, high: float, prior_global_emissions: float) -> float:
    amplification = 1 + .5 * min(1, prior_global_emissions / 50000)
    return clip(mean + persistence * (previous - mean) + sigma * amplification * normal(seed, stream, tick, region), low, high)

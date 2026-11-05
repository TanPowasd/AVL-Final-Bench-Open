"""Exact units, explicit rounding and permutation-invariant allocation."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction

from .determinism import digest

MU = 1_000
NMU_PER_MU = 1_000
NMU_PER_UNIT = 1_000_000
Q = Fraction


def rational(value: int | str | Decimal | Fraction) -> Fraction:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("use integer, decimal text or Fraction, never binary float")
    return Fraction(value)


def floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def clip(value, low, high):
    if low > high:
        raise ValueError("empty interval")
    return min(high, max(low, value))


def units(value: int | str | Decimal | Fraction) -> int:
    result = rational(value) * MU
    if result.denominator != 1:
        raise ValueError("value is not an exact mU quantity")
    return result.numerator


def nonnegative(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected a nonnegative integer")
    return value


def proportional(capacity: int, requests: Mapping[str, int], *,
                 key: tuple[str | int, ...]) -> dict[str, int]:
    """01: proportional floors, then HASH-order tails, not remainder order."""
    nonnegative(capacity)
    for amount in requests.values():
        nonnegative(amount)
    total = sum(requests.values())
    if total <= capacity:
        return dict(requests)
    result = {k: capacity * d // total for k, d in requests.items()}
    remaining = capacity - sum(result.values())
    eligible = (k for k in requests if result[k] < requests[k])
    for k in sorted(eligible, key=lambda k: (digest(*key, k), k))[:remaining]:
        result[k] += 1
    return result


def split_payment(total: int, weights: Mapping[str, Fraction], *,
                  key: tuple[str | int, ...]) -> dict[str, int]:
    """11: ONE integer debit, largest rational remainders, hash on equal ties."""
    nonnegative(total)
    if any(w < 0 for w in weights.values()):
        raise ValueError("negative entitlement")
    weight = sum(weights.values(), Q(0))
    if weight == 0:
        if total:
            raise ValueError("payment without recipients")
        return {k: 0 for k in weights}
    exact = {k: total * w / weight for k, w in weights.items()}
    result = {k: floor(v) for k, v in exact.items()}
    extra = total - sum(result.values())
    if not extra:
        return result
    # Remainders sum to `extra` and are each < 1, so more than `extra` keys have
    # a positive remainder and no zero-remainder key can be chosen; ranking only
    # the positive ones selects the same set without hashing the rest.
    positive = [k for k in weights if exact[k] != result[k]]
    rank = sorted(positive, key=lambda k: (-(exact[k] - result[k]), digest(*key, k), k))
    for k in rank[:extra]:
        result[k] += 1
    return result


def max_feasible(high: int, predicate) -> int:
    """Largest feasible integer for a monotone resource constraint."""
    low = 0
    while low < high:
        mid = (low + high + 1) // 2
        if predicate(mid):
            low = mid
        else:
            high = mid - 1
    return low


def fair_service(capacity: int, demands: Mapping[str, int], received: Mapping[str, int],
                 caps: Mapping[str, int], *, key: tuple[str | int, ...]) -> dict[str, int]:
    """Batch the exact sequence of smallest received/demand, then hash ties.

    Water fill to K-n, then take every discrete token strictly below that
    level. At most n remaining quanta use the original discrete ordering.
    Filling continuously to K and rounding down is NOT a safe lower bound:
    demands (1, 9, 21), K=3 must each receive one before any receives two.
    Unlike proportional(), this supports previously served loads and caps.
    """
    nonnegative(capacity)
    ids = [k for k, d in demands.items() if d > 0 and caps.get(k, 0) > 0]
    target = min(capacity, sum(caps[k] for k in ids))
    result = {k: 0 for k in demands}
    if not target:
        return result
    continuous_target = max(0, target - len(ids))
    events: dict[Fraction, int] = {}
    for k in ids:
        if not 0 <= received[k] <= demands[k] or caps[k] > demands[k] - received[k]:
            raise ValueError('invalid fair-service bounds')
        start, end = Q(received[k], demands[k]), Q(received[k] + caps[k], demands[k])
        events[start] = events.get(start, 0) + demands[k]
        events[end] = events.get(end, 0) - demands[k]
    slope, volume, previous, level = 0, Q(0), Q(0), Q(0)
    for point in sorted(events):
        next_volume = volume + slope * (point - previous)
        if slope and next_volume >= continuous_target:
            level = previous + (continuous_target - volume) / slope
            break
        volume, previous = next_volume, point
        slope += events[point]
        level = point
    for k in ids:
        result[k] = min(caps[k], max(0, ceil(level * demands[k]) - received[k]))
    from heapq import heapify, heappop, heappush
    queue = [(Q(received[k] + result[k], demands[k]), digest(*key, k), k)
             for k in ids if result[k] < caps[k]]
    heapify(queue)
    for _ in range(target - sum(result.values())):
        _, tie, k = heappop(queue)
        result[k] += 1
        if result[k] < caps[k]:
            heappush(queue, (Q(received[k] + result[k], demands[k]), tie, k))
    return result

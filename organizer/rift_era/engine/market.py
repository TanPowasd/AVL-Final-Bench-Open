"""Deterministic uniform-price auctions and finite counterparty allocation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from fractions import Fraction

from .numeric import Q, proportional


@dataclass(frozen=True)
class Order:
    id: str
    owner: str
    side: str
    quantity: int  # integer shares, or mU inventory, chosen by the market
    limit: int  # integer quoted price
    priority: int = 1  # forced liquidation is the earlier priority band

    def __post_init__(self):
        if self.side not in ("buy", "sell") or type(self.quantity) is not int or self.quantity <= 0 or type(self.limit) is not int or self.limit <= 0:
            raise ValueError("invalid market order")


@dataclass(frozen=True)
class Auction:
    price: int
    quantity: int
    buy_fills: Mapping[str, int]
    sell_fills: Mapping[str, int]


def clear_auction(seed: int, tick: int, market_id: str, orders: tuple[Order, ...],
                  previous_price: int, *, quantum: int = 1) -> Auction:
    """Max volume, min imbalance, nearest previous price, lower price.

    Budget/inventory must already be reserved by P1. This pure calculation never
    assumes an unlimited NPC buyer or fills a price outside the crossed interval.
    """
    if quantum <= 0 or previous_price <= 0 or len({o.id for o in orders}) != len(orders):
        raise ValueError("invalid auction configuration")
    if any(o.quantity % quantum for o in orders):
        raise ValueError("order violates trade quantum")
    buys = [o for o in orders if o.side == 'buy']
    sells = [o for o in orders if o.side == 'sell']
    candidates = {previous_price} | {o.limit for o in orders}

    def rank(price):
        demand = sum(o.quantity for o in buys if o.limit >= price)
        supply = sum(o.quantity for o in sells if o.limit <= price)
        return -min(demand, supply), abs(demand - supply), abs(price - previous_price), price

    price = min(candidates, key=rank)
    quantity = -rank(price)[0]
    if quantity == 0:
        return Auction(previous_price, 0, {}, {})

    def fills(side_orders, buy):
        eligible = [o for o in side_orders if (o.limit >= price if buy else o.limit <= price)]
        bands = sorted({(o.priority, -o.limit if buy else o.limit) for o in eligible})
        result, remaining = {}, quantity // quantum
        for priority, limit in bands:
            group = {o.id: o.quantity // quantum for o in eligible
                     if (o.priority, -o.limit if buy else o.limit) == (priority, limit)}
            allocated = proportional(remaining, group,
                key=(seed, "market_fill", tick, market_id, "buy" if buy else "sell", priority, limit))
            result.update({oid: q * quantum for oid, q in allocated.items() if q})
            remaining -= sum(allocated.values())
            if not remaining:
                break
        return result

    return Auction(price, quantity, fills(buys, True), fills(sells, False))


def finite_pool_fills(seed: int, tick: int, cash_mU: int,
                      requested_costs_mU: Mapping[str, int]) -> dict[str, int]:
    """One combined batch across EVERY nation and good, not one pool per good."""
    return proportional(cash_mU, requested_costs_mU,
                        key=(seed, "world_pool", tick, "all_countries_and_goods"))


def debt_limit(tax_history_mU: tuple[int, ...]) -> int:
    # Missing history is zero; max is outside the sum.
    return max(1_000_000, 2 * sum(tax_history_mU[-20:]))


@dataclass
class Bond:
    id: str
    issuer: str
    currency: str
    holders_face_mU: dict[str, int]
    annual_rate_bp: int
    due_cycle: int
    unpaid_interest: Fraction = Q(0)

    @property
    def principal_mU(self) -> int:
        return sum(self.holders_face_mU.values())

    def cycle_interest_mU(self) -> Fraction:
        # Basis points per year, 100 cycles per year.
        return Q(self.principal_mU * self.annual_rate_bp, 1_000_000)


@dataclass(frozen=True)
class DebtSettlement:
    paid: Mapping[str, int]
    unpaid: Mapping[str, int]
    defaulted: bool


def settle_obligations(seed: int, cycle: int, available_mU: int,
                       due: Mapping[str, int], *, kind: str) -> DebtSettlement:
    paid = proportional(available_mU, due, key=(seed, "debt", cycle, kind))
    unpaid = {k: q - paid[k] for k, q in due.items()}
    return DebtSettlement(paid, unpaid, any(unpaid.values()))

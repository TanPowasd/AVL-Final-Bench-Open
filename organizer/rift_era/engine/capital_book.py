"""Bounded auxiliary precision for proportional remaining capital cost.

One nmU contains 10**18 cost-book quanta. Physical inventory remains integer mU;
this grid applies only to remaining historical capital cost, never action-loss
minimums or military HP. Every removal is subtracted exactly from its source.
"""
from .numeric import Q, floor

CAPITAL_BOOK_SCALE=10**18


def proportional_loss(book_nmU, damage, health):
    if type(damage) is not int or type(health) is not int or not 0<=damage<=health:
        raise ValueError('invalid proportional capital damage')
    if not damage:return {}
    if damage==health:return dict(book_nmU)
    return {good:Q(floor(Q(quantity)*damage*CAPITAL_BOOK_SCALE/health),CAPITAL_BOOK_SCALE)
            for good,quantity in book_nmU.items()}


def loss_error_bound_nmU(rounding_steps):
    # Retained cost error contracts under later damage. The total variation of
    # loss errors is at most twice the sum of the new rounding residuals.
    return Q(2*rounding_steps,CAPITAL_BOOK_SCALE)

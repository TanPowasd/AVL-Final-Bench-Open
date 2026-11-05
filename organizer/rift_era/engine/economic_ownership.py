"""Economic title is independent of origin and physical national custody.

A government and its six companies share a nation's command authority, but
their cash and stock income are separate. This identifier names the real
income account without importing, or mutating, either ledger.
"""
from dataclasses import dataclass
from collections import defaultdict

from .numeric import Q, floor


@dataclass(frozen=True, order=True)
class EconomicOwner:
    nation: str
    account: str

    def __post_init__(self):
        if not self.nation or not self.account:
            raise ValueError('economic owner needs a nation and an account')


def income_account(origin, custodian, currency, *, government_account='treasury'):
    """A lot without a company title is real government-owned inventory."""
    from .ledger import CashAccount
    title=origin.economic_owner or EconomicOwner(custodian,government_account)
    return CashAccount(title.nation,title.account,currency)


def purchase_quote(payments,payer,exact,kind):
    from .ledger import CashAccount
    key=(payer,CashAccount('clearing',kind,payer.currency),'invoice:'+kind)
    return floor(exact+payments.residuals.get(key,Q(0)))


def stock_purchase_ceiling(inventory,payments,payer,buyer,stocks,prices,*,kind):
    """Reserve external purchase cost without requiring cash for own stocks."""
    total=Q(0)
    for account,quantity in stocks.items():
        origins=inventory.materials.composition(account)
        if any(income_account(origin,account.owner,payer.currency)!=buyer for origin in origins):
            total+=quantity*prices[account.asset]
    return purchase_quote(payments,payer,total,kind)


def prepare_purchase(payments,payer,events,prices,*,kind,key,buyer=None,payer_scope=None):
    """One conserved invoice, split only among actual consumed-lot owners."""
    from .payments import Obligation
    values=defaultdict(lambda:Q(0))
    for event in events:
        owner=event.owner if hasattr(event,'owner') else event.source.owner
        good=event.good if hasattr(event,'good') else event.source.asset
        target=income_account(event.origin,owner,payer.currency)
        if target!=buyer:values[target,good]+=event.quantity_mU*prices[good]
    obligations=tuple(Obligation(payer,target,amount,good) for (target,good),amount in sorted(values.items()))
    plan=payments.prepare_invoice(obligations,kind=kind,key=key,payer_scope=payer_scope)
    return plan,tuple(row.target for row in obligations)


def record_income(state,recipients,amounts):
    for target,amount in zip(recipients,amounts):
        if target.owner in state.cycles:
            metric=state.cycles[target.owner]
            row=metric.cash_income_by_currency.setdefault(target.currency,{})
            row[target.name]=row.get(target.name,0)+amount
            rate=Q(1) if target.currency=='RU' else state.rates[target.currency]
            # Consolidated realized-cash valuation only. Actual cash remains
            # in its named currency account until a real FX transaction.
            metric.revenue[target.name]=metric.revenue.get(target.name,Q(0))+Q(amount)*rate/state.rates[target.owner]

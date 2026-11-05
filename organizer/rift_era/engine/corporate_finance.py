"""Corporate cash profit, actual tax service and ownership-weighted dividends.

Values in
``CycleEconomy.revenue/costs`` are issuer-native valuations of ACTUAL paid
cash, not inventory output or FX revaluation. Every payment below is native
cash obtained from an existing balance or an actual finite-pool FX fill.
"""
from dataclasses import dataclass

from .command_schema import fields
from .errors import InvariantViolation, RuleViolation
from .ledger import CashAccount
from .numeric import Q, ceil, floor, split_payment
from .payments import FXRequest, Obligation
from .protocol import PreparedCommand
from .rational_carry import RationalCarry


@dataclass(frozen=True)
class CorporateReport:
    profit_native_mU: Q
    tax_due_native_mU: int
    tax_paid_native_mU: int
    tax_arrears_native_mU: int
    distributable_native_mU: Q
    dividend_paid_native_mU: int = 0
    current_tax_assessed_native_mU: int = 0
    current_dividend_declared_native_mU: int = 0


class CorporateFinance:
    def __init__(self,state):
        self.s=state
        self.payout_mU=dict.fromkeys(state.initial.companies,0)
        self.reports={}
        self.last_tax_cycle=-1
        self.last_dividend_cycle=-1
        self.received={p:[] for p in state.initial.known}
        self.tax_carries={cid:RationalCarry() for cid in state.initial.companies}
        self.dividend_carries={cid:RationalCarry() for cid in state.initial.companies}
        self.dividend_claims={cid:{} for cid in state.initial.companies}

    @staticmethod
    def cash(company,currency=None):
        return CashAccount(company.issuer,'corp:'+company.sector,currency or company.issuer)

    @staticmethod
    def tax_pool(company):
        return CashAccount(company.issuer,'tax_receipts',company.issuer)

    @staticmethod
    def shareholder_account(holder,company):
        if holder.startswith('npc:'):
            return CashAccount(holder,'portfolio',company.issuer)
        return CashAccount(holder,'treasury',company.issuer)

    def arrears(self,company):
        return self.s.payments.arrears.get((self.cash(company),self.tax_pool(company),'corporate_tax'),0)

    def profit(self,company):
        metric=self.s.cycles[company.issuer]
        name='corp:'+company.sector
        return metric.revenue.get(name,Q(0))-metric.costs.get(name,Q(0))

    def requirement(self,company):
        profit=max(Q(0),self.profit(company))
        owner=company.issuer
        tax=self.s.tax[owner]
        collection=self.s.governance.collection_factor(owner+'-h0')
        dividends=self.dividend_carries[company.id].prepare(profit*(1-tax)*Q(self.payout_mU[company.id],1000)).due
        taxes=self.tax_carries[company.id].prepare(profit*tax*collection).due
        return taxes+dividends+self.arrears(company)+sum(self.dividend_claims[company.id].values())

    def _record_fx_costs(self,requests):
        state=self.s
        for request in requests:
            fill=state.fx.last_fills[request.id]
            metric=state.cycles[request.source.owner]
            source_rate=Q(1) if request.source.currency=='RU' else state.rates[request.source.currency]
            cost=Q(fill.fee_mU+fill.control_mU)*source_rate/state.rates[request.source.owner]
            metric.costs[request.source.name]=metric.costs.get(request.source.name,Q(0))+cost

    def _fund(self,ctx):
        state=self.s;cash=state.initial.cash
        foreign_requests=[]
        balances=cash.balances()
        for company in state.initial.companies.values():
            short=max(0,self.requirement(company)-cash.balance(self.cash(company)).native_mU)
            needed=max(0,ceil(short*state.rates[company.issuer]*(1+state.fx.fee))+1-
                       cash.balance(self.cash(company,'RU')).native_mU) if short else 0
            for source,balance in sorted(balances.items()):
                if not needed:break
                if (source.owner,source.name)!=(company.issuer,'corp:'+company.sector) or source.currency in (company.issuer,'RU'):continue
                principal=ceil(Q(needed)/state.rates[source.currency])
                surcharge=state.fx.fee+Q(state.fx.controls.get(source.currency,0),10000)
                offered=min(balance.native_mU,ceil(principal*(1+surcharge))+2)
                if offered:
                    foreign_requests.append(FXRequest('corporate_foreign:'+company.id+':'+source.currency,source,
                        self.cash(company,'RU'),offered,state.rates[source.currency],principal_limit_mU=principal))
                    needed=max(0,needed-floor(min(principal,offered)*state.rates[source.currency]))
        if foreign_requests:
            state.fx.execute(ctx.seed,ctx.tick,tuple(foreign_requests))
            self._record_fx_costs(foreign_requests)
        requests=[]
        for company in state.initial.companies.values():
            native=self.cash(company)
            short=max(0,self.requirement(company)-cash.balance(native).native_mU)
            if not short:continue
            source=self.cash(company,'RU')
            principal=ceil(short*state.rates[company.issuer])
            offered=min(cash.balance(source).native_mU,ceil(principal*(1+state.fx.fee))+1)
            if offered:
                requests.append(FXRequest('corporate_settlement:'+company.id,source,native,offered,
                    1/state.rates[company.issuer],principal_limit_mU=principal))
        if requests:
            state.fx.execute(ctx.seed,ctx.tick,tuple(requests))
            self._record_fx_costs(requests)

    def taxes(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle<=self.last_tax_cycle:raise InvariantViolation('corporate tax cycle repeated')
        self._fund(ctx)
        reports={}
        for cid,company in self.s.initial.companies.items():
            profit=self.profit(company)
            taxable=max(Q(0),profit)
            current=taxable*self.s.tax[company.issuer]*self.s.governance.collection_factor(company.issuer+'-h0')
            carry=self.tax_carries[cid].prepare(current)
            paid=self.s.governance.collect_tax(self.cash(company),company.issuer+'-h0',
                Q(carry.due),kind='corporate_tax',allow_arrears=True,settle_arrears=True,apply_collection=False)
            carry.commit()
            reports[cid]=CorporateReport(profit,paid.due_mU,paid.paid_mU,paid.unpaid_mU,
                                         taxable*(1-self.s.tax[company.issuer]),current_tax_assessed_native_mU=carry.due)
        self.reports=reports;self.last_tax_cycle=ctx.cycle

    def dividends(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if self.last_tax_cycle!=ctx.cycle or ctx.cycle<=self.last_dividend_cycle:
            raise InvariantViolation('dividend cycle is missing tax settlement or repeated')
        from dataclasses import replace
        state=self.s
        for cid,company in state.initial.companies.items():
            row=self.reports[cid]
            payout=row.distributable_native_mU*Q(self.payout_mU[cid],1000)
            payer=self.cash(company)
            carry=self.dividend_carries[cid].prepare(payout)
            declared=carry.due
            newly_due=split_payment(declared,{holder:Q(shares) for holder,shares in company.shares.items()},
                                    key=(ctx.seed,ctx.cycle,'dividend_entitlement',cid))
            due=dict(self.dividend_claims[cid])
            for holder,amount in newly_due.items():due[holder]=due.get(holder,0)+amount
            available=0 if row.tax_arrears_native_mU else state.initial.cash.balance(payer).native_mU
            pay=min(sum(due.values()),available)
            paid=split_payment(pay,{holder:Q(amount) for holder,amount in due.items()},
                               key=(ctx.seed,ctx.cycle,'dividend_payment',cid))
            obligations=tuple(Obligation(payer,self.shareholder_account(holder,company),Q(amount),'dividend')
                              for holder,amount in sorted(paid.items()) if amount)
            payment=state.payments.prepare(obligations)
            payment.commit()
            carry.commit()
            self.dividend_claims[cid]={holder:amount-paid[holder] for holder,amount in due.items() if amount>paid[holder]}
            for obligation,amount in zip(obligations,payment.amounts):
                if obligation.target.owner in self.received:
                    owner=obligation.target.owner
                    self.received[owner].append({'company':cid,'currency':company.issuer,'amount_mU':amount,'cycle':ctx.cycle})
                    self.received[owner]=self.received[owner][-128:]
            self.reports[cid]=replace(row,dividend_paid_native_mU=sum(payment.amounts),
                                     current_dividend_declared_native_mU=declared)
            state.stock_market.finance.dividend_compensation(cid,declared,ctx)
        self.last_dividend_cycle=ctx.cycle

    def visible(self,owner):
        return {'payout_policies':{cid:p for cid,p in self.payout_mU.items() if self.s.initial.companies[cid].issuer==owner},
                'companies':[{'company':cid,'payout_mU':self.payout_mU[cid],
                    'profit_native_mU':floor(row.profit_native_mU),
                    'tax_due_mU':row.tax_due_native_mU,'tax_paid_mU':row.tax_paid_native_mU,
                    'current_tax_assessed_mU':row.current_tax_assessed_native_mU,
                    'tax_arrears_mU':row.tax_arrears_native_mU,'dividend_paid_mU':row.dividend_paid_native_mU}
                    for cid,row in self.reports.items() if self.s.initial.companies[cid].issuer==owner],
                'received_dividends':[dict(row) for row in self.received[owner]],
                'dividends_payable':[{'company':cid,'amount_mU':sum(claims.values())} for cid,claims in self.dividend_claims.items()
                                     if self.s.initial.companies[cid].issuer==owner and claims],
                'dividends_receivable':[{'company':cid,'amount_mU':claims[owner]} for cid,claims in self.dividend_claims.items()
                                        if claims.get(owner,0)]}

    def snapshot(self):
        return {'payout_mU':self.payout_mU,'reports':self.reports,'last_tax_cycle':self.last_tax_cycle,
                'last_dividend_cycle':self.last_dividend_cycle,
                'received':self.received,'tax_carries':{cid:c.snapshot() for cid,c in self.tax_carries.items()},
                'dividend_carries':{cid:c.snapshot() for cid,c in self.dividend_carries.items()},'dividend_claims':self.dividend_claims}

    def audit(self):
        if set(self.payout_mU)!=set(self.s.initial.companies) or any(not 0<=v<=1000 for v in self.payout_mU.values()):
            raise InvariantViolation('invalid corporate dividend policy')
        for carry in (*self.tax_carries.values(),*self.dividend_carries.values()):carry.audit()
        if any(q<=0 for claims in self.dividend_claims.values() for q in claims.values()):
            raise InvariantViolation('invalid unpaid dividend claim')


class DividendCommands:
    OPERATIONS=frozenset({('finance','dividend')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'company':str,'payout_mU':int})
        company=self.state.initial.companies.get(order['company'])
        if company is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if company.issuer!=owner or company.shares.get(owner,0)<510000:raise RuleViolation('ERR_NO_PERMISSION')
        if not 0<=order['payout_mU']<=1000:raise RuleViolation('ERR_SCHEMA')
        return PreparedCommand(lambda:self.state.corporate_finance.payout_mU.__setitem__(company.id,order['payout_mU']))

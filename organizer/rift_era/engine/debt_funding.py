"""P1/P7 currency-specific hard-debt funding, interest before principal.

Providers describe obligations; this service reserves only actual free native
cash and successful two-leg RU conversions. Same-priority claims share one
payer/currency pool. A provider cannot consume that pool ahead of its peers.
"""
from collections import defaultdict
from dataclasses import dataclass

from .errors import InvariantViolation
from .ledger import CashAccount,CashTransfer
from .numeric import Q,ceil,proportional
from .payments import FXRequest


@dataclass(frozen=True)
class FundingClaim:
    id: str
    owner: str
    currency: str
    priority: int
    amount_mU: int
    deposit: CashAccount


class DebtFunding:
    def __init__(self,state):
        self.s=state;self.providers=[];self.last_claims=()

    @staticmethod
    def wallet(owner,currency):return CashAccount(owner,'reserves' if currency=='RU' else 'treasury',currency)

    def add_provider(self,provider):self.providers.append(provider)

    def reserve(self,ctx):
        if ctx.clock.is_cycle_end(ctx.tick):self.fund(ctx)

    def fund(self,ctx):
        state=self.s;cash=state.initial.cash
        claims=tuple(claim for provider in self.providers for claim in provider(ctx))
        if len({c.id for c in claims})!=len(claims):raise InvariantViolation('duplicate debt funding claim')
        if any(type(c.amount_mU) is not int or c.amount_mU<0 or c.priority not in (0,1) or
               c.deposit.owner!=c.owner or c.deposit.currency!=c.currency for c in claims):
            raise InvariantViolation('invalid debt funding obligation')
        # Re-arbitrate the protected money at P7. A P1 principal reservation
        # must not outrank interest newly payable to a different creditor.
        # Include disappeared claims so cancellation cannot strand money.
        deposits={c.deposit for c in (*self.last_claims,*claims)}
        cash.transfer_many(CashTransfer(account,self.wallet(account.owner,account.currency),
            cash.balance(account).native_mU) for account in sorted(deposits))
        remaining={claim.id:claim.amount_mU for claim in claims}
        by_id={c.id:c for c in claims}

        def native_pass(priority,round_number):
            grouped=defaultdict(dict)
            for claim in claims:
                if claim.priority==priority:grouped[self.wallet(claim.owner,claim.currency)][claim.id]=remaining[claim.id]
            transfers=[]
            for source,requests in sorted(grouped.items()):
                grants=proportional(cash.balance(source).native_mU,requests,
                    key=(ctx.seed,'hard_debt_funding',ctx.tick,priority,round_number,source.owner,source.currency))
                for cid,amount in grants.items():
                    transfers.append(CashTransfer(source,by_id[cid].deposit,amount));remaining[cid]-=amount
            cash.transfer_many(transfers)

        for priority in (0,1):
            native_pass(priority,0)
            short=defaultdict(int)
            for claim in claims:
                if claim.priority==priority and claim.currency!='RU':short[claim.owner,claim.currency]+=remaining[claim.id]
            requests=[];by_reserve=defaultdict(dict)
            for (owner,currency),amount in sorted(short.items()):
                if amount:
                    principal=ceil(amount*state.rates[currency])
                    by_reserve[owner][currency]=ceil(principal*(1+state.fx.fee))+1
            for owner,amounts in sorted(by_reserve.items()):
                source=CashAccount(owner,'reserves','RU')
                offered=proportional(cash.balance(source).native_mU,amounts,
                    key=(ctx.seed,'hard_debt_fx',ctx.tick,priority,owner))
                for currency,amount in sorted(offered.items()):
                    if amount:requests.append(FXRequest('hard_debt:'+str(priority)+':'+owner+':'+currency,source,
                        self.wallet(owner,currency),amount,1/state.rates[currency],
                        principal_limit_mU=ceil(short[owner,currency]*state.rates[currency])))
            if requests:state.fx.execute(ctx.seed,ctx.tick,tuple(requests))
            native_pass(priority,1)
        self.last_claims=claims

    def snapshot(self):return {'last_claims':self.last_claims}

"""Creditor-specific external payables: an assessment is never pool cash.

Fines can consume actual treasury FX and RU reserves. Unpaid amounts retain a
currency and their incurred historical basis; subsequent payment reduces the
same claim. The shared hard-debt allocator funds old claims at cycle end.
"""
from collections import defaultdict
from dataclasses import dataclass

from .debt_funding import FundingClaim
from .errors import InvariantViolation
from .ledger import CashAccount, CashTransfer, CASH_BOOK_SCALE
from .numeric import Q, ceil, proportional
from .payments import FXRequest
from .rational_carry import RationalCarry


@dataclass
class Payable:
    amount_mU: int
    book_ru_mU: Q


class Payables:
    def __init__(self,state):
        self.s=state;self.claims={};self.carries={};self.reports=[]

    @staticmethod
    def deposit(owner,currency):return CashAccount(owner,'payable_debt_reserved',currency)

    def add(self,owner,creditor,currency,amount,*,reason,book_ru_mU=None):
        if type(amount) is not int or amount<0 or owner==creditor.owner or creditor.currency!=currency:
            raise InvariantViolation('invalid external payable')
        if not amount:return
        key=(owner,creditor,reason)
        book=amount*(Q(1) if currency=='RU' else self.s.rates[currency]) if book_ru_mU is None else book_ru_mU
        if book<0:raise InvariantViolation('negative payable basis')
        row=self.claims.setdefault(key,Payable(0,Q(0)))
        row.amount_mU+=amount;row.book_ru_mU+=book

    def _reduce(self,key,paid):
        row=self.claims[key]
        if not 0<=paid<=row.amount_mU:raise InvariantViolation('overpaid external claim')
        removed=row.book_ru_mU if paid==row.amount_mU else Q(round(row.book_ru_mU*paid/row.amount_mU*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
        row.amount_mU-=paid;row.book_ru_mU-=removed
        if not row.amount_mU:del self.claims[key]

    def funding_claims(self,ctx):
        totals=defaultdict(int)
        for (owner,creditor,_),row in self.claims.items():totals[owner,creditor.currency]+=row.amount_mU
        return tuple(FundingClaim('payables:'+p+':'+currency,p,currency,1,amount,self.deposit(p,currency))
                     for (p,currency),amount in sorted(totals.items()))

    def settle(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        groups=defaultdict(dict)
        for key,row in self.claims.items():groups[key[0],key[1].currency][key]=row.amount_mU
        for (owner,currency),dues in sorted(groups.items()):
            source=self.deposit(owner,currency)
            names={str(i):key for i,key in enumerate(sorted(dues))}
            paid=proportional(self.s.initial.cash.balance(source).native_mU,{n:dues[k] for n,k in names.items()},
                              key=(ctx.seed,'payable_payment',ctx.cycle,owner,currency))
            self.s.initial.cash.transfer_many(CashTransfer(source,names[n][1],q) for n,q in paid.items())
            for n,q in paid.items():self._reduce(names[n],q)
            if any(paid[n]<dues[k] for n,k in names.items()):self.s.sovereign_finance.note_default(owner,ctx.cycle)

    def assess_ru(self,ctx,assessments,*,reason):
        """Freeze every assessment, then batch actual native->RU payments.

        Treasury native cash is used first, then reserves. A failed FX cannot
        debit native principal or invent a WorldClearPool receipt.
        """
        cash=self.s.initial.cash;dues={};requests=[]
        for owner,amount in sorted(assessments.items()):
            carry=self.carries.setdefault((owner,reason),RationalCarry())
            dues[owner]=carry.add(amount)
            if not dues[owner]:continue
            source=self.s.native(owner,'treasury');rate=self.s.rates[owner]
            principal=ceil(Q(dues[owner])/rate)
            surcharge=self.s.fx.fee+Q(self.s.fx.controls.get(owner,0),10000)
            offered=min(cash.balance(source).native_mU,ceil(principal*(1+surcharge))+1)
            if offered:requests.append(FXRequest('penalty:'+reason+':'+owner,source,
                CashAccount(owner,'penalty_payment:'+reason,'RU'),offered,rate,principal_limit_mU=principal))
        if requests:self.s.fx.execute(ctx.seed,ctx.tick,tuple(requests))
        for owner,due in sorted(dues.items()):
            temporary=CashAccount(owner,'penalty_payment:'+reason,'RU');reserves=CashAccount(owner,'reserves','RU')
            target=CashAccount('world','pool','RU')
            first=min(due,cash.balance(temporary).native_mU);cash.transfer(temporary,target,first)
            second=min(due-first,cash.balance(reserves).native_mU);cash.transfer(reserves,target,second)
            excess=cash.balance(temporary).native_mU
            if excess:cash.transfer(temporary,reserves,excess)
            unpaid=due-first-second
            if unpaid:
                self.add(owner,target,'RU',unpaid,reason=reason)
                self.s.sovereign_finance.note_default(owner,ctx.cycle)
            self.reports.append({'owner':owner,'reason':reason,'cycle':ctx.cycle,'currency':'RU',
                                 'due_mU':due,'paid_mU':first+second,'unpaid_mU':unpaid})
        self.reports=self.reports[-128:]

    def debt_book(self,owner):return sum((row.book_ru_mU for (p,_,_),row in self.claims.items() if p==owner),Q(0))

    def visible(self,owner):
        return {'payables':[{'creditor':c.owner,'currency':c.currency,'reason':r,'amount_mU':row.amount_mU}
                            for (p,c,r),row in sorted(self.claims.items()) if p==owner],
                'recent':[dict(row) for row in self.reports if row['owner']==owner]}

    def audit(self):
        if any(row.amount_mU<=0 or row.book_ru_mU<0 for row in self.claims.values()):raise InvariantViolation('invalid payable balance')
        for carry in self.carries.values():carry.audit()

    def snapshot(self):
        return {'claims':[[*key,row] for key,row in sorted(self.claims.items())],
                'carries':[[*key,c.snapshot()] for key,c in sorted(self.carries.items())],'reports':self.reports}

"""Cash-originated bilateral sovereign debt with pledged economic asset shares.

All service money is supplied by the shared DebtFunding provider. This book
never races the bond service for a treasury balance. Collateral cannot convey
more current residual value than the unpaid debt it extinguishes.
"""
from collections import defaultdict
from dataclasses import dataclass,field
from .debt_funding import FundingClaim
from .errors import RuleViolation,InvariantViolation
from .ledger import CashAccount,CashTransfer,CASH_BOOK_SCALE
from .numeric import Q,floor,proportional,split_payment
from .protocol import PreparedCommand


@dataclass(frozen=True)
class LoanProposal:
    id:str
    proposer:str
    counterparty:str
    command_id:str
    currency:str
    principal_mU:int
    annual_rate_bp:int
    due_cycle:int
    collateral:tuple[tuple[str,int],...]
    duration_cycles:int
    expires_cycle:int


@dataclass
class SovereignLoan:
    proposal:LoanProposal
    originated_cycle:int
    face_mU:int
    book_ru_mU:Q
    due_cycle:int
    annual_rate_bp:int
    collateral:dict[str,int]=field(default_factory=dict)
    status:str='ACTIVE'


class SovereignLoans:
    def __init__(self,state):
        self.s=state;self.rows={};self.pending=[];self.last_cycle=-1;self.reports=[];self.coupon_residual={}

    @staticmethod
    def wallet(owner,currency):return CashAccount(owner,'reserves' if currency=='RU' else 'treasury',currency)
    @staticmethod
    def deposit(owner,currency,priority):return CashAccount(owner,'bilateral_loan_'+str(priority),currency)
    def principal_lock(self,p):return CashAccount(p.proposer,'loan_principal:'+p.id,p.currency)

    def count(self,owner):return sum(owner in (r.proposal.proposer,r.proposal.counterparty) and r.face_mU>0 for r in self.rows.values())

    def collateral_value(self,asset_id,shares,currency):
        asset=self.s.initial.assets.get(asset_id)
        if asset is None:return Q(0)
        ru_mU=sum((q*self.s.catalog.weights[g]/1000 for g,q in asset.book_nmU.items()),Q(0))
        return ru_mU*Q(shares,1000)/(Q(1) if currency=='RU' else self.s.rates[currency])

    def validate(self,p):
        state=self.s
        if state.relations.will_be_at_war(p.proposer,p.counterparty):raise RuleViolation('ERR_NO_PERMISSION')
        if state.contracts.embargo_blocks(p.proposer,p.counterparty):raise RuleViolation('ERR_NO_PERMISSION')
        for aid,shares in p.collateral:
            rights=state.authority.assets.get(aid)
            if rights is None or rights.free_shares(p.counterparty)<shares:raise RuleViolation('ERR_NO_PERMISSION','borrower does not have unpledged collateral')

    def offer(self,owner,order,ctx):
        state=self.s;book=state.contracts;other=order['counterparty'];p=order['params']
        if (other==owner or not 10<=order['duration_cycles']<=720 or
                set(p)!={'currency','principal_mU','annual_rate_bp','due_cycle','collateral'} or type(p['currency']) is not str or
                any(type(p[k]) is not int for k in ('principal_mU','annual_rate_bp','due_cycle')) or
                p['principal_mU']<=0 or not 0<=p['annual_rate_bp']<=5000 or p['due_cycle']<=ctx.cycle or
                type(p['collateral']) is not list):raise RuleViolation('ERR_SCHEMA')
        if other not in state.initial.known or p['currency'] not in ('RU',*state.initial.known):raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        collateral=[]
        for row in p['collateral']:
            if type(row) is not dict or set(row)!={'asset_id','shares_mU'} or type(row['asset_id']) is not str or type(row['shares_mU']) is not int or not 1<=row['shares_mU']<=1000:
                raise RuleViolation('ERR_SCHEMA')
            collateral.append((row['asset_id'],row['shares_mU']))
        if len({aid for aid,_ in collateral})!=len(collateral):raise RuleViolation('ERR_SCHEMA')
        if any(book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation'] for party in (owner,other)):raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        proposal=LoanProposal(f'loan_proposal:{owner}:{int(order["id"])}',owner,other,str(int(order['id'])),p['currency'],p['principal_mU'],
            p['annual_rate_bp'],p['due_cycle'],tuple(collateral),order['duration_cycles'],ctx.cycle+order['duration_cycles'])
        self.validate(proposal)
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def prepare_accept(self,p,ctx):
        self.validate(p)
        cash=self.s.initial.cash.prepare_transfers((CashTransfer(self.wallet(p.proposer,p.currency),self.principal_lock(p),p.principal_mU),))
        def accept():
            cash.commit()
            for aid,shares in p.collateral:self.s.authority.pledge(aid,p.id,p.counterparty,shares)
            self.pending.append(p);self.s.contracts.log(p,'SIGNED');del self.s.contracts.proposals[p.id]
        return PreparedCommand(accept)

    def settle_originations(self,ctx):
        transfers=[]
        for p in self.pending:
            target=self.wallet(p.counterparty,p.currency)
            transfers.append(CashTransfer(self.principal_lock(p),target,p.principal_mU))
        self.s.initial.cash.transfer_many(transfers)
        for p in self.pending:
            basis=p.principal_mU*(Q(1) if p.currency=='RU' else self.s.rates[p.currency])
            self.rows[p.id]=SovereignLoan(p,ctx.cycle,p.principal_mU,basis,p.due_cycle,p.annual_rate_bp,dict(p.collateral))
        self.pending.clear()

    def due(self,ctx):
        exact=defaultdict(dict);principal=defaultdict(dict)
        for lid,row in self.rows.items():
            if not row.face_mU:continue
            key=(row.proposal.counterparty,row.proposal.currency)
            if row.originated_cycle<ctx.cycle:exact[key][lid]=Q(row.face_mU*row.annual_rate_bp,1000000)
            if row.due_cycle<=ctx.cycle:principal[key][lid]=row.face_mU
        interest={}
        for key,weights in exact.items():
            amount=floor(sum(weights.values(),Q(0))+self.coupon_residual.get(key,Q(0)))
            interest[key]=split_payment(amount,weights,key=(ctx.seed,'loan_coupon_due',ctx.cycle,*key)) if weights else {}
        return exact,interest,principal

    def funding_claims(self,ctx):
        _,interest,principal=self.due(ctx);claims=[]
        for priority,table in ((0,interest),(1,principal)):
            for (owner,currency),rows in sorted(table.items()):
                claims.append(FundingClaim('bilateral_loan:'+str(priority)+':'+owner+':'+currency,owner,currency,priority,
                    sum(rows.values()),self.deposit(owner,currency,priority)))
        return tuple(claims)

    def reduce(self,row,amount):
        removed=row.book_ru_mU if amount==row.face_mU else Q(round(row.book_ru_mU*amount/row.face_mU*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
        row.face_mU-=amount;row.book_ru_mU-=removed

    def settle(self,ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle<=self.last_cycle:raise InvariantViolation('bilateral loan service repeated')
        self.last_cycle=ctx.cycle
        exact,interest,principal=self.due(ctx);cash=self.s.initial.cash;reports=[]
        for key,weights in exact.items():
            self.coupon_residual[key]=sum(weights.values(),Q(0))+self.coupon_residual.get(key,Q(0))-sum(interest[key].values())
        for priority,table in ((0,interest),(1,principal)):
            transfers=[];foreclose=[]
            for (owner,currency),dues in sorted(table.items()):
                deposit=self.deposit(owner,currency,priority)
                paid=proportional(cash.balance(deposit).native_mU,dues,key=(ctx.seed,'loan_service',ctx.cycle,priority,owner,currency))
                for lid,amount in paid.items():
                    row=self.rows[lid];transfers.append(CashTransfer(deposit,self.wallet(row.proposal.proposer,currency),amount))
                    if priority==1:self.reduce(row,amount)
                    missing=dues[lid]-amount
                    if priority==0 and missing:
                        row.face_mU+=missing;row.book_ru_mU+=missing*(Q(1) if currency=='RU' else self.s.rates[currency])
                    if missing:
                        self.s.sovereign_finance.note_default(owner,ctx.cycle)
                        if priority==1:foreclose.append(row)
                    reports.append({'loan_id':lid,'priority':priority,'due_mU':dues[lid],'paid_mU':amount})
            cash.transfer_many(transfers)
            for row in foreclose:self.foreclose(row)
        for row in self.rows.values():
            if not row.face_mU:self.release(row);row.status='REPAID'
            elif row.due_cycle<=ctx.cycle:row.due_cycle=ctx.cycle+50;row.annual_rate_bp+=500
        self.reports=reports

    def release(self,row):
        for aid in tuple(row.collateral):
            rights=self.s.authority.assets.get(aid)
            if rights is not None: rights.pledged.pop(row.proposal.id,None)
        row.collateral.clear()

    def foreclose(self,row):
        for aid,shares in sorted(row.collateral.items()):
            unit=self.collateral_value(aid,1,row.proposal.currency)
            take=min(shares,floor(Q(row.face_mU)/unit)) if unit else 0
            if take and floor(take*unit)==0:take=0
            if take:
                self.s.authority.transfer_shares(aid,row.proposal.counterparty,row.proposal.proposer,take,collateral_id=row.proposal.id)
                self.reduce(row,min(row.face_mU,floor(take*unit)))
            rights=self.s.authority.assets.get(aid)
            if rights is not None:rights.pledged.pop(row.proposal.id,None)
        row.collateral.clear()

    def audit(self):
        for row in self.rows.values():
            if row.face_mU<0 or row.book_ru_mU<0 or row.due_cycle<=row.originated_cycle:
                raise InvariantViolation('invalid bilateral loan debt')
            for aid,shares in row.collateral.items():
                rights=self.s.authority.assets.get(aid)
                if rights is None or rights.pledged.get(row.proposal.id,{}).get(row.proposal.counterparty)!=shares:
                    raise InvariantViolation('loan collateral differs from pledged asset custody')
        if any(not 0<=q<1 for q in self.coupon_residual.values()):raise InvariantViolation('invalid loan coupon carry')

    def debt_book(self,owner):return sum((r.book_ru_mU for r in self.rows.values() if r.proposal.counterparty==owner),Q(0))
    def creditor_book(self,owner):return sum((r.book_ru_mU for r in self.rows.values() if r.proposal.proposer==owner),Q(0))
    def visible(self,owner):return {'loans':[r for r in self.rows.values() if owner in (r.proposal.proposer,r.proposal.counterparty)],'recent':[r for r in self.reports if self.rows[r['loan_id']].proposal.counterparty==owner]}
    def snapshot(self):return {'rows':self.rows,'pending':self.pending,'last_cycle':self.last_cycle,'reports':self.reports,
                              'coupon_residual':[[*k,q] for k,q in sorted(self.coupon_residual.items())]}

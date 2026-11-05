"""Finite primary bond underwriting and cash-backed sovereign debt service.

Face is a liability only after actual underwriting cash is paid. Coupon
rounding belongs to the issuer's combined obligation, not each tiny bond.
Interest is serviced before principal; unpaid interest becomes real additional
holder face and unpaid matured principal rolls once per cycle.
"""
from dataclasses import dataclass, field

from .command_schema import fields
from .errors import InvariantViolation, RuleViolation
from .ledger import CashAccount, CashTransfer, CASH_BOOK_SCALE
from .market import debt_limit
from .numeric import Q, ceil, floor, proportional, split_payment
from .payments import FXRequest
from .protocol import PreparedCommand


@dataclass
class SovereignBond:
    id: str
    issuer: str
    created_cycle: int
    due_cycle: int
    annual_rate_bp: int
    holders_face_mU: dict[str, int] = field(default_factory=dict)
    proceeds_ru_mU: Q = Q(0)

    @property
    def face(self):
        return sum(self.holders_face_mU.values())


@dataclass
class PrimaryOffer:
    id: str
    owner: str
    command_id: str
    term: int
    remaining_mU: int
    min_price_mU: int


class SovereignFinance:
    def __init__(self, state):
        self.s = state
        self.bonds = {}
        self.offers = {}
        self.pool_allowance = {}
        self.interest_residual = dict.fromkeys(state.initial.known, Q(0))
        self.default_cycles = {p: [] for p in state.initial.known}
        self.default_count = dict.fromkeys(state.initial.known, 0)
        self.reports = {}
        self.last_cycle = -1
        self.last_settled_cycle = -1
        self.last_cleared_tick = -1
        self.aliases = {p: {} for p in state.initial.known}
        self.alias_sequences = dict.fromkeys(state.initial.known, 0)

    def outstanding(self, owner):
        return sum(b.face for b in self.bonds.values() if b.issuer == owner)

    def tax_history(self, owner):
        return tuple(row.taxes_mU for row in self.s.history[owner])

    def spread(self, owner, cycle):
        taxes = self.tax_history(owner)
        annual_tax_mU = 100 * max(Q(sum(taxes[-20:]), 20), Q(25000))
        defaults = sum(c > cycle - 100 for c in self.default_cycles[owner])
        return (1 + 4 * Q(self.outstanding(owner), annual_tax_mU) +
                10 * (1 - self.s.credit[owner]) + 5 * defaults + self.s.relations.spread_pp[owner])

    def rate(self, owner, cycle):
        return max(0, self.s.interest_bp[owner] + round(100 * self.spread(owner, cycle)))

    def account(self, owner):
        return CashAccount(owner, 'sovereign_service_reserved', owner)

    @staticmethod
    def holder_account(holder, currency):
        return CashAccount(holder, 'pool' if holder == 'world' else 'treasury', currency)

    def begin_tick(self, ctx):
        if ctx.tick % ctx.clock.cycle_ticks:
            return
        if ctx.cycle <= self.last_cycle:
            raise InvariantViolation('sovereign accounting cycle repeated')
        self.last_cycle = ctx.cycle
        self.pool_allowance = {p: self.s.initial.cash.balance(CashAccount('world', 'pool', p)).native_mU // 10
                               for p in self.s.initial.known}
        for owner in self.default_cycles:
            self.default_cycles[owner] = [c for c in self.default_cycles[owner] if c > ctx.cycle - 100]

    def interest(self, owner, cycle):
        return {b.id: Q(b.face * b.annual_rate_bp, 1000000) for b in self.bonds.values()
                if b.issuer == owner and b.created_cycle < cycle and b.face}

    def due(self, owner, cycle):
        interest = floor(sum(self.interest(owner, cycle).values(), Q(0)) + self.interest_residual[owner])
        principal = sum(b.face for b in self.bonds.values() if b.issuer == owner and b.due_cycle <= cycle)
        return interest + principal

    def reserve(self, ctx):
        if ctx.clock.is_cycle_end(ctx.tick):
            self._fund_service(ctx)

    def note_default(self,owner,cycle):
        """One national event per cycle, regardless of instrument splitting."""
        if cycle in self.default_cycles[owner]:return
        state=self.s
        self.default_cycles[owner].append(cycle)
        self.default_count[owner]+=1
        state.credit[owner]=max(Q(1,10),state.credit[owner]-Q(3,10))
        state.macro.cycles[owner].defaulted=True
        for hub,population in state.initial.populations.items():
            if state.authority.controls_hub(owner,hub):population.support_mU=max(0,population.support_mU-100)

    def funding_claims(self,ctx):
        from .debt_funding import FundingClaim
        claims=[]
        for owner in self.s.initial.known:
            interest=self.due(owner,ctx.cycle)-sum(b.face for b in self.bonds.values() if b.issuer==owner and b.due_cycle<=ctx.cycle)
            principal=self.due(owner,ctx.cycle)-interest
            claims.extend((FundingClaim('sovereign_interest:'+owner,owner,owner,0,interest,self.account(owner)),
                           FundingClaim('sovereign_principal:'+owner,owner,owner,1,principal,self.account(owner))))
        return tuple(claims)

    def _fund_service(self, ctx):
        state = self.s
        cash = state.initial.cash
        transfers, fx = [], []
        for owner in sorted(state.initial.known):
            account = self.account(owner)
            short = max(0, self.due(owner, ctx.cycle) - cash.balance(account).native_mU)
            treasury = state.native(owner, 'treasury')
            native = min(short, cash.balance(treasury).native_mU)
            transfers.append(CashTransfer(treasury, account, native))
            short -= native
            if short:
                reserves = CashAccount(owner, 'reserves', 'RU')
                offered = min(cash.balance(reserves).native_mU,
                              ceil(short * state.rates[owner] * (1 + state.fx.fee)) + 1)
                if offered:
                    fx.append(FXRequest('debt:' + owner, reserves, account, offered, 1 / state.rates[owner],
                                        principal_limit_mU=ceil(short * state.rates[owner])))
        cash.transfer_many(transfers)
        if fx:
            state.fx.execute(ctx.seed, ctx.tick, tuple(fx))

    def clear_primary(self, ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        if ctx.tick == self.last_cleared_tick:
            return
        if ctx.tick < self.last_cleared_tick:
            raise InvariantViolation('bond underwriting backdated')
        self.last_cleared_tick = ctx.tick
        state = self.s
        cash = state.initial.cash
        for owner in sorted(state.initial.known):
            if self.spread(owner, ctx.cycle) > 30:
                continue
            allowance = min(self.pool_allowance.get(owner, 0), cash.balance(CashAccount('world','pool',owner)).native_mU)
            room = max(0, debt_limit(self.tax_history(owner)) - self.outstanding(owner))
            if not allowance or not room:
                continue
            # Newly issued coupon equals today's required yield; fair primary
            # price is exactly par (1000 mU per unit face), regardless of term.
            coupon = self.rate(owner, ctx.cycle)
            eligible = [o for o in self.offers.values() if o.owner == owner and o.min_price_mU <= 1000]
            for price in sorted({o.min_price_mU for o in eligible}):
                group = {o.id: o.remaining_mU for o in eligible if o.min_price_mU == price}
                fills = proportional(min(allowance, room), group,
                                     key=(ctx.seed, 'bond_underwriting', ctx.tick, owner, price))
                for oid, face in fills.items():
                    if not face:
                        continue
                    offer = self.offers[oid]
                    source = CashAccount('world','pool',owner)
                    target = state.native(owner,'treasury')
                    payment=cash.prepare_transfers((CashTransfer(source,target,face),))
                    historical=sum((move.book_ru_mU for move in payment.movements if move.source==source and move.destination==target),Q(0))
                    payment.commit()
                    bid = f'sovereign:{owner}:{ctx.cycle}:{offer.term}:{coupon}'
                    new_series = bid not in self.bonds
                    bond = self.bonds.setdefault(bid, SovereignBond(bid, owner, ctx.cycle, ctx.cycle + offer.term, coupon))
                    bond.holders_face_mU['world'] = bond.holders_face_mU.get('world', 0) + face
                    bond.proceeds_ru_mU += historical
                    if new_series:state.bond_market.register(bond,initial_books={'world':historical})
                    else:state.bond_market.add_face(bond,'world',face,book=historical)
                    state.macro.cycles[owner].portfolio_inflow_ru_mU += face * state.rates[owner]
                    offer.remaining_mU -= face
                    allowance -= face
                    room -= face
                    self.pool_allowance[owner] -= face
                    if not offer.remaining_mU:
                        del self.offers[oid]
                        state.command_streams[owner].plans.pop(offer.command_id, None)
                if not allowance or not room:
                    break

        self.publish()

    def publish(self):
        """Public securities still use observer-local, non-probing identifiers."""
        for owner, aliases in self.aliases.items():
            for key in tuple(aliases):
                if key not in self.bonds:
                    del aliases[key]
            for bond in sorted(self.bonds.values(), key=lambda b: (b.issuer, b.due_cycle, b.annual_rate_bp, b.face, b.id)):
                if bond.id not in aliases:
                    self.alias_sequences[owner] += 1
                    aliases[bond.id] = f'bond-{self.alias_sequences[owner]}'

    def settle(self, ctx, *, fund_service=True):
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        if ctx.cycle <= self.last_settled_cycle:
            raise InvariantViolation('sovereign service repeated')
        self.last_settled_cycle = ctx.cycle
        state = self.s
        state.bond_market.before_service(ctx)
        if fund_service:
            self._fund_service(ctx)
        cash = state.initial.cash
        reports = {}
        for owner in sorted(state.initial.known):
            bonds = [b for b in self.bonds.values() if b.issuer == owner]
            before = {b.id: dict(b.holders_face_mU) for b in bonds}
            exact = self.interest(owner, ctx.cycle)
            combined = sum(exact.values(), Q(0)) + self.interest_residual[owner]
            due_interest = floor(combined)
            self.interest_residual[owner] = combined - due_interest
            interest_due = split_payment(due_interest, exact, key=(ctx.seed,'coupon_due',ctx.cycle,owner)) if exact else {}
            account = self.account(owner)
            interest_paid = proportional(cash.balance(account).native_mU, interest_due,
                                         key=(ctx.seed,'coupon_payment',ctx.cycle,owner))
            transfers = []
            service_books={}
            principal_portions={}
            capitalized_portions={}
            for bond in bonds:
                paid = interest_paid.get(bond.id, 0)
                portions = split_payment(paid, before[bond.id], key=(ctx.seed,'coupon_holders',ctx.cycle,bond.id))
                transfers.extend(CashTransfer(account,self.holder_account(holder,owner),q) for holder,q in portions.items())
            cash.transfer_many(transfers)
            # Original face is the principal due this cycle; the coupon just
            # capitalized below cannot itself receive interest this cycle.
            principal_due = {b.id: sum(before[b.id].values()) for b in bonds if b.due_cycle <= ctx.cycle}
            principal_paid = proportional(cash.balance(account).native_mU, principal_due,
                                          key=(ctx.seed,'principal_payment',ctx.cycle,owner))
            transfers = []
            for bond in bonds:
                paid = principal_paid.get(bond.id, 0)
                portions = split_payment(paid,before[bond.id],key=(ctx.seed,'principal_holders',ctx.cycle,bond.id))
                principal_portions[bond.id]=portions
                for holder,q in portions.items():
                    transfers.append(CashTransfer(account,self.holder_account(holder,owner),q))
                    bond.holders_face_mU[holder] -= q
                    if holder != owner:
                        flow = q * state.rates[owner]
                        state.macro.cycles[owner].portfolio_inflow_ru_mU -= flow
                        if holder in state.macro.cycles:
                            state.macro.cycles[holder].portfolio_inflow_ru_mU += flow
                original_face=sum(before[bond.id].values())
                old_book=bond.proceeds_ru_mU
                removed=(old_book if paid==original_face else
                         Q(round(old_book*paid/original_face*CASH_BOOK_SCALE),CASH_BOOK_SCALE))
                bond.proceeds_ru_mU-=removed
                unpaid_interest = interest_due.get(bond.id, 0) - interest_paid.get(bond.id, 0)
                capitalized_book=Q(round(unpaid_interest*state.rates[owner]*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
                service_books[bond.id]=capitalized_book
                bond.proceeds_ru_mU+=capitalized_book
                added = split_payment(unpaid_interest,before[bond.id],key=(ctx.seed,'capitalized_coupon',ctx.cycle,bond.id))
                capitalized_portions[bond.id]=added
                for holder,q in added.items():
                    bond.holders_face_mU[holder] += q
                bond.holders_face_mU = {h:q for h,q in bond.holders_face_mU.items() if q}
                if bond.due_cycle <= ctx.cycle and bond.face:
                    bond.due_cycle = ctx.cycle + 50
                    bond.annual_rate_bp += 500
            cash.transfer_many(transfers)
            due = due_interest + sum(principal_due.values())
            paid = sum(interest_paid.values()) + sum(principal_paid.values())
            defaulted = paid < due
            if defaulted:self.note_default(owner,ctx.cycle)
            for bond in bonds:
                state.bond_market.apply_service(bond,before[bond.id],principal_paid=principal_portions[bond.id],
                    capitalized_face=capitalized_portions[bond.id],capitalized_book=service_books[bond.id])
                if not bond.face:
                    del self.bonds[bond.id]
            cash.transfer(account,state.native(owner,'treasury'),cash.balance(account).native_mU)
            reports[owner] = {'due_mU':due,'paid_mU':paid,'defaulted':defaulted}
        self.reports = reports
        self.publish()

    def cancel(self, owner, command_id):
        offer = next((o for o in self.offers.values() if o.owner == owner and o.command_id == command_id),None)
        if offer is None:
            return None
        return PreparedCommand(lambda:self.offers.pop(offer.id))

    def visible(self, owner):
        return {'debt_limit_mU':debt_limit(self.tax_history(owner)), 'outstanding_mU':self.outstanding(owner),
                'defaults':self.default_count[owner], 'settlement':dict(self.reports[owner]) if owner in self.reports else None,
                'issues':[{'id':o.id,'command_id':o.command_id,'remaining_mU':o.remaining_mU,'min_price_mU':o.min_price_mU}
                          for o in self.offers.values() if o.owner == owner],
                'bonds':[{'id':self.aliases[owner][b.id],'issuer':b.issuer,'face_mU':b.face,'held_mU':b.holders_face_mU.get(owner,0),
                          'annual_rate_bp':b.annual_rate_bp,'due_cycle':b.due_cycle} for b in self.bonds.values() if b.id in self.aliases[owner]]}

    def audit(self):
        for b in self.bonds.values():
            if b.issuer not in self.s.initial.known or b.annual_rate_bp < 0 or b.due_cycle <= b.created_cycle:
                raise InvariantViolation('invalid sovereign bond')
            if not b.face or b.proceeds_ru_mU<0 or any(type(q) is not int or q <= 0 for q in b.holders_face_mU.values()):
                raise InvariantViolation('invalid outstanding holder face')
        if any(not 0 <= q < 1 for q in self.interest_residual.values()):
            raise InvariantViolation('coupon rounding residual outside [0,1)')

    def debt_book(self,owner):
        return sum((b.proceeds_ru_mU for b in self.bonds.values() if b.issuer==owner),Q(0))

    def snapshot(self):
        return {'bonds':self.bonds,'offers':self.offers,'pool_allowance':self.pool_allowance,
                'interest_residual':self.interest_residual,'default_cycles':self.default_cycles,
                'default_count':self.default_count,'reports':self.reports,'last_cycle':self.last_cycle,
                'last_settled_cycle':self.last_settled_cycle,'last_cleared_tick':self.last_cleared_tick,
                'aliases':self.aliases,'alias_sequences':self.alias_sequences}


class BondCommands:
    OPERATIONS = frozenset({('finance','bond_issue')})

    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'term':int,'face_mU':int,'min_price_mU':int})
        if order['term'] not in (20,50,100) or order['face_mU'] <= 0 or order['min_price_mU'] <= 0:
            raise RuleViolation('ERR_SCHEMA')
        state=self.state;book=state.sovereign_finance
        if 'F02' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        pending=sum(o.remaining_mU for o in book.offers.values() if o.owner==owner)
        if (book.spread(owner,ctx.cycle)>30 or
                book.outstanding(owner)+pending+order['face_mU']>debt_limit(book.tax_history(owner))):
            raise RuleViolation('ERR_NO_PERMISSION')
        command_id=str(int(order['id']))
        offer=PrimaryOffer(f'bond_issue:{owner}:{command_id}',owner,command_id,order['term'],order['face_mU'],order['min_price_mU'])
        return PreparedCommand(lambda:book.offers.__setitem__(offer.id,offer),persistent=True)

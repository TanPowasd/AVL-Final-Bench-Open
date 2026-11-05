import json
import random
import unittest

from organizer.rift_era.engine.errors import InvariantViolation
from organizer.rift_era.engine.numeric import Q,floor
from organizer.rift_era.engine.rational_carry import RationalCarry


class RationalCarryTests(unittest.TestCase):
    def test_changing_rates_matches_independent_unbounded_fraction_account(self):
        rng=random.Random(917)
        carry=RationalCarry();exact=Q(0)
        for _ in range(1500):
            value=Q(rng.randrange(0,10**8),rng.randrange(1,20001))
            before=carry.snapshot();update=carry.prepare(value)
            self.assertEqual(carry.snapshot(),before)
            exact+=value;due=floor(exact);exact-=due
            self.assertEqual(update.due,due)
            update.commit()
        self.assertEqual(carry.exact(),exact)
        carry.audit()
        restored=RationalCarry.restore(json.loads(json.dumps(carry.snapshot())))
        self.assertEqual(restored.exact(),exact)

    def test_exact_integer_and_near_integer_boundaries_are_not_approximated(self):
        for values in ((Q(1,6),Q(1,3),Q(1,2)),
                       (Q(1,10**30),Q(10**30-1,10**30)),
                       (Q(10**30-1,10**30),Q(1,10**30)),
                       (Q(5,6),Q(1,3),Q(5,6))):
            carry=RationalCarry();exact=Q(0)
            for value in values:
                exact+=value;due=floor(exact);exact-=due
                self.assertEqual(carry.add(value),due)
                self.assertEqual(carry.exact(),exact)
            carry.audit()

    def test_many_coprime_denominators_serialize_without_decimal_integer_limit(self):
        # A constructed stress probe for this numerical component, not a game.
        primes=[]
        for n in range(10001,40000):
            if all(n%p for p in range(2,int(n**.5)+1)):
                primes.append(n)
                if len(primes)==1500:break
        carry=RationalCarry();exact=Q(0)
        for p in primes:
            value=Q(1,p);exact+=value
            self.assertEqual(carry.add(value),0)
        self.assertGreater(exact.denominator.bit_length(),14000)
        self.assertEqual(carry.exact(),exact)
        raw=json.dumps(carry.snapshot())
        self.assertLess(len(raw),25000)
        self.assertEqual(RationalCarry.restore(json.loads(raw)).exact(),exact)

    def test_stale_commit_and_invalid_inputs_leave_carry_unchanged(self):
        carry=RationalCarry();plan=carry.prepare(Q(2,3));carry.add(Q(1,2))
        before=carry.snapshot()
        with self.assertRaises(InvariantViolation):plan.commit()
        self.assertEqual(carry.snapshot(),before)
        for invalid in (True,-1,0.5):
            with self.assertRaises(ValueError):carry.prepare(invalid)
            self.assertEqual(carry.snapshot(),before)

    def test_actual_fx_changing_rates_and_ai_fees_preserve_exact_carries(self):
        from organizer.rift_era.engine.ledger import CashAccount,CashLedger
        from organizer.rift_era.engine.payments import ForeignExchange,FXRequest,Payments
        cash=CashLedger();source=CashAccount('A','reserves','RU');target=CashAccount('A','treasury','A')
        cash.initialize(source,10**9);cash.initialize(CashAccount('world','pool','RU'),10**9)
        cash.initialize(CashAccount('world','pool','A'),10**12);cash.seal()
        fx=ForeignExchange(cash,Payments(cash));principal_carry=Q(0);fee_carry=Q(0)
        total_received=total_given=total_fees=0
        primes=[]
        for n in range(10001,40000):
            if all(n%p for p in range(2,int(n**.5)+1)):
                primes.append(n)
                if len(primes)==1500:break
        for tick,p in enumerate(primes):
            rate=Q(1000,p);discount=Q(1,p+2);fee=Q(1,500)*(1-discount)
            fx.fee_discounts['A']=discount
            budget=20000
            principal=floor(Q(budget)/(1+fee))
            if principal+1+floor((principal+1)*fee+fee_carry)<=budget:principal+=1
            exact_fee=principal*fee+fee_carry;paid_fee=floor(exact_fee);fee_carry=exact_fee-paid_fee
            exact=principal*rate+principal_carry;received=floor(exact);principal_carry=exact-received
            fx.execute(9,tick,(FXRequest('changing',source,target,budget,rate),))
            actual=fx.last_fills['changing']
            self.assertEqual((actual.given_mU,actual.received_mU,actual.fee_mU),(principal,received,paid_fee))
            total_given+=principal;total_received+=received;total_fees+=paid_fee
        self.assertEqual(cash.balance(source).native_mU,10**9-total_given-total_fees)
        self.assertEqual(cash.balance(target).native_mU,total_received)
        self.assertEqual(fx.principal_carries[source,target].exact(),principal_carry)
        self.assertGreater(principal_carry.denominator.bit_length(),14000)
        from organizer.rift_era.engine.determinism import canonical
        self.assertLess(len(canonical(fx.snapshot())),100000)
        cash.audit()

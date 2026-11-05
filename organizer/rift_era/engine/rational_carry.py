"""Compact exact fractional carry under changing rational exchange/fee rates.

A single reduced Fraction accumulates the LCM of every historical denominator.
Serializing that growing integer is unnecessary: coalesce proper fractions by
denominator and store their integral offset. Cached rational intervals certify
almost all floors; ambiguous boundaries use exact arithmetic, never rounding.
"""
from dataclasses import dataclass

from .errors import InvariantViolation
from .numeric import Q, floor


INTERVAL_SCALE=10**12  # certificate precision only; never a rounding of money


@dataclass(frozen=True)
class CarryUpdate:
    carry: 'RationalCarry'
    revision: int
    due: int
    denominator: int
    numerator: int
    offset: int
    lower_grid: int
    uncertain: int
    clear: bool = False

    def commit(self):
        carry=self.carry
        if carry._revision!=self.revision:raise InvariantViolation('stale rational carry update')
        if self.clear:
            carry._terms.clear()
        elif self.numerator:
            carry._terms[self.denominator]=self.numerator
        else:
            carry._terms.pop(self.denominator,None)
        carry._offset=self.offset
        carry._lower_grid=self.lower_grid
        carry._uncertain=self.uncertain
        carry._revision+=1


class RationalCarry:
    VERSION='rational_carry_v1'

    def __init__(self):
        self._terms={}
        self._offset=0
        self._lower_grid=0
        self._uncertain=0
        self._revision=0

    @staticmethod
    def _interval_part(numerator,denominator):
        scaled=numerator*INTERVAL_SCALE
        return scaled//denominator,int(bool(scaled%denominator))

    def prepare(self,value):
        """Pure preflight. Return the exact integer due and a guarded update."""
        if isinstance(value,bool) or not isinstance(value,(int,Q)) or value<0:
            raise ValueError('carry accepts a nonnegative exact rational')
        value=Q(value)
        whole=floor(value)
        denominator=value.denominator
        remainder=value.numerator%denominator
        previous=self._terms.get(denominator,0)
        numerator=previous+remainder
        whole+=numerator//denominator
        numerator%=denominator
        old_grid,old_uncertain=self._interval_part(previous,denominator)
        new_grid,new_uncertain=self._interval_part(numerator,denominator)
        lower=self._lower_grid-old_grid+new_grid
        uncertain=self._uncertain-old_uncertain+new_uncertain
        low_floor=lower//INTERVAL_SCALE
        high_floor=(lower+uncertain-1)//INTERVAL_SCALE if uncertain else low_floor
        clear=False
        if low_floor==high_floor:
            offset=low_floor
            clear=not uncertain and lower%INTERVAL_SCALE==0
        else:
            exact=sum((Q(n,d) for d,n in self._terms.items() if d!=denominator),Q(0))+Q(numerator,denominator)
            offset=floor(exact)
            # Remove an exactly settled zero residue, including cancellation
            # across different denominators (e.g. 1/6+1/3+1/2).
            clear=exact.denominator==1
        due=whole+offset-self._offset
        if due<0:raise InvariantViolation('negative exact carry debit')
        return CarryUpdate(self,self._revision,due,denominator,numerator,0 if clear else offset,
                           0 if clear else lower,0 if clear else uncertain,clear)

    def add(self,value):
        update=self.prepare(value)
        update.commit()
        return update.due

    def exact(self):
        """Diagnostic/rare fallback only; this Fraction is not persistent state."""
        return sum((Q(n,d) for d,n in self._terms.items()),Q(0))-self._offset

    def clone(self):
        result=RationalCarry()
        result._terms=dict(self._terms);result._offset=self._offset
        result._lower_grid=self._lower_grid;result._uncertain=self._uncertain
        return result

    def audit(self):
        if any(type(d) is not int or type(n) is not int or not 0<n<d for d,n in self._terms.items()):
            raise InvariantViolation('invalid exact carry terms')
        parts=[self._interval_part(n,d) for d,n in self._terms.items()]
        if (sum(p for p,_ in parts),sum(u for _,u in parts))!=(self._lower_grid,self._uncertain):
            raise InvariantViolation('exact carry interval cache differs from terms')
        low=self._lower_grid//INTERVAL_SCALE
        high=(self._lower_grid+self._uncertain-1)//INTERVAL_SCALE if self._uncertain else low
        if low==high:
            if self._offset!=low:raise InvariantViolation('exact carry outside [0,1)')
        elif not 0<=self.exact()<1:
            raise InvariantViolation('exact carry outside [0,1)')

    def snapshot(self):
        return {'version':self.VERSION,'offset':self._offset,'terms':[[d,n] for d,n in sorted(self._terms.items())]}

    @classmethod
    def restore(cls,snapshot):
        if set(snapshot)!={'version','offset','terms'} or snapshot['version']!=cls.VERSION:
            raise InvariantViolation('invalid exact carry snapshot')
        result=cls()
        if type(snapshot['offset']) is not int:raise InvariantViolation('invalid exact carry offset')
        result._offset=snapshot['offset']
        for d,n in snapshot['terms']:
            if d in result._terms:raise InvariantViolation('duplicate exact carry denominator')
            result._terms[d]=n
            lower,uncertain=result._interval_part(n,d)
            result._lower_grid+=lower;result._uncertain+=uncertain
        result.audit()
        return result

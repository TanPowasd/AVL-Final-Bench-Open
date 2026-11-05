"""Economic title routing for jointly owned facilities.

Authority shares identify economic claimants but never mint inventory. This
adapter only transforms origin metadata on the one physical lot being moved.
"""
from collections import defaultdict
from dataclasses import replace
from fractions import Fraction
from .economic_ownership import EconomicOwner
from .errors import InvariantViolation
from .numeric import Q,split_payment


class FacilityOwnership:
    def __init__(self,state):self.s=state;self.entities={}

    def register(self,entity,parent):self.entities[entity]=parent

    def shares(self,entity):
        parent=self.entities.get(entity,entity);rights=self.s.authority.assets.get(parent)
        if rights is None:return {EconomicOwner(self.s.initial.assets[parent].owner,'treasury'):Q(1)}
        return {EconomicOwner(owner,'treasury'):Q(shares,1000) for owner,shares in rights.owner_shares.items()}

    def split_output(self,entity,origins):
        shares=self.shares(entity);result=defaultdict(Fraction)
        for origin,quantity in origins.items():
            allocations={title:quantity*share for title,share in shares.items()}
            for title,amount in allocations.items():result[replace(origin,economic_owner=title)]+=amount
        total=sum(origins.values(),Q(0));out=sum(result.values(),Q(0))
        if total!=out:raise InvariantViolation('facility title split changed physical quantity')
        return dict(result)

    def cost_shares(self,entity):return self.shares(entity)
    def snapshot(self):return {'entities':self.entities}
    def audit(self):
        for entity,parent in self.entities.items():
            if parent not in self.s.authority.assets:raise InvariantViolation('joint facility parent missing')

    def prepare_cost_payment(self,entity,amount,currency,kind='facility_cost'):
        """Return atomic cash transfers from each shareholder's real account.

        No fallback account is invented. Callers may commit the plan only when
        every requested shareholder amount is funded; otherwise retain unpaid
        debt externally and do not consume the facility input.
        """
        from .ledger import CashTransfer
        from .ledger import CashAccount
        shares=self.cost_shares(entity);requests=split_payment(amount,shares,key=(self.s.initial.map.seed,'facility_cost',entity,kind))
        transfers=[]
        for title,portion in requests.items():
            source=CashAccount(title.nation,title.account,currency)
            transfers.append(CashTransfer(source,CashAccount('world','facility_clearing:'+entity,currency),portion))
        return tuple(transfers)

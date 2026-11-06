"""04 F04 sovereign fund: buy a direct share of a foreign facility (equity_stake_v1).

Offer: diplomacy/propose_contract clause_type 4 with params
{mode: "stake", asset_id, share_mU, price_ru_mU}. The buyer (proposer) needs
F04; the counterparty is the facility's operator and must own the shares
unpledged. The buyer's direct share of one facility stays <= 490 mU (49%).
Acceptance by the operator is the consent: price_ru_mU moves from the buyer's
RU reserves to the seller's in the same commit as the share transfer.

Dividends are the facility's own output split by economic title
(FacilityOwnership), so the stake earns its share of every produced lot (P7).
While the holder and the operator are at war the holder's share is frozen:
its title routes to the operator until peace (automatic freeze; the author
may prefer an explicit freeze order). Stakes are bought for the life of the
facility; duration_cycles only bounds the offer.
"""
from dataclasses import dataclass

from .errors import RuleViolation
from .ledger import CashAccount, CashTransfer
from .protocol import PreparedCommand

STAKE_PARAMS = frozenset({'mode', 'asset_id', 'share_mU', 'price_ru_mU'})
MAX_FOREIGN_SHARE_MU = 490


@dataclass(frozen=True)
class StakeProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    asset_id: str
    share_mU: int
    price_ru_mU: int
    duration_cycles: int
    expires_cycle: int


def facility_lines(state, asset_id):
    if asset_id in state.initial.mines:
        return (asset_id,)
    facility = state.initial.facilities.get(asset_id)
    return tuple(facility.line_ids) if facility is not None else None


def validate(state, p):
    if 'F04' not in state.research[p.proposer].known:
        raise RuleViolation('ERR_TECH_LOCKED')
    rights = state.authority.assets.get(p.asset_id)
    if rights is None or facility_lines(state, p.asset_id) is None or p.asset_id not in state.initial.assets:
        raise RuleViolation('ERR_NO_VISIBLE_TARGET')
    if rights.operator != p.counterparty:
        raise RuleViolation('ERR_NO_PERMISSION', 'stakes are sold by the facility operator')
    if state.initial.assets[p.asset_id].under_construction:
        raise RuleViolation('ERR_NO_PERMISSION')
    if p.share_mU > rights.free_shares(p.counterparty):
        raise RuleViolation('ERR_INSUFFICIENT_STOCK')
    if rights.owner_shares.get(p.proposer, 0) + p.share_mU > MAX_FOREIGN_SHARE_MU:
        raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'F04 direct share per facility is capped at 49%')
    for lid in facility_lines(state, p.asset_id):
        line = state.authority.assets.get(lid)
        if lid != p.asset_id and line is not None and (
                p.share_mU > line.free_shares(p.counterparty)
                or line.owner_shares.get(p.proposer, 0) + p.share_mU > MAX_FOREIGN_SHARE_MU):
            raise RuleViolation('ERR_INSUFFICIENT_STOCK')
    if state.relations.will_be_at_war(p.proposer, p.counterparty):
        raise RuleViolation('ERR_NO_PERMISSION')


def offer(state, owner, order, ctx):
    book = state.contracts
    p, other = order['params'], order['counterparty']
    if (other == owner or not 10 <= order['duration_cycles'] <= 720 or set(p) != STAKE_PARAMS
            or type(p['asset_id']) is not str or type(p['share_mU']) is not int or type(p['price_ru_mU']) is not int
            or not 1 <= p['share_mU'] <= MAX_FOREIGN_SHARE_MU or p['price_ru_mU'] < 0):
        raise RuleViolation('ERR_SCHEMA')
    if other not in state.research:
        raise RuleViolation('ERR_NO_VISIBLE_TARGET')
    if book.embargo_blocks(owner, other) or book.embargo_blocks(other, owner):
        raise RuleViolation('ERR_NO_PERMISSION', 'joint embargo bars new bilateral contracts')
    for party in (owner, other):
        if book.count(party) >= state.catalog.runtime['limits']['contracts_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
    command_id = str(int(order['id']))
    proposal = StakeProposal(f'stake_proposal:{owner}:{command_id}', owner, other, command_id, p['asset_id'],
                             p['share_mU'], p['price_ru_mU'], order['duration_cycles'], ctx.cycle + order['duration_cycles'])
    validate(state, proposal)
    return PreparedCommand(lambda: book.proposals.__setitem__(proposal.id, proposal))


def prepare_accept(state, p, ctx):
    validate(state, p)
    payment = state.initial.cash.prepare_transfers((CashTransfer(
        CashAccount(p.proposer, 'reserves', 'RU'), CashAccount(p.counterparty, 'reserves', 'RU'), p.price_ru_mU),)) \
        if p.price_ru_mU else None

    def accept():
        if payment is not None:
            payment.commit()
        titled = [p.asset_id]
        for lid in facility_lines(state, p.asset_id):
            if lid != p.asset_id and lid in state.authority.assets:
                titled.append(lid)  # a line with its own rights row moves with the facility
            elif lid not in state.facility_ownership.entities:
                state.facility_ownership.register(lid, p.asset_id)
        for entity in titled:
            state.authority.transfer_shares(entity, p.counterparty, p.proposer, p.share_mU)
            row = state.equity_stakes.setdefault(entity, {})
            row[p.proposer] = row.get(p.proposer, 0) + p.share_mU
        state.contracts.log(p, 'FULFILLED')
        del state.contracts.proposals[p.id]
    return PreparedCommand(accept)


def frozen_holders(state, parent, operator):
    """F04 holders of `parent` currently at war with its operator."""
    from .diplomacy import Relation
    stakes = getattr(state, 'equity_stakes', None)
    if not stakes or parent not in stakes:
        return ()
    dip = state.initial.diplomacy
    return tuple(h for h in stakes[parent] if h in dip.actors and operator in dip.actors
                 and dip.relation(h, operator) == Relation.WAR)


def params(p):
    return {'mode': 'stake', 'asset_id': p.asset_id, 'share_mU': p.share_mU, 'price_ru_mU': p.price_ru_mU}

"""06 diplomacy/terminate_contract: generic termination and attribution (03 section 5).

Contract ids are deterministic and public to both parties (see `contract_id`).
A termination takes effect at once (the treaty no longer exists for the rest of
this tick's later phases and every later tick). Unless an exemption applies, the
terminating party is charged the 03 unilateral-breach credit penalty once per
contract: defence pact -.25, every other terminable type -.15 ("ordinary
supply break"); credit is clamped to [.1, 1].

Exemptions (implementation reading, contract `contract_termination_v1`):
- the treaty has already reached its end cycle (nothing left to break);
- a periodic swap that is SUSPENDED with the counterparty listed in
  `blocked_by` (the counterparty defaulted first);
- the counterparty is at WAR with the terminating party and the terminating
  party is not an aggressor of that war (03: an aggressor cannot be excused).

Not terminable here: truce/DMZ (type 6: ending it is `declare_war`, which
already charges -.35 for a live truce), sovereign loans (type 2: end by
repayment or the 02 default path) and joint ventures (type 4: use
`leave_joint_venture`). These return ERR_NO_PERMISSION, never a no-op.
"""
from .command_schema import fields
from .errors import RuleViolation
from .numeric import Q
from .protocol import PreparedCommand

PENALTY = {'defense': Q(25, 100)}
ORDINARY = Q(15, 100)


def contract_id(kind, treaty):
    if kind == 'transit':
        return f'transit:{treaty.grantor}:{treaty.grantee}:{treaty.start_cycle}:{",".join(treaty.corridors)}'
    if kind == 'embargo':
        return f'embargo:{":".join(treaty.parties)}:{treaty.start_cycle}:{treaty.target}'
    return f'{kind}:{":".join(treaty.parties)}:{treaty.start_cycle}'


def _find(state, owner, cid):
    book = state.contracts
    for kind, rows in (('transit', book.transit), ('embargo', book.embargoes), ('defense', book.defense),
                       ('intel', book.intelligence)):
        for treaty in rows:
            if contract_id(kind, treaty) == cid:
                parties = (treaty.grantor, treaty.grantee) if kind == 'transit' else treaty.parties
                if owner not in parties:
                    raise RuleViolation('ERR_NO_VISIBLE_TARGET')
                return kind, treaty, next(p for p in parties if p != owner)
    row = book.swaps.rows.get(cid)
    if row is not None:
        parties = (row.proposal.proposer, row.proposal.counterparty)
        if owner not in parties or row.status in ('EXPIRED', 'DEFAULTED', 'TERMINATED'):
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        return 'swap', row, next(p for p in parties if p != owner)
    if cid.startswith('license:'):
        _, licensee, tech = cid.split(':', 2)
        record = book.licenses.get(licensee, {}).get(tech)
        if record is not None and owner in (licensee, record['licensor']):
            return 'license', (licensee, tech, record), record['licensor'] if owner == licensee else licensee
        raise RuleViolation('ERR_NO_VISIBLE_TARGET')
    if any(row.proposal.id == cid and owner in (row.proposal.proposer, row.proposal.counterparty)
           for row in book.loans.rows.values()) or cid in book.joint_ventures.rows or cid.startswith('dmz:'):
        raise RuleViolation('ERR_NO_PERMISSION', 'this clause type ends by its own rule, not terminate_contract')
    raise RuleViolation('ERR_NO_VISIBLE_TARGET')


def _exempt(state, owner, other, kind, target, cycle):
    if kind in ('transit', 'embargo', 'defense', 'intel') and cycle >= target.end_cycle:
        return True
    if kind == 'swap' and target.status == 'SUSPENDED' and other in getattr(target, 'blocked_by', ()):
        return True
    from .diplomacy import Relation
    diplomacy = state.initial.diplomacy
    if other in diplomacy.actors and diplomacy.relation(owner, other) == Relation.WAR:
        pair = diplomacy.pairs[diplomacy._key(owner, other)]
        return owner not in pair.aggressors
    return False


def prepare_termination(state, owner, order, ctx):
    fields(order, {'contract_id': str})
    cid = order['contract_id']
    kind, target, other = _find(state, owner, cid)
    if kind == 'swap' and cid in state.contracts.swaps.pending:
        raise RuleViolation('ERR_NO_PERMISSION', 'a delivery of this swap is settling this tick')
    exempt = _exempt(state, owner, other, kind, target, ctx.cycle)
    book = state.contracts

    def commit():
        if kind == 'transit':
            book.transit = [t for t in book.transit if t is not target]
        elif kind == 'embargo':
            book.embargoes = [t for t in book.embargoes if t is not target]
        elif kind == 'defense':
            book.defense = [t for t in book.defense if t is not target]
        elif kind == 'intel':
            book.intelligence = [t for t in book.intelligence if t is not target]
        elif kind == 'swap':
            target.status = 'TERMINATED'
        elif kind == 'license':
            licensee, tech, _ = target
            del book.licenses[licensee][tech]
        if not exempt:
            state.credit[owner] = max(Q(1, 10), state.credit[owner] - PENALTY.get(kind, ORDINARY))
        for party in (owner, other):
            book.recent[party].append({'contract_id': cid, 'status': 'TERMINATED', 'terminated_by': owner,
                                       'attributed': not exempt, 'cycle': ctx.cycle})
            book.recent[party] = book.recent[party][-256:]

    return PreparedCommand(commit)

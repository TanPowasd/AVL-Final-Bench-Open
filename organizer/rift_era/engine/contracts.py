"""Private fixed-clause transit, technology, peace and security treaties.

R06 fees lock actual RU and settle after all P1 streams. Resource-free signed
peace clauses preview as one batch before simultaneous declarations. Territorial
cessions change sovereignty only; intelligence sharing preserves original age.
"""
from collections import defaultdict,deque
from dataclasses import dataclass

from .command_schema import fields
from .errors import RuleViolation, InvariantViolation
from .ledger import CashAccount, CashTransfer, Account, Transfer
from .protocol import PreparedCommand, command_sequence, validate_shape, MAX_COMMANDS
from .diplomacy import AcceptedPeace, Relation
from .periodic_contracts import PeriodicContracts,SwapProposal
from .sovereign_loans import SovereignLoans,LoanProposal
from .joint_ventures import JointVentures,JointVentureProposal


@dataclass(frozen=True)
class LicenseProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    tech: str
    fee_ru_mU: int
    duration_cycles: int
    expires_cycle: int
    source_blockade: bool


@dataclass(frozen=True)
class PeaceProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    mode: str
    regions: tuple[str, ...]
    withdraw_cycles: int
    duration_cycles: int
    expires_cycle: int


@dataclass(frozen=True)
class IntelligenceProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    regions: tuple[str,...]
    minimum_quality_mU: int
    duration_cycles: int
    expires_cycle: int


@dataclass(frozen=True)
class IntelligenceTreaty:
    parties: tuple[str,str]
    regions: tuple[str,...]
    minimum_quality_mU: int
    start_cycle: int
    end_cycle: int


@dataclass(frozen=True)
class TransitProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    corridors: tuple[str,...]
    maximum_flow_mU: int
    toll_cap_ru_mU: int
    allow_basing: bool
    duration_cycles: int
    expires_cycle: int


@dataclass(frozen=True)
class TransitTreaty:
    grantor: str
    grantee: str
    corridors: tuple[str,...]
    maximum_flow_mU: int
    toll_cap_ru_mU: int
    allow_basing: bool
    start_cycle: int
    end_cycle: int


@dataclass(frozen=True)
class EmbargoProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    target: str
    goods: tuple[str,...]
    duration_cycles: int
    expires_cycle: int


@dataclass(frozen=True)
class EmbargoTreaty:
    parties: tuple[str,str]
    target: str
    goods: tuple[str,...]
    start_cycle: int
    end_cycle: int


@dataclass(frozen=True)
class DefenseProposal:
    id: str
    proposer: str
    counterparty: str
    command_id: str
    duration_cycles: int
    expires_cycle: int


@dataclass(frozen=True)
class DefenseTreaty:
    parties: tuple[str,str]
    start_cycle: int
    end_cycle: int


@dataclass
class DefenseObligation:
    ally: str
    defender: str
    aggressor: str
    war_id: int
    start_tick: int
    deadline_tick: int
    aid_value: object = 0
    status: str = 'PENDING'


@dataclass
class DemilitarizedZone:
    parties: tuple[str, str]
    regions: tuple[str, ...]
    deadline_tick: int
    end_tick: int
    breached: bool = False


class Contracts:
    def __init__(self,state):
        self.s=state
        self.proposals={}
        self.swaps=PeriodicContracts(state)
        self.loans=SovereignLoans(state)
        self.joint_ventures=JointVentures(state)
        self.aliases={p:{} for p in state.initial.known}
        self.sequences=dict.fromkeys(state.initial.known,0)
        self.pending=[]
        self.pending_peace=[]
        self.dmz=[]
        self.intelligence=[]
        self.transit=[]
        self.embargoes=[]
        self.defense=[]
        self.defense_obligations=[]
        self.defense_events=set()
        self.defense_used_receipts=set()
        self.defense_aid={}
        self.preview_peace=()
        self.tick=0
        self.licenses={p:{} for p in state.initial.known}
        self.recent={p:[] for p in state.initial.known}

    def count(self,party):
        active_dmz=sum(party in z.parties and self.tick<z.end_tick for z in self.dmz)
        return (active_dmz+sum(party in (r.proposal.proposer,r.proposal.counterparty) and r.status not in ('EXPIRED','DEFAULTED') for r in self.swaps.rows.values())+self.loans.count(party)+self.joint_ventures.count(party)+sum(party in t.parties for t in self.defense)+sum(party in t.parties for t in self.embargoes)+sum(party in (p.proposer,p.counterparty) for p in self.proposals.values())+
                sum(party in t.parties for t in self.intelligence)+sum(party in (t.grantor,t.grantee) for t in self.transit))

    def publish(self):
        for owner,aliases in self.aliases.items():
            self.aliases[owner]={pid:alias for pid,alias in aliases.items() if pid in self.proposals}
            for proposal in sorted(self.proposals.values(),key=lambda p:(p.proposer,p.id)):
                if owner not in (proposal.proposer,proposal.counterparty):continue
                if proposal.id not in self.aliases[owner]:
                    self.sequences[owner]+=1
                    self.aliases[owner][proposal.id]=f'proposal-{self.sequences[owner]}'

    def begin_tick(self,ctx):
        self.tick=ctx.tick if hasattr(ctx,'tick') else self.tick
        for pid,proposal in tuple(self.proposals.items()):
            if ctx.cycle>=proposal.expires_cycle:
                del self.proposals[pid]
        self.intelligence=[t for t in self.intelligence if ctx.cycle<t.end_cycle]
        self.transit=[t for t in self.transit if ctx.cycle<t.end_cycle]
        self.embargoes=[t for t in self.embargoes if ctx.cycle<t.end_cycle]
        self.defense=[t for t in self.defense if ctx.cycle<t.end_cycle]
        self.swaps.begin_tick(ctx) if hasattr(ctx,'clock') else None
        self.publish()

    def resolve(self,owner,alias):
        pid=next((pid for pid,value in self.aliases[owner].items() if value==alias),None)
        proposal=self.proposals.get(pid)
        if proposal is None or proposal.counterparty!=owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        return proposal

    def log(self,proposal,status):
        for owner in (proposal.proposer,proposal.counterparty):
            alias=self.aliases[owner].get(proposal.id)
            self.recent[owner].append({'proposal_id':alias,'counterparty':proposal.counterparty if owner==proposal.proposer else proposal.proposer,
                                      'clause_type':6 if isinstance(proposal,PeaceProposal) else 8 if isinstance(proposal,IntelligenceProposal) else 3 if isinstance(proposal,TransitProposal) else 9 if isinstance(proposal,EmbargoProposal) else 7 if isinstance(proposal,DefenseProposal) else 1 if isinstance(proposal,SwapProposal) else 2 if isinstance(proposal,LoanProposal) else 4 if isinstance(proposal,JointVentureProposal) else 5,'status':status,
                                      **({'mode':proposal.mode} if isinstance(proposal,PeaceProposal) else {} if isinstance(proposal,(IntelligenceProposal,TransitProposal,EmbargoProposal,DefenseProposal,SwapProposal,LoanProposal,JointVentureProposal)) else {'tech_id':proposal.tech})})
            self.recent[owner]=self.recent[owner][-256:]

    def settle(self,ctx):
        self.s.initial.cash.transfer_many(CashTransfer(lock,target,amount) for lock,target,amount in self.pending)
        self.pending.clear()
        self.swaps.settle(ctx)
        self.loans.settle_originations(ctx)

    def peace_valid(self,proposal):
        if proposal.mode == 'cession' and any(self.s.authority.regions[r].sovereign != proposal.proposer for r in proposal.regions):
            raise RuleViolation('ERR_NO_PERMISSION','cession giver no longer has sovereignty')
        key=tuple(sorted((proposal.proposer,proposal.counterparty)))
        if proposal.mode=='cession' and any(p.mode=='cession' and set(p.regions)&set(proposal.regions) for p in self.pending_peace):
            raise RuleViolation('ERR_NO_PERMISSION','sovereignty already promised in this batch')
        if any(tuple(sorted((p.proposer,p.counterparty)))==key for p in self.pending_peace):
            raise RuleViolation('ERR_NO_PERMISSION','one peace treaty per bilateral pair per tick')

    def preview_treaties(self,ctx):
        """Only previously received, resource-free type6 accepts can preview."""
        accepted=[];pairs=set();ceded=set()
        for owner,stream in sorted(self.s.command_streams.items()):
            watermark=stream.watermark;removed=set()
            orders=ctx.orders.get(owner,())
            if len(orders)>MAX_COMMANDS:raise RuleViolation('ERR_WINDOW_ORDER_CAP')
            for order in orders:
                try:
                    seq=command_sequence(order)
                    if seq<=watermark:continue
                    watermark=seq;validate_shape(order)
                    if order.get('kind')!='diplomacy' or order.get('op') not in ('accept_contract','reject_contract'):continue
                    fields(order,{'proposal_id':str})
                    proposal=self.resolve(owner,order['proposal_id'])
                    if proposal.id in removed:continue
                    removed.add(proposal.id)
                    if order['op']=='reject_contract' or not isinstance(proposal,PeaceProposal):continue
                    self.peace_valid(proposal)
                    key=tuple(sorted((proposal.proposer,proposal.counterparty)))
                    if key in pairs or proposal.mode=='cession' and ceded.intersection(proposal.regions):continue
                    if proposal.mode=='cession':ceded.update(proposal.regions)
                    pairs.add(key);accepted.append(proposal)
                except RuleViolation:continue
        self.preview_peace=tuple(accepted)
        return self.preview_peace

    def peace_batch(self,ctx):
        treaties=tuple(AcceptedPeace(p.proposer,p.counterparty,p.mode,p.duration_cycles) for p in self.pending_peace)
        for proposal in self.pending_peace:
            if proposal.mode=='cession':self.s.authority.cede(proposal.proposer,proposal.counterparty,proposal.regions)
            if proposal.regions:
                self.dmz.append(DemilitarizedZone(tuple(sorted((proposal.proposer,proposal.counterparty))),proposal.regions,
                    ctx.tick+proposal.withdraw_cycles*ctx.clock.cycle_ticks,ctx.tick+proposal.duration_cycles*ctx.clock.cycle_ticks))
        self.pending_peace.clear();self.preview_peace=()
        return treaties

    def dmz_breaches(self,ctx):
        declarations=set()
        for zone in self.dmz:
            if zone.breached or ctx.tick<zone.deadline_tick or ctx.tick>=zone.end_tick:continue
            offenders={f.owner for f in self.s.formations.rows.values() if f.owner in zone.parties and
                       f.region in zone.regions and any(self.s.initial.units[u].crew_mU>0 for u in f.unit_ids)}
            for owner in offenders:
                other=next(p for p in zone.parties if p!=owner)
                declarations.add((owner,other))
            if offenders:zone.breached=True
        self.dmz=[z for z in self.dmz if ctx.tick<z.end_tick]
        return tuple(sorted(declarations))

    def dmz_allows(self,owner,origin,target,tick):
        # Existing occupants may leave; entering or remaining by a new task is
        # never a legal withdrawal. This guard reads signed public geography.
        return not any(not z.breached and tick<z.end_tick and owner in z.parties and target in z.regions
                       for z in self.dmz)

    def defense_advance(self,ctx):
        from .numeric import Q
        from .provenance import Use
        state=self.s
        for event in state.relations.last_events:
            aggressor=event['actor'];target=event['target'];war_id=event['war_id']
            pair=state.initial.diplomacy.pairs[state.initial.diplomacy._key(aggressor,target)]
            if target in pair.aggressors:continue
            for treaty in self.defense:
                if target not in treaty.parties or not treaty.start_cycle<=ctx.cycle<treaty.end_cycle:continue
                ally=next(p for p in treaty.parties if p!=target)
                key=(ally,target,aggressor,war_id)
                if key in self.defense_events:continue
                self.defense_events.add(key)
                self.defense_obligations.append(DefenseObligation(ally,target,aggressor,war_id,ctx.tick,ctx.tick+20,Q(0)))
        # Physical use events are never export/aid scoring receipts. Exactly one
        # pending obligation may spend each event; old delivery or transfers do
        # not count until the defender actually consumes military supplies.
        for event in state.initial.inventory.materials.events:
            if event.id in self.defense_used_receipts or event.use not in (Use.ACTION_LOSS,Use.MILITARY_SUPPORT):continue
            if event.good not in state.export_controls.war_goods|{'food','water','fuel','battery','medical_supplies'}:continue
            choices=[o for o in self.defense_obligations if o.status=='PENDING' and o.defender==event.owner and
                     self.defense_aid.get((o.ally,o.defender,event.hub,event.good,event.origin),0)>0 and event.tick>=o.start_tick and event.tick<o.deadline_tick]
            if not choices:continue
            obligation=min(choices,key=lambda o:(o.deadline_tick,o.war_id,o.ally,o.aggressor))
            aid_key=(obligation.ally,obligation.defender,event.hub,event.good,event.origin)
            quantity=min(event.quantity_mU,self.defense_aid[aid_key])
            self.defense_aid[aid_key]-=quantity
            obligation.aid_value+=quantity*state.catalog.weights[event.good]/1000
            self.defense_used_receipts.add(event.id)
        for obligation in self.defense_obligations:
            if obligation.status!='PENDING':continue
            if state.initial.diplomacy.relation(obligation.ally,obligation.aggressor)==Relation.WAR or obligation.aid_value>=50:
                obligation.status='FULFILLED'
            elif ctx.tick>=obligation.deadline_tick:
                obligation.status='DEFAULTED'
                state.credit[obligation.ally]=max(Q(1,10),state.credit[obligation.ally]-Q(25,100))
        # Tick IDs are monotone; only this tick's physical events can replay.
        self.defense_used_receipts={e.id for e in state.initial.inventory.materials.events if e.id in self.defense_used_receipts}
        active={(o.ally,o.defender) for o in self.defense_obligations if o.status=='PENDING'}
        self.defense_aid={key:q for key,q in self.defense_aid.items() if q and key[:2] in active}

    def embargo_blocks(self,seller,buyer,good=None):
        return any(seller in t.parties and buyer==t.target and t.start_cycle<=self.tick//10<t.end_cycle and
                   (good is None or good in t.goods) for t in self.embargoes)

    def transit_right(self,owner,edge):
        cycle=self.tick//10
        valid=[t for t in self.transit if t.grantee==owner and edge in t.corridors and t.start_cycle<=cycle<t.end_cycle and
               self.s.initial.diplomacy.relation(owner,t.grantor)!=Relation.WAR]
        if not valid:return None
        # Rights never add physical capacity; the best individual authorization
        # is selected rather than summing duplicate paper rights.
        return max(valid,key=lambda t:(t.maximum_flow_mU,-t.toll_cap_ru_mU,t.grantor))

    def basing_host(self,owner,hub,domain):
        mode='air' if domain=='Air' else 'sea' if domain.startswith('Sea') else 'road'
        for treaty in self.transit:
            if treaty.grantee!=owner or not treaty.allow_basing or not treaty.start_cycle<=self.tick//10<treaty.end_cycle:continue
            if self.s.initial.diplomacy.relation(owner,treaty.grantor)==Relation.WAR:continue
            for eid in treaty.corridors:
                edge=self.s.cargo.corridors.get(eid)
                if edge is not None and edge.mode==mode and hub in (edge.a,edge.b) and self.s.authority.controls_hub(treaty.grantor,hub):
                    return treaty.grantor
        return None

    def dmz_path_allows(self,owner,regions):
        for zone in self.dmz:
            if zone.breached or self.tick>=zone.end_tick or owner not in zone.parties:continue
            prohibited=set(zone.regions)
            distances={};queue=deque()
            for rid in prohibited:
                if any(r not in prohibited for r in self.s.initial.map.regions[rid].adjacent):
                    distances[rid]=1;queue.append(rid)
            while queue:
                rid=queue.popleft()
                for neighbor in self.s.initial.map.regions[rid].adjacent:
                    if neighbor in prohibited and neighbor not in distances:
                        distances[neighbor]=distances[rid]+1;queue.append(neighbor)
            previous=regions[0]
            for target in regions[1:]:
                if target==previous:continue
                if target in prohibited and (previous not in prohibited or distances.get(target,10**9)>=distances.get(previous,10**9)):
                    return False
                previous=target
        return True

    def visible(self,owner):
        aliases=self.aliases[owner];proposals=[]
        for p in self.proposals.values():
            if p.id not in aliases:continue
            peace=isinstance(p,PeaceProposal);intel=isinstance(p,IntelligenceProposal);transit=isinstance(p,TransitProposal);embargo=isinstance(p,EmbargoProposal);defense=isinstance(p,DefenseProposal);swap=isinstance(p,SwapProposal);loan=isinstance(p,LoanProposal);jv=isinstance(p,JointVentureProposal)
            params=({'hub_id':p.hub,'profile_id':p.profile,'recipe_id':p.recipe,'shares_mU':list(p.shares),'inputs':list(p.inputs),'mode':p.mode,**({'tech_id':p.tech} if p.tech else {})} if jv else
                    {'currency':p.currency,'principal_mU':p.principal_mU,'annual_rate_bp':p.annual_rate_bp,'due_cycle':p.due_cycle,'collateral':[{'asset_id':a,'shares_mU':q} for a,q in p.collateral]} if loan else
                    {'hub_a':p.hub_a,'hub_b':p.hub_b,'good_a':p.good_a,'good_b':p.good_b,'qty_a_mU':p.quantity_a,'qty_b_mU':p.quantity_b,'period_cycles':p.period_cycles,**({'currency':p.cash_currency,'amount_mU':p.cash_amount,'cash_side':p.cash_side} if p.cash_side else {})} if swap else
                    {'response_cycles':2,'min_aid_ru_mU':50000} if defense else
                    {'target_nation':p.target,'good_ids':list(p.goods)} if embargo else
                    {'corridor_ids':list(p.corridors),'max_flow_mU':p.maximum_flow_mU,'toll_cap_ru_mU':p.toll_cap_ru_mU,'allow_basing':p.allow_basing} if transit else
                    {'region_ids':list(p.regions),'min_quality_mU':p.minimum_quality_mU} if intel else
                    {'mode':p.mode,'region_ids':list(p.regions),'withdraw_cycles':p.withdraw_cycles} if peace else
                    {'tech_id':p.tech,'fee_ru_mU':p.fee_ru_mU})
            proposals.append({'id':aliases[p.id],'proposer':p.proposer,'counterparty':p.counterparty,
                'clause_type':6 if peace else 8 if intel else 3 if transit else 9 if embargo else 7 if defense else 1 if swap else 2 if loan else 4 if jv else 5,'duration_cycles':p.duration_cycles,'expires_cycle':p.expires_cycle,
                'params':params,**({} if peace or intel or transit or embargo or defense or swap or loan or jv else {'source_blockade_at_offer':p.source_blockade})})
        return {'proposals':proposals,'licenses_received':{tech:dict(record) for tech,record in self.licenses[owner].items()},
                'dmz':[z for z in self.dmz if owner in z.parties],
                'joint_ventures':self.joint_ventures.visible(owner),
                'loans':self.loans.visible(owner),
                'periodic_swaps':self.swaps.visible(owner),
                'defense_pacts':[t for t in self.defense if owner in t.parties],
                'defense_obligations':[o for o in self.defense_obligations if owner in (o.ally,o.defender)],
                'joint_embargoes':[t for t in self.embargoes if owner in t.parties],
                'transit_rights':[t for t in self.transit if owner in (t.grantor,t.grantee)],
                'intel_sharing':[t for t in self.intelligence if owner in t.parties], 'recent':[dict(row) for row in self.recent[owner]]}

    def audit(self):
        self.loans.audit();self.joint_ventures.audit()
        counts=defaultdict(int)
        for p in self.proposals.values():
            if p.proposer==p.counterparty or p.proposer not in self.aliases or p.counterparty not in self.aliases:
                raise InvariantViolation('invalid license counterparties')
            counts[p.proposer]+=1;counts[p.counterparty]+=1
        for loan in self.loans.rows.values():
            if loan.face_mU:
                counts[loan.proposal.proposer]+=1;counts[loan.proposal.counterparty]+=1
        for venture in self.joint_ventures.rows.values():
            counts[venture.proposal.proposer]+=1;counts[venture.proposal.counterparty]+=1
        for swap in self.swaps.rows.values():
            if swap.status not in ('EXPIRED','DEFAULTED'):
                counts[swap.proposal.proposer]+=1;counts[swap.proposal.counterparty]+=1
        for z in self.dmz:
            if self.tick<z.end_tick:
                for party in z.parties:counts[party]+=1
        for t in self.defense:
            for party in t.parties:counts[party]+=1
        for t in self.embargoes:
            for party in t.parties:counts[party]+=1
        for t in self.intelligence:
            for party in t.parties:counts[party]+=1
        for t in self.transit:
            counts[t.grantor]+=1;counts[t.grantee]+=1
        if any(n>self.s.catalog.runtime['limits']['contracts_per_nation'] for n in counts.values()):
            raise InvariantViolation('bilateral proposal cap exceeded')
        for lock,_,amount in self.pending:
            if self.s.initial.cash.balance(lock).native_mU!=amount:
                raise InvariantViolation('license payment lost actual escrow')

    def snapshot(self):
        return {'proposals':self.proposals,'aliases':self.aliases,'sequences':self.sequences,
                'pending':self.pending,'swaps':self.swaps.snapshot(),'loans':self.loans.snapshot(),'joint_ventures':self.joint_ventures.snapshot(),'pending_peace':self.pending_peace,'dmz':self.dmz,'intelligence':self.intelligence,'transit':self.transit,'embargoes':self.embargoes,'defense':self.defense,'defense_obligations':self.defense_obligations,
                'defense_events':self.defense_events,'defense_used_receipts':self.defense_used_receipts,
                'defense_aid':[[a,b,h,g,o,q] for (a,b,h,g,o),q in self.defense_aid.items() if q],'preview_peace':self.preview_peace,'tick':self.tick,'licenses':self.licenses,'recent':self.recent}


class ContractCommands:
    OPERATIONS=frozenset({('diplomacy','propose_contract'),('diplomacy','accept_contract'),('diplomacy','reject_contract'),('diplomacy','military_aid'),('diplomacy','leave_joint_venture')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        state=self.state;book=state.contracts
        if order['op']=='military_aid':return self.military_aid(owner,order,ctx)
        if order['op']=='leave_joint_venture':
            fields(order,{'venture_id':str})
            row=book.joint_ventures.rows.get(order['venture_id'])
            if row is None or owner not in row.active_members:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            return PreparedCommand(lambda:setattr(row,'active_members',tuple(p for p in row.active_members if p!=owner)))
        if order['op']=='propose_contract':
            fields(order,{'counterparty':str,'clause_type':int,'params':dict,'duration_cycles':int})
            params=order['params'];other=order['counterparty']
            if order['clause_type']==6:return self.peace_offer(owner,order,ctx)
            if order['clause_type']==8:return self.intel_offer(owner,order,ctx)
            if order['clause_type']==3:return self.transit_offer(owner,order,ctx)
            if order['clause_type']==9:return self.embargo_offer(owner,order,ctx)
            if order['clause_type']==7:return self.defense_offer(owner,order,ctx)
            if order['clause_type']==1:return book.swaps.offer(owner,order,ctx)
            if order['clause_type']==2:return book.loans.offer(owner,order,ctx)
            if order['clause_type']==4:return book.joint_ventures.offer(owner,order,ctx)
            if (order['clause_type']!=5 or not 10<=order['duration_cycles']<=720 or
                    set(params)!={'tech_id','fee_ru_mU'} or type(params['tech_id']) is not str or
                    type(params['fee_ru_mU']) is not int or params['fee_ru_mU']<0 or other==owner):
                raise RuleViolation('ERR_SCHEMA')
            if other not in state.research:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            if book.embargo_blocks(owner,other) or book.embargo_blocks(other,owner):raise RuleViolation('ERR_NO_PERMISSION','joint embargo bars new bilateral contracts')
            if state.relations.will_be_at_war(owner,other):raise RuleViolation('ERR_NO_PERMISSION')
            if not {'R06',params['tech_id']}<=state.research[owner].known:raise RuleViolation('ERR_TECH_LOCKED')
            if params['tech_id'] not in state.catalog.technologies:raise RuleViolation('ERR_SCHEMA')
            for party in (owner,other):
                if book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation']:
                    raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
            command_id=str(int(order['id']))
            proposal=LicenseProposal(f'license_proposal:{owner}:{command_id}',owner,other,command_id,
                params['tech_id'],params['fee_ru_mU'],order['duration_cycles'],ctx.cycle+order['duration_cycles'],
                state.governance.active(owner,'R12'))
            return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))
        fields(order,{'proposal_id':str})
        proposal=book.resolve(owner,order['proposal_id'])
        if order['op']=='accept_contract':
            if book.count(owner)>state.catalog.runtime['limits']['contracts_per_nation']:raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if order['op']=='reject_contract':
            def reject():
                book.log(proposal,'REJECTED')
                del book.proposals[proposal.id]
            return PreparedCommand(reject)
        if not isinstance(proposal,PeaceProposal) and (book.embargo_blocks(owner,proposal.proposer) or book.embargo_blocks(proposal.proposer,owner)):
            raise RuleViolation('ERR_NO_PERMISSION','joint embargo bars new bilateral contracts')
        if isinstance(proposal,JointVentureProposal):return book.joint_ventures.prepare_accept(proposal,ctx)
        if isinstance(proposal,LoanProposal):return book.loans.prepare_accept(proposal,ctx)
        if isinstance(proposal,SwapProposal):return book.swaps.prepare_accept(proposal,ctx)
        if isinstance(proposal,DefenseProposal):
            if state.relations.will_be_at_war(owner,proposal.proposer):raise RuleViolation('ERR_NO_PERMISSION')
            def pact():
                book.defense.append(DefenseTreaty(tuple(sorted((owner,proposal.proposer))),ctx.cycle,ctx.cycle+proposal.duration_cycles))
                book.log(proposal,'SIGNED');del book.proposals[proposal.id]
            return PreparedCommand(pact)
        if isinstance(proposal,EmbargoProposal):
            def enact():
                book.embargoes.append(EmbargoTreaty(tuple(sorted((owner,proposal.proposer))),proposal.target,proposal.goods,ctx.cycle,ctx.cycle+proposal.duration_cycles))
                book.log(proposal,'SIGNED');del book.proposals[proposal.id]
            return PreparedCommand(enact)
        if isinstance(proposal,TransitProposal):
            self.validate_transit(proposal)
            def grant():
                book.transit.append(TransitTreaty(proposal.proposer,owner,proposal.corridors,proposal.maximum_flow_mU,
                    proposal.toll_cap_ru_mU,proposal.allow_basing,ctx.cycle,ctx.cycle+proposal.duration_cycles))
                book.log(proposal,'SIGNED');del book.proposals[proposal.id]
            return PreparedCommand(grant)
        if isinstance(proposal,IntelligenceProposal):
            if state.relations.will_be_at_war(owner,proposal.proposer):raise RuleViolation('ERR_NO_PERMISSION')
            parties=tuple(sorted((owner,proposal.proposer)))
            if any(t.parties==parties for t in book.intelligence):raise RuleViolation('ERR_NO_PERMISSION')
            def share():
                book.intelligence.append(IntelligenceTreaty(parties,proposal.regions,proposal.minimum_quality_mU,ctx.cycle,ctx.cycle+proposal.duration_cycles))
                book.log(proposal,'SIGNED');del book.proposals[proposal.id]
            return PreparedCommand(share)
        if isinstance(proposal,PeaceProposal):
            book.peace_valid(proposal)
            def sign():
                book.pending_peace.append(proposal);book.log(proposal,'SIGNED');del book.proposals[proposal.id]
            return PreparedCommand(sign)
        if state.relations.will_be_at_war(owner,proposal.proposer):raise RuleViolation('ERR_NO_PERMISSION')
        research=state.research[owner]
        project=research.projects.get(proposal.tech)
        if proposal.tech in book.licenses[owner] or project and project.cost_modifiers.get('licensed'):
            raise RuleViolation('ERR_NO_PERMISSION','technology already licensed')
        if proposal.tech in research.known or not state.catalog.researchable(proposal.tech,research.known):
            raise RuleViolation('ERR_TECH_LOCKED')
        blocked=state.governance.active(proposal.proposer,'R12')
        source=CashAccount(owner,'reserves','RU')
        lock=CashAccount(owner,'license_payment:'+str(int(order['id'])),'RU')
        target=CashAccount(proposal.proposer,'reserves','RU')
        payment=state.initial.cash.prepare_transfers((CashTransfer(source,lock,proposal.fee_ru_mU),))
        def accept():
            payment.commit()
            research.license(proposal.tech,source_blockade=blocked)
            book.licenses[owner][proposal.tech]={'licensor':proposal.proposer,'accepted_cycle':ctx.cycle,'end_cycle':ctx.cycle+proposal.duration_cycles,
                                               'fee_ru_mU':proposal.fee_ru_mU,'source_blockade':blocked}
            book.pending.append((lock,target,proposal.fee_ru_mU))
            book.log(proposal,'FULFILLED')
            del book.proposals[proposal.id]
        return PreparedCommand(accept)

    def peace_offer(self,owner,order,ctx):
        state=self.state;book=state.contracts;params=order['params'];other=order['counterparty']
        if (other==owner or not 10<=order['duration_cycles']<=720 or
                set(params)-{'mode','region_ids','withdraw_cycles'} or not {'region_ids','withdraw_cycles'}<=params.keys() or
                params.get('mode','armistice') not in ('armistice','peace','cession') or
                type(params['withdraw_cycles']) is not int or not 1<=params['withdraw_cycles']<=5 or
                type(params['region_ids']) is not list or any(type(r) is not str for r in params['region_ids']) or
                len(set(params['region_ids']))!=len(params['region_ids'])):
            raise RuleViolation('ERR_SCHEMA')
        if other not in book.aliases:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if any(r not in state.authority.regions for r in params['region_ids']):raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        mode=params.get('mode','armistice')
        if mode=='cession' and (not params['region_ids'] or any(state.authority.regions[r].sovereign!=owner for r in params['region_ids'])):
            raise RuleViolation('ERR_NO_PERMISSION')
        for party in (owner,other):
            if book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation']:
                raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        command_id=str(int(order['id']))
        proposal=PeaceProposal(f'peace_proposal:{owner}:{command_id}',owner,other,command_id,mode,
            tuple(params['region_ids']),params['withdraw_cycles'],order['duration_cycles'],ctx.cycle+order['duration_cycles'])
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def intel_offer(self,owner,order,ctx):
        state=self.state;book=state.contracts;other=order['counterparty'];params=order['params']
        if (other==owner or not 10<=order['duration_cycles']<=720 or set(params)!={'region_ids','min_quality_mU'} or
                type(params['region_ids']) is not list or not params['region_ids'] or
                any(type(r) is not str for r in params['region_ids']) or
                len(set(params['region_ids']))!=len(params['region_ids']) or type(params['min_quality_mU']) is not int or
                not 0<=params['min_quality_mU']<=1000):raise RuleViolation('ERR_SCHEMA')
        if other not in book.aliases or any(r not in state.authority.regions for r in params['region_ids']):raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if book.embargo_blocks(owner,other) or book.embargo_blocks(other,owner):raise RuleViolation('ERR_NO_PERMISSION','joint embargo bars new bilateral contracts')
        if state.relations.will_be_at_war(owner,other):raise RuleViolation('ERR_NO_PERMISSION')
        for party in (owner,other):
            count=sum(party in (p.proposer,p.counterparty) for p in book.proposals.values())
            count+=sum(party in t.parties for t in book.intelligence)
            count+=sum(party in (t.grantor,t.grantee) for t in book.transit)
            if count>=state.catalog.runtime['limits']['contracts_per_nation']:raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        command_id=str(int(order['id']))
        proposal=IntelligenceProposal(f'intel_proposal:{owner}:{command_id}',owner,other,command_id,
            tuple(params['region_ids']),params['min_quality_mU'],order['duration_cycles'],ctx.cycle+order['duration_cycles'])
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def validate_transit(self,p):
        state=self.state
        if state.relations.will_be_at_war(p.proposer,p.counterparty):raise RuleViolation('ERR_NO_PERMISSION')
        for eid in p.corridors:
            edge=state.cargo.corridors.get(eid)
            if edge is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
            if not all(state.authority.controls_hub(p.proposer,h) for h in (edge.a,edge.b)):
                raise RuleViolation('ERR_NO_PERMISSION','cannot grant transit on foreign endpoints')

    def transit_offer(self,owner,order,ctx):
        state=self.state;book=state.contracts;other=order['counterparty'];params=order['params']
        if (other==owner or not 10<=order['duration_cycles']<=720 or
                set(params)-{'corridor_ids','max_flow_mU','toll_cap_ru_mU','allow_basing'} or
                not {'corridor_ids','max_flow_mU','toll_cap_ru_mU'}<=params.keys() or
                type(params['corridor_ids']) is not list or not params['corridor_ids'] or
                any(type(e) is not str for e in params['corridor_ids']) or len(set(params['corridor_ids']))!=len(params['corridor_ids']) or
                type(params['max_flow_mU']) is not int or params['max_flow_mU']<=0 or
                type(params['toll_cap_ru_mU']) is not int or params['toll_cap_ru_mU']<0 or
                type(params.get('allow_basing',False)) is not bool):raise RuleViolation('ERR_SCHEMA')
        if other not in book.aliases:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        command_id=str(int(order['id']))
        proposal=TransitProposal(f'transit_proposal:{owner}:{command_id}',owner,other,command_id,tuple(params['corridor_ids']),
            params['max_flow_mU'],params['toll_cap_ru_mU'],params.get('allow_basing',False),order['duration_cycles'],ctx.cycle+order['duration_cycles'])
        self.validate_transit(proposal)
        for party in (owner,other):
            if book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation']:
                raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def embargo_offer(self,owner,order,ctx):
        state=self.state;book=state.contracts;other=order['counterparty'];params=order['params']
        if (other==owner or not 10<=order['duration_cycles']<=720 or set(params)!={'target_nation','good_ids'} or
                type(params['target_nation']) is not str or type(params['good_ids']) is not list or not params['good_ids'] or
                any(type(g) is not str for g in params['good_ids']) or len(set(params['good_ids']))!=len(params['good_ids'])):
            raise RuleViolation('ERR_SCHEMA')
        if other not in book.aliases or params['target_nation'] not in state.initial.diplomacy.actors:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if params['target_nation'] in (owner,other):raise RuleViolation('ERR_SCHEMA')
        if not set(params['good_ids'])<=set(state.catalog.goods)|{'inference_service','model_weights','data_license'}:raise RuleViolation('ERR_SCHEMA')
        for party in (owner,other):
            if book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation']:raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        proposal=EmbargoProposal(f'embargo_proposal:{owner}:{int(order["id"])}',owner,other,str(int(order['id'])),params['target_nation'],
            tuple(params['good_ids']),order['duration_cycles'],ctx.cycle+order['duration_cycles'])
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def defense_offer(self,owner,order,ctx):
        state=self.state;book=state.contracts;other=order['counterparty'];params=order['params']
        if (other==owner or not 10<=order['duration_cycles']<=720 or set(params)!={'response_cycles','min_aid_ru_mU'} or
                type(params['response_cycles']) is not int or params['response_cycles']!=2 or
                type(params['min_aid_ru_mU']) is not int or params['min_aid_ru_mU']!=50000):raise RuleViolation('ERR_SCHEMA')
        if other not in book.aliases:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if state.relations.will_be_at_war(owner,other):raise RuleViolation('ERR_NO_PERMISSION')
        for party in (owner,other):
            if book.count(party)>=state.catalog.runtime['limits']['contracts_per_nation']:raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        proposal=DefenseProposal(f'defense_proposal:{owner}:{int(order["id"])}',owner,other,str(int(order['id'])),
            order['duration_cycles'],ctx.cycle+order['duration_cycles'])
        return PreparedCommand(lambda:book.proposals.__setitem__(proposal.id,proposal))

    def military_aid(self,owner,order,ctx):
        from .numeric import Q
        fields(order,{'recipient':str,'hub_id':str,'good':str,'qty_mU':int})
        state=self.state;book=state.contracts;recipient=order['recipient'];hub=order['hub_id'];good=order['good'];quantity=order['qty_mU']
        if recipient==owner or recipient not in state.initial.known:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if quantity<=0 or good not in state.catalog.goods or good not in state.export_controls.war_goods|{'food','water','fuel','battery','medical_supplies'}:
            raise RuleViolation('ERR_SCHEMA')
        if not state.authority.controls_hub(recipient,hub):raise RuleViolation('ERR_NO_PERMISSION')
        if not state.export_controls.permits(recipient,owner,owner,good):raise RuleViolation('ERR_NO_PERMISSION')
        if not any(o.ally==owner and o.defender==recipient and o.status=='PENDING' for o in book.defense_obligations):raise RuleViolation('ERR_NO_PERMISSION')
        source=Account(owner,hub,'available',good);target=Account(recipient,hub,'available',good)
        origins=state.initial.inventory.materials.composition(source)
        if any(not state.export_controls.permits(recipient,owner,o.nation,good) for o in origins):raise RuleViolation('ERR_NO_PERMISSION')
        if any(o.economic_owner is not None for o in origins):
            raise RuleViolation('ERR_NO_PERMISSION','military gifts require government-owned supplies')
        stock=state.initial.inventory.prepare_transfers((Transfer(source,target,quantity,retitle=True),))
        def deliver():
            stock.commit()
            for row in stock.materials.deliveries:
                origin=row.origin
                if origin.economic_owner is not None:
                    from dataclasses import replace
                    origin=replace(origin,economic_owner=None)
                key=(owner,recipient,hub,good,origin)
                book.defense_aid[key]=book.defense_aid.get(key,Q(0))+row.quantity_mU
        return PreparedCommand(deliver)

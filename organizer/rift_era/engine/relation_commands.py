"""Simultaneous declarations, embargo eligibility and war spread persistence."""
from .command_schema import fields
from .diplomacy import Relation
from .errors import InvariantViolation, RuleViolation
from .numeric import Q
from .protocol import PreparedCommand, command_sequence, validate_shape, MAX_COMMANDS


class Relations:
    def __init__(self,state):
        self.s=state
        self.pending=[]
        self.previewed=None
        self.preview_peace_pairs=frozenset()
        self.action_cycles={}
        self.last_events=[]
        self.active_spread={p:0 for p in state.initial.known}
        self.exit_spread={p:(0,0) for p in state.initial.known}
        self.spread_pp={p:Q(0) for p in state.initial.known}

    def preview(self,ctx):
        """Declarations have no resource legs; their eligibility is decidable
        before ordinary commands without committing or advancing watermarks.
        Actual execution must later produce exactly this declaration set.
        """
        peace=self.s.contracts.preview_treaties(ctx)
        self.preview_peace_pairs=frozenset(tuple(sorted((p.proposer,p.counterparty))) for p in peace)
        declarations=[]
        for owner,stream in sorted(self.s.command_streams.items()):
            orders=ctx.orders.get(owner,())
            if len(orders)>MAX_COMMANDS:raise RuleViolation('ERR_WINDOW_ORDER_CAP')
            watermark=stream.watermark
            for order in orders:
                try:
                    seq=command_sequence(order)
                    if seq<=watermark:continue
                    watermark=seq
                    validate_shape(order)
                    if (order.get('kind'),order.get('op')) != ('diplomacy','declare_war'):continue
                    self.validate_declaration(owner,order,declarations)
                    declarations.append((owner,order['target_nation']))
                except RuleViolation:
                    continue
        self.previewed=tuple(declarations)

    def validate_declaration(self,owner,order,pending):
        fields(order,{'target_nation':str})
        target=order['target_nation'];dip=self.s.initial.diplomacy
        if target==owner:raise RuleViolation('ERR_SCHEMA')
        if target not in dip.actors:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        ending=tuple(sorted((owner,target))) in self.preview_peace_pairs
        if dip.relation(owner,target)==Relation.WAR and not ending or (owner,target) in pending:
            raise RuleViolation('ERR_NO_PERMISSION','already declared war')

    def will_be_at_war(self,a,b):
        dip=self.s.initial.diplomacy
        return (a!=b and a in dip.actors and b in dip.actors and
                (dip.relation(a,b)==Relation.WAR and tuple(sorted((a,b))) not in self.preview_peace_pairs or
                 any({a,b}=={p,q} for p,q in self.previewed or ())))

    def trade_allowed(self,buyer,seller,good=None):
        if good is not None:return self.s.export_controls.direct_allowed(buyer,seller,good)
        return not self.will_be_at_war(buyer,seller)

    def settle(self,ctx):
        dip=self.s.initial.diplomacy
        declarations=tuple(self.pending)
        if self.previewed is not None and declarations!=self.previewed:
            raise InvariantViolation('declaration preview differs from actual command execution')
        treaties=self.s.contracts.peace_batch(ctx)
        breaches=self.s.contracts.dmz_breaches(ctx)
        actual=tuple(sorted(set(declarations)|set(breaches)))
        broken=set(dip.phase1(ctx.tick,treaties=treaties,declarations=actual))
        broken.update(breaches)
        self.last_events=[]
        current=self.action_cycles.setdefault(ctx.cycle,{})
        for actor,target in actual:
            current[actor]=current.get(actor,Q(0))+Q(1,1000)
            if (actor,target) in broken:
                self.s.credit[actor]=max(Q(1,10),self.s.credit[actor]-Q(35,100))
            self.last_events.append({'actor':actor,'target':target,'war_id':dip.pairs[dip._key(actor,target)].war_id,
                                     'truce_broken':(actor,target) in broken})
        self.pending.clear()
        self.previewed=None
        self.preview_peace_pairs=frozenset()
        for old in tuple(self.action_cycles):
            if old<ctx.cycle-19:del self.action_cycles[old]
        for owner in self.spread_pp:
            active=dip.active_war_interest_pp(owner)
            if active:
                self.spread_pp[owner]=Q(active)
            else:
                if self.active_spread[owner]:
                    self.exit_spread[owner]=(ctx.tick,self.active_spread[owner])
                ended,initial=self.exit_spread[owner]
                self.spread_pp[owner]=max(Q(0),initial*(1-Q(ctx.tick-ended,20*ctx.clock.cycle_ticks)))
            self.active_spread[owner]=active

    def visible(self,owner):
        return {'war_spread_bp':round(self.spread_pp[owner]*100),
                'pairs':[{'other':next(p for p in pair if p!=owner),'state':row.state.value,
                          'aggressors':sorted(row.aggressors),'expiry_tick':row.expiry_tick,'war_id':row.war_id}
                         for pair,row in sorted(self.s.initial.diplomacy.pairs.items()) if owner in pair]}

    def snapshot(self):
        return {'pending':self.pending,'previewed':self.previewed,'preview_peace_pairs':self.preview_peace_pairs,'action_cycles':{str(c):v for c,v in self.action_cycles.items()},
                'last_events':self.last_events,'active_spread':self.active_spread,
                'exit_spread':self.exit_spread,'spread_pp':self.spread_pp}


class DiplomacyCommands:
    OPERATIONS=frozenset({('diplomacy','declare_war')})

    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        self.state.relations.validate_declaration(owner,order,self.state.relations.pending)
        target=order['target_nation']
        return PreparedCommand(lambda:self.state.relations.pending.append((owner,target)))

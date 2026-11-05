"""Public export policies and provenance-preserving permission decisions.

These decisions authorize new deliveries, not old contractual exemptions.
No merchant or clearing pool may replace an item's actual origin with itself.
"""
from dataclasses import dataclass

from .command_schema import fields
from .errors import InvariantViolation, RuleViolation
from .numeric import Q
from .protocol import PreparedCommand


CONTROLLED_GOODS=frozenset({'accelerator','accelerator_g2','accelerator_g3','advanced_chip','photonic_chip',
                           'inference_service','model_weights','data_license'})


@dataclass(frozen=True)
class ExportPolicy:
    goods: frozenset[str] = frozenset()
    targets: frozenset[str] = frozenset()


class ExportControls:
    def __init__(self,state):
        self.s=state
        self.policies={p:ExportPolicy() for p in state.initial.known}
        # C26's military item set is explicit and catalog-derived: actual
        # fielded chassis/equipment, ammunition and dedicated missile inputs.
        self.war_goods=CONTROLLED_GOODS | frozenset(g for u in state.catalog.units.values() for g in u['installed']) | {
            'hypersonic_missile','guidance_module'}
        self.action_cycles={}
        self.last_tick=-1

    def direct_allowed(self,buyer,seller,good):
        if buyer==seller:return True
        if good in self.war_goods and self.s.relations.will_be_at_war(buyer,seller):return False
        if hasattr(self.s,'contracts') and self.s.contracts.embargo_blocks(seller,buyer,good):return False
        policy=self.policies.get(seller,ExportPolicy())
        return not (good in policy.goods and buyer in policy.targets)

    def permits(self,buyer,seller,origin,good):
        return self.direct_allowed(buyer,seller,good) and self.direct_allowed(buyer,origin,good)

    def record_policy_events(self,ctx):
        if ctx.tick<=self.last_tick:raise InvariantViolation('export policy tick repeated')
        self.last_tick=ctx.tick
        targets={p:set(policy.targets) if policy.goods else set() for p,policy in self.policies.items()}
        if hasattr(self.s,'contracts'):
            for treaty in self.s.contracts.embargoes:
                if treaty.start_cycle<=ctx.cycle<treaty.end_cycle:
                    for party in treaty.parties:targets[party].add(treaty.target)
        for owner in targets:
            targets[owner].update(other for other in self.s.initial.diplomacy.actors
                                  if self.s.relations.will_be_at_war(owner,other))
        losses={p:Q(len(others),1000) for p,others in targets.items() if others}
        row=self.action_cycles.setdefault(ctx.cycle,{})
        for owner,value in losses.items():row[owner]=row.get(owner,Q(0))+value
        self.action_cycles={c:v for c,v in self.action_cycles.items() if c>=ctx.cycle-19}

    def visible(self):
        return [{'nation':p,'goods':sorted(policy.goods),'targets':sorted(policy.targets)}
                for p,policy in sorted(self.policies.items()) if policy.goods and policy.targets]

    def snapshot(self):
        return {'policies':self.policies,'war_goods':self.war_goods,'last_tick':self.last_tick,
                'action_cycles':{str(c):v for c,v in self.action_cycles.items()}}

    def audit(self):
        for owner,policy in self.policies.items():
            if not policy.goods<=CONTROLLED_GOODS or not policy.targets<=self.s.initial.diplomacy.actors or owner in policy.targets:
                raise InvariantViolation('invalid export policy')


class ExportControlCommands:
    OPERATIONS=frozenset({('finance','export_control')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'goods':list,'targets':list})
        goods,targets=order['goods'],order['targets']
        if (any(type(g) is not str for g in goods) or any(type(p) is not str for p in targets)
                or len(set(goods))!=len(goods) or len(set(targets))!=len(targets)
                or not set(goods)<=CONTROLLED_GOODS or owner in targets):
            raise RuleViolation('ERR_SCHEMA')
        if not set(targets)<=self.state.initial.diplomacy.actors:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        policy=ExportPolicy(frozenset(goods),frozenset(targets))
        return PreparedCommand(lambda:self.state.export_controls.policies.__setitem__(owner,policy))

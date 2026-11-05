"""Territorial occupation evidence from surviving stationary land units only.

No order or contact estimate proves a garrison. Neutral forces never create a
claim, and control never conveys sovereignty, inventory, or economic shares.
"""
from collections import defaultdict
from .diplomacy import Relation


class MilitaryGovernance:
    def __init__(self,state):
        self.s=state
        self.events=[]

    def advance(self,ctx):
        state=self.s;present=defaultdict(set);garrisons=defaultdict(set)
        for formation in state.formations.rows.values():
            if not any(state.initial.units[u].hp_mU>0 and state.initial.units[u].crew_mU>0 for u in formation.unit_ids):continue
            # Air and sea platforms may fight or escort but cannot hold land.
            if formation.domain!='Land':continue
            present[formation.region].add(formation.owner)
            stationary=(formation.id not in getattr(state.movement,'moved_formations',()) and
                        not state.movement.planned(formation.id) and formation.id not in state.movement.retreating)
            if stationary and not formation.refitting and formation.task in ('garrison','attack','move'):
                garrisons[formation.region].add(formation.owner)
        sole={};hostile={}
        for region,owners in garrisons.items():
            controller=state.authority.regions[region].controller
            eligible={p for p in owners if controller is not None and p!=controller and
                      state.initial.diplomacy.relation(p,controller)==Relation.WAR}
            if len(eligible)!=1:continue
            owner=next(iter(eligible))
            sole[region]=owner
            hostile[region]=any(p!=owner and state.initial.diplomacy.relation(owner,p)==Relation.WAR for p in present[region])
        self.events=list(state.authority.advance_control(ctx.tick,sole_garrisons=sole,hostile_defenders=hostile))
        return tuple(self.events)

    def snapshot(self):return {'events':self.events}

    def visible(self,owner):
        return {'controlled_regions':[{'region_id':r,'sovereign':row.sovereign,'support_mU':row.support_mU,
                    'occupied_since_tick':row.occupied_since_tick,'version':row.version}
                 for r,row in sorted(self.s.authority.regions.items()) if row.controller==owner],
                'control_changes':[{'region_id':r,'previous_controller':old,'controller':new}
                                   for r,old,new in self.events if owner in (old,new)]}

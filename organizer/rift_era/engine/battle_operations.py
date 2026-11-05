"""P1 local supply custody and simultaneous P4 combat for physical units."""
from collections import defaultdict
from dataclasses import replace
from .scoring import action_loss
from .capital_book import proportional_loss
from .combat import resolve, apply_result, TargetContact, LAND_ATTACK
from .diplomacy import Relation
from .errors import InvariantViolation
from .ledger import Account, Transfer
from .numeric import Q, proportional, units
from .provenance import Use
from .nonland_navigation import base_online


class Battles:
    def __init__(self, state):
        self.s = state
        self.locks = {}
        self.last_result = None
        self.loss_cycles = {}
        self.destroyed_cycles = {}
        self.combat_regions = set()
        self.power_demands = {}
        self.power_context = {}
        self.unit_formations = {}
        self.load_hubs = {}

    def combatants(self):
        return self.s.initial.units | self.s.deception.proxies()

    def contacts(self):
        state=self.s;frame=state.sensors.frame()
        formations=tuple(state.formations.rows.values())
        signals={f.id:state.sensors.signal(f) for f in formations}
        signals.update({d.id:state.deception.signal(d) for d in state.deception.rows.values()})
        members={f.id:f.unit_ids for f in formations}
        members.update({d.id:(d.id,) for d in state.deception.rows.values()})
        combatants=self.combatants()
        contacts={}
        for formation in formations:
            targets=[]
            for target in signals.values():
                if target.owner==formation.owner or state.initial.diplomacy.relation(formation.owner,target.owner)!=Relation.WAR:continue
                if state.deception.exposed(formation.owner,target.id):continue
                quality,_=state.sensors.quality(formation.owner,target,frame)
                if quality<Q(1,5):continue
                hops=state.sensors.distances(formation.region).get(target.region,99)
                targets.extend(TargetContact(uid,quality,hops) for uid in members[target.id] if combatants[uid].hp_mU>0)
            if hasattr(state,'intelligence_sharing'):
                direct_ids={c.target for c in targets}
                shared=state.intelligence_sharing.contacts(formation.owner,formation.region)
                by_target={}
                for contact in shared:
                    if contact.target in direct_ids:continue
                    if contact.target not in by_target or contact.quality>by_target[contact.target].quality:by_target[contact.target]=contact
                targets.extend(by_target.values())
            for uid in formation.unit_ids:contacts[uid]=tuple(targets)
        return contacts

    def reserve(self, ctx):
        state = self.s
        if self.locks:
            raise InvariantViolation('unsettled prior battle supply locks')
        contacts = self.contacts()
        combatants = self.combatants()
        requests = defaultdict(dict)
        self.combat_regions = set()
        self.power_demands = {}
        self.power_context = {}
        self.unit_formations = {}
        self.load_hubs = {}
        for formation in state.formations.rows.values():
            if formation.refitting:continue
            location = formation.hub or formation.id
            for uid in formation.unit_ids:
                moving=state.movement.planned(formation.id) or formation.id in state.movement.retreating
                if not contacts.get(uid) and not moving:
                    continue
                unit = state.initial.units[uid]
                spec = state.catalog.units[unit.unit_type]
                channels = state.catalog.tables['military']['domain_channel']
                if not moving and not any(spec['firepower'][channels[state.catalog.units[(c.snapshot or combatants[c.target]).unit_type]['domain']]] > 0
                           for c in contacts[uid]):
                    continue
                if unit.unit_type == 'energy_weapon' and formation.hub is not None:
                    entity = f'weapon:{uid}'
                    self.power_demands[entity] = (formation.hub, 0, 500)
                    self.power_context[entity] = (unit.owner, formation.hub)
                for good, quantity in spec['combat_tick'].items():
                    if quantity:
                        if good=='fuel':quantity*=unit.fuel_factor
                        if good=='ammunition' and unit.precision:good='precision_ammunition'
                        onboard=Account(unit.owner,formation.id,'available',good)
                        source=onboard if moving or not base_online(state,unit.owner,formation.hub,formation.domain) or state.initial.inventory.balance(onboard) else Account(unit.owner,location,'available',good)
                        requests[source][uid] = units(quantity)
                        self.unit_formations[uid]=formation.id
                        self.load_hubs[uid]=state.movement.location(formation)
        transfers = []
        for source, demand in sorted(requests.items()):
            grants = proportional(state.initial.inventory.balance(source), demand,
                                  key=(ctx.seed,'battle_supply',ctx.tick,source.owner,source.location,source.asset))
            for uid, quantity in grants.items():
                locked = Account(source.owner,self.unit_formations[uid],f'locked_battle:{uid}',source.asset)
                transfers.append(Transfer(source,locked,quantity,destination_hub=self.load_hubs[uid]))
                self.locks[uid,source.asset] = (locked,source)
        state.initial.inventory.transfer_many(transfers)

    def advance(self, ctx):
        state, inv = self.s, self.s.initial.inventory
        supplies = defaultdict(dict)
        for (uid,good),(lock,_) in self.locks.items():
            supplies[uid][good] = inv.balance(lock)
        for entity in self.power_demands:
            supplies[entity.split(':',1)[1]]['electricity'] = state.frame.power_received.get(entity,0)
        unit_owners={uid:u.owner for uid,u in state.initial.units.items()}
        moving,abandoned=state.movement.advance(ctx,supplies)
        contacts = self.contacts()
        terrain_override={}
        for a,b,_,point in state.movement.encounters:
            if point.destination is None or not point.progress:continue
            edge=state.cargo.corridors[point.edge]
            terrains=[state.initial.map.regions[state.initial.map.hubs[h].region].terrain for h in (edge.a,edge.b)]
            terrain=min(terrains,key=lambda t:LAND_ATTACK.get(t,Q(1)))
            for fid in (a,b):
                for uid in state.formations.rows[fid].unit_ids:terrain_override[uid]=terrain
        result = resolve(state.catalog,state.initial.diplomacy,self.combatants(),contacts,supplies,
                         {rid:r.terrain for rid,r in state.initial.map.regions.items()},moving=moving,terrain_by_unit=terrain_override,inactive=state.refits.inactive_units(),target_only=frozenset(state.deception.rows))
        consumed = {}
        for uid, goods in result.consumed_mU.items():
            for good, quantity in goods.items():
                if quantity and good != 'electricity':
                    consumed[self.locks[uid,good][0]] = quantity
        inv.transform(consumed,{},reason='military_fire',use=Use.ACTION_LOSS)
        destroyed = defaultdict(lambda: Q(0))
        dead = []
        for formation in tuple(state.formations.rows.values()):
            for uid in formation.unit_ids:
                unit = state.initial.units[uid]
                damage = result.damage_mU[uid]
                spec = state.catalog.units[unit.unit_type]
                if damage:
                    removed=proportional_loss(unit.book_nmU,damage,unit.hp_mU)
                    for good,quantity in removed.items():unit.book_nmU[good]-=quantity
                    unit.book_rounding_steps+=len(removed)
                    destroyed[unit.owner]+=sum((q*state.catalog.weights[g]/1000000 for g,q in removed.items()),Q(0))
                    removed = proportional(result.casualties_mU[uid],formation.crew_skills[uid],
                                           key=(ctx.seed,'military_casualties',ctx.tick,uid))
                    for skill, quantity in removed.items():
                        formation.crew_skills[uid][skill] -= quantity
                    formation.version += 1
                if damage == unit.hp_mU:
                    dead.append((formation,uid))
        state.vision.record_deaths(tuple(f for f in state.formations.rows.values()
            if all(result.damage_mU[uid]==state.initial.units[uid].hp_mU for uid in f.unit_ids)))
        shortage = set()
        for fid, formation in state.formations.rows.items():
            old = state.frame.military_context.get(fid)
            if old is None:
                continue
            actual = state.frame.military.get(fid,{})
            need = state.frame.military_need[fid]
            if any(actual.get(g,0) < q for g,q in need.items()):
                shortage.update(formation.unit_ids)
        apply_result(state.initial.units,result,food_water_short=frozenset(shortage),moving=moving)
        retired={}
        for formation,uid in dead:
            unit = state.initial.units[uid]
            destroyed[unit.owner]+=state.military_repairs.destroy_credits(uid)
            accounts = {state.formations.equipment_account(unit.owner,uid,g):units(q)
                        for g,q in state.catalog.units[unit.unit_type]['installed'].items()}
            inv.transform(accounts,{},reason='military_equipment_destroyed',use=Use.DESTRUCTION)
            formation.unit_ids = tuple(u for u in formation.unit_ids if u != uid)
            del formation.crew_skills[uid]
            del state.initial.units[uid]
            if not formation.unit_ids:
                retired[formation.id]=formation
                job=next((j for j in state.refits.jobs.values() if j.formation==formation.id),None)
                if job:state.refits.cancel(job)
                del state.formations.rows[formation.id]
        inv.transfer_many(Transfer(lock,source,inv.balance(lock),destination_hub=state.movement.last_hub.get(self.unit_formations[uid],self.load_hubs[uid]))
                          for (uid,good),(lock,source) in self.locks.items())
        for formation in retired.values():state.movement.retire(formation)
        self.locks.clear()
        merged={uid:dict(goods) for uid,goods in result.consumed_mU.items()}
        for uid,goods in abandoned.items():
            for good,quantity in goods.items():
                row=merged.setdefault(uid,{})
                row[good]=row.get(good,0)+quantity
        executed=set(moving)|{shot.source for shot in result.shots}|{uid for uid,goods in merged.items() if any(goods.values())}
        losses=defaultdict(lambda:Q(0))
        for uid in executed:losses[unit_owners[uid]]+=action_loss(merged.get(uid,{}),state.catalog.weights)
        result=replace(result,consumed_mU=merged,action_losses=dict(losses))
        state.movement.evaluate_retreat(ctx)
        for table, values in ((self.loss_cycles,result.action_losses),(self.destroyed_cycles,destroyed)):
            current = table.setdefault(ctx.cycle,{})
            for owner, value in values.items():
                current[owner] = current.get(owner,Q(0))+value
            for old in tuple(table):
                if old < ctx.cycle-19:
                    del table[old]
        for owner,value in destroyed.items():
            state.cycles[owner].destroyed_value += value
        self.combat_regions = set(result.combat_regions)
        self.last_result = result

    def snapshot(self):
        return {'unit_formations':self.unit_formations,'load_hubs':self.load_hubs,'power_demands':self.power_demands,'power_context':self.power_context,'locks':[[uid,good,lock,source] for (uid,good),(lock,source) in sorted(self.locks.items())],
                'last_result':self.last_result,'loss_cycles':{str(c):v for c,v in self.loss_cycles.items()},
                'destroyed_cycles':{str(c):v for c,v in self.destroyed_cycles.items()},
                'combat_regions':self.combat_regions}

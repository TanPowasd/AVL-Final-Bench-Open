"""Physical formation membership, deployment and resident skill custody."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections import defaultdict
from .combat import Unit
from .command_schema import fields, version
from .determinism import digest, primitive
from .capital_book import CAPITAL_BOOK_SCALE
from .errors import RuleViolation, InvariantViolation
from .ledger import Account, Transfer, CashTransfer, DeliveryPayment
from .numeric import Q, units, floor, ceil
from .population import SKILLS
from .protocol import PreparedCommand
from .military_training import recruitable_skills
from .nonland_navigation import base_online


@dataclass
class Formation:
    id: str
    owner: str
    domain: str
    hub: str | None
    region: str
    unit_ids: tuple[str, ...]
    crew_skills: dict[str, dict[str, int]] = field(default_factory=dict)
    task: str = 'garrison'
    version: int = 0
    home_hub: str | None = None
    slot_a: str | None = None
    slot_b: str | None = None
    refitting: bool = False
    emitter_active: bool = True
    ew_mode: str = 'jamming'


@dataclass
class StoredUnit:
    hub: str
    unit: Unit


class Formations:
    def __init__(self, state):
        self.s = state
        grouped = defaultdict(list)
        for unit in state.initial.units.values():
            if not unit.book_nmU:
                unit.book_nmU={g:Q(units(q)*1000) for g,q in state.catalog.units[unit.unit_type]['installed'].items()}
            grouped['/'.join(unit.id.split('/')[:4])].append(unit.id)
        self.rows = {}
        for fid, ids in sorted(grouped.items()):
            first = state.initial.units[ids[0]]
            self.rows[fid] = Formation(fid, first.owner, state.catalog.units[first.unit_type]['domain'],
                f'{first.owner}-h0', first.region, tuple(sorted(ids)),
                {uid: {'general_worker': state.initial.units[uid].crew_mU} for uid in ids}, home_hub=f'{first.owner}-h0')
        transfers = []
        for unit in state.initial.units.values():
            hub = f'{unit.owner}-h0'
            for good, quantity in state.catalog.units[unit.unit_type]['installed'].items():
                transfers.append(Transfer(Account(unit.owner, hub, f'installed_military:{unit.id}', good),
                    self.equipment_account(unit.owner, unit.id, good), units(quantity), destination_hub=hub))
        state.initial.inventory.transfer_many(transfers)
        self.stored_units = {}

    def equipment_account(self, owner, uid, good):
        return Account(owner, uid, 'installed_military', good)

    def stored_account(self, owner, hub, uid, good):
        return Account(owner, hub, f'stored_military:{uid}', good)

    def crew(self, formation):
        return sum(self.s.initial.units[uid].crew_mU for uid in formation.unit_ids)

    def visible(self, owner):
        return [{'id': f.id, 'domain': f.domain, 'hub_id': f.hub, 'region_id': f.region,
                 'version': f.version, 'task': f.task, 'slot_a': f.slot_a, 'slot_b': f.slot_b, 'refitting': f.refitting, 'emitter_active': f.emitter_active if f.slot_a in ('radar','ew') else False, 'ew_mode': f.ew_mode,
                 'units': [{'id': uid, 'unit_id': self.s.initial.units[uid].unit_type,
                    'hp_mU': self.s.initial.units[uid].hp_mU, 'crew_mU': self.s.initial.units[uid].crew_mU,
                    'crew_skills_mU': dict(f.crew_skills[uid]), 'book_nmU': primitive(self.s.initial.units[uid].book_nmU),
                    'morale_mU': floor(self.s.initial.units[uid].morale * 1000),
                    'fatigue_mU': floor(self.s.initial.units[uid].fatigue * 1000)} for uid in f.unit_ids]}
                for f in self.rows.values() if f.owner == owner]

    def audit(self):
        assigned = set()
        for formation in self.rows.values():
            if not 1 <= len(formation.unit_ids) <= (8 if formation.domain == 'Air' else 12):
                raise InvariantViolation('formation membership outside domain cap')
            for uid in formation.unit_ids:
                if uid in assigned or uid not in self.s.initial.units:
                    raise InvariantViolation('unit missing or assigned twice')
                assigned.add(uid)
                unit = self.s.initial.units[uid]
                if unit.owner != formation.owner or self.s.catalog.units[unit.unit_type]['domain'] != formation.domain:
                    raise InvariantViolation('mixed owner/domain formation')
                for good, quantity in self.s.catalog.units[unit.unit_type]['installed'].items():
                    if self.s.initial.inventory.balance(self.equipment_account(unit.owner, uid, good)) != units(quantity):
                        raise InvariantViolation('military equipment differs from actual inventory')
                if sum(formation.crew_skills[uid].values()) != unit.crew_mU:
                    raise InvariantViolation('military skills disagree with living crew')
        for uid, stored in self.stored_units.items():
            unit = stored.unit
            if uid in assigned or uid != unit.id or unit.crew_mU != 0:
                raise InvariantViolation('stored equipment still has active crew or membership')
            for good, quantity in self.s.catalog.units[unit.unit_type]['installed'].items():
                account = self.stored_account(unit.owner, stored.hub, uid, good)
                if self.s.initial.inventory.balance(account) != units(quantity):
                    raise InvariantViolation('stored military equipment missing from inventory')
        for unit in (*self.s.initial.units.values(), *(u.unit for u in self.stored_units.values())):
            if any(q<0 or CAPITAL_BOOK_SCALE%Q(q).denominator for q in unit.book_nmU.values()):
                raise InvariantViolation('invalid military remaining-cost precision')
            maximum=sum(Q(q)*1000000*self.s.catalog.weights[g] for g,q in self.s.catalog.units[unit.unit_type]['installed'].items())
            if sum(q*self.s.catalog.weights[g] for g,q in unit.book_nmU.items())>maximum:
                raise InvariantViolation('military remaining cost exceeds original equipment cost')
        if assigned != self.s.initial.units.keys():
            raise InvariantViolation('active unit without a formation')

    def snapshot(self):
        return {'formations': self.rows, 'stored_units': self.stored_units}


class FormationCommands:
    OPERATIONS = frozenset({('military', 'deploy_formation'), ('military', 'disband')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        if order['op'] == 'disband':
            return self.disband(owner, order)
        fields(order, {'hub_id': str, 'domain': str, 'units': dict}, {'crew_mU': int})
        state = self.state
        hub, domain, requested = order['hub_id'], order['domain'], order['units']
        if domain not in state.catalog.tables['military']['domain_channel'] or not requested:
            raise RuleViolation('ERR_SCHEMA')
        if any(uid not in state.catalog.units or type(q) is not int or q <= 0 for uid, q in requested.items()):
            raise RuleViolation('ERR_SCHEMA')
        if any(state.catalog.units[uid]['domain'] != domain for uid in requested):
            raise RuleViolation('ERR_MIXED_DOMAIN_FORMATION')
        if sum(requested.values()) > (8 if domain == 'Air' else 12):
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if sum(f.owner == owner for f in state.formations.rows.values()) >= state.catalog.runtime['limits']['formations_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if not state.authority.controls_hub(owner, hub) or hub not in state.initial.populations:
            raise RuleViolation('ERR_NO_PERMISSION')
        region = state.initial.map.hubs[hub].region
        if domain!='Land' and not base_online(state,owner,hub,domain):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        needed = sum(units(state.catalog.units[uid]['crew']) * q for uid, q in requested.items())
        if 'crew_mU' in order and order['crew_mU'] != needed:
            raise RuleViolation('ERR_SCHEMA')
        population = state.initial.populations[hub]
        chosen = recruitable_skills(state, owner, hub, needed, ctx)
        fid = 'formation:' + digest(owner, str(int(order['id']))).hex()[:24]
        new_units, transfers, skills = {}, [], {}
        reused = set()
        pool = dict(chosen)
        for kind, count in sorted(requested.items()):
            spec = state.catalog.units[kind]
            stored = sorted((u for u in state.formations.stored_units.values()
                if u.unit.owner == owner and u.hub == hub and u.unit.unit_type == kind and u.unit.hp_mU > 0),
                key=lambda u: (digest(owner, ctx.tick, 'redeploy', u.unit.id), u.unit.id))
            for index in range(count):
                previous = stored[index] if index < len(stored) else None
                uid = previous.unit.id if previous else f'{fid}/{kind}/{index}'
                crew = units(spec['crew'])
                new_units[uid] = (replace(previous.unit, region=region, crew_mU=crew, morale=Q(85,100), book_nmU=dict(previous.unit.book_nmU), precision=False, armor_bonus=0, speed_factor=Q(1),
                                          fuel_factor=Q(1), sensor_bonus=Q(0), asw_factor=Q(1), electronic_warfare=False) if previous else
                                  Unit(uid, owner, kind, region, units(spec['hp']), crew, morale=Q(85,100),
                                       book_nmU={g:Q(units(q)*1000) for g,q in spec['installed'].items()}))
                if previous:
                    reused.add(uid)
                allocation, left = {}, crew
                for skill in SKILLS:
                    amount = min(left, pool[skill])
                    allocation[skill] = amount
                    pool[skill] -= amount
                    left -= amount
                skills[uid] = allocation
                transfers.extend(Transfer(state.formations.stored_account(owner, hub, uid, good) if previous else
                    Account(owner, hub, 'available', good),
                    state.formations.equipment_account(owner, uid, good), units(q), destination_hub=hub,retitle=True)
                    for good, q in spec['installed'].items())
        stock = state.initial.inventory.prepare_transfers(transfers)
        from .economic_ownership import prepare_purchase,record_income
        payment,recipients=prepare_purchase(state.payments,state.native(owner,'treasury'),stock.materials.deliveries,
            {t.source.asset:state.price(t.source.asset,hub) for t in transfers},kind='military_equipment',
            key=(state.initial.map.seed,ctx.tick,'military_equipment',fid),buyer=state.native(owner,'treasury'))
        formation = Formation(fid, owner, domain, hub, region, tuple(new_units), skills, home_hub=hub)
        def commit():
            payment.commit(stock)
            record_income(state,recipients,payment.amounts)
            population.reserve_people(chosen)
            for uid in reused:
                del state.formations.stored_units[uid]
            state.initial.units.update(new_units)
            state.formations.rows[fid] = formation
        return PreparedCommand(commit)

    def disband(self, owner, order):
        fields(order, {'formation_id': str}, {'expected_version': int})
        state = self.state
        formation = state.formations.rows.get(order['formation_id'])
        if formation is None or formation.owner != owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order, formation)
        hub = formation.hub
        if hub is None or not state.authority.controls_hub(owner, hub) or hub not in state.initial.populations:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        refunds, transfers, stored = defaultdict(int), [], {}
        for uid in formation.unit_ids:
            unit = state.initial.units[uid]
            for skill, quantity in formation.crew_skills[uid].items():
                refunds[skill] += quantity
            stored[uid] = StoredUnit(hub, replace(unit, crew_mU=0, book_nmU=dict(unit.book_nmU)))
            for good, quantity in state.catalog.units[unit.unit_type]['installed'].items():
                transfers.append(Transfer(state.formations.equipment_account(owner, uid, good),
                    state.formations.stored_account(owner, hub, uid, good), units(quantity)))
        from .military_refits import SLOTS_A,SLOTS_B
        refit=next((j for j in state.refits.jobs.values() if j.formation==formation.id),None)
        for slot,selected,table in (('A',formation.slot_a,SLOTS_A),('B',formation.slot_b,SLOTS_B)):
            transfers.extend(Transfer(state.refits.slot_account(formation,slot,g),Account(owner,hub,'available',g),q)
                             for g,q in table[selected][0].items())
        if refit:
            transfers.extend(Transfer(refit.lock(g),Account(owner,hub,'available',g),state.initial.inventory.balance(refit.lock(g)))
                             for g in refit.materials)
        transfers.extend(Transfer(a,Account(owner,hub,'available',a.asset),q) for a,q in state.initial.inventory.balances().items()
                         if a.owner==owner and a.location==formation.id and a.bucket=='available')
        added=sum(t.quantity*state.cargo_weights[t.source.asset] for t in transfers if t.source.location!=hub and t.destination.location==hub)
        held=refit.reserved_capacity if refit else Q(0)
        if state.warehouse_load()[hub]-held+added>state.initial.warehouse_mU[hub]:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED','no warehouse capacity for returned equipment')
        stock = state.initial.inventory.prepare_transfers(transfers)
        money=state.initial.cash.prepare_transfers((CashTransfer(refit.cash_lock(),state.native(owner,'treasury'),
            state.initial.cash.balance(refit.cash_lock()).native_mU),) if refit else ())
        def commit():
            DeliveryPayment(stock,money).commit()
            if refit:del state.refits.jobs[refit.id]
            state.initial.populations[hub].receive(refunds)
            state.formations.stored_units.update(stored)
            for uid in formation.unit_ids:
                del state.initial.units[uid]
            state.vision.own_retired(owner,formation.id)
            state.movement.retire(formation, wreck=False)
            del state.formations.rows[formation.id]
        return PreparedCommand(commit)

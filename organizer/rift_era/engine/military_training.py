"""Reinforcement residents remain fed, housed and mortal during training."""
from dataclasses import dataclass

from .command_schema import fields, version
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .numeric import Q, ceil, units
from .population import SKILLS
from .protocol import PreparedCommand


def recruitable_skills(state, owner, hub, needed, ctx):
    population = state.initial.populations[hub]
    remaining, chosen = needed, {}
    participation = state.labor_participation(owner, hub)
    for skill in SKILLS:
        committed = sum(workers.get(skill, 0) for entity, workers in state.frame.labor.items()
                        if state.frame.work_context.get(entity, (None,None,None))[1] == hub)
        committed+=ceil(state.ai_deployment.held_at(hub,skill))
        minimum = (ceil(Q(committed * ctx.clock.cycle_ticks) / participation) if participation else
                   population.skills_mU.get(skill, 0)) if committed else 0
        take = min(remaining, max(0, population.skills_mU.get(skill, 0) - minimum))
        chosen[skill] = take
        remaining -= take
    if remaining:
        raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'uncommitted resident crew unavailable')
    return chosen


@dataclass
class Reinforcement:
    id: str
    command_id: str
    owner: str
    formation: str
    unit: str
    hub: str
    ready_tick: int
    original_skills: dict[str, int]

    def pool_key(self, skill):
        return f'{self.id}:{skill}'


class MilitaryTraining:
    def __init__(self, state):
        self.s = state
        self.jobs = {}

    def training_ids(self):
        return {job.pool_key(skill) for job in self.jobs.values() for skill in job.original_skills}

    def finish(self, job, *, join):
        state = self.s
        population = state.initial.populations[job.hub]
        people = {skill: population.training_mU.pop(job.pool_key(skill)) for skill in job.original_skills}
        if join:
            formation = state.formations.rows[job.formation]
            unit = state.initial.units[job.unit]
            maximum = units(state.catalog.units[unit.unit_type]['crew'])
            space = max(0, maximum - unit.crew_mU)
            for skill, amount in people.items():
                take = min(space, amount)
                unit.crew_mU += take
                formation.crew_skills[unit.id][skill] = formation.crew_skills[unit.id].get(skill, 0) + take
                people[skill] -= take
                space -= take
            formation.version += 1
        population.receive(people)
        del self.jobs[job.id]

    def advance(self, ctx):
        for job in tuple(self.jobs.values()):
            formation = self.s.formations.rows.get(job.formation)
            if formation is None or job.unit not in self.s.initial.units:
                self.finish(job, join=False)
            elif ctx.tick >= job.ready_tick and formation.hub == job.hub and self.s.authority.controls_hub(job.owner,job.hub):
                self.finish(job, join=True)

    def prepare_cancel(self, owner, command_id):
        job = next((j for j in self.jobs.values() if j.owner==owner and j.command_id==command_id),None)
        return PreparedCommand(lambda:self.finish(job,join=False)) if job else None

    def visible(self, owner):
        return [{'id':j.command_id,'formation_id':j.formation,'unit_id':j.unit,'hub_id':j.hub,
                 'ready_tick':j.ready_tick,'survivors_mU':sum(self.s.initial.populations[j.hub].training_mU[j.pool_key(s)]
                     for s in j.original_skills)} for j in self.jobs.values() if j.owner==owner]

    def audit(self):
        targets=set()
        for job in self.jobs.values():
            if job.unit in targets:
                raise InvariantViolation('unit has overlapping reinforcement reservations')
            targets.add(job.unit)
            for skill, original in job.original_skills.items():
                actual=self.s.initial.populations[job.hub].training_mU.get(job.pool_key(skill))
                if actual is None or not 0 <= actual <= original:
                    raise InvariantViolation('reinforcement resident custody mismatch')

    def snapshot(self):
        return {'jobs':self.jobs}


class ReinforcementCommands:
    OPERATIONS=frozenset({('military','reinforce')})

    def __init__(self,state):
        self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'formation_id':str,'unit_index':int,'crew_mU':int},{'expected_version':int})
        state=self.state
        formation=state.formations.rows.get(order['formation_id'])
        if formation is None or formation.owner != owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order,formation)
        hub=formation.hub
        if hub is None or not state.authority.controls_hub(owner,hub) or hub not in state.initial.populations:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if not 0 <= order['unit_index'] < len(formation.unit_ids) or order['crew_mU'] <= 0:
            raise RuleViolation('ERR_SCHEMA')
        uid=formation.unit_ids[order['unit_index']]
        unit=state.initial.units[uid]
        if order['crew_mU'] > units(state.catalog.units[unit.unit_type]['crew']) - unit.crew_mU:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        if any(j.unit==uid for j in state.recruitment.jobs.values()):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        skills=recruitable_skills(state,owner,hub,order['crew_mU'],ctx)
        job=Reinforcement('recruit:'+digest(owner,str(int(order['id']))).hex()[:24],str(int(order['id'])),
                          owner,formation.id,uid,hub,ctx.tick+2*ctx.clock.cycle_ticks,{s:q for s,q in skills.items() if q})
        def commit():
            population=state.initial.populations[hub]
            population.reserve_people(skills)
            for skill,quantity in job.original_skills.items():
                population.training_mU[job.pool_key(skill)]=quantity
            state.recruitment.jobs[job.id]=job
        return PreparedCommand(commit)

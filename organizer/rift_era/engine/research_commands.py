"""Research command validation, laboratory binding and physical validation jobs."""

from .command_schema import fields, version
from .determinism import fingerprint
from .errors import RuleViolation
from .numeric import Q, units
from .production import Line, Recipe, Production
from .protocol import PreparedCommand
from .provenance import Use
from .research import SECURITY_MULTIPLIER, ValidationJob


class ResearchCommands:
    OPERATIONS = frozenset({('research', 'set_focus'), ('research', 'validate_project'), ('research', 'set_backup_hub')})

    def __init__(self, state):
        self.state = state

    def _lab(self, owner, lid):
        line = self.state.initial.lines.get(lid)
        if line is None or line.owner != owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if line.recipe.id not in self.state.catalog.recipes or self.state.catalog.recipes[line.recipe.id]['facility'] != '实验室':
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        return line

    def _discard(self, job):
        draft = self.state.initial.inventory.materials.draft()
        for good, amount in job.line.credit_nmU.items():
            draft.consume(Production.credit_account(job.line, good), Q(amount, 1000),
                          use=Use.DESTRUCTION, reason='validation_project_change')
        return self.state.initial.inventory.prepare_transform({}, {}, reason='validation_project_change', material_plan=draft.finish())

    def set_backup_hub(self, owner, order):
        """06: needs R08 and an online backup centre (computing + communication) at backup_hub."""
        state = self.state
        fields(order, {'primary_hub': str, 'backup_hub': str})
        primary, backup = order['primary_hub'], order['backup_hub']
        research = state.research[owner]
        if 'R08' not in research.known:
            raise RuleViolation('ERR_TECH_LOCKED')
        hubs = state.initial.map.hubs
        if primary not in hubs or backup not in hubs or primary == backup:
            raise RuleViolation('ERR_SCHEMA')
        if not (state.authority.controls_hub(owner, primary) and state.authority.controls_hub(owner, backup)):
            raise RuleViolation('ERR_NO_PERMISSION')
        if not any(l.owner == owner and l.hub == primary and l.recipe.id in state.catalog.recipes
                   and state.catalog.recipes[l.recipe.id]['facility'] == '实验室' for l in state.initial.lines.values()):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'primary hub has no laboratory')
        if not state.systems.online(backup, 'research_backup', owner=owner):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'backup hub lacks an online research_backup centre')

        def commit():
            research.backups[primary] = backup

        return PreparedCommand(commit)

    def prepare(self, owner, order, ctx):
        state = self.state
        research = state.research[owner]
        operation = order['op']
        if operation == 'set_backup_hub':
            return self.set_backup_hub(owner, order)
        required = {'line_id': str, 'tech_id': (str, type(None)) if operation == 'set_focus' else str}
        if operation == 'set_focus':
            required['sec_mode'] = str
        fields(order, required, {'expected_version': int})
        lab = self._lab(owner, order['line_id'])
        asset = state.initial.assets[lab.id]
        version(order, asset)
        tech = order['tech_id']
        if tech is not None and tech not in state.catalog.technologies:
            raise RuleViolation('ERR_SCHEMA', 'unknown research node')
        job_id = 'validation:' + fingerprint(lab.id)[:24]
        old = state.validation_jobs.get(job_id)
        if operation == 'set_focus':
            mode = order['sec_mode']
            if mode not in SECURITY_MULTIPLIER:
                raise RuleViolation('ERR_SCHEMA')
            if tech is not None and (tech in research.known or not state.catalog.researchable(tech, research.known)):
                raise RuleViolation('ERR_TECH_LOCKED')
            stopping = old is not None and old.active and old.tech_id != tech

            def commit():
                if stopping:
                    project = research.projects[old.tech_id]
                    if not old.interrupted:
                        project.validate_tick(old.required_ticks, fully_supplied=False, standard_records='R01' in research.known)
                    old.active = False
                research.set_focus(lab.id, tech)
                research.security[lab.id] = mode
                asset.version += 1

            return PreparedCommand(commit)
        project = research.projects.get(tech)
        if project is None or not project.theory_complete or project.verified:
            raise RuleViolation('ERR_TECH_LOCKED', 'theory is not awaiting validation')
        if old is not None and old.active or any(j.active and j.line.owner == owner and j.tech_id == tech
                                                for j in state.validation_jobs.values()):
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'laboratory or project validation slot occupied')
        tier_number = state.catalog.technologies[tech]['tier']
        if tier_number == 6:
            sid = state.installed_systems.get(lab.hub, {}).get('big_science')
            if ('R09' not in research.known or sid is None or sid not in state.initial.assets
                    or state.initial.assets[sid].owner != owner
                    or not any(f.hub == lab.hub and f.owner == owner and f.profile == 'S5'
                               for f in state.initial.facilities.values())):
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        tier = next(t for t in state.catalog.tables['technology']['research_tiers'] if t['tier'] == tier_number)
        rate = units(Q(tier['validation_amount'], tier['validation_cycles']))
        recipe = Recipe(f'validate:{tech}', {tier['validation_good']: rate}, {}, frozenset(), frozenset())
        new_job = ValidationJob(job_id, lab.id, tech, Line(job_id, owner, lab.hub, recipe),
                                tier['validation_cycles'] * ctx.clock.cycle_ticks)
        discard = self._discard(old) if old else None

        def commit():
            if discard:
                discard.commit()
            research.set_focus(lab.id, tech)
            state.validation_jobs[job_id] = new_job
            asset.version += 1

        return PreparedCommand(commit)

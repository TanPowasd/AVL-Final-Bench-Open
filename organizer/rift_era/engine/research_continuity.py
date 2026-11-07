"""04 R01/R08: research continuity when a laboratory is destroyed or captured.

Checked once per cycle end. A laboratory is lost when its line is gone, its
asset health is zero, or its Hub is no longer controlled by the line owner.
Without a valid R08 backup the focused project keeps 30% of its unsettled
theory and loses all validation progress; with an online backup centre at the
configured backup Hub (still controlled by the owner) it keeps 90% of theory
and 75% of validation. The focus is cleared so a loss is applied only once.
"""
from .numeric import Q


def _lab_lost(state, owner, lid):
    line = state.initial.lines.get(lid)
    asset = state.initial.assets.get(lid)
    if line is None or asset is None or asset.health_mU <= 0:
        return True, (line.hub if line is not None else (asset.hub if asset is not None else None))
    return not state.authority.controls_hub(owner, line.hub), line.hub


def settle_research_continuity(state, ctx):
    if not ctx.clock.is_cycle_end(ctx.tick):
        return
    for owner in sorted(state.research):
        research = state.research[owner]
        for lid in sorted(research.focus):
            lost, hub = _lab_lost(state, owner, lid)
            if not lost:
                continue
            tech = research.focus.pop(lid)
            project = research.projects.get(tech)
            backup = research.backups.get(hub) if hub is not None else None
            protected = (backup is not None and state.authority.controls_hub(owner, backup)
                         and state.systems.online(backup, 'research_backup', owner=owner))
            if project is not None and not project.verified:
                if not project.theory_complete:
                    project.theory_mU = project.theory_mU * (90 if protected else 30) // 100
                project.validation_progress_mU = project.validation_progress_mU * 75 // 100 if protected else 0
            for job in state.validation_jobs.values():
                if job.lab_id == lid and job.active:
                    job.active = False
                    job.interrupted = True
            research.security.pop(lid, None)

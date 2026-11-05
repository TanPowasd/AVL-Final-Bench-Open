"""Shared physical transport corridors and actual military basing requirements.

Sea and air movement reuses completed infrastructure, never the non-operational
world atlas. A formation's onboard inventory remains usable between bases.
"""
from .diplomacy import Relation


def mode_for(domain):
    return 'air' if domain == 'Air' else 'sea' if domain.startswith('Sea') else 'road'


def base_online(state, owner, hub, domain):
    if hub is None:return False
    host=owner if state.authority.controls_hub(owner,hub) else (state.contracts.basing_host(owner,hub,domain) if hasattr(state,'contracts') else None)
    if host is None:return False
    if domain == 'Land':
        return True
    terminal = getattr(state.cargo, 'terminal_online', None)
    return terminal is not None and terminal(hub, mode_for(domain), owner=host)


def legal_edge(state, formation, edge):
    mode = getattr(state.cargo.corridors[edge], 'mode', 'road')
    if formation.domain == 'Land':
        return mode in ('road', 'rail')
    return mode == mode_for(formation.domain)

"""Release readiness is separate from component correctness or rule approval."""

from .errors import InfrastructureFault

# A checklist is deliberately explicit; no no-op handler may satisfy a gate.
PENDING_RELEASE_GATES = (
    'full_spatial_world_generator_and_150_tick_bootstrap',
    'all_11_command_families_integrated_with_reservations',
    'eight_nation_city_state_economy_and_all_P7_couplings',
    'multi_modal_congestion_acl_and_provenance_integration',
    'military_occupation_diplomacy_intelligence_orbit_integration',
    'financial_contracts_positions_and_all_default_state_machines',
    'all_15_Round6_dynamic_scenarios_end_to_end',
    'target_host_kernel_sandbox_escape_tests',
    '16MiB_IPC_and_full_scale_CPU_memory_measurements',
    'observation_only_reference_pool_and_64_seed_balance',
)


def require_official_readiness() -> None:
    if PENDING_RELEASE_GATES:
        raise InfrastructureFault('OFFICIAL_ENGINE_NOT_READY: ' + ', '.join(PENDING_RELEASE_GATES))

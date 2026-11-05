"""Generator operating calendar and physical startup requirements."""

from .determinism import uniform
from .numeric import Q, floor
from .power import PLANT_CALENDAR, PlantMode


def calendar(plant, debt, ctx, *, outage_multiplier=Q(1)):
    if ctx.tick % ctx.clock.cycle_ticks:
        return
    outage_bp, period, stop = PLANT_CALENDAR[plant.spec.recipe]
    late = plant.next_maintenance_cycle is not None and ctx.cycle > plant.next_maintenance_cycle + 20
    probability = min(Q(1), Q(outage_bp, 10000) * (1 + debt) * (3 if late else 1) * outage_multiplier)
    if plant.mode == PlantMode.ON and uniform(ctx.seed, 'outage', ctx.cycle, plant.id) < probability:
        plant.mode = PlantMode.TRIPPED
        cycles = 10 if plant.spec.recipe == 'P086' else 3 + floor(Q.from_float(uniform(ctx.seed, 'outage_duration', ctx.cycle, plant.id)) * 6)
        plant.timer = ctx.clock.cycle_ticks * cycles
    if (period and ctx.cycle >= plant.next_maintenance_cycle + plant.maintenance_shift
            and plant.mode not in (PlantMode.TRIPPED, PlantMode.MAINTENANCE, PlantMode.SCRAM)):
        plant.mode = PlantMode.MAINTENANCE
        plant.timer = stop * ctx.clock.cycle_ticks
        plant.next_maintenance_cycle += period


def warming(plant):
    return plant.online and plant.spec.start_ticks > 0 and (
        plant.mode == PlantMode.OFF or plant.mode == PlantMode.STARTING and plant.timer > 0
        or plant.mode in (PlantMode.TRIPPED, PlantMode.MAINTENANCE, PlantMode.SCRAM) and plant.timer <= 1)


def preheat_inputs(plant, coefficients):
    if plant.spec.ignition_mU_tick:
        return {}
    return {g: Q(q, 10 if plant.spec.nuclear and g == 'water' else 100) for g, q in coefficients.items()}


def pump_expected(plant):
    return plant.spec.nuclear and plant.online and (
        plant.mode in (PlantMode.OFF, PlantMode.STARTING, PlantMode.ON)
        or plant.mode in (PlantMode.TRIPPED, PlantMode.MAINTENANCE, PlantMode.SCRAM) and plant.timer <= 1)

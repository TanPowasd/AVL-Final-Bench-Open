"""Physical cargo weights from 02; distinct from scoring shadow prices."""

from .errors import InvariantViolation
from .numeric import Q


FIXED = {'vehicle': Q(5), 'ground_drone': Q(4), 'air_drone': Q(2),
         'armored_unit': Q(15), 'artillery_unit': Q(12), 'locomotive': Q(25)}


def cargo_weights(catalog):
    result = {g: Q(1) for g, row in catalog.goods.items() if not row['indivisible']}
    result.update(FIXED)
    visiting = set()

    def weight(good):
        if good in result:
            return result[good]
        if good in visiting:
            raise InvariantViolation('whole-machine cargo weight dependency cycle')
        visiting.add(good)
        recipe = next((r for _, r in sorted(catalog.recipes.items()) if good in r['outputs']), None)
        if recipe is None:
            raise InvariantViolation(f'no manufacturing recipe for cargo {good}')
        mass = sum((Q(q) * weight(g) for g, q in recipe['inputs'].items()), Q(0))
        result[good] = max(Q(1), mass / Q(recipe['outputs'][good]))
        visiting.remove(good)
        return result[good]

    for good, row in catalog.goods.items():
        if row['kind'] == 'inventory':
            weight(good)
    return result


def warehouse_load(inventory, weights, hubs):
    result = dict.fromkeys(hubs, Q(0))
    for account, quantity in inventory.balances().items():
        if account.location in result and account.bucket != 'outdoor' and not account.bucket.startswith('locked_outdoor_subsistence:'):
            result[account.location] += quantity * weights[account.asset]
    return result

"""One nation-local command stream, explicit domain handlers and P1 arbitration."""

from .commerce_commands import CommerceCommands
from .research_commands import ResearchCommands
from .industry_commands import IndustryCommands
from .public_systems import SystemCommands
from .energy_commands import EnergyCommands
from .governance import GovernanceCommands
from .energy_storage import StorageCommands
from .macroeconomy import MonetaryCommands
from .errors import RuleViolation
from .transport_operations import TransportCommands, CorridorPolicyCommands
from .passenger_operations import RelocationCommands
from .city_state_economy import CityStateCommands
from .corridor_commands import CorridorCommands
from .formations import FormationCommands
from .military_training import ReinforcementCommands
from .relation_commands import DiplomacyCommands
from .military_movement import MarchCommands
from .military_refits import RefitCommands
from .military_sensors import RadarCommands
from .deception import IntelligenceCommands
from .orbital_operations import OrbitalCommands
from .sovereign_finance import BondCommands
from .bond_market import BondOrderCommands
from .contracts import ContractCommands
from .ai_operations import AICommands
from .asat_operations import ASATCommands
from .export_controls import ExportControlCommands
from .ai_deployment import DeploymentCommands
from .ai_licenses import AILicenseCommands
from .compute_refill import ComputeRefillCommands
from .ai_services import AIServiceCommands
from .corporate_finance import DividendCommands
from .stock_market import StockCommands
from .conditional_plans import ConditionalPlanCommands, cancel as cancel_plan, run_plans
from .climate import ClimateCommands, cancel_consent
from .blockade import BlockadeCommands, cancel as cancel_blockade
from .fx_shorts import FXShortCommands


class EngineCommands:
    def __init__(self, state):
        self.state = state
        self.commerce = CommerceCommands(state)
        industry = IndustryCommands(state)
        self.handlers = (self.commerce, CityStateCommands(state), CorridorPolicyCommands(state), RelocationCommands(state), StockCommands(state), DividendCommands(state), AIServiceCommands(state), ComputeRefillCommands(state), AILicenseCommands(state), DeploymentCommands(state), ExportControlCommands(state), ResearchCommands(state), industry, SystemCommands(state, industry),
                         EnergyCommands(state), GovernanceCommands(state), StorageCommands(state, industry),
                         MonetaryCommands(state), TransportCommands(state), CorridorCommands(state, industry), FormationCommands(state), ReinforcementCommands(state), DiplomacyCommands(state), MarchCommands(state), RefitCommands(state), RadarCommands(state), IntelligenceCommands(state), OrbitalCommands(state), BondCommands(state), BondOrderCommands(state), ContractCommands(state), AICommands(state), ASATCommands(state), ConditionalPlanCommands(state), ClimateCommands(state), BlockadeCommands(state), FXShortCommands(state))
        self.dispatch = {operation: handler for handler in self.handlers for operation in handler.OPERATIONS}
        if len(self.dispatch) != sum(len(h.OPERATIONS) for h in self.handlers):
            raise ValueError('duplicate command handler')

    def prepare(self, owner, order, ctx):
        kind, operation = order.get('kind'), order.get('op')
        if not isinstance(kind, str) or not isinstance(operation, str):
            raise RuleViolation('ERR_SCHEMA')
        if ((kind, operation) in {('logistics','market_bid'), ('logistics','market_ask'),
                ('trade','export_offer'), ('trade','buy_from'), ('finance','bond_order')} and
                self.state.active_order_count(owner) >= self.state.catalog.runtime['limits']['active_orders_per_nation']):
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if (kind, operation) == ('cancel', 'cancel_by_id'):
            from .command_schema import fields
            fields(order, {'target_id': str})
            target = order['target_id']
            if target.isascii() and target.isdecimal():
                target = str(int(target))
                training = self.state.ai.cancel(owner, target, ctx)
                if training is not None:
                    return training
                stock_order=self.state.stock_market.cancel(owner,target)
                if stock_order is not None:return stock_order
                license_offer = self.state.ai_licenses.cancel(owner,target)
                if license_offer is not None:
                    return license_offer
                service_order = self.state.ai_services.cancel(owner,target)
                if service_order is not None:
                    return service_order
                deployment = next((d for d in self.state.compute.deployments.values()
                                   if d.owner==owner and d.command_id==target),None)
                if deployment is not None:
                    from .protocol import PreparedCommand
                    return PreparedCommand(lambda:self.state.compute.deployments.pop((owner,deployment.hub)))
                plan = (cancel_plan(self.state, owner, target) or cancel_consent(self.state, owner, target)
                        or cancel_blockade(self.state, owner, target))
                if plan is not None:
                    return plan
                bond_order = self.state.bond_market.cancel(owner, target)
                if bond_order is not None:
                    return bond_order
                issue = self.state.sovereign_finance.cancel(owner, target)
                if issue is not None:
                    return issue
                launch = self.state.orbit.cancel(owner, target)
                if launch is not None:
                    return launch
                refit = self.state.refits.prepare_cancel(owner, target)
                if refit is not None:
                    return refit
                movement = self.state.movement.cancel(owner, target)
                if movement is not None:
                    return movement
                reinforcement = self.state.recruitment.prepare_cancel(owner, target)
                if reinforcement is not None:
                    return reinforcement
                cargo = self.state.cargo.cancel(owner, target)
                if cargo is not None:
                    return cargo
                passenger = self.state.passengers.cancel(owner, target)
                if passenger is not None:
                    return passenger
                intermodal = self.state.intermodal.cancel(owner, target)
                if intermodal is not None:
                    return intermodal
                policy = self.state.macro.policies.get(owner)
                if policy is not None and policy.command_id == target:
                    from .protocol import PreparedCommand
                    return PreparedCommand(lambda: self.state.macro.policies.pop(owner))
                if any(j.owner == owner and j.command_id == target for j in self.state.work.jobs.values()):
                    return self.state.work.prepare_cancel(owner, target)
                job = next((j for j in self.state.governance.training.values()
                            if j.owner == owner and j.command_id == target), None)
                if job is not None:
                    from .protocol import PreparedCommand
                    return PreparedCommand(lambda: self.state.governance.finish_training(job, completed=False))
        handler = self.dispatch.get((kind, operation))
        if handler is None:
            raise RuleViolation('ERR_SCHEMA', 'operation not implemented in this assembly')
        return handler.prepare(owner, order, ctx)

    def commands(self, state, ctx):
        self.commerce.commands(state,ctx,prepare=self.prepare,before_orders=lambda:run_plans(state,ctx,self.prepare),pool_payments=state.stock_market.pool_claims,
            on_pool_cleared=lambda:state.stock_market.after_commands(ctx))

    def clear(self, state, ctx):
        self.commerce.clear(state, ctx)

"""Actual same-generation accelerator refills, with dated fresh cohorts.

Refilling does not heal the host, reset existing cohorts, or manufacture
warehouse capacity. Unspecified installation work is not given hidden costs.
"""
from .command_schema import fields, version
from .errors import RuleViolation
from .ledger import Account
from .numeric import Q, floor
from .protocol import PreparedCommand
from .provenance import Use


class ComputeRefillCommands:
    OPERATIONS=frozenset({('industry','refill_accelerators')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'facility_id':str,'qty':int},{'expected_version':int})
        state=self.state;initial=state.initial;target=order['facility_id'];quantity=order['qty']
        if not 1<=quantity<=50:raise RuleViolation('ERR_SCHEMA')
        asset=initial.assets.get(target)
        if asset is None or asset.owner!=owner:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order,asset)
        if not state.authority.controls_hub(owner,asset.hub):raise RuleViolation('ERR_NO_PERMISSION')
        facility=initial.facilities.get(target)
        lines=set(facility.line_ids if facility else (target,))
        rows=sorted((r for r in state.compute.centers.values() if r.line_id in lines),key=lambda r:r.line_id)
        if not rows:raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        assignments=[];remaining=quantity
        available={}
        for row in rows:
            line_asset=initial.assets[row.line_id]
            if not asset.availability or not line_asset.availability:continue
            recipe=initial.lines[row.line_id].recipe.id
            if not set(state.catalog.recipes[recipe]['requires_all'])<=state.research[owner].known:
                raise RuleViolation('ERR_TECH_LOCKED')
            online=sum((c.online(ctx.cycle) for c in row.center.cohorts),Q(0))
            slots=floor(50-online)
            source=Account(owner,asset.hub,'available',row.good)
            stock=available.setdefault(source,initial.inventory.balance(source))//1000
            take=min(remaining,slots,stock)
            if take:
                assignments.append((row,take))
                available[source]-=take*1000;remaining-=take
            if not remaining:break
        if remaining:
            spare=sum(max(0,floor(50-sum((c.online(ctx.cycle) for c in r.center.cohorts),Q(0)))) for r in rows)
            raise RuleViolation('ERR_CAPACITY_EXCEEDED' if spare<quantity else 'ERR_INSUFFICIENT_STOCK')
        draft=initial.inventory.materials.draft();inputs={}
        for row,take in assignments:
            line_asset=initial.assets[row.line_id]
            amount=take*1000
            if line_asset.book_nmU.get(row.good,Q(0))+amount*1000>line_asset.original_nmU.get(row.good,0):
                raise RuleViolation('ERR_CAPACITY_EXCEEDED')
            source=Account(owner,line_asset.hub,'available',row.good)
            # Freeze every source before any embodied-material credit.
            inputs[source]=inputs.get(source,0)+amount
        pieces={source:draft.take(source,Q(amount)) for source,amount in inputs.items()}
        from .materials import split_origins
        for row,take in assignments:
            source=Account(owner,asset.hub,'available',row.good)
            selected=split_origins(Q(take*1000),pieces[source],capped=True,
                key=(ctx.seed,'compute_refill',ctx.tick,row.line_id))
            for origin,q in selected.items():pieces[source][origin]-=q
            capital=state.asset_lifecycle.account(initial.assets[row.line_id],row.good)
            draft.add(capital,selected,virtual=True)
            draft.capitalize(capital,selected,use=Use.CONSTRUCTION,reason='accelerator_refill')
        stock=initial.inventory.prepare_transform(inputs,{},reason='accelerator_refill',material_plan=draft.finish())
        from .economic_ownership import prepare_purchase,record_income
        payment,recipients=prepare_purchase(state.payments,state.native(owner,'treasury'),stock.materials.events,
            {row.good:state.price(row.good,asset.hub) for row,_ in assignments},kind='accelerator_refill',
            key=(ctx.seed,ctx.tick,'accelerator_refill',target),buyer=state.native(owner,'treasury'))
        def commit():
            payment.commit(stock)
            record_income(state,recipients,payment.amounts)
            for row,take in assignments:
                line_asset=initial.assets[row.line_id]
                line_asset.book_nmU[row.good]=line_asset.book_nmU.get(row.good,Q(0))+take*1000000
                line_asset.version+=1
                state.compute.installed(row.line_id,{row.good:take*1000},ctx.cycle)
        return PreparedCommand(commit)

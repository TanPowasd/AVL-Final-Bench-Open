# Equity finance handoff — 2026-11-05

Development implementation; not release or complete-match acceptance evidence.

## Files

- `engine/equity_finance.py`: margin/borrowed-share custody, whole-share P1 pool claims, lender-return claims, cover, liquidation orders, service claims and valuation hooks.
- `engine/stock_market.py`: minimal role-aware hooks for financed orders. `StockExchange` itself remains the existing whole-share DVP engine.
- `tests/test_equity_finance.py`: focused funding, DVP, partial-fill, custody, rate-lot, collateral and service checks.

## Current behavior

Margin commands reserve actual half notional plus fee from the investor. `pool_claims()` joins the existing FX/IPO pool batch. `after_commands(ctx)` accepts only actual pool transfers, reduces orders to affordable whole shares, and returns unused own cash and unused loans. Real lower-price fills retain half the actual principal as debt, returning excess borrowed reservation. Actual loan cash basis is preserved; entry-FX lots are explicit.

Short borrowing uses only the three NPC accounts' currently available physical shares. The aggregate limit is min(actual available shares, quarter float minus outstanding and reserved loans). Unfilled borrowed shares return to their lenders. Filled shares move through the regular DVP auction, while lenders receive return claims carrying original long-lot basis rather than a second economic company share. Short sale receipts plus actual initial collateral remain in explicit escrow.

Cover orders reserve only their proportional available collateral slice and real additional investor funds. Partial fills return only bought shares and restore lender historical cost/entry FX; cancelled unmatched cover reservations return to collateral. Multiple simultaneous cover reservations cannot reserve the same short shares.

Margin and short maintenance mark checks set `LIQUIDATING`; next-tick forced orders use the shared priority-zero auction band and actual stock/cash custody. No counterparty means no fill and the debt remains. A fully sold margin position with unpaid principal is transferred to `Payables` with explicit WorldPool creditor, currency and historical basis. Normal sell commands can close custodied margin shares when ordinary available stock cannot satisfy the order.

Interest uses issuer rate in basis points, `max(0,i_bp+200)/1_000_000` per cycle for margin, and `2/10000` of marked borrowed stock value for lending fees. Unpaid fees retain explicit creditors; no cash is issued. Dividend compensation is an independent lender claim.

## Integration interfaces (parent connected during final handoff)

- `state.debt_funding.add_provider(state.stock_market.finance.funding_claims)`.
- P7 before unified debt funding: `state.stock_market.finance.accrue(ctx)`; idempotent.
- P7 after unified debt funding: `state.stock_market.finance.settle(ctx)`; pays only actual service deposits.
- On each corporate dividend declaration: `finance.dividend_compensation(cid, declared_mU, ctx)`.
- Scoring: `debt_book(owner)` for funded margin debt and owed fees; `short_liability_book(owner,cycle)` for borrowed-share liabilities at original entry FX. Actual collateral cash remains cash. Avoid counting lender return claims as company shares.
- Parent connected the unified funding, P7 accrual/service and dividend-compensation hooks before the final handoff regression. Formal scoring remains unimplemented.
- StockMarket already owns initialization, P1 pool combination, after-command materialization, expiry, role-aware auction processing, mark checks, observations and snapshot hooks.

## Remaining limitations — do not claim frozen completion

1. Additional forced-liquidation partial-fill/multi-account scenarios remain necessary. The existing focused test checks next-tick priority and custody, not every cascade.
2. Lender historical cost division uses exact proportional fractions; long-run bounded-grid normalization requires a dedicated stress audit before release.
3. Fees remain explicit fee claims rather than automatically merging into principal. This preserves cash conservation and creditors; full default/restructure policy needs alignment with the shared debt book.
4. Mixed ordinary and financed long stock in one sell command is rejected unless one custody source alone can satisfy it; no silent cross-custody allocation is implemented.
5. Financed orders expose developer-generated forced IDs and need final published SDK/version review.
6. No complete game or official scoring acceptance has been run for this feature. Developer source-integrity gates must be regenerated only by the parent after final code stabilization.

## Validation

`python3 -m unittest organizer.rift_era.tests.test_equity_finance` — 11 focused checks passed; included in the final combined rerun.

`python3 -m unittest organizer.rift_era.tests.test_equity_finance organizer.rift_era.tests.test_stock_market organizer.rift_era.tests.test_stock_exchange` — 23 tests passed after the final ordinary-sell patch (23.554 seconds).

`python3 -m py_compile organizer/rift_era/engine/equity_finance.py organizer/rift_era/engine/stock_market.py` passed.

No commit or push performed by this sub-agent.

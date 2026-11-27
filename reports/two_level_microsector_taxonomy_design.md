# Two-level microsector taxonomy — Phase 3 design

## Objective
Separate company identity from rotation peer grouping. The verified primary microsector remains the stock's business identity. A second-level rotation peer group may aggregate only genuinely comparable businesses with shared demand, margin, commodity, regulatory, or capital-cycle drivers.

## Current verified baseline
- Verified stocks: 422
- Distinct verified primary microsector labels: 302
- Primary baskets with 3+ stocks: 26
- Two-stock primary baskets: 24
- Single-stock primary baskets: 252

## Proposed two-level model
1. **Primary business microsector** — retain `MIT_Primary_Microsector` from the verified master, normalized only through the already-approved alias map.
2. **Rotation peer group** — a separately reviewed mapping, not inferred automatically from broad sector labels. It can combine multiple primary microsectors only when their business and market drivers are comparable.

## Rules
- Never merge companies solely because they share a broad sector label (for example, Financials, Industrials, Consumer or Technology).
- Never merge unrelated businesses to reach a minimum basket size.
- Do not use secondary tags as automatic basket membership.
- Keep a single primary rotation peer group per stock only after explicit review; secondary exposures remain descriptive metadata.
- Groups with fewer than 3 stocks are flagged for review, not automatically merged.
- Until a group is approved, the stock remains in its verified primary microsector and its group is marked pending.
- The current legacy broad-group field is preserved as metadata only; it is not a rotation calculation input.

## Rotation readiness
- A primary microsector with 3+ verified stocks is eligible for peer-group review, not automatically approved.
- A two-stock microsector requires review of comparability and sufficient history.
- A singleton should remain visible as a company-level classification; it should not generate a standalone breadth or relative-strength claim. It may join a larger peer group only after evidence-based approval.

## Dashboard migration safeguards
- This phase is taxonomy design and inventory only.
- Do not change `app.py`, legacy baskets, ticker price retrieval, return windows, equal-weight calculations, score weights, breadth formulas, zone thresholds, ranking logic, or Fyers/Yahoo source priority.
- Shadow peer groups must be compared with legacy membership and existing metrics before any live switch.
- Require a separate review artifact and passing CI check before enabling any approved peer group in dashboard calculations.

## Stock-level inventory
See `reports/two_level_taxonomy_stock_inventory.csv`. All 422 verified stocks are included. No rotation peer-group assignments are approved in this phase.

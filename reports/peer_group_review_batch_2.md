# Peer-group review — Batch 2

Source: verified 422-stock classification master, branch `phase1-fyers-main-dashboard-data`. This is a proposal only; no assignments are approved or active.

## Scope
Specialty chemicals, industrial manufacturing, power and energy, automotive, logistics and transport.

## Important review notes
- Chemicals are not one homogeneous peer group. Paints, crop protection, fluorochemicals and diversified specialty chemicals have distinct demand and margin drivers.
- `FLUOROCHEM` and `GFL` appear in the master with the same company name. Resolve security/ticker identity before either is used as a separate constituent.
- Do not combine power generators, renewable equipment OEMs, transmission EPC and transformer manufacturers into one power peer group.
- Shipbuilding, railway rolling stock and telecom network equipment are candidate groups requiring order-book/product-mix checks.
- Logistics operators have different parcel, freight, port and 3PL mixes; candidate consolidation is not automatic.
- Tata Motors remains in the same passenger-vehicle peer group as Hyundai and Maruti as a candidate, while its broader business mix should be considered in peer metrics.
- Off-highway tyre specialist BALKRISIND is not automatically merged with general tyre makers.
- Auto component manufacturers are split by business model where necessary; no blanket consolidation.

## Status
Every row is `PENDING_USER_REVIEW`. No live dashboard code or calculations are changed.

Detailed symbol list and rationale: `reports/peer_group_review_batch_2.csv`.

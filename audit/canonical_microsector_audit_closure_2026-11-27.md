# Canonical Microsector Audit — Closure Record

**Date:** 2026-11-27  
**Repository:** powertrader-frontpage/MARKET-UPDATES  
**Target branch:** phase1-fyers-main-dashboard-data  
**Status:** Taxonomy review decisions consolidated; implementation remains gated on shadow validation.

## 1. Audit scope and baseline

The reconciliation worklist contains 422 stock records and 278 canonical primary microsectors. Recorded baseline categories:

| Reconciliation status | Count |
|---|---:|
| MATCH_ONE_LABEL | 69 |
| LEGACY_LABEL_DIFFERS | 295 |
| LEGACY_MULTI_MEMBERSHIP | 56 |
| MASTER_ONLY | 2 |
| LEGACY_ONLY | 0 |
| **Total** | **422** |

The 295 count is the number of stocks whose legacy label differs from the canonical primary label, not the number of distinct microsectors. The 56 count is the number of stocks with multiple legacy memberships.

## 2. Reviewed and approved decisions

### Master-only stock inclusion
- **AMBUJACEM** — include; canonical primary: **Cement Manufacturers**.
- **HAL** — include; canonical primary: **Aircraft & Aerospace OEMs**.

### Legacy symbol continuity
- **KAVVERITEL → KAVDEFENCE** — preserve as a historical symbol mapping, not as two separate current securities. Canonical primary: **Defence & Wireless RF Equipment, Antennas & Subsystems**. NSE circular 1039/2025 states the new symbol KAVDEFENCE and new company name Kavveri Defence & Wireless Technologies Limited are effective 2025-06-23. Current NSE filings identify KAVDEFENCE and ISIN INE641C01019.

### Approved peer-group / classification guardrails
1. **Transformers:** TRANSFORMERS and VOLTAMP share the peer group “Power & Distribution Transformer Manufacturers”; retain subtype tags (TRANSFORMERS: high-voltage & specialty transformers; VOLTAMP: power & distribution transformers). Peer grouping does not replace primary classifications.
2. **Renewables:** keep renewable power producers, solar manufacturers and wind OEMs distinct. Retain canonical classifications for KPIGREEN, PREMIERENE, WAAREEENER, INOXWIND and SUZLON.
3. **Auto components:** retain exact canonical labels for MINDACORP, ASAHIINDIA, GABRIEL and BHARATFORG. ENDURANCE and SUPRAJIT remain separate under their existing Two-Wheeler Components labels.
4. **Power utilities:** use peer labels only—NTPC generation-led, TATAPOWER vertically integrated, CESC and TORNTPOWER distribution-led integrated utilities. Do not replace primary canonical labels.
5. **IT services:** “Technology Consulting & Digital Engineering” is a peer group for KELLTONTEC and ZENSARTECH; retain TANLA as “CPaaS & Enterprise Messaging Platforms” and BBOX as “Digital Infrastructure & IT Services”.

### Final reviewed batches
The following final 52-stock review batches were approved in the conversation. The approved canonical primary assignments are those displayed in the corresponding review batches; this closure record does not recreate or alter those row-level values:
- TECHM, TECHNOE, TEJASNET, TEXRAIL, THANGAMAYL, THERMAX, THOMASCOOK, THYROCARE, TITAGARH, TITAN
- TNPL, TORNTPHARM, TORNTPOWER, TRANSFORMERS, TRENT, TRIDENT, TRIVENI, TVSMOTOR, TVTODAY, UBL
- UCOBANK, UGROCAP, UJJIVANSFB, ULTRACEMCO, UNIONBANK, UNITDSPR, UNOMINDA, UPL, URBANCO, UTIAMC
- V2RETAIL, VA TECH WABAG, VEDL, VERANDA, VIJAYA, VMM, VOLTAMP, VOLTAS, VSTIND, VTL
- WAAREEENER, WELCORP, WELSPUNLIV, WESTLIFE, WIPRO, YATHARTH, YATRA, YESBANK, ZEEL, ZENSARTECH, ZENTEC, ZYDUSLIFE

## 3. Single-primary-membership rule and secondary exposure metadata

Each stock must have exactly one primary canonical microsector for rotation-basket membership. Secondary business exposures may be retained as tags/metadata only; they must not create duplicate primary basket membership.

Specific approved handling:
- TRIDENT — one primary: Home Textiles, Paper & Yarn; secondary legacy exposure remains metadata only.
- TVTODAY — one primary: News Broadcasting & Digital Media.
- UNOMINDA — one primary: Automotive Components & Systems; EV exposure is secondary metadata.
- VIJAYA — one primary: Diagnostics & Laboratory Services.
- WELSPUNLIV — one primary: Home Textiles & Consumer Textile Products.
- YATHARTH — one primary: Hospitals & Healthcare Services; medical-device exposure is secondary metadata.
- ZEEL — one primary: Television Broadcasting & Digital Entertainment.
- VOLTAMP — primary: Power & Distribution Transformers; transformer peer-group tag is separate.
- ZENSARTECH — primary: IT Services & Consulting; technology consulting peer-group tag is separate.
- ZENTEC — primary: Defence Training Simulators & Anti-Drone Systems; legacy “Gaming” mismatch is not a classification basis.

## 4. Mandatory implementation safeguards

1. Do not create global aliases for legacy labels that map to multiple canonical microsectors.
2. Do not permit a stock to appear in multiple primary microsector rotation baskets.
3. Keep secondary business exposure as metadata/tags, not additional primary basket membership.
4. Do not change scoring formulas, weights, return periods, breadth calculations, zone thresholds, ranking rules or data-source priorities during taxonomy migration.
5. Build canonical baskets alongside legacy baskets in shadow mode.
6. Produce per-tab stock-membership diffs and compare legacy versus canonical rotation outputs.
7. Verify that ranking calculations remain unchanged before enabling the canonical layer.
8. Preserve the exact spelling “VA TECH WABAG”.

## 5. Explicitly not completed by this record

- The source reconciliation CSV still contains PENDING review fields; this document records the consolidated decisions but does not rewrite those row-level fields.
- No production code, canonical master CSV, scoring logic, dashboard tab or live deployment is changed by this closure record.
- Shadow-mode comparison, per-tab diff generation, duplicate-membership checks and ranking-invariance checks remain the next engineering gate.
- Do not enable the canonical layer until those checks pass and the results are reviewed.

## 6. Next engineering stage

Implement a read-only shadow comparison on branch `phase1-fyers-main-dashboard-data`: load canonical and legacy classifications in parallel, generate per-tab membership differences, assert one primary membership per stock, and compare rankings with all existing formulas and inputs held constant. Keep the live dashboard on the legacy path until validation is complete.

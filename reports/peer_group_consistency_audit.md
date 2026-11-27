# Peer-group consistency audit — initial findings

Branch: `phase1-fyers-main-dashboard-data`

Audit basis: `reports/consolidated_peer_group_review_register.csv` (422 rows), checked against `data/stock_classification_master.csv` and official exchange disclosures where symbol identity required verification.

## Coverage and structural counts

- Master securities / review-register rows: **422**
- Distinct proposed rotation peer-group labels: **204**
- Multi-security groups: **79**
- Singleton groups: **125**
- Groups with inconsistent per-security recommendation wording: **12**
- Every row remains `PENDING_USER_REVIEW`.
- This is an audit report only. No register assignments, primary microsectors, app.py, dashboard calculations, weights, data sources or live rotation baskets were changed.

## High-priority security identity and symbol exceptions

### 1. FINOLEXIND and FINPIPE — same issuer, different exchange identifiers

The register contains two rows named Finolex Industries Limited and places both in the same proposed group. Official company disclosures identify FINPIPE as the NSE symbol and FINOLEXIND as the BSE scrip identifier. These are not two independent operating companies and must not be counted as two separate constituents in one rotation basket. Keep both source identifiers traceable, but resolve them to one issuer/security identity for membership and breadth calculations.

Evidence: NSE-hosted company disclosure lists FINPIPE (NSE) and FINOLEXIND / 500940 (BSE): https://nsearchives.nseindia.com/corporate/FINPIPE_08062020155248_LetterCSResignation.pdf

### 2. PBFINTECH and POLICYBZR — likely duplicate issuer identity and one company-name mismatch

The register includes PBFINTECH and POLICYBZR as separate rows. NSE filings identify POLICYBZR as the NSE symbol for **PB Fintech Limited** (ISIN INE417T01026). The POLICYBZR row currently names Policybazaar Insurance Brokers Private Limited, which is not the listed issuer name. Resolve the row identity and any legacy/alternate symbol relationship before membership. Do not count the same listed issuer twice.

Evidence: NSE filing identifying POLICYBZR / PB Fintech Limited: https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_182846_05082026191952_iXBRL_WEB.html

### 3. KAVVERITEL — obsolete NSE symbol

NSE circular NSE/CML/68578 dated 17 June 2025 states KAVVERITEL changed to KAVDEFENCE effective 23 June 2025, with the company name changed to Kavveri Defence & Wireless Technologies Limited. The current master/register symbol should be reconciled to KAVDEFENCE before any live membership; retain KAVVERITEL only as historical alias metadata if needed.

Evidence: https://nsearchives.nseindia.com/content/circulars/CML68578.pdf

### 4. FLUOROCHEM and GFL — issuer/symbol identity needs explicit mapping

The register currently flags GFL as a possible duplicate of FLUOROCHEM. NSE filings identify FLUOROCHEM as Gujarat Fluorochemicals Limited. The exact status and provenance of GFL in the source universe still needs reconciliation; do not delete it or count it as a second issuer until the upstream symbol source and exchange identifier are checked.

Evidence: NSE filing identifying FLUOROCHEM / Gujarat Fluorochemicals Limited: https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_209116_03112026210315_iXBRL_WEB.html

## Assignment conflicts and taxonomy consistency

### 5. TRANSFORMERS and VOLTAMP — unresolved competing group proposals

Both symbols retain the earlier proposal **Power & Distribution Transformers**, while Batch 4 had proposed **Power Equipment & Grid Technology**. The register correctly flags the conflict and retains only one provisional assignment per symbol. A final choice is still required before approval.

### 6. Broad groups that need explicit subgroups or tighter membership rules

- **Specialty Chemicals Platform:** FINEORG is marked `RETAIN DISTINCT PENDING EVIDENCE` while sharing the same group label as the other six candidates. Clarify whether this is a parent research universe with a distinct subgroup, or remove the common basket label for FINEORG.
- **Renewable Energy Equipment & Manufacturing:** wind turbine OEMs and solar cell/module manufacturers are combined despite the rationale explicitly noting different supply-chain economics. Split wind OEMs and solar equipment/manufacturing into separate rotation peer groups unless a documented common factor basket is intended.
- **Automotive Components & Mobility Systems:** eight members cover electronics, driveline, suspension, mirrors/systems and cables. Several rows explicitly say `RETAIN DISTINCT SUBGROUPS`; the shared group label must not imply one undifferentiated basket.
- **Power Utilities & Generation:** the group combines integrated utilities, generators and distribution businesses. Preserve generation-only, integrated utility and distribution exposures as distinguishable subgroups.
- **Fashion & Lifestyle Retail:** value fashion, premium department stores and diversified apparel retail have different customer and format economics; subgroup rules are required.
- **Education & Skills Training:** test preparation, vocational training and managed corporate learning should not automatically be treated as one peer cohort.
- **Packaged Foods & Consumer Brands / Household & Personal Care:** broad consumer labels combine materially different product categories; use only as parent groups unless subgroups are specified.
- **IT Services vs IT Services & Consulting:** two overlapping group labels exist. Define whether these are separate cohorts or a parent/child relationship to avoid fragmented and inconsistent rotation comparisons.
- **Diversified Energy & Infrastructure:** ADANIENT, RELIANCE and SWANENERGY have very different conglomerate mixes; do not use the label as a comparable pure-play basket without a clearly defined shared driver.

## Recommendation wording inconsistencies

Twelve multi-security groups contain differing recommendation labels across their member rows. Some differences are reasonable (e.g., core cohort member versus candidate entrant), but each group needs one explicit group-level decision and inclusion rule. Examples include private/public banks, cement, hospitals, automotive components, specialty chemicals, renewable power producers, IT services/consulting, engineering R&D and telecom tower infrastructure.

Recommendation wording should be normalised to group-level states such as:
- `CORE_CANDIDATE`
- `CANDIDATE_PENDING_COMPARABILITY`
- `SUBGROUP_REQUIRED`
- `RETAIN_DISTINCT`
- `IDENTITY_RESOLUTION_REQUIRED`

Do not bulk-change existing per-stock recommendations until the group-level rules have been agreed.

## Singleton group interpretation

The 125 singleton labels are not automatically errors. Many represent intentionally specialised businesses (for example, LNG terminals, HPC systems, local search, medical devices, specialty hospitals and niche industrial products). Other singleton entries are marked `CANDIDATE WITH ...`, indicating possible peer expansion. Keep specialist singleton groups intact unless a genuinely comparable issuer is verified; do not merge them merely to increase group size.

## Recommended order of resolution

1. Resolve listed-issuer identity and ticker aliases: FINOLEXIND/FINPIPE, PBFINTECH/POLICYBZR, KAVVERITEL/KAVDEFENCE, and GFL/FLUOROCHEM.
2. Decide the TRANSFORMERS/VOLTAMP competing group proposal.
3. Define parent groups versus actual rotation peer baskets for broad mixed groups.
4. Standardise recommendation states and record one group-level inclusion rule.
5. Review candidate singleton expansions without forced membership.
6. Only after explicit user approval, create a separate approved mapping and run shadow validation.

## Safety boundary

This consistency audit does **not** approve any proposed peer group. All 422 rows remain `PENDING_USER_REVIEW`. No primary business microsector has been edited, and no live dashboard or rotation logic has been changed.

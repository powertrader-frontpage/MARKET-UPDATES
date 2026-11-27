# Listed-security identity resolution — findings and register annotations

Branch: `phase1-fyers-main-dashboard-data`

This report resolves identity exceptions in the peer-group review register using NSE filings, exchange quote records and the issuer's own FAQ. The changes are annotations for source traceability and exclusion of duplicate issuer counts; they do not approve peer groups or alter the primary microsector master.

## Confirmed identity resolutions

### FINPIPE / FINOLEXIND — one issuer, two exchange identifiers

NSE filings identify Finolex Industries Limited with NSE symbol `FINPIPE`, BSE scrip code `500940`, and ISIN `INE183A01024`. The register's `FINOLEXIND` is the BSE identifier/scrip label, not a second operating company. Both rows are retained for traceability, but `FINOLEXIND` is annotated as an alias and must not be counted separately from `FINPIPE`.

Sources: NSE filing https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_207738_30102026223333_iXBRL_WEB.html ; NSE quote https://www.nseindia.in/get-quote/equity/FINPIPE/Finolex-Industries-Limited

### POLICYBZR / PBFINTECH — one listed issuer

NSE filings identify `POLICYBZR` as PB Fintech Limited, ISIN `INE417T01026`. PB Fintech's own FAQ confirms NSE symbol POLICYBZR and BSE code 543390. Policybazaar Insurance Brokers Private Limited is a subsidiary, not the listed issuer name. The `POLICYBZR` company name annotation is corrected in the review register; `PBFINTECH` is annotated as a duplicate/internal alias and must not be counted separately.

Sources: NSE filing https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_182842_05082026191257_iXBRL_WEB.html ; PB Fintech FAQ https://www.pbfintech.in/faq/

### KAVVERITEL → KAVDEFENCE — obsolete symbol

NSE circular NSE/CML/68578 dated 17 June 2025 states that KAVVERITEL changed to KAVDEFENCE effective 23 June 2025, with the company name changed to Kavveri Defence & Wireless Technologies Limited. The register retains the legacy row for traceability and marks it ineligible for live basket use under the obsolete symbol. The primary stock master and all dependent symbol references need a controlled migration before this security can be activated.

Sources: NSE circular https://nsearchives.nseindia.com/content/circulars/CML68578.pdf ; NSE filing https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_187483_14082026180639_iXBRL_WEB.html

## Unresolved identity — GFL / FLUOROCHEM

NSE filings confirm `FLUOROCHEM` as Gujarat Fluorochemicals Limited, ISIN `INE09N301011`. The company also uses “GFL” as an abbreviation/brand in its communications. The evidence reviewed does not establish `GFL` as a separate listed security symbol. The register now explicitly says not to assume a duplicate or separate issuer without a distinct exchange symbol and ISIN. Keep this exception open pending verification of the original source from which the GFL row entered the stock universe.

Sources: NSE filing https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_209116_03112026210315_iXBRL_WEB.html ; company filing https://nsearchives.nseindia.com/corporate/FLUOROCHEM_03112026141707_GFCL__Outcome.pdf

## What was changed

- Updated only the identity-related recommendation and consolidation-note fields for FINOLEXIND, FINPIPE, PBFINTECH, POLICYBZR, KAVVERITEL, GFL and FLUOROCHEM in the consolidated review register.
- No rows were deleted or merged.
- No primary microsector master values were changed.
- All rows remain `PENDING_USER_REVIEW`.
- No peer group was approved or activated.
- No `app.py`, live rotation logic, scores, weights, rankings or data sources were changed.

## Required controlled follow-up

1. Reconcile the source universe and master so duplicate exchange identifiers map to one issuer identity, while preserving exchange-specific symbols where needed.
2. Replace the obsolete KAVVERITEL symbol with KAVDEFENCE across all dependent files in one controlled migration.
3. Trace the original GFL row to its source; exclude it from security counts until a distinct listed instrument is proven.
4. Re-run unique-issuer counts and shadow basket membership checks before any peer-group activation.

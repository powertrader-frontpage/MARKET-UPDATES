# MIT Peer-Group Consolidation — Shadow Design Report

**Date:** 2026-11-28  
**Branch:** `phase1-fyers-main-dashboard-data`  
**Status:** Design inventory only — no stock assignments or dashboard logic changed.

## 1. Purpose and hard boundary

Create a second-level **Comparable Rotation Peer Group** above the existing **MIT Primary Microsector**. The primary microsector remains the stock's detailed business identity and remains the sole source for detailed microsector membership. Peer groups are an additional aggregation layer for comparing genuinely similar businesses.

This report does not approve any new peer-group assignments. It records existing approved guardrails, inventories the current master, identifies candidate consolidation families for controlled review, and highlights a count discrepancy that must be resolved before implementation.

## 2. Current master inventory (read from the branch master)

- Stock rows: **422**
- Distinct `MIT_Primary_Microsector` labels in the current CSV: **302**
- Microsectors with one stock: **252**
- Microsectors with two or more stocks: **50**
- Stocks with blank primary microsector: **0**
- Distinct values in `Legacy_Broad_Groups`: **36** (including blank and multi-tag combinations)

### Current microsector size distribution

| Stocks per primary microsector | Number of microsectors |
|---:|---:|
| 1 | 252 |
| 2 | 24 |
| 3 | 10 |
| 4 | 7 |
| 5 | 4 |
| 6 | 2 |
| 10 | 2 |
| 12 | 1 |
| **Total** | **302** |

**Important reconciliation discrepancy:** the 2026-11-27 audit closure records **278 canonical primary microsectors**, while the current `data/stock_classification_master.csv` contains **302 distinct primary microsector strings** across 422 rows. This is a difference of 24 labels. Do not normalize, merge, or overwrite these labels silently. First reconcile the exact master version / canonical taxonomy snapshot used by the 278-count audit with the current branch master. The 278 figure is not interchangeable with the 302 count.

## 3. Existing approved peer-group guardrails

These are the only peer-group decisions explicitly recorded as approved in the closure record. They are peer relationships only; they do not replace primary microsector values.

| Peer group | Stocks / treatment | Guardrail |
|---|---|---|
| Power & Distribution Transformer Manufacturers | TRANSFORMERS; VOLTAMP | Preserve their distinct subtype tags and primary microsectors. |
| Renewable energy ecosystem | KPIGREEN; PREMIERENE; WAAREEENER; INOXWIND; SUZLON | Keep renewable power producers, solar manufacturers and wind OEMs distinct; no single broad primary label. |
| Auto components peer comparison | MINDACORP; ASAHIINDIA; GABRIEL; BHARATFORG; ENDURANCE; SUPRAJIT | Preserve exact canonical labels; ENDURANCE and SUPRAJIT remain separate under their existing Two-Wheeler Components labels. |
| Power utilities peer comparison | NTPC; TATAPOWER; CESC; TORNTPOWER | Use peer descriptors only: generation-led, vertically integrated, distribution-led integrated utilities. Do not replace primary labels. |
| Technology Consulting & Digital Engineering | KELLTONTEC; ZENSARTECH | Peer group only. TANLA remains CPaaS & Enterprise Messaging Platforms; BBOX remains Digital Infrastructure & IT Services. |

## 4. Candidate families for the next controlled peer review

These are **candidate review families**, not approved merges. Membership must be derived from the exact master rows and checked for business comparability, not just similar wording.

| Candidate family | Why a common rotation comparison may help | Keep separate where relevant |
|---|---|---|
| Banking | Compare private banks, public banks and small finance banks on common financial-sector rotation views. | Preserve bank subtype in primary microsector and peer subtype. |
| Lending & housing finance | Compare lenders using common financial conditions and credit-cycle signals. | Housing finance, vehicle finance, gold loans, microfinance, MSME lending and diversified lenders remain distinguishable subtypes. |
| Asset / wealth management & broking | Common capital-markets sensitivity and financial-market activity. | Asset managers, wealth managers, brokers, exchanges, depositories and RTAs are not interchangeable. |
| Pharmaceuticals & life sciences | Compare pharma-sector leadership. | Formulations, APIs, CDMO/CRDMO, intermediates, biosimilars and specialty pharma remain separate subtypes. |
| Healthcare delivery | Hospitals and healthcare service providers share demand and operating-cycle drivers. | Hospitals, diagnostics, specialty hospitals, healthcare technology and medical devices remain distinct. |
| IT services & engineering | Compare IT services, digital transformation and engineering services. | Product engineering, telecom software, CPaaS, cloud infrastructure and digital platforms require separate subtypes. |
| Automotive OEMs & components | Common auto-cycle comparison. | Passenger vehicles, commercial vehicles, two/three-wheelers, tyres, batteries, auto glass and component sub-industries remain separate. |
| Power generation & utilities | Common power demand, fuel and regulation sensitivities. | Generation, transmission, distribution, renewables, equipment, EPC and financing remain separate peer subtypes. |
| Renewable energy value chain | Compare the renewable investment cycle. | Renewable generators, solar cells/modules/glass, wind OEMs and storage/battery businesses remain distinct. |
| Chemicals & materials | Compare industrial input cycles. | Specialty chemicals, agrochemicals, fluorochemicals, fertilisers, metals, steel, cement, glass, paper and packaging are not one peer group. |
| Real estate & infrastructure | Compare investment and project cycles. | Developers, REIT-like/mall operators, roads, ports, railways, EPC, water and transport infrastructure remain separate. |
| Consumer / retail | Compare discretionary demand and consumption. | Apparel, footwear, jewellery, food, beverages, QSR, supermarkets and e-commerce retain separate subtypes. |
| Logistics & transport | Compare freight and trade-cycle conditions. | Ports, shipping, airlines, rail rolling stock, courier, integrated logistics and travel platforms remain distinct. |
| Telecom & digital infrastructure | Shared connectivity and capex drivers. | Telecom operators, towers, fibre, network equipment and digital services remain separate. |
| Media & entertainment | Shared advertising / subscription / content cycle. | Broadcasting, print, cinema, music, gaming, news and VFX remain separate. |
| Agriculture & agri-inputs | Shared agricultural demand and monsoon exposure. | Seeds, crop protection, fertilisers, sugar, food processing and animal nutrition remain separate. |
| Defence & aerospace | Shared defence procurement cycle. | Platforms, shipbuilding, missiles, electronics, simulators, aerospace OEMs and RF equipment remain distinct. |
| Industrial equipment & automation | Shared industrial capex sensitivity. | Transformers, cables, automation, HVAC, pumps, process equipment, electrical equipment and EPC remain distinct. |

## 5. Proposed data model (not yet implemented)

Add a separate field such as `MIT_Comparable_Peer_Group` to the master only after peer decisions are reviewed and approved.

Rules:
1. Every stock has exactly one `MIT_Primary_Microsector`.
2. Every stock may have zero or one comparable peer group; use an explicit `UNASSIGNED` state where no defensible peer group exists.
3. Peer group does not create additional primary microsector membership.
4. Keep subtype and secondary exposure tags as metadata.
5. A peer group must be based on common business economics / operating drivers, not a broad legacy group label alone.
6. Do not force singleton microsectors into groups merely to reduce the singleton count.
7. Preserve approved peer decisions exactly; no automatic label normalization.
8. Keep existing scoring formulas, weights, return periods, breadth calculations, zone thresholds, ranking rules and data-source priorities unchanged.
9. Peer-group rotation is an additional view. Existing detailed microsector rotation remains available and unchanged.
10. Record decision source, rationale, reviewer, date and status for each peer assignment.

## 6. Validation gates before any dashboard integration

- Resolve the 278-versus-302 taxonomy snapshot discrepancy.
- Produce a stock-level proposed peer assignment register with current primary microsector, proposed peer group, rationale and status.
- Verify 422 unique current symbols and one primary microsector per stock.
- Verify no peer assignment alters primary microsector membership.
- Compare per-tab membership and rotation outputs in shadow mode.
- Compare rankings on a frozen price/history snapshot; formulas and inputs must be identical.
- Confirm all dashboard consumers use the same master-derived classification.
- Keep production on the legacy path until all checks pass and the user approves the report.

## 7. Source record

This report uses the current branch master and the existing audit closure record dated 2026-11-27. The closure record contains the five approved peer-group guardrails listed above and explicitly states that shadow validation and ranking-invariance checks remain incomplete. No new peer assignments are approved by this report.

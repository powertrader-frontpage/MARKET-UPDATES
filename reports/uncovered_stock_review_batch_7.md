# Uncovered stock review — Batch 7

Branch: `phase1-fyers-main-dashboard-data`

This batch reviews the next 25 uncovered securities alphabetically. Primary microsector labels are reproduced from the verified master and are not changed. Proposed rotation peer groups are recommendations only; every row remains `PENDING_USER_REVIEW`.

| Symbol | Company | Verified primary microsector | Proposed rotation peer group | Recommendation |
|---|---|---|---|---|
| JKPAPER | JK Paper Limited | Paper & Packaging Manufacturers | Paper & Packaging Manufacturers | RETAIN AS DISTINCT PEER GROUP |
| JSWSTEEL | JSW Steel Limited | Integrated Steel Producers | Integrated Steel Producers | CANDIDATE PEER GROUP |
| JUSTDIAL | Just Dial Limited | Local Search & Digital Discovery Platforms | Local Search & Digital Discovery Platforms | RETAIN DISTINCT |
| KAVVERITEL | Kavveri Defence & Wireless Technologies Limited (formerly Kavveri Telecom Products Limited) | Defence & Wireless RF Equipment, Antennas & Subsystems | Defence Electronics & RF Systems | RETAIN DISTINCT / SYMBOL VALIDATION REQUIRED |
| KFINTECH | KFin Technologies Limited | Financial Market Infrastructure & Investor Services | Financial Market Infrastructure & Investor Services | CANDIDATE PEER GROUP |
| KPITTECH | KPIT Technologies Limited | Automotive Software & Engineering | Automotive Software & Engineering | RETAIN DISTINCT PEER GROUP |
| LAURUSLABS | Laurus Labs Limited | API & CDMO | API & CDMO | CANDIDATE PEER GROUP |
| LT | Larsen & Toubro Limited | Engineering, Procurement & Construction (EPC) | Diversified Engineering & EPC | RETAIN DISTINCT |
| LTF | L&T Finance Limited | Diversified Retail & Rural Lending | Diversified Retail & Rural Lending | CANDIDATE PEER GROUP |
| LTM | LTM Limited | IT Services, Consulting & Digital Transformation | IT Services & Consulting | CANDIDATE PEER GROUP |
| LTTS | L&T Technology Services Limited | Engineering R&D & Digital Engineering | Engineering R&D & Digital Engineering | RETAIN AS DISTINCT PEER GROUP |
| M&M | Mahindra & Mahindra Limited | Diversified Automotive & Farm Equipment | Diversified Automotive & Farm Equipment | RETAIN DISTINCT |
| M&MFIN | Mahindra & Mahindra Financial Services Limited | Vehicle & Rural Finance | Vehicle & Rural Finance | CANDIDATE PEER GROUP |
| MCX | Multi Commodity Exchange of India Limited | Commodity Derivatives Exchange & Market Infrastructure | Exchanges & Market Infrastructure | CANDIDATE PEER GROUP |
| MPHASIS | Mphasis Limited | IT Services, Cloud & Application Modernization | IT Services & Consulting | CANDIDATE PEER GROUP |
| MRPL | Mangalore Refinery and Petrochemicals Limited | Oil Refining & Petrochemicals | Oil Refining & Marketing | CANDIDATE PEER GROUP |
| NBCC | NBCC (India) Limited | Project Management, Construction & EPC | Construction & EPC Services | CANDIDATE PEER GROUP |
| NETWEB | Netweb Technologies India Limited | High-Performance Computing & Data-Centre Systems | High-Performance Computing & Data-Centre Systems | RETAIN DISTINCT |
| NEULANDLAB | Neuland Laboratories Limited | API & CDMO | API & CDMO | CANDIDATE PEER GROUP |
| NTPCGREEN | NTPC Green Energy Limited | Renewable Power Generation | Renewable Power Producers | CANDIDATE PEER GROUP |
| NYKAA | FSN E-Commerce Ventures Limited | Beauty, Fashion & Lifestyle E-Commerce | Beauty, Fashion & Lifestyle E-Commerce | RETAIN DISTINCT |
| OFSS | Oracle Financial Services Software Limited | Banking & Financial Services Software | Banking & Financial Services Software | RETAIN DISTINCT |
| OIL | Oil India Limited | Oil & Gas Exploration, Production and Transportation | Oil & Gas Exploration & Production | CANDIDATE PEER GROUP |
| ONGC | Oil and Natural Gas Corporation Limited | Oil & Gas Exploration and Production | Oil & Gas Exploration & Production | CANDIDATE PEER GROUP |
| PAGEIND | Page Industries Limited | Innerwear, Athleisure & Apparel | Innerwear & Branded Apparel | RETAIN DISTINCT PEER GROUP |

## Rationale

- **JKPAPER — Paper & Packaging Manufacturers:** Paper, paperboard and packaging grades; compare product mix with other integrated paper and packaging producers.
- **JSWSTEEL — Integrated Steel Producers:** Integrated steelmaking and downstream flat/long steel exposure; compare with other integrated steel producers.
- **JUSTDIAL — Local Search & Digital Discovery Platforms:** Local listings, search and lead-generation marketplace; not a generic e-commerce or IT-services peer.
- **KAVVERITEL — Defence Electronics & RF Systems:** Master notes this legacy ticker is no longer current and points to successor KAVDEFENCE; preserve record for reconciliation, but do not activate a rotation basket until security-symbol mapping is resolved.
- **KFINTECH — Financial Market Infrastructure & Investor Services:** Registrar/transfer agency and issuer/investor servicing economics; keep separate from exchanges and depositories unless broader infrastructure cohort is explicitly intended.
- **KPITTECH — Automotive Software & Engineering:** Automotive embedded software, SDV and mobility engineering are specialised versus generic IT services.
- **LAURUSLABS — API & CDMO:** API and contract development/manufacturing exposure; retain distinction from formulation-led pharma and biologics pure plays.
- **LT — Diversified Engineering & EPC:** Large diversified engineering/EPC conglomerate across infrastructure, energy and manufacturing; do not classify as a narrow industrial equipment peer.
- **LTF — Diversified Retail & Rural Lending:** Retail, rural, vehicle and housing lending exposure; compare diversified NBFC portfolios and asset mix.
- **LTM — IT Services & Consulting:** Broad IT services and consulting model; suitable for comparison with large IT services providers, distinct from niche engineering R&D.
- **LTTS — Engineering R&D & Digital Engineering:** Engineering R&D, embedded systems and product engineering; do not merge with generic IT services.
- **M&M — Diversified Automotive & Farm Equipment:** Both automotive and farm equipment are core businesses; avoid forcing into passenger vehicle OEM or tractor-only peer basket.
- **M&MFIN — Vehicle & Rural Finance:** Vehicle, tractor and rural lending franchise; compare with vehicle finance lenders while monitoring rural credit mix.
- **MCX — Exchanges & Market Infrastructure:** Commodity derivatives exchange economics; compare with exchange operators but retain asset-class and revenue-mix distinctions.
- **MPHASIS — IT Services & Consulting:** IT services, cloud and application modernisation; broad services peer cohort, with client/vertical mix differences.
- **MRPL — Oil Refining & Marketing:** Standalone refinery and petrochemical exposure; compare refining margins and complexity, not upstream E&P.
- **NBCC — Construction & EPC Services:** Project management consultancy and construction/EPC contracts; government project mix differentiates it from private diversified EPCs.
- **NETWEB — High-Performance Computing & Data-Centre Systems:** HPC servers, data-centre systems and integrated computing platforms; not a generic electronics manufacturing or IT services peer.
- **NEULANDLAB — API & CDMO:** Complex APIs and peptide CDMO; close operating comparability with API/CDMO manufacturers, while product and customer concentration should be monitored.
- **NTPCGREEN — Renewable Power Producers:** Renewable generation asset ownership and capacity build-out; compare with renewable IPPs, not solar module/equipment makers.
- **NYKAA — Beauty, Fashion & Lifestyle E-Commerce:** Digital-first beauty, fashion and lifestyle platform with brand and fulfilment economics distinct from general marketplaces.
- **OFSS — Banking & Financial Services Software:** Specialised banking/financial software product business; do not merge with IT outsourcing/service providers.
- **OIL — Oil & Gas Exploration & Production:** Integrated upstream oil and gas producer with pipeline/transport exposure; compare with upstream producers, noting portfolio differences.
- **ONGC — Oil & Gas Exploration & Production:** Large upstream oil and gas producer; compare with exploration/production companies rather than refiners or gas distributors.
- **PAGEIND — Innerwear & Branded Apparel:** Innerwear and athleisure brand/licensing model has distinct product, sourcing and distribution economics from broad apparel retail.

No live dashboard logic, app.py, scoring, ranking, weights, data sources or primary classifications were modified.
# Uncovered stock review — Batch 8 (final)

Branch: `phase1-fyers-main-dashboard-data`

This final batch reviews the remaining 27 uncovered securities. Primary microsector labels and secondary tags are copied from the verified master and are not modified. Proposed rotation peer groups are review recommendations only; all rows remain `PENDING_USER_REVIEW`.

| Symbol | Company | Verified primary microsector | Proposed rotation peer group | Recommendation |
|---|---|---|---|---|
| PETRONET | Petronet LNG Limited | LNG Import, Regasification & Gas Infrastructure | LNG Import, Regasification & Gas Infrastructure | RETAIN DISTINCT PEER GROUP |
| PFOCUS | Prime Focus Limited | Media Production, VFX & Post-Production Services | Media Production, VFX & Post-Production Services | RETAIN DISTINCT |
| PNB | Punjab National Bank | Public Sector Banking | Public Sector Banking | CANDIDATE PEER GROUP |
| RAMCOCEM | The Ramco Cements Limited | Cement & Building Materials | Cement & Building Materials | CANDIDATE PEER GROUP |
| RBLBANK | RBL Bank Limited | Private Sector Banking | Private Sector Banking | CANDIDATE PEER GROUP |
| RITES | RITES Limited | Transport Infrastructure Consultancy & Engineering | Transport Infrastructure Consultancy & Engineering | RETAIN DISTINCT PEER GROUP |
| RVNL | Rail Vikas Nigam Limited | Railway Infrastructure EPC & Project Execution | Railway Infrastructure EPC & Project Execution | CANDIDATE PEER GROUP |
| SAGILITY | Sagility Limited | Healthcare Technology & Business Process Services | Healthcare Technology & Business Process Services | RETAIN DISTINCT |
| SANSERA | Sansera Engineering Limited | Precision Automotive & Aerospace Components | Precision Automotive & Aerospace Components | CANDIDATE PEER GROUP |
| SEAMECLTD | SEAMEC Limited | Offshore Oilfield Support & Marine Services | Offshore Oilfield Support & Marine Services | CANDIDATE PEER GROUP |
| SHREECEM | Shree Cement Limited | Cement & Building Materials | Cement & Building Materials | CANDIDATE PEER GROUP |
| SHRIRAMFIN | Shriram Finance Limited | Commercial Vehicle & Retail Lending | Commercial Vehicle & Retail Lending | CANDIDATE PEER GROUP |
| SIS | SIS Limited | Security, Facility Management & Cash Logistics | Security, Facility Management & Cash Logistics | CANDIDATE PEER GROUP |
| SPANDANA | Spandana Sphoorty Financial Limited | Microfinance & Inclusive Lending | Microfinance & Inclusive Lending | CANDIDATE PEER GROUP |
| SUNDARMFIN | Sundaram Finance Limited | Vehicle Finance & Retail Lending | Vehicle Finance & Retail Lending | CANDIDATE PEER GROUP |
| SUPREMEIND | Supreme Industries Limited | Plastic Pipes, Products & Packaging | Plastic Pipes, Products & Packaging | CANDIDATE PEER GROUP |
| TANLA | Tanla Platforms Limited | CPaaS & Enterprise Messaging Platforms | CPaaS & Enterprise Messaging Platforms | RETAIN DISTINCT PEER GROUP |
| TATAELXSI | Tata Elxsi Limited | Product Engineering, Design & Technology Services | Product Engineering, Design & Technology Services | CANDIDATE PEER GROUP |
| TBOTEK | TBO Tek Limited | Travel Distribution & Technology Platforms | Travel Distribution & Technology Platforms | RETAIN DISTINCT PEER GROUP |
| TECHNOE | Techno Electric & Engineering Company Limited | Power EPC, Transmission & Smart Infrastructure | Power EPC, Transmission & Smart Infrastructure | CANDIDATE PEER GROUP |
| THERMAX | Thermax Limited | Industrial Energy, Environment & Process Equipment | Industrial Energy, Environment & Process Equipment | RETAIN DISTINCT PEER GROUP |
| THOMASCOOK | Thomas Cook (India) Limited | Travel Services & Foreign Exchange | Travel Services & Foreign Exchange | CANDIDATE PEER GROUP |
| TITAN | Titan Company Limited | Jewellery, Watches & Lifestyle Retail | Jewellery, Watches & Lifestyle Retail | RETAIN DISTINCT PEER GROUP |
| UGROCAP | UGRO Capital Limited | MSME & Small Business Lending | MSME & Small Business Lending | CANDIDATE PEER GROUP |
| URBANCO | Urban Company Limited | Home & Beauty Services Marketplace | Home & Beauty Services Marketplace | RETAIN DISTINCT |
| VSTIND | VST Industries Limited | Tobacco & Cigarette Manufacturers | Tobacco & Cigarette Manufacturers | CANDIDATE PEER GROUP |
| WELCORP | Welspun Corp Limited | Steel Pipes & Water Infrastructure Products | Steel Pipes & Water Infrastructure Products | CANDIDATE PEER GROUP |

## Rationale

- **PETRONET — LNG Import, Regasification & Gas Infrastructure:** LNG import terminals, regasification and long-term gas infrastructure contracts; distinct from upstream producers and city gas distributors.
- **PFOCUS — Media Production, VFX & Post-Production Services:** Film/TV production services, visual effects and post-production; project-based creative services differ from broadcasters and content owners.
- **PNB — Public Sector Banking:** Public-sector universal bank; compare with PSU banks using asset quality, deposit franchise and capital metrics.
- **RAMCOCEM — Cement & Building Materials:** Cement manufacturing and construction demand/capacity drivers; monitor regional footprint and product mix.
- **RBLBANK — Private Sector Banking:** Private-sector universal bank; compare with private banks while accounting for its distinct size, portfolio and turnaround profile.
- **RITES — Transport Infrastructure Consultancy & Engineering:** Transport consultancy, engineering and export services; distinct from railway EPC contractors and rolling-stock manufacturers.
- **RVNL — Railway Infrastructure EPC & Project Execution:** Railway project execution and EPC exposure; compare with railway infrastructure contractors, not rolling-stock OEMs.
- **SAGILITY — Healthcare Technology & Business Process Services:** Healthcare-focused BPM and technology-enabled services; vertical specialisation differentiates it from broad IT services.
- **SANSERA — Precision Automotive & Aerospace Components:** Precision-engineered components for automotive and aerospace customers; compare with component suppliers with similar machining/content mix.
- **SEAMECLTD — Offshore Oilfield Support & Marine Services:** Offshore support vessels and marine services tied to offshore energy activity; keep separate from shipping liners and port operators.
- **SHREECEM — Cement & Building Materials:** Cement manufacturing, capacity utilisation and regional pricing are comparable with cement producers.
- **SHRIRAMFIN — Commercial Vehicle & Retail Lending:** Commercial vehicle and diversified retail lending; compare NBFCs by vehicle, secured/unsecured and rural portfolio mix.
- **SIS — Security, Facility Management & Cash Logistics:** Contract security, facility management and cash logistics; service-contract labour intensity is central to comparability.
- **SPANDANA — Microfinance & Inclusive Lending:** Microfinance-focused lending; compare with other MFIs while tracking borrower leverage, collection efficiency and geographic concentration.
- **SUNDARMFIN — Vehicle Finance & Retail Lending:** Vehicle finance-led NBFC; compare with vehicle financiers while distinguishing prime customer and product mix.
- **SUPREMEIND — Plastic Pipes, Products & Packaging:** Plastic pipes, fittings and broader polymer products; compare with pipe manufacturers but retain diversified product exposure.
- **TANLA — CPaaS & Enterprise Messaging Platforms:** Cloud communications and enterprise messaging platform economics; do not merge with general IT services.
- **TATAELXSI — Product Engineering, Design & Technology Services:** Product engineering, design and embedded technology; compare with engineering R&D firms, while noting design/auto/media mix.
- **TBOTEK — Travel Distribution & Technology Platforms:** B2B travel distribution platform and supplier network; distinct from consumer online travel agencies and travel agents.
- **TECHNOE — Power EPC, Transmission & Smart Infrastructure:** Power transmission EPC and smart-grid infrastructure; compare with electrical EPC contractors, not power generators.
- **THERMAX — Industrial Energy, Environment & Process Equipment:** Industrial boilers, energy systems, water and environmental solutions; diversified process equipment mix warrants its own cohort.
- **THOMASCOOK — Travel Services & Foreign Exchange:** Travel services, corporate travel and foreign exchange; broader service mix differs from pure online travel platforms.
- **TITAN — Jewellery, Watches & Lifestyle Retail:** Jewellery and watches with brand-led retail economics; diversified lifestyle mix and gold inventory dynamics distinguish it from apparel retail.
- **UGROCAP — MSME & Small Business Lending:** MSME-focused secured and unsecured lending; compare with SME lenders while monitoring co-lending and portfolio mix.
- **URBANCO — Home & Beauty Services Marketplace:** Two-sided services marketplace for home and beauty categories; not a conventional consumer retailer or general e-commerce platform.
- **VSTIND — Tobacco & Cigarette Manufacturers:** Cigarette and tobacco products; compare with listed tobacco manufacturers while accounting for category and brand concentration.
- **WELCORP — Steel Pipes & Water Infrastructure Products:** Large-diameter steel pipes and water infrastructure products; compare with steel pipe producers, not integrated steelmakers.

No live dashboard logic, app.py, scoring, ranking, weights, data sources or primary classifications were modified.
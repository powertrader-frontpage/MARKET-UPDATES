# Peer-group review — Batch 4

Source: verified classification master `data/stock_classification_master.csv` (422 verified rows) on `phase1-fyers-main-dashboard-data`. This batch is a proposal and inventory only; no live taxonomy or dashboard calculation is changed.

## Coverage
Defence/aerospace, agriculture/fertilizers, healthcare and medical products, water/waste services, building materials, textiles/packaging, metals/mining and utilities/renewable energy.

## High-priority identity and classification checks
1. `FINOLEXIND` and `FINPIPE` both show Finolex Industries Limited in the master; verify security/ticker identity before treating them as separate stocks.
2. `FLUOROCHEM` and `GFL` were already flagged in Batch 2 as same company-name identity; resolve before any peer basket count.
3. Keep defence primes/platform manufacturers separate from electronics/subsystem vendors unless comparable revenue drivers are demonstrated.
4. Keep hospitals, diagnostics, medical devices, biosimilars, API/CDMO and formulations as distinct healthcare peer groups.
5. Do not combine all building materials: pipes, plywood, tiles, glass, paper/packaging and solar glass have different drivers.
6. Do not combine all metals/mining: coal, iron ore, copper, aluminium, steel, graphite and specialty alloys have different commodity exposure.
7. Power generation, regulated transmission, grid equipment, renewable project owners and renewable equipment OEMs must remain distinct subgroups.
8. Sugar/ethanol groupings are candidates only; integrated agri/food businesses need mix review.

## Status
Every proposed assignment remains `PENDING_USER_REVIEW`. The CSV includes all symbols and rationale for row-level review.

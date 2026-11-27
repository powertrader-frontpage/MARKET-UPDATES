# MIT Stock Classification Master — Audit

- **Audit file:** `data/stock_classification_master.csv`
- **Total stock rows:** 422
- **Verified:** 422
- **Pending / other status:** 0
- **Unique verified primary microsectors:** 302

## Validation checks

- [x] Unique stock symbols (no duplicates)
- [x] All verified stocks have a primary microsector
- [x] All verified stocks have evidence source, URL, type and verification date
- [x] HYUNDAI and TATAMOTORS share the same primary microsector

## Consistency spot-check

- HYUNDAI: Passenger Vehicle OEMs
- TATAMOTORS: Passenger Vehicle OEMs

## Interpretation

The master is structurally complete based on the checks above. It currently contains 302 distinct primary microsector labels across 422 stocks. This is a classification-granularity observation, not an error by itself; a separate taxonomy-normalisation review may be appropriate before the dashboard-wide migration.

This report is a snapshot. Regenerate it after any change to the master by running:

```bash
python scripts/audit_classification_master.py
```

## Scope

This phase adds a reusable CSV loader and a validation report. It does not migrate dashboard calculations or replace the existing `MICRO_SECTORS` dictionary yet.

# Consolidated peer-group review register

Branch: `phase1-fyers-main-dashboard-data`

Source files: the four peer-group review batch CSVs and verified stock master `data/stock_classification_master.csv` (master SHA `7a34eb13c643bc872f21390782ece677d43ccd97`).

## Consolidation audit

- Verified master securities: **422**
- Unique securities reviewed across Batches 1–8: **422**
- Master securities covered by the review register: **422**
- Master securities not yet reviewed: **0**
- Duplicate symbol mentions consolidated to one row: **10**
- Conflicting proposed peer-group assignments: **2**
- Batch symbols absent from verified master: **0**

Batch symbol mentions: Batch 1: 62 mentions across 10 rows; Batch 2: 80 mentions across 32 rows; Batch 3: 99 mentions across 37 rows; Batch 4: 89 mentions across 54 rows; Batch 5: 25 reviewed stocks; Batch 6: 25 reviewed stocks; Batch 7: 25 reviewed stocks; Batch 8: 27 reviewed stocks.

## Conflict handling

- Repeated diagnostic symbols and shipbuilding symbols appeared in both their original batch and Batch 4 with the same proposed group. These are consolidated to one symbol row.
- `TRANSFORMERS` and `VOLTAMP` appear in Batch 2 under **Power & Distribution Transformers**, but Batch 4 proposes **Power Equipment & Grid Technology**. The register retains the earlier Batch 2 proposal provisionally and flags the alternative as a conflict. They must not enter two rotation peer groups.
- The duplicate company-name pairs `FINOLEXIND` / `FINPIPE` and `FLUOROCHEM` / `GFL` remain separate master symbol rows but are flagged for security-identity validation. Company-name duplication alone is not grounds to delete or merge a ticker.

## Important interpretation

This is a **coverage and review register**, not an approved taxonomy. All rows remain `PENDING_USER_REVIEW`. All 422 master securities now have a proposed review entry in the register. Batches 5–8 added 102 proposed reviews, all still `PENDING_USER_REVIEW`; review coverage does not imply user approval or live activation.

No primary business microsector, live rotation basket, dashboard tab, score, weight, calculation, data source or ranking has been changed. No changes were made to `app.py`.

## Next checkpoint

Review the flagged duplicate identities and group conflicts, then obtain the user's explicit approval/revisions for proposed peer groups, including flagged conflicts and symbol-identity exceptions. Only after user approval should a separate approved peer-group mapping be produced for shadow validation.

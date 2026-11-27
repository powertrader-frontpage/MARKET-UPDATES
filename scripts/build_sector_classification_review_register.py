"""Build a stock-by-stock sector classification review register (read-only)."""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data" / "stock_classification_master.csv"
OUT = ROOT / "reports" / "sector_classification_review_register.csv"
SUMMARY = ROOT / "reports" / "sector_classification_review_register_summary.md"

def main():
    df = pd.read_csv(MASTER, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required = [
        "Symbol", "Company", "NSE_Macro_Economic_Sector", "NSE_Sector",
        "NSE_Industry", "NSE_Basic_Industry", "MIT_Primary_Microsector",
        "MIT_Secondary_Tags", "Evidence_Source", "Evidence_URL",
        "Evidence_Type", "Verified_On", "Verification_Status", "Review_Notes",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit("Missing required master columns: " + ", ".join(missing))
    if df["Symbol"].duplicated().any() or df["Symbol"].eq("").any():
        raise SystemExit("Master contains blank or duplicate symbols.")

    register = df[required].copy()
    register.insert(2, "Sector_Review_Status", "PENDING_OFFICIAL_NSE_CLASSIFICATION")
    register.insert(3, "Proposed_Classification_Source", "")
    register.insert(4, "Proposed_Source_URL", "")
    register.insert(5, "Segment_Revenue_Basis", "")
    register.insert(6, "Reviewer_Decision", "")
    register.insert(7, "Review_Comments", "")
    register.to_csv(OUT, index=False, encoding="utf-8-sig")

    total = len(register)
    verified_micro = int(register["MIT_Primary_Microsector"].str.strip().ne("").sum())
    hierarchy_complete = (
        register[["NSE_Macro_Economic_Sector", "NSE_Sector", "NSE_Industry", "NSE_Basic_Industry"]]
        .apply(lambda col: col.str.strip().ne(""))
        .all(axis=1)
    )
    lines = [
        "# Sector classification review register",
        "",
        "Generated from the stock classification master. This is a review queue, not an approved classification dataset.",
        "",
        f"- Stocks in register: **{total:,}**",
        f"- Stocks with existing MIT primary microsector: **{verified_micro:,}**",
        f"- Stocks with all four NSE hierarchy fields populated: **{int(hierarchy_complete.sum()):,}**",
        f"- Stocks awaiting official NSE hierarchy review: **{int((~hierarchy_complete).sum()):,}**",
        "",
        "Every row starts as PENDING. Existing microsector values and evidence are carried forward unchanged.",
        "Do not fill NSE hierarchy fields by translating MIT microsector names or by guessing from a company's name.",
        "Use the official NSE Indices classification as the target hierarchy. Where an official company-level classification cannot be confirmed, leave it pending and record the limitation.",
        "For independent MIT business validation, use audited consolidated annual financials and segment revenue as primary evidence. Record the reporting year and the segment revenue rationale.",
        "",
        "Review order: Macro-Economic Sector -> Sector -> Industry -> Basic Industry. Preserve MIT Primary Microsector as a separate MIT classification layer.",
        "",
        "No dashboard code, production classification, scores, weights, ranking logic, or live calculations are changed by this register.",
        "",
    ]
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()

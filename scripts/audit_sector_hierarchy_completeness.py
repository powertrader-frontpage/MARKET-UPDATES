"""Read-only completeness audit for the stock sector hierarchy."""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MASTER_PATH = ROOT / "data" / "stock_classification_master.csv"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)
REPORT_PATH = REPORT_DIR / "sector_hierarchy_completeness_audit.md"
DETAIL_PATH = REPORT_DIR / "sector_hierarchy_missing_values.csv"

HIERARCHY = [
    "NSE_Macro_Economic_Sector",
    "NSE_Sector",
    "NSE_Industry",
    "NSE_Basic_Industry",
    "MIT_Primary_Microsector",
]
REQUIRED = ["Symbol", "Company", "Verification_Status"] + HIERARCHY

def main():
    frame = pd.read_csv(MASTER_PATH, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing_cols = [c for c in REQUIRED if c not in frame.columns]
    if missing_cols:
        raise SystemExit("Master missing required columns: " + ", ".join(missing_cols))
    for col in REQUIRED:
        frame[col] = frame[col].astype(str).str.strip()
    if frame["Symbol"].eq("").any():
        raise SystemExit("Blank symbols found in master.")
    duplicates = sorted(frame.loc[frame["Symbol"].duplicated(keep=False), "Symbol"].unique())
    if duplicates:
        raise SystemExit("Duplicate symbols found: " + ", ".join(duplicates))

    total = len(frame)
    verified = frame["Verification_Status"].str.upper().eq("VERIFIED")
    lines = [
        "# Sector hierarchy completeness audit",
        "",
        "Read-only audit of data/stock_classification_master.csv.",
        "No classifications, dashboard code, scores, or live calculations are modified.",
        "",
        "## Master overview",
        f"- Total stock rows: **{total:,}**",
        f"- Verified rows: **{int(verified.sum()):,}**",
        f"- Non-verified rows: **{int((~verified).sum()):,}**",
        f"- Duplicate symbols: **{len(duplicates)}**",
        "",
        "## Field completeness",
        "| Field | Populated | Blank | Populated % | Unique values |",
        "|---|---:|---:|---:|---:|",
    ]
    missing_rows = []
    for col in HIERARCHY:
        populated = frame[col].ne("")
        count = int(populated.sum())
        unique_count = int(frame.loc[populated, col].nunique())
        pct = (count / total * 100) if total else 0
        lines.append(f"| {col} | {count:,} | {total-count:,} | {pct:.1f}% | {unique_count:,} |")
        for _, row in frame.loc[~populated].iterrows():
            missing_rows.append({
                "Symbol": row["Symbol"],
                "Company": row["Company"],
                "Verification_Status": row["Verification_Status"],
                "Missing_Field": col,
                "Existing_Sector": row.get("NSE_Sector", ""),
                "Existing_Industry": row.get("NSE_Industry", ""),
                "Existing_Basic_Industry": row.get("NSE_Basic_Industry", ""),
                "Existing_Primary_Microsector": row.get("MIT_Primary_Microsector", ""),
            })
    detail = pd.DataFrame(missing_rows, columns=[
        "Symbol", "Company", "Verification_Status", "Missing_Field",
        "Existing_Sector", "Existing_Industry", "Existing_Basic_Industry",
        "Existing_Primary_Microsector"
    ])
    detail.to_csv(DETAIL_PATH, index=False, encoding="utf-8-sig")

    lines += ["", "## Completeness by verification status",
              "| Field | Verified populated | Verified blank | Non-verified populated | Non-verified blank |",
              "|---|---:|---:|---:|---:|"]
    for col in HIERARCHY:
        vpop = verified & frame[col].ne("")
        vblank = verified & frame[col].eq("")
        npop = (~verified) & frame[col].ne("")
        nblank = (~verified) & frame[col].eq("")
        lines.append(f"| {col} | {int(vpop.sum()):,} | {int(vblank.sum()):,} | {int(npop.sum()):,} | {int(nblank.sum()):,} |")

    lines += [
        "",
        "## Interpretation and safeguards",
        "- NSE macro sector, NSE sector, industry, and basic industry are separate hierarchy levels; do not substitute one for another.",
        "- MIT primary microsector is a separate MIT taxonomy level and must have exactly one primary assignment for each verified stock.",
        "- NIFTY sector index membership is a benchmark grouping, not automatic proof of a company's NSE sector classification.",
        "- Blank fields are reported as missing; this audit does not infer or fill classifications.",
        "- Any proposed correction must be supported by an official exchange/company source and reviewed before approval.",
        "- The legacy dashboard sector and microsector mappings remain unchanged until the complete master is validated and shadow comparisons pass.",
        "",
        "Stock-field gaps: reports/sector_hierarchy_missing_values.csv.",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()

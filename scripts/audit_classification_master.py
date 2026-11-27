"""Generate a classification-master audit report.

Run from the repository root:
    python scripts/audit_classification_master.py
"""
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from classification_master import load_classification_master, primary_microsector_map

CSV_PATH = ROOT / "data" / "stock_classification_master.csv"
REPORT_PATH = ROOT / "reports" / "stock_classification_audit.md"


def main():
    frame = load_classification_master(CSV_PATH)
    total = len(frame)
    status_counts = Counter(frame["Verification_Status"].str.upper())
    micro_counts = Counter(
        frame.loc[frame["Verification_Status"].str.upper().eq("VERIFIED"), "MIT_Primary_Microsector"]
    )
    missing_evidence = frame[
        frame["Verification_Status"].str.upper().eq("VERIFIED")
        & (
            frame["Evidence_Source"].eq("")
            | frame["Evidence_URL"].eq("")
            | frame["Evidence_Type"].eq("")
            | frame["Verified_On"].eq("")
        )
    ]["Symbol"].tolist()
    mapping = primary_microsector_map(frame)
    duplicate_symbols = frame.loc[frame["Symbol"].duplicated(keep=False), "Symbol"].unique().tolist()
    hyundai = mapping.get("HYUNDAI")
    tata = mapping.get("TATAMOTORS")
    same_auto_microsector = bool(hyundai and tata and hyundai == tata)

    checks = [
        ("Unique symbols", not duplicate_symbols),
        ("Verified rows have a primary microsector", not frame.loc[
            frame["Verification_Status"].str.upper().eq("VERIFIED"), "MIT_Primary_Microsector"
        ].eq("").any()),
        ("Verified rows have evidence metadata", not missing_evidence),
        ("HYUNDAI and TATAMOTORS share a primary microsector", same_auto_microsector),
    ]
    passed = sum(ok for _, ok in checks)

    lines = [
        "# MIT Stock Classification Master — Audit",
        "",
        f"- **Audit file:** `data/stock_classification_master.csv`",
        f"- **Total stock rows:** {total}",
        f"- **Verified:** {status_counts.get('VERIFIED', 0)}",
        f"- **Pending / other status:** {total - status_counts.get('VERIFIED', 0)}",
        f"- **Unique verified primary microsectors:** {len(micro_counts)}",
        f"- **Audit checks passed:** {passed}/{len(checks)}",
        "",
        "## Validation checks",
        "",
    ]
    for label, ok in checks:
        lines.append(f"- [{'x' if ok else ' '}] {label}")
    lines += [
        "",
        "## Highest-coverage primary microsectors",
        "",
        "| Primary microsector | Stocks |",
        "|---|---:|",
    ]
    for name, count in micro_counts.most_common(20):
        lines.append(f"| {name.replace('|', '\\|')} | {count} |")
    lines += [
        "",
        "## Consistency spot-check",
        "",
        f"- HYUNDAI: {hyundai or 'not found'}",
        f"- TATAMOTORS: {tata or 'not found'}",
        "",
        "## Evidence metadata gaps",
        "",
    ]
    lines.append("- None." if not missing_evidence else "- " + ", ".join(missing_evidence))
    if duplicate_symbols:
        lines += ["", "## Duplicate symbols", "", "- " + ", ".join(sorted(duplicate_symbols))]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

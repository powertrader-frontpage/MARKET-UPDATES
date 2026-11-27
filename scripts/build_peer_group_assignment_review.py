"""Build a stock-level comparable peer-group review register (read-only)."""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data" / "stock_classification_master.csv"
ALIASES = ROOT / "data" / "microsector_canonical_alias_map.csv"
OUT = ROOT / "reports" / "peer_group_assignment_review_register.csv"
SUMMARY = ROOT / "reports" / "peer_group_assignment_review_summary.md"

# Only explicit, previously approved peer relationships are pre-populated.
APPROVED_PEER_GROUPS = {
    "TRANSFORMERS": "Power & Distribution Transformer Manufacturers",
    "VOLTAMP": "Power & Distribution Transformer Manufacturers",
    "KELLTONTEC": "Technology Consulting & Digital Engineering",
    "ZENSARTECH": "Technology Consulting & Digital Engineering",
}
POWER_UTILITY_DESCRIPTORS = {
    "NTPC": "Generation-led",
    "TATAPOWER": "Vertically integrated",
    "CESC": "Distribution-led integrated utility",
    "TORNTPOWER": "Distribution-led integrated utility",
}

def main():
    master = pd.read_csv(MASTER, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    aliases = pd.read_csv(ALIASES, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required = {"Symbol", "Company", "MIT_Primary_Microsector", "MIT_Secondary_Tags",
                "Legacy_Broad_Groups", "Verification_Status"}
    missing = sorted(required - set(master.columns))
    if missing:
        raise SystemExit("Master missing columns: " + ", ".join(missing))
    if master["Symbol"].eq("").any() or master["Symbol"].duplicated().any():
        raise SystemExit("Master has blank or duplicate symbols.")
    if master["MIT_Primary_Microsector"].eq("").any():
        raise SystemExit("Master has blank primary microsector values.")
    if not aliases["Approval_Status"].eq("APPROVED_FOR_MAPPING_ONLY").all():
        raise SystemExit("Alias map contains non-approved rows.")

    register = master[["Symbol", "Company", "MIT_Primary_Microsector",
                       "MIT_Secondary_Tags", "Legacy_Broad_Groups",
                       "Verification_Status"]].copy()
    register["Approved_Comparable_Peer_Group"] = register["Symbol"].map(APPROVED_PEER_GROUPS).fillna("")
    register["Approved_Peer_Descriptor"] = register["Symbol"].map(POWER_UTILITY_DESCRIPTORS).fillna("")
    register["Peer_Group_Review_Status"] = register["Symbol"].map(
        lambda s: "APPROVED_EXISTING_PEER_GROUP" if s in APPROVED_PEER_GROUPS
        else "APPROVED_PEER_DESCRIPTOR_ONLY" if s in POWER_UTILITY_DESCRIPTORS
        else "PENDING_PEER_REVIEW"
    )
    register["Proposed_Peer_Group"] = ""
    register["Rationale"] = ""
    register["Evidence_Source"] = ""
    register["Evidence_URL"] = ""
    register["Reviewer_Decision"] = ""
    register["Review_Notes"] = ""
    register.to_csv(OUT, index=False, encoding="utf-8-sig")

    approved_groups = int(register["Approved_Comparable_Peer_Group"].ne("").sum())
    descriptors = int(register["Approved_Peer_Descriptor"].ne("").sum())
    pending = int(register["Peer_Group_Review_Status"].eq("PENDING_PEER_REVIEW").sum())
    lines = [
        "# Comparable peer-group assignment review",
        "",
        "Generated from the current stock master. This is a review register, not a new production taxonomy.",
        "",
        f"- Master stock rows: **{len(register):,}**",
        f"- Unique symbols: **{register['Symbol'].nunique():,}**",
        f"- Existing explicitly approved peer-group assignments carried forward: **{approved_groups:,}**",
        f"- Existing approved power-utility descriptors carried forward (not a shared peer group): **{descriptors:,}**",
        f"- Stocks pending peer-group review: **{pending:,}**",
        "",
        "The register pre-populates only the explicit approved relationships from the audit closure: transformer manufacturers and technology consulting/digital engineering. Power-utility role descriptors are retained separately and do not create a shared peer group.",
        "",
        "Renewable generators, solar manufacturers and wind OEMs remain distinct. Auto-component companies retain their exact primary microsectors. Secondary tags and legacy broad groups are metadata only and do not assign peer groups.",
        "",
        "All other proposed peer groups remain blank for review. Do not auto-assign based on label similarity or legacy broad-group strings. A stock may have zero or one comparable peer group; its detailed MIT primary microsector remains unchanged.",
        "",
        "No scoring, weights, return windows, breadth, zones, ranking logic, source priority, app.py, or live dashboard calculations are changed.",
        "",
        f"Detailed register: `{OUT.relative_to(ROOT)}`",
        "",
    ]
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()

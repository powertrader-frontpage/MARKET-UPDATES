"""Produce per-consumer legacy-vs-canonical membership diffs without changing app.py.

All listed consumers receive the shared MICRO_SECTORS universe in the current
app. The report deliberately separates membership comparison from ranking:
ranking invariance requires a frozen price/data snapshot and is not inferred
from identical basket inputs.
"""
import ast
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from classification_master import load_classification_master
from canonical_microsectors import load_microsector_alias_map, build_canonical_classification

APP = ROOT / "app.py"
MASTER = ROOT / "data" / "stock_classification_master.csv"
ALIASES = ROOT / "data" / "microsector_canonical_alias_map.csv"
OUT = ROOT / "reports" / "microsector_per_consumer_membership_diff.csv"
SUMMARY = ROOT / "reports" / "microsector_per_consumer_shadow_summary.md"

# Confirmed app.py consumers of the shared MICRO_SECTORS dictionary.
CONSUMERS = [
    "Main Dashboard — Microsector Rotation",
    "Sector Leadership & Heatmap — Microsector Rotation",
    "Main Dashboard — Microsector Drilldown",
    "Main Dashboard — NSE 500 Leading Microsector B1/B2 Watchlist",
    "Hourly Strength Screener",
    "Institutional Volume Intelligence",
    "News Intelligence",
    "Weakness Breakdown Radar",
]


def read_legacy_baskets():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MICRO_SECTORS"
            for target in node.targets
        ):
            raw = ast.literal_eval(node.value)
            result = {}
            for label, value in raw.items():
                _group, symbols = value
                result[str(label)] = sorted({
                    str(symbol).strip().upper().removesuffix(".NS")
                    for symbol in symbols
                })
            return result
    raise RuntimeError("Could not locate literal MICRO_SECTORS assignment in app.py")


def main():
    master = load_classification_master(MASTER)
    aliases = load_microsector_alias_map(ALIASES)
    canonical = build_canonical_classification(master, aliases)
    canonical_map = dict(zip(canonical["Symbol"], canonical["Canonical_Microsector"]))
    legacy = read_legacy_baskets()

    legacy_by_symbol = {}
    for label, symbols in legacy.items():
        for symbol in symbols:
            legacy_by_symbol.setdefault(symbol, []).append(label)

    rows = []
    all_symbols = sorted(set(canonical_map) | set(legacy_by_symbol))
    for consumer in CONSUMERS:
        for symbol in all_symbols:
            canonical_label = canonical_map.get(symbol, "")
            legacy_labels = sorted(legacy_by_symbol.get(symbol, []))
            status = (
                "MATCH_ONE_LABEL" if canonical_label and legacy_labels == [canonical_label]
                else "LEGACY_MULTI_MEMBERSHIP" if len(legacy_labels) > 1
                else "LEGACY_LABEL_DIFFERS" if canonical_label and legacy_labels
                else "MASTER_ONLY" if canonical_label
                else "LEGACY_ONLY"
            )
            rows.append({
                "Consumer": consumer,
                "Symbol": symbol,
                "Canonical_Primary_Microsector": canonical_label,
                "Legacy_Microsector_Count": len(legacy_labels),
                "Legacy_Microsector_Labels": ";".join(legacy_labels),
                "Membership_Status": status,
            })

    frame = pd.DataFrame(rows)
    frame.to_csv(OUT, index=False, encoding="utf-8-sig")
    counts = frame.groupby(["Consumer", "Membership_Status"]).size().unstack(fill_value=0)
    lines = [
        "# Per-consumer microsector shadow comparison",
        "",
        "Generated from the shared app.py MICRO_SECTORS universe and verified canonical master.",
        "",
        "- Mode: **SHADOW ONLY**; app.py and live rotation code are not modified.",
        f"- Consumers audited: **{len(CONSUMERS)}**",
        f"- Distinct stock symbols compared: **{len(all_symbols)}**",
        "- Each consumer currently receives the same shared legacy MICRO_SECTORS dictionary; therefore base membership diffs are expected to be identical across consumers.",
        "",
        "## Consumer membership counts",
        "",
        counts.to_markdown(),
        "",
        "## Ranking invariance gate",
        "",
        "**NOT YET EXECUTED.** This report compares constituent membership only. Ranking invariance must be tested against a frozen, identical price/history snapshot by running the existing production ranking pipeline twice (legacy basket input and canonical basket input) without changing return windows, weighting, breadth, zone, ranking, or source-priority logic. No ranking equivalence is claimed here.",
        "",
        "## Integration gate",
        "",
        "Do not switch any consumer to canonical baskets until the membership diffs are reviewed and the frozen-snapshot ranking comparison passes.",
        "",
        f"Detailed output: `{OUT.relative_to(ROOT)}`.",
        "",
    ]
    SUMMARY.write_text("\\n".join(lines), encoding="utf-8")
    print("\\n".join(lines))


if __name__ == "__main__":
    main()

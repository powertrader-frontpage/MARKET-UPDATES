"""Run a stock-mapped ranking impact audit on one frozen OHLCV snapshot."""
from __future__ import annotations
import ast
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from canonical_microsector_runtime import load_canonical_runtime
from ranking_snapshot import load_ranking_snapshot
APP_PATH = ROOT / "app.py"
SNAPSHOT_PATH = ROOT / "data" / "ranking_invariance_snapshot.json"
GROUP_OUT = ROOT / "reports" / "ranking_invariance_group_scores.csv"
STOCK_OUT = ROOT / "reports" / "ranking_invariance_stock_transitions.csv"
SUMMARY_OUT = ROOT / "reports" / "ranking_invariance_summary.md"
BENCHMARK = "^CRSLDX"
PRODUCTION_WEIGHTS = (0.25, 0.35, 0.25, 0.15)
PRODUCTION_WINDOWS = (22, 64, 127)
MINIMUM_HISTORY = 60

def load_legacy_baskets():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MICRO_SECTORS" for t in node.targets):
            value = ast.literal_eval(node.value)
            return {str(label): sorted({str(t).upper() for t in entry[1]}) for label, entry in value.items()}
    raise RuntimeError("Could not safely locate literal MICRO_SECTORS in app.py.")

def close_series(frame, symbol):
    part = frame.loc[frame["Symbol"].astype(str).eq(symbol), ["Date", "Close"]]
    if part.empty:
        return pd.Series(dtype=float)
    return part.drop_duplicates("Date").set_index("Date")["Close"].sort_index().astype(float)

def calculate_rotation(frame, baskets, version):
    benchmark = close_series(frame, BENCHMARK)
    if benchmark.empty:
        raise ValueError("Snapshot is missing NIFTY 500 benchmark.")
    bench1 = benchmark.iloc[-1] / benchmark.iloc[-min(22, len(benchmark))] * 100 - 100 if len(benchmark) >= 22 else np.nan
    bench3 = benchmark.iloc[-1] / benchmark.iloc[-min(64, len(benchmark))] * 100 - 100 if len(benchmark) >= 64 else np.nan
    rows = []
    stock_to_groups = {}
    for label, symbols in sorted(baskets.items()):
        returns, breadth = [], []
        available = []
        for symbol in symbols:
            close = close_series(frame, symbol)
            if len(close) < MINIMUM_HISTORY:
                continue
            last = float(close.iloc[-1])
            r1 = (last / float(close.iloc[-min(22, len(close))]) - 1) * 100
            r3 = (last / float(close.iloc[-min(64, len(close))]) - 1) * 100
            r6 = (last / float(close.iloc[-min(127, len(close))]) - 1) * 100
            ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
            returns.append((r1, r3, r6))
            breadth.append(float(last > float(ema50.iloc[-1])))
            available.append(symbol)
            stock_to_groups.setdefault(symbol, []).append(label)
        if not returns:
            continue
        arr = np.asarray(returns, dtype=float)
        avg1, avg3, avg6 = np.nanmean(arr, axis=0)
        rows.append({"Version": version, "Micro sector": label, "Stocks available": len(returns),
                     "Stocks listed": len(symbols), "1M %": avg1, "3M %": avg3, "6M %": avg6,
                     "50 EMA breadth %": float(np.mean(breadth))*100,
                     "_bench1": bench1, "_bench3": bench3, "_available_symbols": available})
    result = pd.DataFrame(rows)
    if result.empty:
        return result, stock_to_groups
    for col in ["1M %", "3M %", "6M %", "50 EMA breadth %"]:
        result[col + " percentile"] = result[col].rank(pct=True) * 100
    result["Score"] = (result["1M % percentile"]*PRODUCTION_WEIGHTS[0] +
                       result["3M % percentile"]*PRODUCTION_WEIGHTS[1] +
                       result["6M % percentile"]*PRODUCTION_WEIGHTS[2] +
                       result["50 EMA breadth % percentile"]*PRODUCTION_WEIGHTS[3]).round().astype(int)
    result["1M vs NIFTY 500 (pp)"] = result["1M %"] - result["_bench1"]
    result["3M vs NIFTY 500 (pp)"] = result["3M %"] - result["_bench3"]
    def zone(row):
        ahead = pd.notna(row["3M vs NIFTY 500 (pp)"]) and row["3M vs NIFTY 500 (pp)"] >= 0
        rising = pd.notna(row["1M vs NIFTY 500 (pp)"]) and row["1M vs NIFTY 500 (pp)"] >= 0
        return "Leading" if ahead and rising else "Weakening" if ahead else "Improving" if rising else "Lagging"
    result["Zone"] = result.apply(zone, axis=1)
    result["Rank"] = result["Score"].rank(method="min", ascending=False).astype(int)
    result = result.drop(columns=["_bench1", "_bench3"])
    return result, stock_to_groups

def main():
    if not SNAPSHOT_PATH.exists():
        raise SystemExit("No frozen OHLCV snapshot. Save weekly history from the dashboard first.")
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    frame, metadata = load_ranking_snapshot(snapshot)
    legacy = load_legacy_baskets()
    canonical_frame, canonical_map, canonical, audit = load_canonical_runtime()
    old, old_assignments = calculate_rotation(frame, legacy, "LEGACY")
    new, new_assignments = calculate_rotation(frame, canonical, "CANONICAL")
    groups = pd.concat([old, new], ignore_index=True)
    groups.to_csv(GROUP_OUT, index=False)
    old_rank = old.set_index("Micro sector")["Rank"].to_dict()
    old_score = old.set_index("Micro sector")["Score"].to_dict()
    new_rank = new.set_index("Micro sector")["Rank"].to_dict()
    new_score = new.set_index("Micro sector")["Score"].to_dict()
    canonical_by_symbol = {str(k).upper(): str(v) for k, v in canonical_map.items()}
    transition_rows = []
    for symbol, labels in sorted(old_assignments.items()):
        primary = canonical_by_symbol.get(symbol.removesuffix(".NS"))
        if not primary or primary not in new_rank:
            continue
        for label in labels:
            if label not in old_rank:
                continue
            transition_rows.append({
                "Stock": symbol.removesuffix(".NS"),
                "Legacy microsector": label,
                "Canonical primary microsector": primary,
                "Legacy rank": old_rank[label],
                "Canonical rank": new_rank[primary],
                "Rank change": new_rank[primary] - old_rank[label],
                "Legacy score": old_score[label],
                "Canonical score": new_score[primary],
                "Score change": new_score[primary] - old_score[label],
                "Legacy membership count": len(labels),
            })
    transitions = pd.DataFrame(transition_rows)
    transitions.to_csv(STOCK_OUT, index=False)
    union = {s for members in legacy.values() for s in members}
    union |= {s for members in canonical.values() for s in members}
    available = set(frame["Symbol"].astype(str).unique()) - {BENCHMARK}
    missing = sorted(union - available)
    coverage = 1 - len(missing) / max(1, len(union))
    changed = int((transitions["Rank change"] != 0).sum()) if not transitions.empty else 0
    median_delta = float(transitions["Rank change"].abs().median()) if not transitions.empty else float("nan")
    lines = [
        "# Frozen-snapshot ranking impact audit", "",
        f"- Snapshot as-of: {metadata.get('as_of')}",
        f"- Source: {metadata.get('source')}",
        f"- SHA-256: {snapshot.get('sha256')}",
        f"- OHLCV rows / symbols: {snapshot.get('row_count')} / {snapshot.get('symbol_count')}",
        f"- Legacy / canonical ranked microsectors: {len(old)} / {len(new)}",
        f"- Union constituent coverage: {len(union)-len(missing)} / {len(union)} ({coverage:.1%})",
        f"- Missing constituents in snapshot: {len(missing)}",
        f"- Stock-to-group rank transitions: {len(transitions)}",
        f"- Stock memberships with changed group rank: {changed}",
        f"- Median absolute rank change: {median_delta}",
        f"- Production weights preserved: {PRODUCTION_WEIGHTS}",
        f"- Production return lookback positions: {PRODUCTION_WINDOWS}",
        f"- Minimum history preserved: {MINIMUM_HISTORY} sessions",
        f"- NSE hierarchy remains separate: {not audit.get('nse_hierarchy_complete')}",
        "",
        "Interpretation: group rankings may legitimately change because the legacy and canonical baskets have different labels, granularity and membership. This report maps each stock's legacy group rank(s) to its canonical primary-group rank; it does not falsely require unlike group labels to match.",
        "A final pass requires complete snapshot coverage and manual acceptance of the documented rank transitions. No live formulas are changed by this audit.",
        "",
        f"Group scores: {GROUP_OUT.relative_to(ROOT)}",
        f"Stock transitions: {STOCK_OUT.relative_to(ROOT)}",
    ]
    SUMMARY_OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    if missing:
        raise SystemExit("FAIL: frozen snapshot does not cover the complete old/canonical constituent union.")
    if transitions.empty:
        raise SystemExit("FAIL: no stock-level rank transitions could be evaluated.")

if __name__ == "__main__":
    main()

"""Compare legacy and canonical microsector rankings on one frozen OHLCV snapshot."""
from __future__ import annotations
import ast
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
OUT_PATH = ROOT / "reports" / "ranking_invariance_comparison.csv"
SUMMARY_PATH = ROOT / "reports" / "ranking_invariance_summary.md"
BENCHMARK = "^CRSLDX"

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

def calculate_rotation(frame, baskets):
    benchmark = close_series(frame, BENCHMARK)
    if benchmark.empty:
        raise ValueError("Snapshot is missing NIFTY 500 benchmark.")
    bench1 = benchmark.iloc[-1] / benchmark.iloc[-min(22, len(benchmark))] * 100 - 100 if len(benchmark) >= 22 else np.nan
    bench3 = benchmark.iloc[-1] / benchmark.iloc[-min(64, len(benchmark))] * 100 - 100 if len(benchmark) >= 64 else np.nan
    rows = []
    for label, symbols in sorted(baskets.items()):
        returns, breadth_values = [], []
        for symbol in symbols:
            close = close_series(frame, symbol)
            if len(close) < 60:
                continue
            last = float(close.iloc[-1])
            r1 = (last / float(close.iloc[-min(22, len(close))]) - 1) * 100
            r3 = (last / float(close.iloc[-min(64, len(close))]) - 1) * 100
            r6 = (last / float(close.iloc[-min(127, len(close))]) - 1) * 100
            ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
            returns.append((r1, r3, r6))
            breadth_values.append(float(last > float(ema50.iloc[-1])))
        if not returns:
            continue
        arr = np.asarray(returns, dtype=float)
        avg1, avg3, avg6 = np.nanmean(arr, axis=0)
        rows.append({"Micro sector": label, "Stocks available": len(returns), "Stocks listed": len(symbols),
                     "1M %": avg1, "3M %": avg3, "6M %": avg6,
                     "50 EMA breadth %": float(np.mean(breadth_values))*100,
                     "_bench1": bench1, "_bench3": bench3})
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    for col in ["1M %", "3M %", "6M %", "50 EMA breadth %"]:
        result[col + " percentile"] = result[col].rank(pct=True) * 100
    result["Score"] = (result["1M % percentile"]*.25 + result["3M % percentile"]*.35 +
                       result["6M % percentile"]*.25 + result["50 EMA breadth % percentile"]*.15).round().astype(int)
    result["1M vs NIFTY 500 (pp)"] = result["1M %"] - result["_bench1"]
    result["3M vs NIFTY 500 (pp)"] = result["3M %"] - result["_bench3"]
    def zone(row):
        ahead = pd.notna(row["3M vs NIFTY 500 (pp)"]) and row["3M vs NIFTY 500 (pp)"] >= 0
        rising = pd.notna(row["1M vs NIFTY 500 (pp)"]) and row["1M vs NIFTY 500 (pp)"] >= 0
        return "Leading" if ahead and rising else "Weakening" if ahead else "Improving" if rising else "Lagging"
    result["Zone"] = result.apply(zone, axis=1)
    result["Rank"] = result["Score"].rank(method="min", ascending=False).astype(int)
    return result.drop(columns=["_bench1", "_bench3"])

def main():
    if not SNAPSHOT_PATH.exists():
        raise SystemExit("No frozen OHLCV snapshot. Save weekly history from the dashboard first.")
    import json
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    frame, metadata = load_ranking_snapshot(snapshot)
    legacy = load_legacy_baskets()
    _, _, canonical, audit = load_canonical_runtime()
    old_result = calculate_rotation(frame, legacy)
    new_result = calculate_rotation(frame, canonical)
    merged = old_result.merge(new_result, on="Micro sector", how="outer",
                               suffixes=(" · legacy", " · canonical"), indicator=True)
    merged["Score delta"] = merged["Score · canonical"] - merged["Score · legacy"]
    merged["Rank delta"] = merged["Rank · canonical"] - merged["Rank · legacy"]
    merged["Zone changed"] = merged["Zone · legacy"].fillna("") != merged["Zone · canonical"].fillna("")
    merged.to_csv(OUT_PATH, index=False)
    both = merged[merged["_merge"].eq("both")]
    score_ok = bool((both["Score delta"] == 0).all())
    rank_ok = bool((both["Rank delta"] == 0).all())
    zone_ok = bool((~both["Zone changed"]).all())
    lines = ["# Frozen-snapshot ranking invariance", "",
             f"- As-of: {metadata.get('as_of')}",
             f"- Source: {metadata.get('source')}",
             f"- SHA-256: {snapshot.get('sha256')}",
             f"- OHLCV rows / symbols: {snapshot.get('row_count')} / {snapshot.get('symbol_count')}",
             f"- Legacy / canonical ranked groups: {len(old_result)} / {len(new_result)}",
             f"- Same-label comparisons: {len(both)}",
             f"- Exact score equality: {score_ok}",
             f"- Exact rank equality: {rank_ok}",
             f"- Exact zone equality: {zone_ok}",
             f"- Master audit rows: {audit.get('master_rows')}", "",
             "Both basket sets were evaluated against the same frozen OHLCV inputs using the production score weights, return windows, percentile ranks, 50-EMA breadth and relative-strength zone rules.",
             "Labels present on only one side are retained in the outer-join comparison and block a pass.", ""]
    SUMMARY_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    if merged["_merge"].ne("both").any() or not (score_ok and rank_ok and zone_ok):
        raise SystemExit("FAIL: ranking invariance not achieved; inspect comparison CSV.")

if __name__ == "__main__":
    main()

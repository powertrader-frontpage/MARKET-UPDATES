"""Unit checks for deterministic ranking input snapshots."""
import pandas as pd

from ranking_snapshot import build_ranking_snapshot, load_ranking_snapshot


def sample_frame():
    return pd.DataFrame([
        ["2026-11-26", "ABC", 10, 11, 9, 10.5, 1000],
        ["2026-11-27", "ABC", 10.5, 12, 10, 11.5, 1200],
        ["2026-11-26", "^CRSLDX", 100, 101, 99, 100.5, 10000],
        ["2026-11-27", "^CRSLDX", 100.5, 102, 100, 101.5, 11000],
    ], columns=["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"])


def main():
    frame = sample_frame()
    first = build_ranking_snapshot(frame, as_of="2026-11-27", source="test")
    second = build_ranking_snapshot(frame.iloc[::-1], as_of="2026-11-27", source="test")
    assert first["sha256"] == second["sha256"], "Input ordering must not change snapshot hash."
    restored, metadata = load_ranking_snapshot(first)
    assert len(restored) == 4
    assert metadata["benchmark_symbol"] == "^CRSLDX"
    assert sorted(restored["Symbol"].unique()) == ["ABC", "^CRSLDX"]

    tampered = dict(first)
    tampered["payload"] = first["payload"][:-4] + "AAAA"
    try:
        load_ranking_snapshot(tampered)
    except ValueError:
        pass
    else:
        raise AssertionError("Tampered snapshot should fail integrity validation.")

    no_benchmark = frame[frame["Symbol"] != "^CRSLDX"]
    try:
        build_ranking_snapshot(no_benchmark, as_of="2026-11-27", source="test")
    except ValueError as exc:
        assert "benchmark" in str(exc).lower()
    else:
        raise AssertionError("Snapshot without benchmark must be rejected.")
    print("PASS: deterministic ordering, round-trip, integrity, benchmark guard.")


if __name__ == "__main__":
    main()

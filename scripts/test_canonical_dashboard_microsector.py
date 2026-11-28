"""Preview integration checks for canonical microsector and ticker mappings."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical_microsector import load_canonical_microsectors, canonical_microsector_lookup


def main():
    baskets, metadata, joined = load_canonical_microsectors()
    lookup = canonical_microsector_lookup()

    assert metadata["master_stocks"] == 422, metadata
    assert metadata["canonical_microsectors"] == 278, metadata
    assert metadata["alias_coverage_errors"] == 0, metadata
    assert metadata["verified_price_aliases"] == 8, metadata
    assert len(joined) == 422
    assert len(lookup) >= 422
    assert sum(len(tickers) for _, tickers in baskets.values()) == metadata["eligible_price_stocks"]
    assert len({ticker for _, tickers in baskets.values() for ticker in tickers}) == metadata["eligible_price_stocks"]
    assert "GFL" in metadata["excluded_price_symbols"], metadata

    for old, current in {
        "AMARAJABAT": "ARE&M",
        "IIFLSEC": "IIFLCAPS",
        "KALPATPOWR": "KPIL",
        "KAVVERITEL": "KAVDEFENCE",
        "SUVENPHAR": "COHANCE",
        "TATAMOTORS": "TMPV",
        "TRANSFORMERS": "TARIL",
        "VA TECH WABAG": "WABAG",
    }.items():
        assert lookup[old] == lookup[current], (old, current, lookup.get(old), lookup.get(current))

    print("CANONICAL DASHBOARD PREVIEW: PASS")
    for key, value in metadata.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()

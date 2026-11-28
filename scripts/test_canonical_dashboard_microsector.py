"""Trial checks for the canonical Nifty 500 Sector/Microsector mapping."""
from canonical_microsector import (
    load_canonical_microsectors,
    canonical_microsector_lookup,
    canonical_sector_lookup,
    canonical_classification_lookup,
    map_to_canonical_price_ticker,
)


def main():
    baskets, metadata, joined = load_canonical_microsectors()
    micro_lookup = canonical_microsector_lookup()
    sector_lookup = canonical_sector_lookup()
    detail_lookup = canonical_classification_lookup()

    assert len(joined) == 500, metadata
    assert joined["Symbol"].nunique() == 500
    assert joined["Sector"].nunique() == 20
    assert joined["Microsector"].nunique() == 125
    assert len(baskets) == 125
    assert metadata["detail_master_matches"] == 300, metadata
    assert metadata["rotation_only_stocks"] == 200, metadata
    assert metadata["detail_master_only_stocks"] == 122, metadata
    assert metadata["draft_assignments"] == 470, metadata
    assert sum(len(tickers) for _, tickers in baskets.values()) == 500

    # Each microsector has one and only one parent Sector.
    assert joined.groupby("Microsector")["Sector"].nunique().eq(1).all()
    for microsector, (sector, tickers) in baskets.items():
        rows = joined[joined["Microsector"].eq(microsector)]
        assert rows["Sector"].nunique() == 1
        assert rows["Sector"].iloc[0] == sector
        assert len(tickers) == len(rows)
        assert len(tickers) == len(set(tickers))

    # All dashboard consumers resolve the same mapping by NSE symbol.
    for row in joined.itertuples(index=False):
        assert micro_lookup[row.Symbol] == row.Microsector
        assert sector_lookup[row.Symbol] == row.Sector
        assert detail_lookup[row.Symbol]["Microsector"] == row.Microsector
        assert micro_lookup[row.Symbol + ".NS"] == row.Microsector
        assert sector_lookup[row.Symbol + ".NS"] == row.Sector

    assert map_to_canonical_price_ticker("TCS") == "TCS.NS"
    assert map_to_canonical_price_ticker("TCS.NS") == "TCS.NS"
    print("PASS: 500 stocks, 20 sectors, 125 microsectors; exact-symbol detail join and parent consistency verified.")
    print("Assignment statuses:", metadata["assignment_status_counts"])


if __name__ == "__main__":
    main()

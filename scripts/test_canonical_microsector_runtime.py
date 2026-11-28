"""CI checks for the master-driven canonical classification runtime."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical_microsector_runtime import load_canonical_runtime


def main():
    frame, mapping, baskets, audit = load_canonical_runtime()
    assert audit["master_rows"] == 422, audit
    assert audit["verified_rows"] == 422, audit
    assert audit["canonical_microsectors"] == 278, audit
    assert audit["primary_memberships"] == 422, audit
    assert audit["duplicate_primary_memberships"] == 0, audit
    assert audit["alias_coverage_errors"] == 0, audit
    assert len(mapping) == 422
    assert sum(len(items) for items in baskets.values()) == 422
    assert len({ticker for items in baskets.values() for ticker in items}) == 422
    print("CANONICAL RUNTIME SHADOW: PASS")
    for key, value in audit.items():
        print(f"{key}: {value}")
    print("NSE hierarchy remains a separately tracked review gate; no values inferred.")


if __name__ == "__main__":
    main()

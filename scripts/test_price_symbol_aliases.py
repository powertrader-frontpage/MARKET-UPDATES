from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from price_symbol_aliases import load_price_symbol_aliases, map_price_symbol

ALIASES = load_price_symbol_aliases(ROOT / "data" / "price_symbol_alias_map.csv")
EXPECTED = {
    "AMARAJABAT": "ARE&M",
    "IIFLSEC": "IIFLCAPS",
    "KALPATPOWR": "KPIL",
    "KAVVERITEL": "KAVDEFENCE",
    "SUVENPHAR": "COHANCE",
    "TATAMOTORS": "TMPV",
    "TRANSFORMERS": "TARIL",
    "VA TECH WABAG": "WABAG",
}
assert ALIASES == EXPECTED, (ALIASES, EXPECTED)
for old, new in EXPECTED.items():
    assert map_price_symbol(old + ".NS", ALIASES) == new + ".NS"
    assert map_price_symbol(old, ALIASES) == new
assert map_price_symbol("GFL.NS", ALIASES) == "GFL.NS", "Unverified GFL identity must remain unmapped"
assert map_price_symbol("^CRSLDX", ALIASES) == "^CRSLDX"
print(f"PASS: {len(ALIASES)} verified one-to-one price-symbol aliases; unverified symbols remain unchanged.")

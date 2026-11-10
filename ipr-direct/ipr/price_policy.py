"""ENTRY admission only; never drop a TRADE or change open-trade monitoring.

Alpaca Market Data FAQ, stock minute-bar OC and HL eligibility, reviewed
2026-11-08: https://docs.alpaca.markets/us/docs/market-data-faq
All conditions must permit both price components for the supplied tape.
Unknown/missing metadata fails closed for ENTRY. No size threshold.
"""
from types import MappingProxyType

# R10.3: only this isolated flag controls future ENTRY enforcement. SHADOW now.
ENTRY_PRICE_POLICY_ENFORCED = False

def price_policy_allows_entry(message):
    if not ENTRY_PRICE_POLICY_ENFORCED:
        return True
    return entry_price_eligible(message)

MINUTE_PRICE_CONDITIONS = MappingProxyType({
    'A': frozenset((' ', 'E', 'F', 'K', 'L', 'O', 'T', 'X', '5', '6')),
    'B': frozenset((' ', 'E', 'F', 'K', 'L', 'O', 'T', 'X', '5', '6')),
    'C': frozenset(('@', 'A', 'B', 'D', 'F', 'K', 'L', 'O', 'T', 'X', 'Y', '5', '6')),
    'O': frozenset(('@', 'T')),
})

def entry_price_eligible(message):
    tape = message.get('z')
    conditions = message.get('c')
    if not isinstance(tape, str) or tape not in MINUTE_PRICE_CONDITIONS:
        return False
    if not isinstance(conditions, (list, tuple)) or not conditions:
        return False
    permitted = MINUTE_PRICE_CONDITIONS[tape]
    return all(isinstance(c, str) and c in permitted for c in conditions)

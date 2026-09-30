from __future__ import annotations

import hashlib
from pathlib import Path

from mhr_engine_frozen_c import audit_split_adjustment_consistency
from mhr_input_frozen_c import (
    stock_bars, daily_close_map, fetch_corporate_actions_chain, normalize_actions,
)

EXPECTED_ENGINE_SHA = "ae248433dc6e0daab5c08e7b9e5e3733159f639c9c0c6cb615bd9958266d2b4a"
EXPECTED_INPUT_SHA = "7f6661ef1d6b37a1b66b2c0df0d12b5bfee1bb3c1b344ead9789e579293ab77d"

CASES = (
    ("ABTC", "2026-07-01", "2026-07-10"),
    ("AEMD", "2026-08-01", "2026-08-10"),
    ("AAPL", "2026-07-01", "2026-07-10"),
)

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run_network_dryrun() -> dict:
    here = Path(__file__).parent
    if _sha(here / "mhr_engine_frozen_c.py") != EXPECTED_ENGINE_SHA:
        raise RuntimeError("FREEZEC_ENGINE_HASH_MISMATCH")
    if _sha(here / "mhr_input_frozen_c.py") != EXPECTED_INPUT_SHA:
        raise RuntimeError("FREEZEC_INPUT_HASH_MISMATCH")

    results = []
    for sym, start, end in CASES:
        ca, chain = fetch_corporate_actions_chain(sym, start, end)
        acts = normalize_actions(ca, chain)
        raw = daily_close_map(stock_bars(
            sym, "1Day", start + "T00:00:00-04:00", end + "T23:59:59-04:00", "raw", start
        ))
        spl = daily_close_map(stock_bars(
            sym, "1Day", start + "T00:00:00-04:00", end + "T23:59:59-04:00", "split", start
        ))
        split_acts = [a for a in acts if a.action_type in ("split", "reverse_split")]
        aud = audit_split_adjustment_consistency(raw, spl, split_acts, end)
        results.append({
            "symbol": sym,
            "start": start,
            "end": end,
            "symbol_chain": chain,
            "actions": [
                {
                    "effective_session": a.effective_session,
                    "action_type": a.action_type,
                    "share_factor": None if a.share_factor is None else str(a.share_factor),
                    "resolvable": a.resolvable,
                }
                for a in acts
            ],
            "raw_dates": len(raw),
            "split_dates": len(spl),
            "split_audit_status": aud.status,
            "split_audit_suspicious_dates": list(aud.suspicious_dates),
        })
    return {
        "id": "MHR-1.0.0-FREEZEC-NETWORK-DRYRUN-OUTSIDE-583-B",
        "used_frozen_engine_sha256": EXPECTED_ENGINE_SHA,
        "used_frozen_input_sha256": EXPECTED_INPUT_SHA,
        "symbols_outside_583": ["ABTC", "AEMD", "AAPL"],
        "mhr_583_paths_read": False,
        "results": results,
    }

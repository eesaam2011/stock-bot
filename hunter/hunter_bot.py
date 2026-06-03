from market_sources.alpaca_assets import get_static_clean_assets
from market_sources.top_volume import get_top_volume
from market_sources.top_gainers import get_top_gainers
from market_sources.live_movers import get_live_movers
from market_sources.premarket_movers import get_premarket_movers

from hunter.hunter_filters import (
    passes_market_layer0,
    calculate_candidate_score,
)

from hunter.candidate_manager import (
    build_candidate_record,
    update_candidate_record,
)

from hunter.redis_store import (
    get_candidate,
    save_candidate,
    delete_expired_candidates,
)


# =========================================
# Hunter Bot
# =========================================

def merge_source_results(source_results):
    merged = {}

    for source_name, rows in source_results.items():
        for row in rows:
            symbol = row.get("symbol")

            if not symbol:
                continue

            symbol = str(symbol).upper().strip()

            if symbol not in merged:
                merged[symbol] = row.copy()
                merged[symbol]["sources"] = [source_name]
            else:
                merged[symbol]["sources"].append(source_name)

    return list(merged.values())


def build_candidate_reason(row):
    reasons = []

    if row.get("rvol", 0) >= 3:
        reasons.append("High RVOL")

    if row.get("volume_acceleration"):
        reasons.append("Volume Acceleration")

    if row.get("gain_pct", 0) >= 5:
        reasons.append("Strong Gain")

    if row.get("gap_pct", 0) >= 5:
        reasons.append("Strong Gap")

    if row.get("dollar_volume", 0) >= 2_000_000:
        reasons.append("Strong Dollar Volume")

    if not reasons:
        reasons.append("Hunter Candidate")

    return " + ".join(reasons)


def process_candidate(row):
    symbol = row.get("symbol")
    price = row.get("price")
    day_volume = row.get("day_volume") or row.get("premarket_volume") or 0
    dollar_volume = row.get("dollar_volume") or 0

    if not passes_market_layer0(
        price=price,
        day_volume=day_volume,
        dollar_volume=dollar_volume,
    ):
        return None

    candidate_score = calculate_candidate_score(
        rvol=row.get("rvol", 0),
        premarket_volume=row.get("premarket_volume", 0),
        dollar_volume=dollar_volume,
        gap_pct=row.get("gap_pct", 0),
        volume_acceleration=row.get("volume_acceleration", False),
        near_high=row.get("near_high", False),
        above_vwap=row.get("above_vwap", False),
        clean_price_range=True,
    )

    reason = build_candidate_reason(row)

    existing_candidate = get_candidate(symbol)

    if existing_candidate:
        candidate = update_candidate_record(
            existing_candidate,
            reason=reason,
            is_active_now=True,
        )
    else:
        candidate = build_candidate_record(
            symbol=symbol,
            source=row.get("source", "hunter"),
            candidate_score=candidate_score,
            reason=reason,
        )

    candidate["price"] = price
    candidate["day_volume"] = day_volume
    candidate["dollar_volume"] = dollar_volume
    candidate["rvol"] = row.get("rvol", 0)
    candidate["gap_pct"] = row.get("gap_pct", 0)
    candidate["gain_pct"] = row.get("gain_pct", 0)
    candidate["volume_acceleration"] = row.get("volume_acceleration", False)
    candidate["sources"] = row.get("sources", [])

    return candidate


def run_hunter_scan():
    deleted_count = delete_expired_candidates()

    print(
        f"🧹 Expired candidates deleted: {deleted_count}",
        flush=True,
    )

    clean_assets = get_static_clean_assets()

    symbols = [
        asset["symbol"]
        for asset in clean_assets
    ]

    print(
        f"✅ Clean assets loaded: {len(symbols)}",
        flush=True,
    )

    top_volume = get_top_volume(symbols)
    top_gainers = get_top_gainers(symbols)
    live_movers = get_live_movers(symbols)
    premarket_movers = get_premarket_movers(symbols)

    print(
        f"✅ Top volume: {len(top_volume)}",
        flush=True,
    )

    print(
        f"✅ Top gainers: {len(top_gainers)}",
        flush=True,
    )

    print(
        f"✅ Live movers: {len(live_movers)}",
        flush=True,
    )

    print(
        f"✅ Premarket movers: {len(premarket_movers)}",
        flush=True,
    )

    source_results = {
        "top_volume": top_volume,
        "top_gainers": top_gainers,
        "live_movers": live_movers,
        "premarket_movers": premarket_movers,
    }

    merged_candidates = merge_source_results(
        source_results
    )

    print(
        f"✅ Merged candidates: {len(merged_candidates)}",
        flush=True,
    )

    saved_count = 0

    for row in merged_candidates:
        candidate = process_candidate(row)

        if not candidate:
            continue

        if save_candidate(candidate):
            saved_count += 1

    print(
        f"✅ Candidates saved to Redis: {saved_count}",
        flush=True,
    )

    return saved_count
  

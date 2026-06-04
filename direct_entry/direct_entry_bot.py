# =========================
# Bot Clean Direct Entry
# =========================

from hunter.redis_store import get_all_candidates
import time
from market_sources.alpaca_assets import get_static_clean_assets
from market_sources.top_volume import get_top_volume
from market_sources.top_gainers import get_top_gainers
from market_sources.live_movers import get_live_movers
from market_sources.bars_data import get_daily_bars

from direct_entry.entry_data import build_entry_data
from direct_entry.entry_filters import analyze_entry_opportunity
from direct_entry.alert_manager import send_direct_entry_alert
from direct_entry.alert_tracker import (
    add_alert_to_monitoring,
    get_active_alerts,
    update_alert_tracking,
)
WATCH_DEBUG_SYMBOLS = [
    "LASE",
    "STI",
    "FOXX",
    "SPRC",
]

def get_hunter_symbols():
    candidates = get_all_candidates()

    if not candidates:
        return []

    symbols = []

    for candidate in candidates:
        symbol = candidate.get("symbol")

        if symbol:
            symbols.append(str(symbol).upper().strip())

    return symbols

def get_clean_asset_symbols():
    assets = get_static_clean_assets()

    symbols = []

    for asset in assets:
        if isinstance(asset, dict):
            symbol = asset.get("symbol")
        else:
            symbol = getattr(asset, "symbol", None)

        if symbol:
            symbols.append(str(symbol).upper().strip())

    return symbols


def get_live_opportunity_symbols(clean_symbols):
    top_volume = get_top_volume(clean_symbols)
    top_gainers = get_top_gainers(clean_symbols)
    live_movers = get_live_movers(clean_symbols)

    symbols = []

    for source_rows in [
        top_volume,
        top_gainers,
        live_movers,
    ]:
        for row in source_rows:
            symbol = row.get("symbol")

            if symbol:
                symbols.append(str(symbol).upper().strip())

    return symbols


def merge_symbols(
    hunter_symbols,
    live_symbols,
):
    merged = []

    for symbol in hunter_symbols + live_symbols:
        if symbol and symbol not in merged:
            merged.append(symbol)

    return merged


def update_active_alerts():
    active_alerts = get_active_alerts()

    if not active_alerts:
        return

    if isinstance(active_alerts, dict):
        symbols = list(active_alerts.keys())
    else:
        symbols = []

        for alert in active_alerts:
            if isinstance(alert, dict):
                symbol = alert.get("symbol")
            else:
                symbol = alert

            if symbol:
                symbols.append(str(symbol).upper().strip())

    daily_bars = get_daily_bars(
        symbols=symbols,
        limit=1,
    )

    for symbol in symbols:
        entry_data = build_entry_data(
            symbol=symbol,
            daily_bar=daily_bars.get(symbol),
        )

        if entry_data is None:
            continue

        current_price = entry_data.get("price")

        status = update_alert_tracking(
            symbol=symbol,
            current_price=current_price,
        )

        if status:
            print(
                f"📌 Alert tracking update: "
                f"{symbol} | {status}",
                flush=True,
            )
            
def run_direct_entry_scan():
    update_active_alerts()

    clean_symbols = get_clean_asset_symbols()

    hunter_symbols = get_hunter_symbols()
    live_symbols = get_live_opportunity_symbols(clean_symbols)

    symbols = merge_symbols(
        hunter_symbols=hunter_symbols,
        live_symbols=live_symbols,
    )

    print(
        f"✅ Direct Entry Sources | "
        f"Hunter: {len(hunter_symbols)} | "
        f"Live: {len(live_symbols)} | "
        f"Merged: {len(symbols)}",
        flush=True,
    )

    if not symbols:
        print("No symbols for Direct Entry scan", flush=True)
        return []

    daily_bars = get_daily_bars(
        symbols=symbols,
        limit=1,
    )

    alerts = []

    for symbol in symbols:
        entry_data = build_entry_data(
            symbol=symbol,
            daily_bar=daily_bars.get(symbol),
        )

        if entry_data is None:
            continue

        result = analyze_entry_opportunity(
            entry_data
        )

        if symbol in WATCH_DEBUG_SYMBOLS:
            print(
                f"🔎 Direct Entry Debug | "
                f"{symbol} | "
                f"ready={result.get('ready_to_alert')} | "
                f"reason={result.get('reason')}",
                flush=True,
            )

        if not result.get("ready_to_alert"):
            continue

        alert = {
            **entry_data,
            **result,
        }

        sent = send_direct_entry_alert(alert)

        if sent:
            add_alert_to_monitoring(alert)

        alerts.append(alert)

        print(
            f"🚨 DIRECT ENTRY READY: "
            f"{symbol} | "
            f"Grade: {result.get('grade')} | "
            f"Reason: {result.get('reason')}",
            flush=True,
        )

    print(
        f"Direct Entry scan finished. Alerts: {len(alerts)}",
        flush=True,
    )

    return alerts 

def run_direct_entry_loop(
    scan_interval_seconds=60,
):
    print(
        "🚀 Bot Clean Direct Entry loop started",
        flush=True,
    )

    while True:
        try:
            run_direct_entry_scan()

        except Exception as error:
            print(
                f"Direct Entry loop error: {error}",
                flush=True,
            )

        time.sleep(scan_interval_seconds)

if __name__ == "__main__":
    run_direct_entry_loop()

# =========================
# Unified Bot Runner
# Hunter + Direct Entry
# =========================

import time
from datetime import datetime

from hunter.hunter_bot import run_hunter_scan
from direct_entry.direct_entry_bot import run_direct_entry_scan

from shared.config import (
    HUNTER_SCAN_INTERVAL_OVERNIGHT,
    HUNTER_SCAN_INTERVAL_AFTER_HOURS,
    HUNTER_SCAN_INTERVAL_PREMARKET,
    HUNTER_SCAN_INTERVAL_MARKET,
)


DIRECT_ENTRY_INTERVAL_SECONDS = 60


def get_hunter_interval():
    now = datetime.utcnow()

    hour = now.hour

    # Overnight
    if hour < 9:
        return HUNTER_SCAN_INTERVAL_OVERNIGHT

    # Premarket
    if 9 <= hour < 13:
        return HUNTER_SCAN_INTERVAL_PREMARKET

    # Market Hours
    if 13 <= hour < 20:
        return HUNTER_SCAN_INTERVAL_MARKET

    # After Hours
    return HUNTER_SCAN_INTERVAL_AFTER_HOURS


def run_bot_runner():
    print(
        "🚀 Unified Bot Runner started",
        flush=True,
    )

    last_hunter_run = 0
    last_direct_entry_run = 0

    while True:
        current_time = time.time()

        try:
            hunter_interval = get_hunter_interval()

            if (
                current_time - last_hunter_run
                >= hunter_interval
            ):
                print(
                    f"🔎 Running Hunter scan "
                    f"(interval={hunter_interval}s)",
                    flush=True,
                )

                run_hunter_scan()

                last_hunter_run = current_time

        except Exception as error:
            print(
                f"Hunter error: {error}",
                flush=True,
            )

        try:
            if (
                current_time - last_direct_entry_run
                >= DIRECT_ENTRY_INTERVAL_SECONDS
            ):
                print(
                    "🎯 Running Direct Entry scan...",
                    flush=True,
                )

                run_direct_entry_scan()

                last_direct_entry_run = current_time

        except Exception as error:
            print(
                f"Direct Entry error: {error}",
                flush=True,
            )

        time.sleep(5)


if __name__ == "__main__":
    run_bot_runner()

# =========================
# Unified Bot Runner
# Hunter + Direct Entry
# =========================

import os
import time
import threading

from flask import Flask

from datetime import datetime, UTC
from hunter.hunter_bot import run_hunter_scan
from direct_entry.direct_entry_bot import run_direct_entry_scan
from direct_entry.alert_manager import send_telegram_message

from shared.config import (
    HUNTER_SCAN_INTERVAL_OVERNIGHT,
    HUNTER_SCAN_INTERVAL_AFTER_HOURS,
    HUNTER_SCAN_INTERVAL_PREMARKET,
    HUNTER_SCAN_INTERVAL_MARKET,
)
from paper_trading.paper_executor import run_paper_executor_once
from paper_trading.paper_tracker import run_paper_tracker_once

app = Flask(__name__)


@app.route("/")
def home():
    return "Hunter Direct Entry Runner Running"


def run_web_server():
    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
    
DIRECT_ENTRY_INTERVAL_SECONDS = 60


def get_hunter_interval():
    from datetime import datetime, UTC
    now = datetime.now(UTC)

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

    send_telegram_message(
        "🚀 تم تشغيل Hunter Direct Entry بنجاح\n\n"
        "✅ Hunter جاهز\n"
        "✅ Direct Entry جاهز\n"
        "✅ بدأ التشغيل بنجاح"
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
                run_paper_executor_once()
                run_paper_tracker_once()

                last_direct_entry_run = current_time

        except Exception as error:
            print(
                f"Direct Entry error: {error}",
                flush=True,
            )

        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(
        target=run_bot_runner,
        daemon=True,
    ).start()

    run_web_server()

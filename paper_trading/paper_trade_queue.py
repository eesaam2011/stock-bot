import os
import json
import uuid
import requests
from datetime import datetime, UTC


UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

PAPER_TRADE_QUEUE_KEY = "direct_entry_paper_trade_queue"

ALLOWED_PAPER_GRADES = ["A++", "++A", "CONFIRMED_BREAKOUT"]

def redis_ready():
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def redis_headers():
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }


def queue_paper_trade(alert):
    if not redis_ready():
        print("❌ Paper trade queue skipped: Redis not ready", flush=True)
        return False

    grade = alert.get("grade")

    if grade not in ALLOWED_PAPER_GRADES:
        return False

    symbol = alert.get("symbol")
    price = alert.get("price")

    if not symbol or not price:
        print("❌ Paper trade queue skipped: missing symbol or price", flush=True)
        return False

    safe_alert = dict(alert)
    safe_alert.pop("bars", None)

    trade_request = {
        "trade_id": str(uuid.uuid4()),
        "symbol": symbol,
        "grade": grade,
        "price": price,
        "stop_loss": alert.get("stop_loss"),
        "target1": alert.get("target1"),
        "target2": alert.get("target2"),
        "resistance": alert.get("resistance") or alert.get("confirmed_resistance"),
        "created_at": datetime.now(UTC).isoformat(),
        "source": "Bot Clean Direct Entry",
        "status": "queued",
        "alert": safe_alert,
    }

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/rpush/{PAPER_TRADE_QUEUE_KEY}"
        response = requests.post(
            url,
            headers=redis_headers(),
            data=json.dumps(json.dumps(trade_request)),
            timeout=10,
        )

        if response.status_code not in [200, 201]:
            print(
                f"❌ Paper trade queue error: {response.status_code} {response.text}",
                flush=True,
            )
            return False

        print(
            f"📥 Paper trade queued: {symbol} | {grade}",
            flush=True,
        )
        return True

    except Exception as e:
        print(
            f"❌ Paper trade queue exception: {e}",
            flush=True,
        )
        return False

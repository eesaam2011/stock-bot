import os
import json
import time
import threading
from datetime import datetime, timezone

import websocket

WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"


def get_env(*names):
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


API_KEY = get_env(
    "APCA_API_KEY_ID",
    "ALPACA_API_KEY",
    "ALPACA_KEY_ID",
)

API_SECRET = get_env(
    "APCA_API_SECRET_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
)


def parse_iso_utc(value):
    if not value:
        return None
    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def on_open(ws):
    print("✅ WebSocket connected.", flush=True)
    print("🔐 Sending Alpaca authentication...", flush=True)

    ws.send(json.dumps({
        "action": "auth",
        "key": API_KEY,
        "secret": API_SECRET,
    }))


def on_message(ws, raw):
    received_at = datetime.now(timezone.utc)

    try:
        payload = json.loads(raw)
    except Exception:
        print(f"⚠️ Non-JSON message: {raw}", flush=True)
        return

    messages = payload if isinstance(payload, list) else [payload]

    for msg in messages:
        if not isinstance(msg, dict):
            print(f"ℹ️ Message: {msg}", flush=True)
            continue

        msg_type = msg.get("T")

        if msg_type == "success":
            message = msg.get("msg")
            print(f"✅ Alpaca success: {message}", flush=True)

            if message == "authenticated":
                print('📡 Sending subscription: news=["*"]', flush=True)
                ws.send(json.dumps({
                    "action": "subscribe",
                    "news": ["*"],
                }))

        elif msg_type == "subscription":
            news_subs = msg.get("news")
            print(f"✅ Subscription confirmed: news={news_subs}", flush=True)

            if isinstance(news_subs, list) and "*" in news_subs:
                print(
                    "🎯 CONFIRMED: this single WebSocket is subscribed to the full Alpaca news feed.",
                    flush=True,
                )
                print("📰 Waiting for live news...", flush=True)

        elif msg_type == "error":
            print(
                f"❌ Alpaca error | code={msg.get('code')} | msg={msg.get('msg')}",
                flush=True,
            )

        elif msg_type == "n":
            created_raw = msg.get("created_at")
            created_at = parse_iso_utc(created_raw)

            latency = None
            if created_at is not None:
                latency = (received_at - created_at).total_seconds()

            print("\n" + "=" * 90, flush=True)
            print("📰 LIVE NEWS RECEIVED", flush=True)
            print(f"ID: {msg.get('id')}", flush=True)
            print(f"Source: {msg.get('source')}", flush=True)
            print(f"Symbols: {msg.get('symbols')}", flush=True)
            print(f"Headline: {msg.get('headline')}", flush=True)
            print(f"Created at:  {created_raw}", flush=True)
            print(f"Received at: {received_at.isoformat()}", flush=True)

            if latency is not None:
                print(f"Latency: {latency:.3f} sec", flush=True)
            else:
                print("Latency: unavailable", flush=True)

            print("=" * 90, flush=True)

        else:
            print(f"ℹ️ Other message: {msg}", flush=True)


def on_error(ws, error):
    print(f"❌ WebSocket error: {error}", flush=True)


def on_close(ws, status_code, close_msg):
    print(
        f"🛑 WebSocket closed | code={status_code} | message={close_msg}",
        flush=True,
    )


def main():
    if not API_KEY or not API_SECRET:
        print("❌ Missing Alpaca credentials.", flush=True)
        print(
            "Expected one of: "
            "APCA_API_KEY_ID / ALPACA_API_KEY / ALPACA_KEY_ID "
            "and APCA_API_SECRET_KEY / ALPACA_SECRET_KEY / ALPACA_API_SECRET",
            flush=True,
        )
        raise SystemExit(1)

    print("🔌 Connecting to Alpaca News WebSocket...", flush=True)
    print(f"URL: {WS_URL}", flush=True)

    websocket.enableTrace(False)

    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    ws.run_forever(
        ping_interval=20,
        ping_timeout=10,
        reconnect=5,
    )


if __name__ == "__main__":
    main()

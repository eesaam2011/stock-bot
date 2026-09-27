import os
import json
import time
import threading
from datetime import datetime, timezone

import websocket
from flask import Flask, jsonify, Response

WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"

app = Flask(__name__)

state_lock = threading.Lock()
state = {
    "ws_connected": False,
    "authenticated": False,
    "subscribed_all_news": False,
    "last_error": None,
    "last_connect_at": None,
    "last_news_at": None,
    "news_count": 0,
    "benzinga_count": 0,
    "last_news": None,
}


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


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


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


def safe_state():
    with state_lock:
        return json.loads(json.dumps(state))


@app.route("/")
def home():
    s = safe_state()
    ok = bool(s["authenticated"] and s["subscribed_all_news"])

    status = "✅ READY" if ok else "⏳ STARTING / NOT CONFIRMED"

    last = s.get("last_news") or {}
    last_block = ""
    if last:
        last_block = f"""
        <hr>
        <h3>Last live news</h3>
        <p><b>Source:</b> {last.get('source')}</p>
        <p><b>Symbols:</b> {last.get('symbols')}</p>
        <p><b>Headline:</b> {last.get('headline')}</p>
        <p><b>Created:</b> {last.get('created_at')}</p>
        <p><b>Received:</b> {last.get('received_at')}</p>
        <p><b>Latency:</b> {last.get('latency_sec')} sec</p>
        """

    html = f"""
    <!doctype html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Alpaca News Probe</title>
      <style>
        body {{
          font-family: Arial, sans-serif;
          max-width: 760px;
          margin: 30px auto;
          padding: 0 18px;
          line-height: 1.55;
        }}
        .box {{
          border: 1px solid #aaa;
          border-radius: 12px;
          padding: 18px;
        }}
        code {{ word-break: break-word; }}
      </style>
    </head>
    <body>
      <h1>Alpaca News WebSocket Probe</h1>
      <div class="box">
        <h2>{status}</h2>
        <p><b>WebSocket connected:</b> {s['ws_connected']}</p>
        <p><b>Authenticated:</b> {s['authenticated']}</p>
        <p><b>Subscribed to news ["*"]:</b> {s['subscribed_all_news']}</p>
        <p><b>Total news received:</b> {s['news_count']}</p>
        <p><b>Benzinga news received:</b> {s['benzinga_count']}</p>
        <p><b>Last connection:</b> {s['last_connect_at']}</p>
        <p><b>Last error:</b> {s['last_error']}</p>
        {last_block}
      </div>
      <p>JSON status: <a href="/status">/status</a></p>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.route("/status")
def status():
    s = safe_state()
    s["ok"] = bool(s["authenticated"] and s["subscribed_all_news"])
    return jsonify(s)


@app.route("/health")
def health():
    return jsonify({"ok": True})


def on_open(ws):
    with state_lock:
        state["ws_connected"] = True
        state["last_connect_at"] = iso_now()
        state["last_error"] = None

    print("✅ WebSocket connected.", flush=True)
    print("🔐 Sending Alpaca authentication...", flush=True)

    ws.send(json.dumps({
        "action": "auth",
        "key": API_KEY,
        "secret": API_SECRET,
    }))


def on_message(ws, raw):
    received_at = utc_now()

    try:
        payload = json.loads(raw)
    except Exception:
        print(f"⚠️ Non-JSON message: {raw}", flush=True)
        return

    messages = payload if isinstance(payload, list) else [payload]

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("T")

        if msg_type == "success":
            message = msg.get("msg")
            print(f"✅ Alpaca success: {message}", flush=True)

            if message == "authenticated":
                with state_lock:
                    state["authenticated"] = True

                print('📡 Sending subscription: news=["*"]', flush=True)
                ws.send(json.dumps({
                    "action": "subscribe",
                    "news": ["*"],
                }))

        elif msg_type == "subscription":
            news_subs = msg.get("news") or []
            print(f"✅ Subscription confirmed: news={news_subs}", flush=True)

            if "*" in news_subs:
                with state_lock:
                    state["subscribed_all_news"] = True

                print(
                    "🎯 CONFIRMED: this single WebSocket is subscribed to the full Alpaca news feed.",
                    flush=True,
                )
                print("📰 Waiting for live news...", flush=True)

        elif msg_type == "error":
            error_text = f"code={msg.get('code')} msg={msg.get('msg')}"
            with state_lock:
                state["last_error"] = error_text

            print(f"❌ Alpaca error | {error_text}", flush=True)

        elif msg_type == "n":
            created_raw = msg.get("created_at")
            created_at = parse_iso_utc(created_raw)

            latency = None
            if created_at is not None:
                latency = (received_at - created_at).total_seconds()

            source = msg.get("source")

            news_record = {
                "id": msg.get("id"),
                "source": source,
                "symbols": msg.get("symbols"),
                "headline": msg.get("headline"),
                "created_at": created_raw,
                "received_at": received_at.isoformat(),
                "latency_sec": round(latency, 3) if latency is not None else None,
            }

            with state_lock:
                state["news_count"] += 1
                if str(source).lower() == "benzinga":
                    state["benzinga_count"] += 1
                state["last_news_at"] = received_at.isoformat()
                state["last_news"] = news_record

            print("\n" + "=" * 90, flush=True)
            print("📰 LIVE NEWS RECEIVED", flush=True)
            print(f"ID: {news_record['id']}", flush=True)
            print(f"Source: {news_record['source']}", flush=True)
            print(f"Symbols: {news_record['symbols']}", flush=True)
            print(f"Headline: {news_record['headline']}", flush=True)
            print(f"Created at:  {news_record['created_at']}", flush=True)
            print(f"Received at: {news_record['received_at']}", flush=True)
            print(f"Latency: {news_record['latency_sec']} sec", flush=True)
            print("=" * 90, flush=True)


def on_error(ws, error):
    with state_lock:
        state["last_error"] = str(error)

    print(f"❌ WebSocket error: {error}", flush=True)


def on_close(ws, status_code, close_msg):
    with state_lock:
        state["ws_connected"] = False
        state["authenticated"] = False
        state["subscribed_all_news"] = False

    print(
        f"🛑 WebSocket closed | code={status_code} | message={close_msg}",
        flush=True,
    )


def websocket_worker():
    if not API_KEY or not API_SECRET:
        msg = (
            "Missing Alpaca credentials. Expected ALPACA_API_KEY + "
            "ALPACA_SECRET_KEY (or supported APCA aliases)."
        )
        with state_lock:
            state["last_error"] = msg
        print(f"❌ {msg}", flush=True)
        return

    while True:
        try:
            print("🔌 Connecting to Alpaca News WebSocket...", flush=True)
            print(f"URL: {WS_URL}", flush=True)

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
            )

        except Exception as exc:
            with state_lock:
                state["last_error"] = f"{type(exc).__name__}: {exc}"
            print(f"❌ WebSocket worker failed: {exc}", flush=True)

        print("↻ Reconnecting in 5 seconds...", flush=True)
        time.sleep(5)


def start_ws_thread():
    t = threading.Thread(
        target=websocket_worker,
        name="alpaca-news-ws",
        daemon=True,
    )
    t.start()


if __name__ == "__main__":
    start_ws_thread()

    port = int(os.getenv("PORT", "10000"))
    print(f"🌐 Starting HTTP server on port {port}", flush=True)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )

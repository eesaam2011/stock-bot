import os
import json
import time
import requests
import alpaca_trade_api as tradeapi
from datetime import datetime, UTC


APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.getenv(
    "APCA_API_BASE_URL",
    "https://paper-api.alpaca.markets",
)

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

PAPER_TRADE_QUEUE_KEY = "direct_entry_paper_trade_queue"
PAPER_ACTIVE_TRADES_KEY = "paper_active_trades"

PAPER_TRADE_DOLLARS = float(os.getenv("PAPER_TRADE_DOLLARS", "100"))
DEFAULT_STOP_LOSS_PCT = 1.5

PAPER_TRADING_ENABLED = (
    os.getenv("PAPER_TRADING_ENABLED", "false").lower() == "true"
)


api = tradeapi.REST(
    APCA_API_KEY_ID,
    APCA_API_SECRET_KEY,
    APCA_API_BASE_URL,
    api_version="v2",
)


def redis_ready():
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def redis_headers():
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }


def pop_paper_trade_request():
    if not redis_ready():
        print("❌ Paper executor skipped: Redis not ready", flush=True)
        return None

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/lpop/{PAPER_TRADE_QUEUE_KEY}"
        response = requests.get(
            url,
            headers=redis_headers(),
            timeout=10,
        )

        if response.status_code != 200:
            print(
                f"❌ Paper queue pop error: {response.status_code} {response.text}",
                flush=True,
            )
            return None

        data = response.json()
        result = data.get("result")

        if not result:
            return None

        return json.loads(result)

    except Exception as e:
        print(
            f"❌ Paper queue pop exception: {e}",
            flush=True,
        )
        return None


def save_active_paper_trade(trade):
    if not redis_ready():
        return False

    symbol = trade.get("symbol")

    if not symbol:
        return False

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/hset/{PAPER_ACTIVE_TRADES_KEY}/{symbol}"
        response = requests.post(
            url,
            headers=redis_headers(),
            data=json.dumps(json.dumps(trade)),
            timeout=10,
        )

        if response.status_code not in [200, 201]:
            print(
                f"❌ Save paper trade error: {response.status_code} {response.text}",
                flush=True,
            )
            return False

        return True

    except Exception as e:
        print(
            f"❌ Save paper trade exception: {e}",
            flush=True,
        )
        return False


def get_existing_position(symbol):
    try:
        return api.get_position(symbol)
    except Exception:
        return None


def calculate_qty(price):
    if not price or price <= 0:
        return 0

    qty = int(PAPER_TRADE_DOLLARS / price)

    if qty < 1:
        qty = 1

    return qty


def calculate_stop_loss(alert_price, stop_loss):
    if stop_loss:
        return round(float(stop_loss), 4)

    fallback_stop = alert_price * (1 - DEFAULT_STOP_LOSS_PCT / 100)
    return round(fallback_stop, 4)


def wait_for_order_fill(order_id, max_wait_seconds=20):
    started_at = time.time()

    while time.time() - started_at <= max_wait_seconds:
        try:
            order = api.get_order(order_id)

            if order.status == "filled":
                return order

            if order.status in ["canceled", "expired", "rejected"]:
                print(
                    f"❌ Paper buy order not filled. Status: {order.status}",
                    flush=True,
                )
                return None

        except Exception as e:
            print(
                f"❌ Paper fill check error: {e}",
                flush=True,
            )
            return None

        time.sleep(2)

    print("⚠️ Paper buy order fill timeout", flush=True)
    return None


def submit_market_buy(symbol, qty):
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force="day",
        )

        print(
            f"🟢 Paper BUY submitted: {symbol} | qty={qty}",
            flush=True,
        )
        return order

    except Exception as e:
        print(
            f"❌ Paper BUY error: {symbol} | {e}",
            flush=True,
        )
        return None


def submit_stop_loss(symbol, qty, stop_price):
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            time_in_force="gtc",
            stop_price=stop_price,
        )

        print(
            f"🛑 Paper STOP submitted: {symbol} | qty={qty} | stop={stop_price}",
            flush=True,
        )
        return order

    except Exception as e:
        print(
            f"❌ Paper STOP error: {symbol} | {e}",
            flush=True,
        )
        return None

def send_paper_trade_opened_message(active_trade):
    try:
        from direct_entry.alert_manager import send_telegram_message, format_price

        symbol = active_trade.get("symbol")
        grade = active_trade.get("grade")
        qty = active_trade.get("qty")
        entry_price = active_trade.get("entry_price")
        stop_price = active_trade.get("stop_price")
        target1 = active_trade.get("target1")
        target2 = active_trade.get("target2")

        message = (
            "🧪 تم فتح صفقة تجريبية Paper\n\n"
            f"السهم: {symbol}\n"
            f"القوة: {grade}\n"
            f"الكمية: {qty}\n"
            f"الدخول: {format_price(entry_price)}\n"
            f"وقف الخسارة: {format_price(stop_price)}\n"
            f"الهدف الأول: {format_price(target1)}\n"
            f"الهدف الثاني: {format_price(target2)}\n\n"
            "✅ تم الشراء التجريبي\n"
            "🛑 تم وضع وقف الخسارة مباشرة"
        )

        send_telegram_message(message)

    except Exception as e:
        print(
            f"❌ Paper opened telegram error: {e}",
            flush=True,
        )
        
def execute_paper_trade_request(trade_request):
    symbol = str(trade_request.get("symbol", "")).upper().strip()
    grade = trade_request.get("grade")
    alert_price = float(trade_request.get("price") or 0)
    stop_loss = trade_request.get("stop_loss")

    if not symbol or alert_price <= 0:
        return False

    existing_position = get_existing_position(symbol)

    if existing_position:
        print(
            f"⏳ Paper trade skipped. Existing position: {symbol}",
            flush=True,
        )
        return False

    qty = calculate_qty(alert_price)

    if qty <= 0:
        return False

    stop_price = calculate_stop_loss(alert_price, stop_loss)

    buy_order = submit_market_buy(symbol, qty)

    if not buy_order:
        return False

    filled_order = wait_for_order_fill(buy_order.id)

    if not filled_order:
        return False

    filled_price = float(filled_order.filled_avg_price or alert_price)

    stop_order = submit_stop_loss(
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
    )

    if not stop_order:
        return False

    active_trade = {
        "trade_id": trade_request.get("trade_id"),
        "symbol": symbol,
        "grade": grade,
        "qty": qty,
        "entry_price": filled_price,
        "alert_price": alert_price,
        "stop_price": stop_price,
        "buy_order_id": buy_order.id,
        "stop_order_id": stop_order.id,
        "target1": trade_request.get("target1"),
        "target2": trade_request.get("target2"),
        "resistance": trade_request.get("resistance"),
        "opened_at": datetime.now(UTC).isoformat(),
        "status": "active",
        "highest_price": filled_price,
        "source": "Paper Trade Bridge",
    }

    save_active_paper_trade(active_trade)
    send_paper_trade_opened_message(active_trade)

    print(
        f"✅ Paper trade active: {symbol} | entry={filled_price} | stop={stop_price}",
        flush=True,
    )

    return True


def run_paper_executor_once():
    if not PAPER_TRADING_ENABLED:
        return False

    trade_request = pop_paper_trade_request()

    if not trade_request:
        return False

    return execute_paper_trade_request(trade_request) 

import os
import json
import requests
import alpaca_trade_api as tradeapi
from datetime import datetime, UTC


PAPER_TRADING_ENABLED = (
    os.getenv("PAPER_TRADING_ENABLED", "false").lower() == "true"
)

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.getenv(
    "APCA_API_BASE_URL",
    "https://paper-api.alpaca.markets",
)

PAPER_ACTIVE_TRADES_KEY = "paper_active_trades"

TRAILING_STOP_PCT = 1.5


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


def get_all_active_paper_trades():
    if not redis_ready():
        return {}

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/hgetall/{PAPER_ACTIVE_TRADES_KEY}"
        response = requests.get(
            url,
            headers=redis_headers(),
            timeout=10,
        )

        if response.status_code != 200:
            print(
                f"❌ Paper tracker hgetall error: {response.status_code} {response.text}",
                flush=True,
            )
            return {}

        result = response.json().get("result") or []

        trades = {}

        for i in range(0, len(result), 2):
            symbol = result[i]
            raw_trade = result[i + 1]

            try:
                trades[symbol] = json.loads(raw_trade)
            except Exception:
                continue

        return trades

    except Exception as e:
        print(
            f"❌ Paper tracker hgetall exception: {e}",
            flush=True,
        )
        return {}


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
                f"❌ Paper tracker save error: {response.status_code} {response.text}",
                flush=True,
            )
            return False

        return True

    except Exception as e:
        print(
            f"❌ Paper tracker save exception: {e}",
            flush=True,
        )
        return False

  def get_current_price(symbol):
    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)
    except Exception as e:
        print(
            f"❌ Paper tracker price error: {symbol} | {e}",
            flush=True,
        )
        return None


def cancel_old_stop_order(stop_order_id):
    if not stop_order_id:
        return True

    try:
        api.cancel_order(stop_order_id)
        return True
    except Exception as e:
        print(
            f"⚠️ Could not cancel old stop order: {stop_order_id} | {e}",
            flush=True,
        )
        return False


def submit_new_stop_order(symbol, qty, stop_price):
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            time_in_force="gtc",
            stop_price=round(float(stop_price), 4),
        )

        print(
            f"🛑 New Paper STOP submitted: {symbol} | stop={stop_price}",
            flush=True,
        )

        return order

    except Exception as e:
        print(
            f"❌ New Paper STOP error: {symbol} | {e}",
            flush=True,
        )
        return None


def send_stop_raised_message(symbol, old_stop, new_stop, reason):
    try:
        from direct_entry.alert_manager import send_telegram_message, format_price

        message = (
            "🔼 تم رفع وقف الصفقة التجريبية Paper\n\n"
            f"السهم: {symbol}\n"
            f"الوقف السابق: {format_price(old_stop)}\n"
            f"الوقف الجديد: {format_price(new_stop)}\n"
            f"السبب: {reason}"
        )

        send_telegram_message(message)

    except Exception as e:
        print(
            f"❌ Paper stop raise telegram error: {e}",
            flush=True,
        )


def record_stop_raise_event(trade, old_stop, new_stop, reason):
    events = trade.get("stop_raise_events") or []

    events.append(
        {
            "time": datetime.now(UTC).isoformat(),
            "old_stop": old_stop,
            "new_stop": new_stop,
            "reason": reason,
        }
    )

    trade["stop_raise_events"] = events
    trade["stop_raise_count"] = len(events)

    return trade

def calculate_new_stop(trade, current_price):
    entry_price = float(trade.get("entry_price") or 0)
    current_stop = float(trade.get("stop_price") or 0)
    target1 = trade.get("target1")
    target2 = trade.get("target2")
    highest_price = float(trade.get("highest_price") or entry_price)

    if not entry_price or not current_price:
        return None, None

    target1 = float(target1) if target1 else None
    target2 = float(target2) if target2 else None

    if current_price > highest_price:
        highest_price = current_price
        trade["highest_price"] = highest_price

    if (
        target1
        and current_price >= target1
        and not trade.get("target1_hit")
        and current_stop < entry_price
    ):
        trade["target1_hit"] = True
        trade["target1_hit_at"] = datetime.now(UTC).isoformat()
        return entry_price, "Target 1 hit - stop raised to entry"

    if (
        target2
        and current_price >= target2
        and not trade.get("target2_hit")
        and target1
        and current_stop < target1
    ):
        trade["target2_hit"] = True
        trade["target2_hit_at"] = datetime.now(UTC).isoformat()
        return target1, "Target 2 hit - stop raised to Target 1"

    if (
        target2
        and highest_price > target2
        and trade.get("target2_hit")
    ):
        trailing_stop = highest_price * (1 - TRAILING_STOP_PCT / 100)

        if trailing_stop > current_stop:
            return trailing_stop, "Trailing stop after Target 2"

    return None, None

def raise_stop_if_needed(trade, current_price):
    symbol = trade.get("symbol")
    qty = trade.get("qty")
    old_stop = float(trade.get("stop_price") or 0)
    old_stop_order_id = trade.get("stop_order_id")

    new_stop, reason = calculate_new_stop(
        trade=trade,
        current_price=current_price,
    )

    if not new_stop or not reason:
        return trade

    new_stop = round(float(new_stop), 4)

    if new_stop <= old_stop:
        return trade

    canceled = cancel_old_stop_order(old_stop_order_id)

    if not canceled:
        return trade

    new_stop_order = submit_new_stop_order(
        symbol=symbol,
        qty=qty,
        stop_price=new_stop,
    )

    if not new_stop_order:
        return trade

    trade = record_stop_raise_event(
        trade=trade,
        old_stop=old_stop,
        new_stop=new_stop,
        reason=reason,
    )

    trade["stop_price"] = new_stop
    trade["stop_order_id"] = new_stop_order.id
    trade["updated_at"] = datetime.now(UTC).isoformat()

    save_active_paper_trade(trade)

    send_stop_raised_message(
        symbol=symbol,
        old_stop=old_stop,
        new_stop=new_stop,
        reason=reason,
    )

    return trade 

def update_trade_metrics(trade, current_price):
    entry_price = float(trade.get("entry_price") or 0)

    if not entry_price or not current_price:
        return trade

    highest_price = float(trade.get("highest_price") or entry_price)
    lowest_price = float(trade.get("lowest_price") or entry_price)

    if current_price > highest_price:
        highest_price = current_price

    if current_price < lowest_price:
        lowest_price = current_price

    max_gain_pct = ((highest_price - entry_price) / entry_price) * 100
    max_drawdown_pct = ((lowest_price - entry_price) / entry_price) * 100

    trade["highest_price"] = highest_price
    trade["lowest_price"] = lowest_price
    trade["max_gain_pct"] = round(max_gain_pct, 2)
    trade["max_drawdown_pct"] = round(max_drawdown_pct, 2)
    trade["last_price"] = current_price
    trade["last_checked_at"] = datetime.now(UTC).isoformat()

    return trade


def track_single_trade(trade):
    symbol = trade.get("symbol")

    if not symbol:
        return

    current_price = get_current_price(symbol)

    if not current_price:
        return

    trade = update_trade_metrics(
        trade=trade,
        current_price=current_price,
    )

    trade = raise_stop_if_needed(
        trade=trade,
        current_price=current_price,
    )

    save_active_paper_trade(trade)


def run_paper_tracker_once():
    if not PAPER_TRADING_ENABLED:
        return False

    active_trades = get_all_active_paper_trades()

    if not active_trades:
        return False

    for symbol, trade in active_trades.items():
        try:
            track_single_trade(trade)
        except Exception as e:
            print(
                f"❌ Paper tracker error: {symbol} | {e}",
                flush=True,
            )

    return True

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
                f"❌ Paper report hgetall error: {response.status_code} {response.text}",
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
            f"❌ Paper report hgetall exception: {e}",
            flush=True,
        )
        return {}


def get_current_price(symbol, fallback_price=None):
    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)
    except Exception:
        return fallback_price


def format_money(value):
    try:
        value = float(value)
        return f"${value:.2f}"
    except Exception:
        return "-"


def format_pct(value):
    try:
        value = float(value)
        return f"{value:.2f}%"
    except Exception:
        return "-"


def format_price_safe(value):
    try:
        from direct_entry.alert_manager import format_price

        return format_price(value)
    except Exception:
        try:
            return f"{float(value):.4f}"
        except Exception:
            return "-"


def calculate_trade_pl(trade, current_price):
    entry_price = float(trade.get("entry_price") or 0)
    qty = float(trade.get("qty") or 0)

    if not entry_price or not qty or not current_price:
        return 0.0, 0.0

    pl_dollars = (current_price - entry_price) * qty
    pl_pct = ((current_price - entry_price) / entry_price) * 100

    return round(pl_dollars, 2), round(pl_pct, 2)


def build_stop_events_text(trade):
    events = trade.get("stop_raise_events") or []

    if not events:
        return "لا يوجد"

    lines = []

    for event in events:
        old_stop = format_price_safe(event.get("old_stop"))
        new_stop = format_price_safe(event.get("new_stop"))
        reason = event.get("reason", "-")
        event_time = event.get("time", "-")

        lines.append(
            f"- {event_time}\n"
            f"  من {old_stop} إلى {new_stop}\n"
            f"  السبب: {reason}"
        )

    return "\n".join(lines)


def build_single_trade_report(symbol, trade):
    entry_price = float(trade.get("entry_price") or 0)
    qty = float(trade.get("qty") or 0)
    last_price = trade.get("last_price") or entry_price
    current_price = get_current_price(symbol, fallback_price=last_price)

    if trade.get("status") == "closed":
    pl_dollars = float(
        trade.get("paper_pl_dollars") or 0
    )

    pl_pct = float(
        trade.get("paper_pl_pct") or 0
    )

else:
    pl_dollars, pl_pct = calculate_trade_pl(
        trade=trade,
        current_price=current_price,
    )
    
    max_gain_pct = trade.get("max_gain_pct", 0)
    max_drawdown_pct = trade.get("max_drawdown_pct", 0)

    target1_hit = "نعم" if trade.get("target1_hit") else "لا"
    target2_hit = "نعم" if trade.get("target2_hit") else "لا"

    stop_events_text = build_stop_events_text(trade)

    report = (
        f"━━━━━━━━━━━━━━\n"
        f"📌 السهم: {symbol}\n"
        f"النوع: {trade.get('grade', '-')}\n"
        f"الحالة: {trade.get('status', '-')}\n"
        f"وقت الخروج: {trade.get('exit_time', '-')}\n"
        f"سعر الخروج: {format_price_safe(trade.get('exit_price'))}\n"
        f"سبب الخروج: {trade.get('exit_reason', '-')}\n"
        f"وقت الدخول: {trade.get('opened_at', '-')}\n\n"
        f"الدخول: {format_price_safe(entry_price)}\n"
        f"آخر سعر/سعر التقييم: {format_price_safe(current_price)}\n"
        f"الكمية: {qty:.0f}\n\n"
        f"وقف البداية/الحالي: {format_price_safe(trade.get('stop_price'))}\n"
        f"Target 1: {format_price_safe(trade.get('target1'))}\n"
        f"Target 2: {format_price_safe(trade.get('target2'))}\n\n"
        f"أعلى سعر: {format_price_safe(trade.get('highest_price'))}\n"
        f"أدنى سعر: {format_price_safe(trade.get('lowest_price'))}\n"
        f"أقصى ربح: {format_pct(max_gain_pct)}\n"
        f"أقصى تراجع: {format_pct(max_drawdown_pct)}\n\n"
        f"وصل T1: {target1_hit}\n"
        f"وصل T2: {target2_hit}\n"
        f"عدد مرات رفع الوقف: {trade.get('stop_raise_count', 0)}\n\n"
        f"تفاصيل رفع الوقف:\n{stop_events_text}\n\n"
        f"الربح/الخسارة التجريبية: {format_money(pl_dollars)} "
        f"({format_pct(pl_pct)})\n"
    )

    return report, pl_dollars, pl_pct


def build_daily_paper_report():
    trades = get_all_active_paper_trades()

    if not trades:
        return (
            "📊 تقرير Paper Trading اليومي\n\n"
            "لا توجد صفقات تجريبية نشطة أو محفوظة لهذا اليوم."
        )

    total_pl = 0.0
    winners = 0
    losers = 0
    flat = 0

    trade_reports = []

    for symbol, trade in trades.items():
        single_report, pl_dollars, _ = build_single_trade_report(
            symbol=symbol,
            trade=trade,
        )

        total_pl += pl_dollars

        if pl_dollars > 0:
            winners += 1
        elif pl_dollars < 0:
            losers += 1
        else:
            flat += 1

        trade_reports.append(single_report)

    total_trades = len(trades)

    summary = (
        "📊 تقرير Paper Trading اليومي التفصيلي\n\n"
        f"عدد الصفقات: {total_trades}\n"
        f"الرابحة: {winners}\n"
        f"الخاسرة: {losers}\n"
        f"بدون تغير: {flat}\n"
        f"صافي الربح/الخسارة التجريبي: {format_money(total_pl)}\n\n"
    )

    return summary + "\n".join(trade_reports)


def send_daily_paper_report():
    try:
        from direct_entry.alert_manager import send_telegram_message

        message = build_daily_paper_report()
        send_telegram_message(message)

        print(
            "📊 Daily Paper report sent",
            flush=True,
        )

        return True

    except Exception as e:
        print(
            f"❌ Daily Paper report error: {e}",
            flush=True,
        )
        return False

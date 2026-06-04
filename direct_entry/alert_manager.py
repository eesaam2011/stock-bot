# =========================
# Direct Entry Alert Manager
# =========================

import os
import time
import requests


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ALERT_COOLDOWN_SECONDS = 60 * 30

sent_direct_entry_alerts = {}


def telegram_ready():
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def format_number(value, decimals=2):
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return "N/A"


def get_alert_key(alert):
    symbol = str(alert.get("symbol", "")).upper().strip()
    grade = str(alert.get("grade", "")).upper().strip()

    return f"{symbol}:{grade}"


def can_send_alert(alert):
    alert_key = get_alert_key(alert)
    current_time = time.time()
    last_sent_time = sent_direct_entry_alerts.get(alert_key)

    if last_sent_time is None:
        return True

    return bool(
        current_time - last_sent_time >= ALERT_COOLDOWN_SECONDS
    )


def mark_alert_sent(alert):
    alert_key = get_alert_key(alert)

    sent_direct_entry_alerts[alert_key] = time.time()


def get_entry_title(grade):
    if grade == "A++":
        return "⚡ دخول مباشر مؤكد"

    if grade == "A+":
        return "🚀 دخول مباشر قوي"

    return "🎯 دخول مباشر"


def estimate_target_time(alert):
    instant_rvol = float(alert.get("instant_rvol", 0) or 0)
    move_3m = float(alert.get("move_3m", 0) or 0)
    move_5m = float(alert.get("move_5m", 0) or 0)
    close_position = float(alert.get("close_position", 0) or 0)
    resistance_distance = float(
        alert.get("resistance_distance_pct", 0) or 0
    )
    volume_acceleration = bool(
        alert.get("volume_acceleration", False)
    )

    if (
        instant_rvol >= 4
        and move_3m >= 0.80
        and volume_acceleration
        and close_position >= 0.75
    ):
        return "5 - 15 دقيقة"

    if (
        instant_rvol >= 3
        and move_5m >= 1.00
        and volume_acceleration
        and resistance_distance <= 2.0
    ):
        return "15 - 30 دقيقة"

    return "30 - 60 دقيقة"

def format_price(value):
    try:
        value = float(value)
    except Exception:
        return "N/A"

    if value < 1:
        return f"{value:.4f}"

    if value < 10:
        return f"{value:.3f}"

    return f"{value:.2f}"

def build_direct_entry_message(alert):
    symbol = alert.get("symbol", "N/A")
    grade = alert.get("grade", "A")
    title = get_entry_title(grade)

    price = float(alert.get("price", 0) or 0)

    stop_loss = price * 0.985
    target_1 = price * 1.02
    target_2 = price * 1.04

    message = (
        f"🎯 بوت الدخول المباشر\n\n"
        f"{title}\n\n"
        f"السهم: {symbol}\n"
        f"التقييم: {grade}\n"
        f"سعر الدخول: {format_price(price)}\n\n"
        f"القوة اللحظية:\n"
        f"RVOL: {format_number(alert.get('instant_rvol'), 2)}\n"
        f"حركة 3 دقائق: {format_number(alert.get('move_3m'), 2)}%\n"
        f"حركة 5 دقائق: {format_number(alert.get('move_5m'), 2)}%\n\n"
        f"المقاومة:\n"
        f"المستوى: {format_price(alert.get('nearest_resistance'))}\n"
        f"البعد عن المقاومة: "
        f"{format_number(alert.get('resistance_distance_pct'), 2)}%\n\n"
        f"المدة المتوقعة للهدف:\n"
        f"{estimate_target_time(alert)}\n\n"
        f"الخطة:\n"
        f"وقف الخسارة: {format_price(stop_loss)}\n"
        f"الهدف الأول: {format_price(target_1)}\n"
        f"الهدف الثاني: {format_price(target_2)}\n\n"
        f"سبب التنبيه:\n"
        f"{alert.get('reason', 'تم تأكيد الدخول المباشر')}\n\n"
        f"TradingView:\n"
        f"https://www.tradingview.com/chart/?symbol={symbol}"
    )

    return message

def send_telegram_message(message):
    if not telegram_ready():
        print(
            "Telegram is not ready. Missing token or chat id.",
            flush=True,
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=10,
        )

        return bool(response.status_code == 200)

    except Exception as error:
        print(
            f"Telegram send error: {error}",
            flush=True,
        )
        return False


def send_direct_entry_alert(alert):
    symbol = str(alert.get("symbol", "")).upper().strip()

    if not symbol:
        return False

    from direct_entry.alert_tracker import get_active_alerts

    active_alerts = get_active_alerts()

    if symbol in active_alerts:
        print(
            f"⏳ Alert skipped. {symbol} is already under monitoring.",
            flush=True,
        )
        return False

    if not can_send_alert(alert):
        return False

    message = build_direct_entry_message(alert)
    sent = send_telegram_message(message)

    if sent:
        mark_alert_sent(alert)

    return sent

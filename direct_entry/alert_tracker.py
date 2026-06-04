# =========================
# Direct Entry Alert Tracker
# =========================

import os
import json
import requests
from datetime import datetime, timedelta

from direct_entry.alert_manager import send_telegram_message


UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

REDIS_ACTIVE_ALERTS_KEY = "direct_entry_active_alerts"

MONITORING_MINUTES = 120


def redis_headers():
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }


def redis_ready():
    return bool(
        UPSTASH_REDIS_REST_URL
        and UPSTASH_REDIS_REST_TOKEN
    )


def get_now():
    return datetime.utcnow()


def format_number(value, decimals=2):
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return "N/A"


def save_tracking_record(symbol, record):
    if not redis_ready():
        return False

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/hset/{REDIS_ACTIVE_ALERTS_KEY}/{symbol}"

        response = requests.post(
            url,
            headers=redis_headers(),
            json=json.dumps(record),
            timeout=10,
        )

        return bool(response.status_code == 200)

    except Exception as error:
        print(
            f"Save tracking record error: {error}",
            flush=True,
        )
        return False


def get_active_alerts():
    if not redis_ready():
        return {}

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/hgetall/{REDIS_ACTIVE_ALERTS_KEY}"

        response = requests.get(
            url,
            headers=redis_headers(),
            timeout=10,
        )

        if response.status_code != 200:
            return {}

        data = response.json().get("result", [])

        alerts = {}

        for i in range(0, len(data), 2):
            symbol = data[i]
            raw_record = data[i + 1]

            try:
                alerts[symbol] = json.loads(raw_record)
            except Exception:
                continue

        return alerts

    except Exception as error:
        print(
            f"Get active alerts error: {error}",
            flush=True,
        )
        return {}


def remove_alert_from_monitoring(symbol):
    if not redis_ready():
        return False

    symbol = str(symbol).upper().strip()

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/hdel/{REDIS_ACTIVE_ALERTS_KEY}/{symbol}"

        response = requests.post(
            url,
            headers=redis_headers(),
            timeout=10,
        )

        return bool(response.status_code == 200)

    except Exception as error:
        print(
            f"Remove tracking record error: {error}",
            flush=True,
        )
        return False


def build_tracking_record(alert):
    symbol = str(alert.get("symbol", "")).upper().strip()
    entry_price = float(alert.get("price", 0) or 0)

    stop_loss = entry_price * 0.985
    target_1 = entry_price * 1.02
    target_2 = entry_price * 1.04

    now = get_now()
    expires_at = now + timedelta(minutes=MONITORING_MINUTES)

    return {
        "symbol": symbol,
        "grade": alert.get("grade"),
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "sent_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "highest_price": entry_price,
        "lowest_price": entry_price,
        "target_1_hit": False,
        "target_2_hit": False,
        "stop_hit": False,
        "status": "monitoring",
    }


def add_alert_to_monitoring(alert):
    symbol = str(alert.get("symbol", "")).upper().strip()

    if not symbol:
        return False

    record = build_tracking_record(alert)

    return save_tracking_record(symbol, record)


def build_target_2_message(record, current_price):
    symbol = record.get("symbol")
    grade = record.get("grade")
    entry_price = float(record.get("entry_price", 0) or 0)

    gain_pct = 0

    if entry_price > 0:
        gain_pct = ((current_price - entry_price) / entry_price) * 100

    return (
        f"🎯 هدف ثاني تحقق\n\n"
        f"🎯 بوت الدخول المباشر\n\n"
        f"السهم: {symbol}\n"
        f"التقييم: {grade}\n\n"
        f"سعر الدخول:\n"
        f"{format_number(entry_price, 4)}\n\n"
        f"السعر الحالي:\n"
        f"{format_number(current_price, 4)}\n\n"
        f"الربح التقريبي:\n"
        f"+{format_number(gain_pct, 2)}%\n\n"
        f"تم إنهاء المراقبة بعد تحقق الهدف الثاني."
    )


def build_stop_message(record, current_price):
    symbol = record.get("symbol")
    grade = record.get("grade")
    entry_price = float(record.get("entry_price", 0) or 0)
    highest_price = float(record.get("highest_price", 0) or 0)

    loss_pct = 0
    highest_pct = 0

    if entry_price > 0:
        loss_pct = ((current_price - entry_price) / entry_price) * 100
        highest_pct = ((highest_price - entry_price) / entry_price) * 100

    return (
        f"❌ وقف الخسارة تحقق\n\n"
        f"🎯 بوت الدخول المباشر\n\n"
        f"السهم: {symbol}\n"
        f"التقييم: {grade}\n\n"
        f"سعر الدخول:\n"
        f"{format_number(entry_price, 4)}\n\n"
        f"السعر الحالي:\n"
        f"{format_number(current_price, 4)}\n\n"
        f"التغير:\n"
        f"{format_number(loss_pct, 2)}%\n\n"
        f"أعلى سعر قبل الوقف:\n"
        f"{format_number(highest_price, 4)} "
        f"({format_number(highest_pct, 2)}%)\n\n"
        f"تم إنهاء المراقبة بعد ضرب الوقف."
    )


def build_final_update_message(record, current_price):
    symbol = record.get("symbol")
    grade = record.get("grade")
    entry_price = float(record.get("entry_price", 0) or 0)
    highest_price = float(record.get("highest_price", 0) or 0)

    change_pct = 0
    highest_pct = 0

    if entry_price > 0:
        change_pct = ((current_price - entry_price) / entry_price) * 100
        highest_pct = ((highest_price - entry_price) / entry_price) * 100

    if record.get("target_1_hit"):
        target_1_status = "✅ وصل الهدف الأول"
    else:
        target_1_status = "❌ لم يصل الهدف الأول"

    if current_price > entry_price:
        price_status = "🟢 ما زال فوق سعر الدخول"
    elif current_price < entry_price:
        price_status = "🔴 تحت سعر الدخول"
    else:
        price_status = "⚪ قريب من سعر الدخول"

    return (
        f"📊 تحديث نهاية المتابعة\n\n"
        f"🎯 بوت الدخول المباشر\n\n"
        f"السهم: {symbol}\n"
        f"التقييم: {grade}\n\n"
        f"سعر الدخول:\n"
        f"{format_number(entry_price, 4)}\n\n"
        f"السعر الحالي:\n"
        f"{format_number(current_price, 4)}\n\n"
        f"التغير:\n"
        f"{format_number(change_pct, 2)}%\n\n"
        f"أعلى سعر أثناء المتابعة:\n"
        f"{format_number(highest_price, 4)} "
        f"({format_number(highest_pct, 2)}%)\n\n"
        f"الحالة الحالية:\n"
        f"{target_1_status}\n"
        f"⏳ لم يصل الهدف الثاني\n"
        f"{price_status}\n\n"
        f"تقييم نهاية المتابعة:\n"
        f"انتهت مدة المتابعة دون وصول الهدف الثاني أو ضرب الوقف.\n"
        f"راجع السيولة والاتجاه قبل قرار البقاء أو الخروج.\n\n"
        f"انتهت مدة المتابعة (120 دقيقة)\n\n"
        f"تمت إزالة السهم من قائمة المراقبة."
    )


def update_alert_tracking(symbol, current_price):
    symbol = str(symbol).upper().strip()
    current_price = float(current_price or 0)

    if current_price <= 0:
        return None

    active_alerts = get_active_alerts()

    if symbol not in active_alerts:
        return None

    record = active_alerts[symbol]

    if isinstance(record, str):
        try:
            record = json.loads(record)
        except Exception:
            remove_alert_from_monitoring(symbol)
            return "invalid_tracking_record_removed"
            
    record["highest_price"] = max(
        float(record.get("highest_price", 0) or 0),
        current_price,
    )

    record["lowest_price"] = min(
        float(record.get("lowest_price", current_price) or current_price),
        current_price,
    )

    if current_price >= float(record.get("target_1", 0) or 0):
        record["target_1_hit"] = True

    if current_price >= float(record.get("target_2", 0) or 0):
        record["target_2_hit"] = True
        record["status"] = "target_2_hit"

        message = build_target_2_message(
            record=record,
            current_price=current_price,
        )

        send_telegram_message(message)
        remove_alert_from_monitoring(symbol)

        return "target_2_hit"

    if current_price <= float(record.get("stop_loss", 0) or 0):
        record["stop_hit"] = True
        record["status"] = "stop_hit"

        message = build_stop_message(
            record=record,
            current_price=current_price,
        )

        send_telegram_message(message)
        remove_alert_from_monitoring(symbol)

        return "stop_hit"

    expires_at = datetime.fromisoformat(record["expires_at"])

    if get_now() >= expires_at:
        message = build_final_update_message(
            record=record,
            current_price=current_price,
        )

        send_telegram_message(message)
        remove_alert_from_monitoring(symbol)

        return "expired_final_update_sent"

    save_tracking_record(symbol, record)

    return "monitoring"

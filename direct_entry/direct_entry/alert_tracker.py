# =========================
# Direct Entry Alert Tracker
# =========================

from datetime import datetime, timedelta

from direct_entry.alert_manager import send_telegram_message


MONITORING_MINUTES = 120

active_direct_entry_alerts = {}


def get_now():
    return datetime.utcnow()


def format_number(value, decimals=2):
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return "N/A"


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

    active_direct_entry_alerts[symbol] = build_tracking_record(alert)

    return True


def remove_alert_from_monitoring(symbol):
    symbol = str(symbol).upper().strip()

    if symbol in active_direct_entry_alerts:
        del active_direct_entry_alerts[symbol]
        return True

    return False


def get_active_alerts():
    return active_direct_entry_alerts


def build_final_update_message(record, current_price):
    symbol = record.get("symbol")
    grade = record.get("grade")
    entry_price = float(record.get("entry_price", 0) or 0)
    highest_price = float(record.get("highest_price", 0) or 0)

    if entry_price > 0:
        change_pct = ((current_price - entry_price) / entry_price) * 100
        highest_pct = ((highest_price - entry_price) / entry_price) * 100
    else:
        change_pct = 0
        highest_pct = 0

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

    message = (
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

    return message


def update_alert_tracking(symbol, current_price):
    symbol = str(symbol).upper().strip()

    if symbol not in active_direct_entry_alerts:
        return None

    record = active_direct_entry_alerts[symbol]
    current_price = float(current_price or 0)

    if current_price <= 0:
        return None

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
        remove_alert_from_monitoring(symbol)
        return "target_2_hit"

    if current_price <= float(record.get("stop_loss", 0) or 0):
        record["stop_hit"] = True
        record["status"] = "stop_hit"
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

    return "monitoring"

"""Fixed plain text; payload values only. No trading API, no secret access."""
from zoneinfo import ZoneInfo
from datetime import datetime
from math import isfinite

MISSING='غير متوفر في الحمولة'
def format_message(event,created):
    payload=event.get('payload')
    payload=payload if isinstance(payload,dict) else {}
    def number(key):
        value=payload.get(key)
        if type(value) not in (int,float) or not isfinite(value):return MISSING
        return format(value,'.10g')
    kind=event.get('event_type')
    price='first_observed_breach_price' if kind=='STOP' else 'entry_alert_price'
    raw=payload.get('entry_alert_sent_at') if kind=='ENTRY' else payload.get('event_ts')
    try:
        when=datetime.fromisoformat(raw.replace('Z','+00:00'))
        if when.tzinfo is None:raise ValueError()
        stamp=when.astimezone(ZoneInfo('Asia/Riyadh')).isoformat();time_label='وقت الحدث — الرياض'
    except (ValueError,TypeError,AttributeError):
        stamp=created.astimezone(ZoneInfo('Asia/Riyadh')).isoformat();time_label='وقت إنشاء السجل — الرياض (وقت الحدث غير متوفر)'
    # Symbol/type are validated by the sender, not inferred from a parent symbol.
    return '\n'.join(['OPR — رسالة معلومات SHADOW',f"الرمز: {event['symbol']}",
        f'نوع الحدث: {kind}',f'السعر: {number(price)}',f"الوقف: {number('structural_stop')}",
        f"الهدف الأول: {number('t1')}",f"الهدف الثاني: {number('t2')}",
        f'{time_label}: {stamp}','رسالة فقط — لا تتضمن أمر تداول.'])

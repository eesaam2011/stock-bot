#!/usr/bin/env python3
"""
اختبار حساسية عرض الوقف (Stop-Loss Width Sensitivity)
=========================================================

الهدف: معرفة هل توسيع الوقف يحسّن النتيجة، بدون إعادة تشغيل
مرحلة الاكتشاف الطويلة (collector / worker_loop) التي تاخذ يومين.

ليش هذا آمن وسريع:
- يعيد استخدام قائمة المرشحين الجاهزة أصلاً (collector.simulation_candidates())
  وهذي محفوظة بـ Redis من قبل، ما يعيد اكتشافها.
- يجيب بيانات الشموع (bars) فقط لهالمرشحين المحددين (169 رمز/جلسة تقريباً)
  مو الكون كامل (5,685 رمز × 60 يوم) — نفس فكرة simulation_loop الأصلية.
- يخزّن الشموع بمفتاح Redis منفصل خاص بهذا الاختبار، فلو شغّلته لعدة
  عروض وقف مختلفة بنفس الجلسة، ما يعيد الطلب من Alpaca إلا أول مرة.
- ما يلمس ولا يعدّل أي مفتاح من مفاتيح الـcollector أو الـsimulation
  الأصلية إطلاقاً.

الاستخدام:
    export UPSTASH_REDIS_REST_URL=...
    export UPSTASH_REDIS_REST_TOKEN=...
    export ALPACA_API_KEY=...
    export ALPACA_SECRET_KEY=...
    python3 stop_width_sensitivity.py

يتطلب وجود ndr_backtest_engine.py بنفس المجلد أو بـ PYTHONPATH
(يستورد منه BacktestCollector, RedisREST, Alpaca, parse_dt مباشرة
بدون ما يعيد كتابة أي منطق).
"""

import copy
import json
import sys
import time

from ndr_backtest_engine import BacktestCollector, RedisREST, Alpaca, parse_dt, now_iso

# ============ إعدادات قابلة للتعديل ============
STOP_WIDTHS_PCT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
# 1.5 موجود كنقطة مرجعية (قريبة من متوسط الوقف الأصلي) للتأكد إن
# النتائج تتطابق تقريباً مع التقرير الأصلي قبل ما نثق بالباقي.

POLICIES = [("full_t1", "conservative"), ("scaled", "conservative")]
# نكتفي بسيناريو conservative (الأكثر تحفظاً/واقعية) لكل سياسة، لتوفير الوقت.
# غيّرها لو تبي كل الأربع تركيبات مثل التقرير الأصلي.

BATCH_SIZE = 10          # نفس دفعة الطلبات المستخدمة بالكود الأصلي
REQUEST_DELAY_SECONDS = 0.15
CACHE_KEY_SUFFIX = "stopwidth_test:bars"   # مفتاح Redis منفصل تماماً عن الأصلي
# =================================================


def fetch_or_cache_bars(collector: BacktestCollector, candidates: list) -> dict:
    """
    يجيب شموع الدقيقة لكل مرشح (رمز + جلسة)، ويخزّنها بمفتاح Redis
    منفصل خاص بهذا الاختبار فقط. إعادة التشغيل لاحقاً (لعرض وقف آخر)
    تستخدم النسخة المخزّنة بدل ما تطلب Alpaca من جديد.

    يرجّع: dict مفتاحه "session|symbol" وقيمته قائمة الشموع.
    """
    bars_key = collector.key(CACHE_KEY_SUFFIX)
    grouped: dict[str, list] = {}
    for c in candidates:
        grouped.setdefault(c["session"], []).append(c)

    all_bars: dict[str, list] = {}
    total = len(candidates)
    processed = 0

    for session, items in sorted(grouped.items()):
        start, end = collector.session_window(session)
        for begin in range(0, len(items), BATCH_SIZE):
            batch = items[begin:begin + BATCH_SIZE]
            symbols = [x["symbol"] for x in batch]
            fields = [f"{session}|{x['symbol']}" for x in batch]

            existing = collector.redis.command("HMGET", bars_key, *fields) or [None] * len(fields)
            todo = [x for x, value in zip(batch, existing) if value is None]
            cached_now = [x for x, value in zip(batch, existing) if value is not None]

            for x, value in zip(batch, existing):
                if value is not None:
                    all_bars[f"{session}|{x['symbol']}"] = json.loads(value)

            if todo:
                todo_symbols = [x["symbol"] for x in todo]
                sip = collector.alpaca.bars(todo_symbols, start, end, "sip")
                writes = []
                for x in todo:
                    bars = sip.get(x["symbol"], [])
                    all_bars[f"{session}|{x['symbol']}"] = bars
                    writes.append((f"{session}|{x['symbol']}", json.dumps(bars, separators=(",", ":"))))
                collector.hset_bounded(bars_key, writes)
                time.sleep(REQUEST_DELAY_SECONDS)

            processed += len(batch)
            print(f"  bars: {processed}/{total}  (session={session}, fetched_new={len(todo)}, from_cache={len(cached_now)})",
                  file=sys.stderr)

    return all_bars


def candidate_with_override_stop(candidate: dict, new_stop_pct: float) -> dict:
    """
    يرجع نسخة من المرشح بنفس كل شيء، ما عدا الوقف (stop) اللي يُعاد
    حسابه بنفس منطق البوت الأصلي:  stop = price * (1 - stop_pct/100)
    (تحقّقنا من هذا المنطق من الكود الأصلي: next_day_explosion_radar.py)
    """
    c = copy.deepcopy(candidate)
    price = float(c["signal"]["price"])
    c["signal"]["stop"] = round(price * (1 - new_stop_pct / 100), 6)
    c["signal"]["stop_pct"] = new_stop_pct
    return c


def simulation_summary(rows):
    """نفس دالة BacktestCollector.simulation_summary بالضبط، منسوخة هنا
    فقط لأنها staticmethod ونستخدمها مباشرة."""
    return BacktestCollector.simulation_summary(rows)


def main():
    collector = BacktestCollector(RedisREST(), Alpaca())

    print("جاري تحميل قائمة المرشحين الجاهزة (بدون إعادة اكتشاف)...", file=sys.stderr)
    candidates = collector.simulation_candidates()
    print(f"عدد المرشحين: {len(candidates)}", file=sys.stderr)

    print("جاري تجهيز بيانات الشموع (استخدام الكاش إن وجد)...", file=sys.stderr)
    bars_by_key = fetch_or_cache_bars(collector, candidates)

    report = {
        "generated_at": now_iso(),
        "note": "Stop-width sensitivity test. Reuses cached candidates; does not touch the collector.",
        "stop_widths_tested": STOP_WIDTHS_PCT,
        "policies_tested": [f"{p}_{a}" for p, a in POLICIES],
        "results": {},
    }

    for stop_pct in STOP_WIDTHS_PCT:
        print(f"\n=== محاكاة عند عرض وقف {stop_pct}% ===", file=sys.stderr)
        report["results"][str(stop_pct)] = {}

        for policy, assumption in POLICIES:
            key = f"{policy}_{assumption}"
            rows_all, rows_dev, rows_hold = [], [], []

            for c in candidates:
                bars = bars_by_key.get(f"{c['session']}|{c['symbol']}", [])
                c_override = candidate_with_override_stop(c, stop_pct)
                run = BacktestCollector.simulate_trade(c_override, bars, assumption=assumption, policy=policy)
                rows_all.append(run)
                (rows_dev if c["partition"] == "development" else rows_hold).append(run)

            report["results"][str(stop_pct)][key] = {
                "all": simulation_summary(rows_all),
                "development": simulation_summary(rows_dev),
                "holdout": simulation_summary(rows_hold),
            }

            s = report["results"][str(stop_pct)][key]["all"]
            print(f"  [{key}] win_rate={s['win_rate']}%  profit_factor={s['profit_factor']}  "
                  f"compounded={s['compounded_return_pct']}%  max_dd={s['max_drawdown_pct']}%", file=sys.stderr)

    out_path = "stop_width_sensitivity_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nتم الحفظ: {out_path}", file=sys.stderr)

    # ملخص مختصر يطبع مباشرة بالشاشة للمقارنة السريعة
    print("\n=== ملخص مقارن (سياسة full_t1_conservative، كل العينة) ===")
    print(f"{'stop%':>6} | {'win_rate':>9} | {'profit_factor':>13} | {'compounded%':>12}")
    for w in STOP_WIDTHS_PCT:
        s = report["results"][str(w)]["full_t1_conservative"]["all"]
        print(f"{w:>6} | {s['win_rate']:>8.1f}% | {s['profit_factor']:>13.3f} | {s['compounded_return_pct']:>11.1f}%")


if __name__ == "__main__":
    main()

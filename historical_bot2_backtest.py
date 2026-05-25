import os
import time
import json
import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta, timezone

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_FAST_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")

MASTER_LIST_FILE = "master_list.json"
LIVE_MOVERS_FILE = "live_movers.json"
RESULTS_FILE = "historical_bot2_backtest_results.json"

MONTHS_BACK = 1
MAX_SYMBOLS_TO_TEST = 100

PRICE_MIN = 0.4
PRICE_MAX = 25

FORWARD_MINUTES = 60
COOLDOWN_MINUTES = 15


def send_telegram_msg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e, flush=True)


def read_gist_file(filename, default=None):
    if default is None:
        default = []

    if not GIST_ID or not GITHUB_TOKEN:
        return default

    try:
        res = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            },
            timeout=20
        )

        data = res.json()
        file_data = data.get("files", {}).get(filename)

        if not file_data:
            return default

        content = file_data.get("content", "")
        if not content:
            return default

        return json.loads(content)

    except Exception as e:
        print(f"Gist read error {filename}: {e}", flush=True)
        return default


def save_gist_file(filename, data):
    if not GIST_ID or not GITHUB_TOKEN:
        print("Missing GIST keys", flush=True)
        return

    try:
        res = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            },
            json={
                "files": {
                    filename: {
                        "content": json.dumps(data, ensure_ascii=False)
                    }
                }
            },
            timeout=30
        )

        print(f"Saved {filename}: {res.status_code}", flush=True)

    except Exception as e:
        print("Gist save error:", e, flush=True)


def clean_symbol(symbol):
    if not symbol:
        return None

    symbol = str(symbol).upper().strip()

    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return None

    if len(symbol) > 5:
        return None

    if not symbol.isalpha():
        return None

    return symbol


def extract_symbols_from_gist_data(data):
    symbols = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                symbol = item.get("symbol")
            else:
                symbol = item

            symbol = clean_symbol(symbol)

            if symbol:
                symbols.append(symbol)

    return list(dict.fromkeys(symbols))


def load_symbols_for_backtest():
    live_data = read_gist_file(LIVE_MOVERS_FILE, default=[])
    live_symbols = extract_symbols_from_gist_data(live_data)

    if live_symbols:
        print(f"⚡ Using live_movers symbols: {len(live_symbols)}", flush=True)
        return live_symbols[:MAX_SYMBOLS_TO_TEST]

    master_data = read_gist_file(MASTER_LIST_FILE, default=[])
    master_symbols = extract_symbols_from_gist_data(master_data)

    print(f"🛟 live_movers empty, using master_list symbols: {len(master_symbols)}", flush=True)

    return master_symbols[:MAX_SYMBOLS_TO_TEST]


def fetch_historical_1m(symbol):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30 * MONTHS_BACK)

    chunks = []
    chunk_start = start

    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=7), end)

        try:
            bars = api.get_bars(
                symbol,
                tradeapi.TimeFrame.Minute,
                start=chunk_start.isoformat(),
                end=chunk_end.isoformat(),
                adjustment="raw"
            ).df

            if bars is not None and not bars.empty:
                if "symbol" in bars.columns:
                    bars = bars[bars["symbol"] == symbol]

                bars = bars.rename(columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume"
                })

                needed = ["Open", "High", "Low", "Close", "Volume"]

                if all(c in bars.columns for c in needed):
                    chunks.append(bars[needed].dropna())

            print(f"✅ {symbol}: {chunk_start.date()} -> {chunk_end.date()}", flush=True)

        except Exception as e:
            print(f"Fetch error {symbol}: {e}", flush=True)

        chunk_start = chunk_end
        time.sleep(0.25)

    if not chunks:
        return pd.DataFrame()

    return pd.concat(chunks).sort_index()


def calculate_rsi(close, period=14):
    if len(close) < period + 1:
        return 50

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()

    if loss.iloc[-1] == 0:
        return 100

    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def calculate_trade_plan(entry):
    if entry < 2:
        t1 = entry + 0.04
        t2 = entry + 0.08
    elif entry < 5:
        t1 = entry + 0.07
        t2 = entry + 0.14
    else:
        t1 = entry * 1.025
        t2 = entry * 1.05

    return {
        "entry": round(entry, 4),
        "target_1": round(t1, 4),
        "target_2": round(t2, 4),
        "stop_loss": round(entry * 0.985, 4)
    }


def calculate_micro_scalp_plan(entry):
    return {
        "entry": round(entry, 4),
        "target_1": round(entry + 0.05, 4),
        "target_2": round(entry + 0.10, 4),
        "stop_loss": round(entry - 0.04, 4)
    }


def analyze_bot2_window(symbol, df, source_group):
    try:
        if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
            return None

        cp = float(df["Close"].iloc[-1])

        if not (PRICE_MIN <= cp <= PRICE_MAX):
            return None

        day_high = float(df["High"].max())
        day_low = float(df["Low"].min())
        vwap = float((df["Close"] * df["Volume"]).sum() / max(df["Volume"].sum(), 1))

        rsi = calculate_rsi(df["Close"])
        instant_rvol = df["Volume"].tail(3).mean() / max(df["Volume"].mean(), 1)

        recent_move = ((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100
        move_5m = ((cp - df["Close"].iloc[-5]) / df["Close"].iloc[-5]) * 100
        move_3m = ((cp - df["Close"].iloc[-3]) / df["Close"].iloc[-3]) * 100

        df = df.copy()
        df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        ema9 = float(df["EMA9"].iloc[-1])
        ema20 = float(df["EMA20"].iloc[-1])

        latest_volume = float(df["Volume"].tail(10).sum())
        dollar_volume = cp * latest_volume

        near_high = cp >= day_high * 0.975
        above_vwap = cp > vwap
        above_ema9 = cp > ema9
        ema_ok = ema9 >= ema20 * 0.995

        last_open = float(df["Open"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])

        prev_close = float(df["Close"].iloc[-2])
        prev_high = float(df["High"].iloc[-2])

        candle_range = last_high - last_low
        if candle_range <= 0:
            return None

        close_position = (last_close - last_low) / candle_range
        upper_wick_pct = (last_high - last_close) / candle_range
        body_ratio = abs(last_close - last_open) / candle_range

        last_3_volume = float(df["Volume"].tail(3).mean())
        prev_10_volume = float(df["Volume"].tail(13).head(10).mean())

        volume_acceleration = last_3_volume >= prev_10_volume * 1.6

        strong_candle = (
            close_position >= 0.70
            and upper_wick_pct <= 0.30
            and body_ratio >= 0.35
        )

        vwap_reclaim = (
            float(df["Close"].iloc[-2]) < vwap
            and last_close > vwap
        )

        ema_reclaim = (
            float(df["Close"].iloc[-2]) < ema9
            and last_close > ema9
        )

        behavior_change = (
            recent_move >= 0.35
            and volume_acceleration
            and strong_candle
        )

        new_money_flow = (
            cp > vwap
            and cp > ema9
            and volume_acceleration
        )

        real_breakout = (
            last_close > prev_high
            and prev_close > prev_high * 0.998
            and instant_rvol >= 2.2
        )

        fake_breakout_risk = (
            upper_wick_pct >= 0.45
            and close_position < 0.55
            and instant_rvol >= 2.5
        )

        overextended = (
            rsi > 78
            or recent_move > 3.0
        )

        distribution_score = 0

        if instant_rvol >= 3.0 and recent_move < 0.75:
            distribution_score += 15

        if upper_wick_pct >= 0.40 and close_position < 0.60:
            distribution_score += 12

        if volume_acceleration and body_ratio < 0.28:
            distribution_score += 10

        early_confirmed_explosion = (
            0.45 <= recent_move <= 1.50
            and instant_rvol >= 3.0
            and volume_acceleration
            and cp > vwap
            and cp > ema9
            and ema9 >= ema20
            and near_high
            and (real_breakout or vwap_reclaim or ema_reclaim)
            and close_position >= 0.78
            and upper_wick_pct <= 0.22
            and body_ratio >= 0.40
            and move_3m >= 0.45
            and move_5m >= 0.75
            and move_3m >= move_5m * 0.75
            and distribution_score < 12
            and not fake_breakout_risk
            and not overextended
        )

        micro_scalp_setup = (
            1.00 <= cp <= 10.00
            and recent_move <= 1.20
            and instant_rvol >= 2.5
            and dollar_volume >= 300000
            and volume_acceleration
            and cp > vwap
            and cp > ema9
            and near_high
            and (real_breakout or vwap_reclaim)
            and close_position >= 0.80
            and upper_wick_pct <= 0.20
            and move_3m >= 0.20
            and move_5m >= 0.40
            and move_3m >= move_5m * 0.75
            and distribution_score < 15
            and not fake_breakout_risk
            and not overextended
        )

        if (
            instant_rvol >= 4
            and move_3m < move_5m
            and move_5m < recent_move
        ):
            return None

        if recent_move > 2.2:
            return None

        if not near_high:
            return None

        if not (real_breakout or vwap_reclaim or ema_reclaim):
            return None

        if not early_confirmed_explosion and not micro_scalp_setup:
            return None

        technical_score = 0
        technical_score += min(instant_rvol * 10, 24)
        technical_score += min(max(recent_move, 0) * 10, 26)

        if above_vwap:
            technical_score += 10

        if above_ema9:
            technical_score += 10

        if ema_ok:
            technical_score += 8

        if near_high:
            technical_score += 8

        if real_breakout:
            technical_score += 15

        if 52 <= rsi <= 68:
            technical_score += 10
        elif 45 <= rsi < 52:
            technical_score += 5

        if fake_breakout_risk:
            technical_score -= 15

        if source_group == "RADAR":
            technical_score += 12

            if volume_acceleration:
                technical_score += 10
            if strong_candle:
                technical_score += 8
            if vwap_reclaim:
                technical_score += 8
            if ema_reclaim:
                technical_score += 6
            if behavior_change:
                technical_score += 10
            if new_money_flow:
                technical_score += 8

        radar_bonus = 8 if source_group == "RADAR" else 0
        final_score = technical_score + radar_bonus

        if final_score >= 90:
            grade = "A++"
        elif final_score >= 80:
            grade = "A+"
        elif final_score >= 70:
            grade = "A"
        elif final_score >= 60:
            grade = "B"
        else:
            grade = "C"

        if grade not in ["A", "A+", "A++"]:
            return None

        if micro_scalp_setup:
            plan = calculate_micro_scalp_plan(cp)
            setup_type = "MICRO_SCALP"
        else:
            plan = calculate_trade_plan(cp)
            setup_type = "EARLY_CONFIRMED_EXPLOSION"

        return {
            "bot": "BOT2",
            "symbol": symbol,
            "source_group": source_group,
            "setup_type": setup_type,
            "time": str(df.index[-1]),
            "price": round(cp, 4),
            "entry": plan["entry"],
            "target_1": plan["target_1"],
            "target_2": plan["target_2"],
            "stop_loss": plan["stop_loss"],
            "grade": grade,
            "technical_score": round(float(technical_score), 2),
            "final_score": round(float(final_score), 2),
            "rsi": round(float(rsi), 2),
            "instant_rvol": round(float(instant_rvol), 2),
            "recent_move": round(float(recent_move), 2),
            "move_3m": round(float(move_3m), 2),
            "move_5m": round(float(move_5m), 2),
            "dollar_volume_10m": round(float(dollar_volume), 2),
            "real_breakout": bool(real_breakout),
            "vwap_reclaim": bool(vwap_reclaim),
            "ema_reclaim": bool(ema_reclaim),
            "volume_acceleration": bool(volume_acceleration),
            "strong_candle": bool(strong_candle),
            "fake_breakout_risk": bool(fake_breakout_risk),
            "distribution_score": round(float(distribution_score), 2),
            "close_position": round(float(close_position), 2),
            "upper_wick_pct": round(float(upper_wick_pct), 2),
            "body_ratio": round(float(body_ratio), 2),
            "day_high": round(float(day_high), 4),
            "day_low": round(float(day_low), 4),
        }

    except Exception as e:
        print(f"Analyze Bot2 historical error {symbol}: {e}", flush=True)
        return None


def evaluate_after_signal(df, signal_index, signal):
    entry = float(signal["entry"])
    t1 = float(signal["target_1"])
    t2 = float(signal["target_2"])
    sl = float(signal["stop_loss"])

    future = df.iloc[signal_index:signal_index + FORWARD_MINUTES]

    if future.empty:
        return None

    max_gain = ((float(future["High"].max()) - entry) / entry) * 100
    max_drawdown = ((float(future["Low"].min()) - entry) / entry) * 100

    hit_t1 = False
    hit_t2 = False
    hit_stop = False

    minutes_to_t1 = None
    minutes_to_t2 = None
    minutes_to_stop = None

    outcome = "NO_HIT"

    for n, (_, row) in enumerate(future.iterrows(), start=1):
        high = float(row["High"])
        low = float(row["Low"])

        if low <= sl and not hit_stop:
            hit_stop = True
            minutes_to_stop = n

        if high >= t1 and not hit_t1:
            hit_t1 = True
            minutes_to_t1 = n

        if high >= t2 and not hit_t2:
            hit_t2 = True
            minutes_to_t2 = n

        if hit_stop or hit_t1:
            break

    if hit_t1 and hit_stop:
        outcome = "TARGET_FIRST" if minutes_to_t1 <= minutes_to_stop else "STOP_FIRST"
    elif hit_t1:
        outcome = "TARGET"
    elif hit_stop:
        outcome = "STOP"

    return {
        "hit_target_1": bool(hit_t1),
        "hit_target_2": bool(hit_t2),
        "hit_stop": bool(hit_stop),
        "outcome": outcome,
        "max_gain_60m": round(float(max_gain), 2),
        "max_drawdown_60m": round(float(max_drawdown), 2),
        "minutes_to_target_1": minutes_to_t1,
        "minutes_to_target_2": minutes_to_t2,
        "minutes_to_stop": minutes_to_stop
    }


def backtest_symbol(symbol):
    df = fetch_historical_1m(symbol)

    if df.empty:
        print(f"⚠️ No data for {symbol}", flush=True)
        return []

    signals = []
    last_signal_time_by_group = {}

    for i in range(120, len(df) - FORWARD_MINUTES):
        window = df.iloc[i-120:i]
        signal_time = df.index[i]

        for source_group in ["STRONG", "RADAR"]:
            signal = analyze_bot2_window(symbol, window, source_group=source_group)

            if not signal:
                continue

            last_signal_time = last_signal_time_by_group.get(source_group)

            if last_signal_time is not None:
                minutes_since_last = (signal_time - last_signal_time).total_seconds() / 60

                if minutes_since_last < COOLDOWN_MINUTES:
                    continue

            forward = evaluate_after_signal(df, i, signal)

            if not forward:
                continue

            signal.update(forward)
            signals.append(signal)
            last_signal_time_by_group[source_group] = signal_time

    print(f"✅ {symbol}: {len(signals)} Bot 2 signals", flush=True)

    return signals


def main():
    symbols = load_symbols_for_backtest()

    if not symbols:
        print("⚠️ No symbols loaded for backtest", flush=True)
        send_telegram_msg("⚠️ Bot 2 Historical Backtest: No symbols loaded")
        return

    print(f"🚀 Starting Bot 2 historical backtest: {len(symbols)} symbols", flush=True)

    all_results = []

    for idx, symbol in enumerate(symbols, start=1):
        print(f"📊 Testing {idx}/{len(symbols)}: {symbol}", flush=True)
        all_results.extend(backtest_symbol(symbol))

    if not all_results:
        print("⚠️ No Bot 2 historical signals found", flush=True)
        send_telegram_msg("⚠️ Bot 2 Historical Backtest: No signals found")
        save_gist_file(
            RESULTS_FILE,
            {
                "summary": {
                    "bot": "BOT2",
                    "total_symbols_tested": len(symbols),
                    "total_signals": 0,
                    "generated_at": str(datetime.now(timezone.utc))
                },
                "results": []
            }
        )
        return

    df = pd.DataFrame(all_results)

    total = len(df)
    t1_hits = int(df["hit_target_1"].sum())
    t2_hits = int(df["hit_target_2"].sum())
    stop_hits = int(df["hit_stop"].sum())
    target_first = int((df["outcome"] == "TARGET_FIRST").sum())
    stop_first = int((df["outcome"] == "STOP_FIRST").sum())

    by_setup = df.groupby("setup_type")["outcome"].count().to_dict()
    by_group = df.groupby("source_group")["outcome"].count().to_dict()

    summary = {
        "bot": "BOT2",
        "months_back": MONTHS_BACK,
        "total_symbols_tested": len(symbols),
        "max_symbols_to_test": MAX_SYMBOLS_TO_TEST,
        "total_signals": total,
        "target_1_hits": t1_hits,
        "target_2_hits": t2_hits,
        "stop_hits": stop_hits,
        "target_first": target_first,
        "stop_first": stop_first,
        "target_1_hit_rate": round((t1_hits / total) * 100, 2),
        "target_2_hit_rate": round((t2_hits / total) * 100, 2),
        "stop_rate": round((stop_hits / total) * 100, 2),
        "avg_max_gain_60m": round(float(df["max_gain_60m"].mean()), 2),
        "avg_max_drawdown_60m": round(float(df["max_drawdown_60m"].mean()), 2),
        "signals_by_setup": by_setup,
        "signals_by_source_group": by_group,
        "generated_at": str(datetime.now(timezone.utc))
    }

    output = {
        "summary": summary,
        "results": all_results
    }

    save_gist_file(RESULTS_FILE, output)

    msg = (
        "📊 Bot 2 Historical Backtest Finished\n\n"
        f"Symbols Tested: {len(symbols)}\n"
        f"Signals: {total}\n"
        f"T1 Hit Rate: {summary['target_1_hit_rate']}%\n"
        f"T2 Hit Rate: {summary['target_2_hit_rate']}%\n"
        f"Stop Rate: {summary['stop_rate']}%\n"
        f"Avg Max Gain 60m: {summary['avg_max_gain_60m']}%\n"
        f"Avg Max Drawdown 60m: {summary['avg_max_drawdown_60m']}%"
    )

    print(json.dumps(summary, indent=2), flush=True)
    send_telegram_msg(msg)


if __name__ == "__main__":
    main()

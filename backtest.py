"""
Backtest untuk XAU/USD SMC Signal Bot (bot.py).

Cara kerja:
  1. Ambil data historis 5min & 15min dari TwelveData (di-cache ke logs/backtest_cache).
  2. Simulasi loop bot asli baris demi baris: setiap close candle 5M,
     bangun window 100 bar 5M + 100 bar 15M (seperti get_candles outputsize=100)
     lalu panggil bot.generate_signal() apa adanya.
  3. Sinyal yang lolos filter cooldown 45 menit & limit 10/hari diambil:
     entry = open bar berikutnya, SL/TP dari calculate_sl_distance (R:R 1:2),
     resolve pakai high/low bar (SL diprioritaskan bila kena di bar yang sama).
  4. Hitung win rate, profit factor, expectancy, drawdown, dll.

Usage:
  python backtest.py            # data ±60 hari (cache dulu, request dihemat)
  python backtest.py --days 20  # rentang lebih pendek
"""

import contextlib
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytz
import requests

# Windows console (cp1252) tak bisa encode emoji → paksa UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "logs", "backtest_cache")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

import bot  # noqa: E402  (bot.py men-load .env saat import — tidak ada network call)

API = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"
IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.utc

MAX_HOLD_BARS = 288          # timeout posisi: 24 jam (288 bar 5M)
BARS_PER_SIGNAL_WINDOW = 100 # = outputsize bot
REQUEST_GAP_SEC = 9          # free plan: 8 credits/menit


def load_api_key():
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise RuntimeError("TWELVEDATA_API_KEY tidak ada di .env")
    return key


def parse_dt(s):
    # TwelveData forex default timezone = UTC
    return UTC.localize(datetime.strptime(s, "%Y-%m-%d %H:%M:%S"))


def _span_days(rows):
    if len(rows) < 2:
        return 0.0
    times = sorted(r["time"] for r in rows)
    d0 = datetime.strptime(times[0], "%Y-%m-%d %H:%M:%S")
    d1 = datetime.strptime(times[-1], "%Y-%m-%d %H:%M:%S")
    return (d1 - d0).total_seconds() / 86400


def fetch_series(interval, min_days, max_requests, api_key):
    """Pagination mundur dari sekarang, cache per interval."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"XAU_USD_{interval}.json")
    if os.path.exists(cache):
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        with open(cache, encoding="utf-8") as f:
            rows = json.load(f)
        span = _span_days(rows)
        if age_h < 12 and span >= min_days:
            print(f"[fetch] cache {interval}: {len(rows)} bars, {span:.1f} hari (pakai ulang)")
            return rows
        print(f"[fetch] cache {interval} terlalu tua/pendek (age={age_h:.1f}h, {span:.1f}d) -> refetch")

    print(f"[fetch] {interval}: target >={min_days} hari, maks {max_requests} request...")
    by_time = {}
    end_date = None
    for req_i in range(max_requests):
        params = {
            "symbol": SYMBOL, "interval": interval,
            "outputsize": 5000, "format": "JSON",
            "timezone": "UTC", "apikey": api_key,
        }
        if end_date:
            params["end_date"] = end_date
        data = {"status": "error"}
        for attempt in range(4):
            try:
                r = requests.get(API, params=params, timeout=30)
                data = r.json()
                break
            except Exception as e:  # noqa: BLE001
                print(f"[fetch] attempt {attempt+1} error: {e}")
                time.sleep(15)
        if data.get("status") != "ok" or not data.get("values"):
            print(f"[fetch] stop ({data.get('status')}: {data.get('message')})")
            break

        new = 0
        oldest = None
        for v in data["values"]:
            dt = v["datetime"]
            if dt not in by_time:
                new += 1
                by_time[dt] = {
                    "time": dt,
                    "open": float(v["open"]), "high": float(v["high"]),
                    "low": float(v["low"]), "close": float(v["close"]),
                }
            if oldest is None or dt < oldest:
                oldest = dt
        span = _span_days(list(by_time.values()))
        print(f"[fetch]   req {req_i+1}: +{new} bars -> total {len(by_time)} ({span:.1f} hari, oldest={oldest})")
        if span >= min_days or new == 0:
            break
        end_date = (parse_dt(oldest) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(REQUEST_GAP_SEC)

    rows = sorted(by_time.values(), key=lambda c: c["time"])
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    print(f"[fetch] tersimpan {len(rows)} bars -> {cache}")
    return rows


def quiet_signal(c5, c15):
    """generate_signal dengan print dibungkam (stdout dialihkan)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sig = bot.generate_signal(c5, c15)
    return sig, buf.getvalue()


def session_label(dt_utc):
    return bot.get_session(dt_utc.astimezone(IST))


def run_simulation(m5, m15):
    """Simulasi loop bot asli per bar 5M. Mengembalikan (stats, trades)."""
    # data terakhir adalah bar yang masih terbuka -> buang
    m5 = m5[:-1]

    j = BARS_PER_SIGNAL_WINDOW - 1  # pointer window 15M
    stats = {
        "bars_evaluated": 0, "gate_neutral_15m": 0, "raw_signals": 0,
        "skipped_cooldown": 0, "skipped_daily_limit": 0, "skipped_session": 0,
    }
    trades_today = 0
    last_trade_date = None
    last_signal_direction = None
    last_signal_time = 0.0
    trades = []

    for i in range(BARS_PER_SIGNAL_WINDOW - 1, len(m5)):
        bar = m5[i]
        eval_dt = parse_dt(bar["time"]) + timedelta(minutes=5)  # close candle 5M
        stats["bars_evaluated"] += 1

        # window 15M yang terlihat bot saat eval (15M start < eval time)
        while j + 1 < len(m15) and parse_dt(m15[j + 1]["time"]) < eval_dt:
            j += 1
        if parse_dt(m15[j]["time"]) >= eval_dt:
            continue  # window 15M belum tersedia di titik ini
        c15 = m15[j - BARS_PER_SIGNAL_WINDOW + 1: j + 1]
        c5 = m5[i - BARS_PER_SIGNAL_WINDOW + 1: i + 1]
        if len(c15) < BARS_PER_SIGNAL_WINDOW:
            continue

        sig, out = quiet_signal(c5, c15)
        if sig is None:
            if "NEUTRAL — no trade" in out:
                stats["gate_neutral_15m"] += 1
            continue

        stats["raw_signals"] += 1

        # filter sesi (sama dengan SKIP_SESSIONS di main loop bot)
        if session_label(eval_dt) in getattr(bot, "SKIP_SESSIONS", []):
            stats["skipped_session"] += 1
            continue

        ist_day = eval_dt.astimezone(IST).date()
        if last_trade_date != ist_day:
            last_trade_date = ist_day
            trades_today = 0

        # filter yang sama dengan main() di bot.py
        if (last_signal_direction == sig["signal"]
                and eval_dt.timestamp() - last_signal_time < 2700):
            stats["skipped_cooldown"] += 1
            continue
        if trades_today >= bot.MAX_TRADES_PER_DAY:
            stats["skipped_daily_limit"] += 1
            continue

        trades_today += 1
        last_signal_direction = sig["signal"]
        last_signal_time = eval_dt.timestamp()

        # ---- eksekusi: entry = open bar berikutnya ----
        if i + 1 >= len(m5):
            break
        entry = m5[i + 1]["open"]
        if sig["signal"] == "LONG":
            sl, tp = entry - sig["sl_dollars"], entry + sig["tp_dollars"]
        else:
            sl, tp = entry + sig["sl_dollars"], entry - sig["tp_dollars"]

        risk_d = sig["potential_loss"]

        outcome, exit_price, exit_dt, r_multiple = None, None, None, 0.0
        for k in range(i + 1, min(i + 1 + MAX_HOLD_BARS, len(m5))):
            kb = m5[k]
            hi, lo = kb["high"], kb["low"]
            hit_sl = lo <= sl if sig["signal"] == "LONG" else hi >= sl
            hit_tp = hi >= tp if sig["signal"] == "LONG" else lo <= tp
            if hit_sl:  # SL & TP kena di bar sama → SL dulu (konservatif)
                outcome, exit_price, r_multiple = "SL", sl, -1.0
                break
            if hit_tp:
                outcome, exit_price, r_multiple = "TP", tp, 2.0
                break
            exit_price = kb["close"]
            exit_dt = parse_dt(kb["time"])
        if outcome is None:
            if exit_price is None:
                continue
            outcome = "TIMEOUT"
            r_multiple = ((exit_price - entry) / sig["sl_dollars"]
                          if sig["signal"] == "LONG"
                          else (entry - exit_price) / sig["sl_dollars"])
        if exit_dt is None:
            exit_dt = eval_dt

        pnl = r_multiple * risk_d
        trades.append({
            "signal_time": bar["time"] + "+00:00",
            "exit_time": exit_dt.strftime("%Y-%m-%d %H:%M") + "+00:00",
            "direction": sig["signal"], "session": session_label(eval_dt),
            "confidence": sig["confidence"], "entry": round(entry, 2),
            "sl": round(sl, 2), "tp": round(tp, 2),
            "lots": sig["lots"], "risk_dollars": risk_d, "outcome": outcome,
            "r": round(r_multiple, 3), "pnl_dollars": round(pnl, 2),
            "reasons": sig["reasons"], "tf_bias": sig["tf_bias"],
        })

    return stats, trades


def report(stats, trades, m5):
    print("\n" + "=" * 64)
    print("BACKTEST REPORT — XAUUSD SMC Bot (bot.py)")
    print("=" * 64)
    span = _span_days(m5[:-1])
    print(f"Periode            : {m5[0]['time']} -> {m5[-2]['time']} UTC  ({span:.1f} hari)")
    print(f"Bars 5M dievaluasi : {stats['bars_evaluated']}")
    print(f"Gate 15M NEUTRAL   : {stats['gate_neutral_15m']}")
    print(f"Sinyal mentah      : {stats['raw_signals']}")
    print(f"  - skip cooldown  : {stats['skipped_cooldown']}")
    print(f"  - skip limit/hari: {stats['skipped_daily_limit']}")
    print(f"  - skip sesi      : {stats['skipped_session']}")
    print(f"Trade dieksekusi   : {len(trades)}")

    if not trades:
        print("\nTidak ada trade sama sekali.")
        return

    wins = [t for t in trades if t["outcome"] == "TP"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]
    n = len(trades)

    gross_win = sum(t["pnl_dollars"] for t in wins)
    gross_loss = -sum(t["pnl_dollars"] for t in losses)
    total_pnl = sum(t["pnl_dollars"] for t in trades)
    total_r = sum(t["r"] for t in trades)

    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    win_rate = 100.0 * len(wins) / n
    avg_r = total_r / n
    expectancy = total_pnl / n

    eq, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t["pnl_dollars"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    print(f"\nHasil: {len(wins)} TP | {len(losses)} SL | {len(timeouts)} TIMEOUT")
    print(f"Win rate (TP saja): {win_rate:.1f}%  (breakeven @1:2 = 33.3%)")
    print(f"Profit factor      : {pf:.2f}")
    print(f"Total PnL          : ${total_pnl:+.2f}  (modal ${bot.CAPITAL})")
    print(f"Total R            : {total_r:+.2f} R  (avg {avg_r:+.3f} R/trade)")
    print(f"Expectancy         : ${expectancy:+.2f}/trade")
    print(f"Max drawdown       : ${max_dd:.2f}")

    print("\nPer arah:")
    for d in ("LONG", "SHORT"):
        ts = [t for t in trades if t["direction"] == d]
        if not ts:
            continue
        w = sum(1 for t in ts if t["outcome"] == "TP")
        print(f"  {d:5s}: {len(ts):3d} trade | WR {100*w/len(ts):5.1f}% | "
              f"PnL ${sum(t['pnl_dollars'] for t in ts):+8.2f}")

    print("\nPer sesi (IST):")
    for s in ("Asian Session 🌏", "London Session 🇬🇧", "New York Session 🗽", "Off Hours 🌙"):
        ts = [t for t in trades if t["session"] == s]
        if not ts:
            continue
        w = sum(1 for t in ts if t["outcome"] == "TP")
        print(f"  {s:10s}: {len(ts):3d} trade | WR {100*w/len(ts):5.1f}% | "
              f"PnL ${sum(t['pnl_dollars'] for t in ts):+8.2f}")

    print("\n10 trade pertama:")
    for t in trades[:10]:
        print(f"  {t['signal_time']}  {t['direction']:5s} @{t['entry']:>9.2f}  "
              f"{t['outcome']:7s}  {t['r']:+.2f}R  ${t['pnl_dollars']:+.2f}  conf={t['confidence']}")

    os.makedirs(LOG_DIR, exist_ok=True)
    trades_path = os.path.join(LOG_DIR, "backtest_trades.jsonl")
    with open(trades_path, "w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    summary = {
        "period": [m5[0]["time"], m5[-2]["time"]], "days": round(span, 1),
        "bars_evaluated": stats["bars_evaluated"], "raw_signals": stats["raw_signals"],
        "trades": n, "wins": len(wins), "losses": len(losses), "timeouts": len(timeouts),
        "win_rate": round(win_rate, 2), "profit_factor": round(pf, 3),
        "total_pnl_dollars": round(total_pnl, 2), "total_r": round(total_r, 2),
        "expectancy_dollars": round(expectancy, 3),
        "max_drawdown_dollars": round(max_dd, 2),
        "config": {
            "MIN_CONFIDENCE": bot.MIN_CONFIDENCE, "CAPITAL": bot.CAPITAL,
            "RISK_PERCENT": bot.RISK_PERCENT,
            "MAX_TRADES_PER_DAY": bot.MAX_TRADES_PER_DAY,
            "MIN_SL_DOLLARS": bot.MIN_SL_DOLLARS, "MAX_SL_DOLLARS": bot.MAX_SL_DOLLARS,
            "MAX_HOLD_BARS": MAX_HOLD_BARS,
        },
    }
    with open(os.path.join(LOG_DIR, "backtest_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nDetail trade : {trades_path}")
    print(f"Ringkasan    : {os.path.join(LOG_DIR, 'backtest_summary.json')}")


def main():
    args = sys.argv[1:]
    days = 60
    if "--days" in args:
        days = int(args[args.index("--days") + 1])

    api_key = load_api_key()
    # 5M: target utama; 15M: +buffer 2 hari untuk warmup window 100 bar
    m5 = fetch_series("5min", days, max_requests=6, api_key=api_key)
    m15 = fetch_series("15min", days + 3, max_requests=3, api_key=api_key)

    if len(m5) < BARS_PER_SIGNAL_WINDOW + 50 or len(m15) < BARS_PER_SIGNAL_WINDOW + 10:
        print("Data tidak cukup untuk backtest.")
        return 1

    stats, trades = run_simulation(m5, m15)
    report(stats, trades, m5)
    return 0


if __name__ == "__main__":
    sys.exit(main())


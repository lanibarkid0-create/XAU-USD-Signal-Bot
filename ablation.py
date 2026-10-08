"""
Ablation study: bandingkan konfigurasi pada data 5M/15M yang sama.

Konfigurasi diuji:
  1. Baseline lama        : sizing lama (2x budget), conf 65, tanpa filter sesi
  2. Sizing baru saja     : strict budget, conf 65, tanpa filter sesi
  3. Sizing + conf70
  4. Sizing + skip London
  5. Sizing + conf70 + skip London  (konfigurasi "deploy" saat ini)
  6. conf70 + skip London, sizing lama
  7. Sizing + skip London + skip Off Hours

Usage: python ablation.py
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

import bot      # noqa: E402
import backtest  # noqa: E402

NEW_CALCULATE_LOTS = bot.calculate_lots  # versi strict (baru)


def old_calculate_lots(sl_dollars):
    """Versi lama bot.py — ada di sini hanya untuk perbandingan."""
    if sl_dollars <= 0:
        return 0.01, 0, False
    raw_lots = (bot.RISK_AMOUNT / sl_dollars) * 0.01
    lots = round(raw_lots, 2)
    if lots < 0.01:
        return 0.01, round(sl_dollars * 1.0, 2), False
    lots = min(lots, 0.50)
    actual_risk = sl_dollars * (lots / 0.01) * bot.USD_PER_DOLLAR_MOVE_PER_001_LOT
    is_safe = actual_risk <= bot.RISK_AMOUNT * 2.0
    return lots, round(actual_risk, 2), is_safe


LONDON = ["London Session \U0001F1EC\U0001F1E7"]
OFF_HOURS = ["Off Hours \U0001F319"]

CONFIGS = [
    ("1 baseline lama (conf65, sizing lama, tanpa filter)",
     dict(sizing="old", conf=65, skip=[])),
    ("2 sizing baru saja (conf65, tanpa filter sesi)",
     dict(sizing="new", conf=65, skip=[])),
    ("3 sizing + conf70",
     dict(sizing="new", conf=70, skip=[])),
    ("4 sizing + skip London",
     dict(sizing="new", conf=65, skip=LONDON)),
    ("5 sizing + conf70 + skip London (deploy saat ini)",
     dict(sizing="new", conf=70, skip=LONDON)),
    ("6 conf70 + skip London, sizing lama",
     dict(sizing="old", conf=70, skip=LONDON)),
    ("7 sizing + skip London + skip Off Hours",
     dict(sizing="new", conf=65, skip=LONDON + OFF_HOURS)),
]


def metrics(trades):
    if not trades:
        return dict(n=0, wr=0, pf=0, total_r=0, avg_r=0, pnl=0, dd_r=0, dd_usd=0)
    wins = [t for t in trades if t["outcome"] == "TP"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    gw = sum(t["pnl_dollars"] for t in wins)
    gl = -sum(t["pnl_dollars"] for t in losses)
    total_r = sum(t["r"] for t in trades)
    eq = peak = dd = 0.0
    eq_r = peak_r = dd_r = 0.0
    for t in trades:
        eq += t["pnl_dollars"]; peak = max(peak, eq); dd = max(dd, peak - eq)
        eq_r += t["r"]; peak_r = max(peak_r, eq_r); dd_r = max(dd_r, peak_r - eq_r)
    return dict(
        n=len(trades), wr=100 * len(wins) / len(trades),
        pf=(gw / gl) if gl > 0 else float("inf"),
        total_r=total_r, avg_r=total_r / len(trades),
        pnl=sum(t["pnl_dollars"] for t in trades),
        dd_r=dd_r, dd_usd=dd,
    )


def main():
    api_key = backtest.load_api_key()
    m5 = backtest.fetch_series("5min", 60, 6, api_key=api_key)
    m15 = backtest.fetch_series("15min", 63, 3, api_key=api_key)

    print("\n%-58s %5s %6s %5s %8s %8s %7s %7s" % (
        "Konfigurasi", "n", "WR%", "PF", "totR", "avgR", "DD_R", "DD$"))
    print("-" * 120)

    for name, cfg in CONFIGS:
        bot.calculate_lots = NEW_CALCULATE_LOTS if cfg["sizing"] == "new" else old_calculate_lots
        bot.MIN_CONFIDENCE = cfg["conf"]
        bot.SKIP_SESSIONS = cfg["skip"]

        stats, trades = backtest.run_simulation(m5, m15)
        m = metrics(trades)
        print("%-58s %5d %6.1f %5.2f %+8.1f %+8.3f %7.1f %7.0f" % (
            name, m["n"], m["wr"], m["pf"], m["total_r"], m["avg_r"],
            m["dd_r"], m["dd_usd"]))

    # kembalikan ke konfigurasi default (sama dengan bot.py saat ini)
    bot.calculate_lots = NEW_CALCULATE_LOTS
    bot.MIN_CONFIDENCE = 70
    bot.SKIP_SESSIONS = LONDON


if __name__ == "__main__":
    main()

"""
Studi komponen skor SMC: mana yang benar-benar membedakan menang vs kalah?

Lapis 1 (presence):  dari logs/backtest_trades.jsonl — WR/avgR saat komponen
                      menyala vs mati pada 488 trade tersimpan.
Lapis 2 (ablation):   matikan 1 komponen dari skor generate_signal(), jalankan
                      ulang simulasi penuh 69 hari → efek kausal ke threshold 65.

Usage: python component_study.py
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

import bot       # noqa: E402
import backtest  # noqa: E402
from ablation import metrics  # noqa: E402

# (label, predicate over reason string)
REASON_MAP = [
    ("15M structure",  lambda r: r.startswith("15M Bullish") or r.startswith("15M Bearish")),
    ("CHoCH bonus",    lambda r: r == "15M CHoCH Reversal"),
    ("M5 structure",   lambda r: r.startswith("M5 Bullish") or r.startswith("M5 Bearish")),
    ("Order Block",    lambda r: "Order Block" in r),
    ("Liq. sweep",     lambda r: "Swept" in r),
    ("Liq. grab",      lambda r: "Liquidity Grab" in r),
    ("FVG",            lambda r: "FVG" in r),
    ("Pattern",        lambda r: r.startswith("Pattern:")),
    ("RSI extreme",    lambda r: r.startswith("RSI ")),
]


def part1_presence():
    path = os.path.join("logs", "backtest_trades.jsonl")
    with open(path, encoding="utf-8") as f:
        trades = [json.loads(line) for line in f]
    n = len(trades)
    base_wr = 100 * sum(1 for t in trades if t["outcome"] == "TP") / n
    base_r = sum(t["r"] for t in trades) / n

    print("=" * 110)
    print(f"LAPIS 1 — PRESENCE ANALYSIS ({n} trade tersimpan, baseline WR {base_wr:.1f}%, avgR {base_r:+.3f})")
    print("=" * 110)
    print(f"{'Komponen':<14} {'nON':>4} {'WR_on':>6} {'avgR_on':>8} "
          f"{'nOFF':>5} {'WR_off':>6} {'avgR_off':>8} {'dWR':>7} {'dAvgR':>7}")
    print("-" * 110)
    for label, pred in REASON_MAP:
        on = [t for t in trades if any(pred(r) for r in t["reasons"])]
        off = [t for t in trades if not any(pred(r) for r in t["reasons"])]
        if not on or not off:
            print(f"{label:<14} {len(on):>4}  (komponen selalu on/off — tak bisa dibandingkan)")
            continue
        wr_on = 100 * sum(1 for t in on if t["outcome"] == "TP") / len(on)
        wr_off = 100 * sum(1 for t in off if t["outcome"] == "TP") / len(off)
        ar_on = sum(t["r"] for t in on) / len(on)
        ar_off = sum(t["r"] for t in off) / len(off)
        print(f"{label:<14} {len(on):>4} {wr_on:>6.1f} {ar_on:>+8.3f} "
              f"{len(off):>5} {wr_off:>6.1f} {ar_off:>+8.3f} {wr_on-wr_off:>+7.1f} {ar_on-ar_off:>+7.3f}")

    # bucket confidence (skor total) vs outcome
    print("\nSkor/confidence vs outcome:")
    buckets = {}
    for t in trades:
        b = (t["confidence"] // 5) * 5
        buckets.setdefault(b, []).append(t)
    for b in sorted(buckets):
        ts = buckets[b]
        wr = 100 * sum(1 for t in ts if t["outcome"] == "TP") / len(ts)
        ar = sum(t["r"] for t in ts) / len(ts)
        print(f"  conf {b:2d}-{b+4:2d}: n={len(ts):3d} | WR {wr:5.1f}% | avgR {ar:+.3f}")

    # jumlah alasan (konfluens) vs outcome
    print("\nJumlah konfluens (len reasons) vs outcome:")
    by_n = {}
    for t in trades:
        by_n.setdefault(len(t["reasons"]), []).append(t)
    for k in sorted(by_n):
        ts = by_n[k]
        wr = 100 * sum(1 for t in ts if t["outcome"] == "TP") / len(ts)
        ar = sum(t["r"] for t in ts) / len(ts)
        print(f"  {k} alasan: n={len(ts):3d} | WR {wr:5.1f}% | avgR {ar:+.3f}")

        print(f"  {k} alasan: n={len(ts):3d} | WR {wr:5.1f}% | avgR {ar:+.3f}")


# ===================== LAPIS 2 — DROP-ONE ABLATION =====================
# Setiap konfigurasi MENONAKTIFKAN 1 komponen dari skor generate_signal(),
# lalu menjalankan ulang simulasi penuh 69 hari pada data yang sama.
# restore() mengembalikan fungsi asli antar konfigurasi.

_ORIG = {}


def _wrap(name, fn):
    if name not in _ORIG:
        _ORIG[name] = getattr(bot, name)
    setattr(bot, name, fn)


def restore():
    for name, fn in _ORIG.items():
        setattr(bot, name, fn)
    _ORIG.clear()


def drop_fvg():
    _wrap("detect_fvg", lambda candles: (None, 0))


def drop_pattern():
    _wrap("check_candle_pattern", lambda candles: (None, 0))


def drop_rsi():
    # RSI netral 50 → tak pernah kena <35 / >65 → skor RSI mati total
    _wrap("calculate_rsi", lambda candles, period=14: 50)


def drop_grab():
    _wrap("detect_liquidity_grab", lambda candles: (None, 0))


def drop_sweep():
    # eq_highs/eq_lows dipertahankan untuk data display, hanya swept (skor) dimatikan
    _wrap("detect_liquidity_zones", lambda candles: (0, 0, False, False))


def drop_ob_score():
    # ob_high/ob_low DIPERTAHANKAN untuk penempatan SL; hanya price_in_ob (skor +20) mati
    orig = _ORIG.get("detect_order_block") or bot.detect_order_block

    def _no_score(candles, bias):
        h, l, _inob = orig(candles, bias)
        return h, l, False

    _wrap("detect_order_block", _no_score)


def _structure_drop(which):
    """which='m5': hasil call 5M dipaksa NEUTRAL (skor +15 mati, gate 15M utuh).
       which='choch': flag choch pada call 15M dipaksa False (bonus +10 mati)."""
    orig = _ORIG.get("analyze_structure_bos") or bot.analyze_structure_bos
    orig_gen = _ORIG.get("generate_signal") or bot.generate_signal
    state = {"n": 0}

    def _counted(candles):
        state["n"] += 1
        bias, bos, choch = orig(candles)
        is_15m = state["n"] % 2 == 1  # genap = panggilan ke-2 (5M)
        if which == "m5" and not is_15m:
            return "NEUTRAL", None, False
        if which == "choch" and is_15m:
            return bias, bos, False
        return bias, bos, choch

    def _reset_then(*a, **k):
        state["n"] = 0
        return orig_gen(*a, **k)

    _wrap("analyze_structure_bos", _counted)
    _wrap("generate_signal", _reset_then)


def drop_m5_structure():
    _structure_drop("m5")


def drop_choch_bonus():
    _structure_drop("choch")


DROPS = [
    ("baseline (tanpa drop)",   None),
    ("DROP Order Block (+20)",  drop_ob_score),
    ("DROP M5 structure (+15)", drop_m5_structure),
    ("DROP Liq. sweep (+15)",   drop_sweep),
    ("DROP CHoCH bonus (+10)",  drop_choch_bonus),
    ("DROP FVG (+10)",          drop_fvg),
    ("DROP Liq. grab (+10)",    drop_grab),
    ("DROP Pattern (+s/d 10)",  drop_pattern),
    ("DROP RSI extreme (+8)",   drop_rsi),
]


def part2_ablation():
    api_key = backtest.load_api_key()
    m5 = backtest.fetch_series("5min", 60, 6, api_key=api_key)
    m15 = backtest.fetch_series("15min", 63, 3, api_key=api_key)

    # konfigurasi final bot (conf65, strict sizing, tanpa filter sesi)
    bot.MIN_CONFIDENCE = 65
    bot.SKIP_SESSIONS = []

    print("\n" + "=" * 112)
    print("LAPIS 2 — DROP-ONE ABLATION (simulasi ulang penuh per komponen)")
    print("=" * 112)
    print(f"{'Konfigurasi':<30} {'n':>4} {'WR%':>6} {'PF':>5} {'totR':>7} "
          f"{'avgR':>7} {'DD$':>6}  vs baseline")
    print("-" * 112)

    base = None
    for name, drop_fn in DROPS:
        restore()
        if drop_fn:
            drop_fn()
        stats, trades = backtest.run_simulation(m5, m15)
        m = metrics(trades)
        if base is None:
            base = dict(m)
            delta = "(baseline)"
        else:
            delta = (f"dR {m['total_r'] - base['total_r']:+7.1f} | "
                     f"dWR {m['wr'] - base['wr']:+5.1f}pp")
        print(f"{name:<30} {m['n']:>4} {m['wr']:>6.1f} {m['pf']:>5.2f} "
              f"{m['total_r']:>+7.1f} {m['avg_r']:>+7.3f} {m['dd_usd']:>6.0f}  {delta}")
    restore()


if __name__ == "__main__":
    part1_presence()
    part2_ablation()

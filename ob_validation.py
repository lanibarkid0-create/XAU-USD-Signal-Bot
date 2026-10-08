"""
Validasi out-of-sample: apakah menonaktifkan skor Order Block (OB_SCORE=0)
mengungguli baseline di KEDUA paruh waktu (bukan cuma di sampel penuh)?

Data 69 hari di-split jadi paruh-1 (awal) & paruh-2 (akhir). Tiap paruh
dijalankan 2x: baseline vs drop-OB (repatch dari component_study.py).
Verdict konsisten = tanpa-OB menang di kedua paruh → boleh diterapkan.

Usage: python ob_validation.py
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

import bot               # noqa: E402
import backtest          # noqa: E402
import component_study as cs  # noqa: E402
from ablation import metrics  # noqa: E402


def run_case(m5, m15, drop_ob):
    cs.restore()
    if drop_ob:
        cs.drop_ob_score()
    _stats, trades = backtest.run_simulation(m5, m15)
    m = metrics(trades)
    cs.restore()
    return m


def main():
    # konfigurasi final bot yang divalidasi (conf65, strict sizing, tanpa filter sesi)
    bot.MIN_CONFIDENCE = 65
    bot.SKIP_SESSIONS = []

    api_key = backtest.load_api_key()
    m5 = backtest.fetch_series("5min", 60, 6, api_key=api_key)
    m15 = backtest.fetch_series("15min", 63, 3, api_key=api_key)

    split = len(m5) // 2
    halves = [
        ("Paruh 1 (30 Jul - pertengahan)", m5[:split]),
        ("Paruh 2 (pertengahan - 8 Okt)",  m5[split - backtest.BARS_PER_SIGNAL_WINDOW:]),
    ]

    print("\n" + "=" * 96)
    print("VALIDASI OUT-OF-SAMPLE — baseline vs tanpa skor Order Block")
    print("=" * 96)
    print(f"{'Paruh':<32} {'Konfig':<18} {'n':>4} {'WR%':>6} {'PF':>5} {'totR':>7} {'avgR':>7}")
    print("-" * 96)

    consistent = True
    for label, h5 in halves:
        b = run_case(h5, m15, drop_ob=False)
        n = run_case(h5, m15, drop_ob=True)
        for cfg_name, m in (("baseline", b), ("tanpa OB score", n)):
            print(f"{label:<32} {cfg_name:<18} {m['n']:>4} {m['wr']:>6.1f} "
                  f"{m['pf']:>5.2f} {m['total_r']:>+7.1f} {m['avg_r']:>+7.3f}")
        win = n["total_r"] > b["total_r"]
        consistent = consistent and win
        print(f"{'':<32} -> {'TANPA-OB MENANG' if win else 'BASELINE MENANG'} "
              f"({n['total_r'] - b['total_r']:+.1f}R)\n")

    print("-" * 96)
    print("VERDICT:", "KONSISTEN — tanpa-OB unggul di kedua paruh ✓"
          if consistent else "TIDAK KONSISTEN — jangan terapkan perubahan ✗")


if __name__ == "__main__":
    main()

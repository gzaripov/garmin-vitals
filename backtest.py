# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare sleep and strain estimates with your own WHOOP API export.

    uv run backtest.py

Requires private data/whoop.json with `cycles` and `sleep` arrays from the
WHOOP API, alongside the normalized Garmin files. No export is included.
Wear gaps dominate disagreement; report both all paired days and clean-wear
days. Agreement with another wearable is not clinical validation.
"""
import json
import math
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA as BASE

from scores import (series, sleep_score, strain, SS_HINGE, SS_B, STRAIN_B,
                          CONS_INTERCEPT, CONS_BED, CONS_WAKE)

H = 3600e3


def _loc(iso, off):
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if not off or off == "Z":
        return ts
    sg = 1 if off.startswith("+") else -1
    return ts + sg * timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))


def whoop_truth():
    path = BASE / "whoop.json"
    if not path.exists():
        raise SystemExit(
            "Comparison needs your own whoop.json (cycles and sleep arrays) in "
            "the configured data directory. No personal export is bundled.")
    w = json.loads(path.read_text())
    out = {}
    for c in w["cycles"]:
        sc = c.get("score") or {}
        if sc.get("strain") is None or not c.get("end"):
            continue
        d = _loc(c["end"], c.get("timezone_offset") or "+00:00").date().isoformat()
        out.setdefault(d, {}).update(strain=sc["strain"], cyc_max=sc.get("max_heart_rate"),
                                     kcal=(sc.get("kilojoule") or 0) / 4.184)
    for s in w["sleep"]:
        sc = s.get("score") or {}
        if not sc or s.get("nap"):
            continue
        d = _loc(s["end"], s.get("timezone_offset") or "+00:00").date().isoformat()
        ss = sc["stage_summary"]
        asleep = (ss["total_light_sleep_time_milli"] + ss["total_slow_wave_sleep_time_milli"]
                  + ss["total_rem_sleep_time_milli"]) / H
        out.setdefault(d, {}).update(
            perf=sc["sleep_performance_percentage"], eff=sc["sleep_efficiency_percentage"],
            cons=sc["sleep_consistency_percentage"], asleep=asleep,
            need=sum(sc["sleep_needed"].values()) / H)
    return out


def stats(pairs, label, bands=None):
    if not pairs:
        print(f"  {label:38s} no data")
        return
    y = [a for a, _ in pairs]
    p = [b for _, b in pairs]
    e = [b - a for a, b in pairs]
    my, mp = st.mean(y), st.mean(p)
    num = sum((a - my) * (b - mp) for a, b in pairs)
    den = math.sqrt(sum((a - my) ** 2 for a in y) * sum((b - mp) ** 2 for b in p))
    line = (f"  {label:38s} n={len(pairs):3d}  mae {st.mean(map(abs, e)):5.2f}  "
            f"med {st.median(list(map(abs, e))):5.2f}  rmse {math.sqrt(st.mean([x*x for x in e])):5.2f}  "
            f"bias {st.mean(e):+5.2f}  r {num/den if den else float('nan'):.3f}")
    if bands:
        ay = [bands(a) for a in y]
        ap = [bands(b) for b in p]
        po = sum(1 for a, b in zip(ay, ap) if a == b) / len(ay)
        ks = set(ay) | set(ap)
        pe = sum((ay.count(k) / len(ay)) * (ap.count(k) / len(ap)) for k in ks)
        w1 = sum(1 for a, b in zip(ay, ap) if abs(a - b) <= 1) / len(ay)
        line += f"  band {po:.0%} k {(po-pe)/(1-pe):.2f} w1 {w1:.0%}"
    print(line)


def main():
    W = whoop_truth()
    G = series()
    if not W or not G:
        raise SystemExit("Comparison needs scored WHOOP nights and Garmin days.")
    gd = json.loads((BASE / "garmin_hrv_resp.json").read_text())
    sband = lambda v: 0 if v < 10 else (1 if v < 14 else (2 if v < 18 else 3))

    print(f"replica days {min(G)} .. {max(G)};  WHOOP truth {min(W)} .. {max(W)}\n")

    # ---------------- strain ----------------------------------------------------
    print("DAY STRAIN   strain = %.3f %+.3f*ln(1+training_load) %+.3f*ln(1+active_kcal)"
          % STRAIN_B)
    allp, clean, dropped = [], [], []
    for d, g in G.items():
        w = W.get(d)
        if not w or g.get("strain") is None or w.get("strain") is None:
            continue
        allp.append((w["strain"], g["strain"]))
        tot = (gd.get(d) or {}).get("kcal_total")
        gmax = max((gd.get(d) or {}).get("hr_max_day") or 0, g.get("act_max_hr") or 0)
        worn = (tot and w.get("kcal", 0) >= 0.70 * tot
                and abs(gmax - (w.get("cyc_max") or 0)) <= 15)
        (clean if worn else dropped).append((w["strain"], g["strain"]))
    stats(allp, "all paired days", sband)
    stats(clean, "both devices worn all day", sband)
    stats(dropped, "one device with a gap", sband)
    print(f"  -> {len(clean)}/{len(allp)} = {len(clean)/max(1,len(allp)):.0%} of days have "
          f"clean wear on BOTH devices")
    worst = sorted(dropped, key=lambda t: -abs(t[0] - t[1]))[:3]
    print("     largest gap-day disagreements (WHOOP vs replica): "
          + ", ".join(f"{a:.1f}/{b:.1f}" for a, b in worst))

    # ---------------- sleep -----------------------------------------------------
    print(f"\nSLEEP SCORE  score = {SS_B[0]:.2f} {SS_B[1]:+.2f}*dur {SS_B[2]:+.2f}*min(dur,{SS_HINGE})"
          f" {SS_B[3]:+.3f}*eff {SS_B[4]:+.3f}*cons")
    comp, legacy, gscore, need, asleep, eff, cons = [], [], [], [], [], [], []
    for d, g in G.items():
        w = W.get(d)
        if not w or w.get("perf") is None or g.get("sleep_score") is None:
            continue
        comp.append((w["perf"], g["sleep_score"]))
        legacy.append((w["perf"], g["sleep_score_legacy"]))
        if g.get("garmin_sleep_score"):
            gscore.append((w["perf"], float(g["garmin_sleep_score"])))
        if w.get("need"):
            need.append((w["need"] * 60, g["need_h"] * 60))
        if w.get("asleep"):
            asleep.append((w["asleep"] * 60, g["asleep_h"] * 60))
        if w.get("eff") is not None and g.get("efficiency") is not None:
            eff.append((w["eff"], g["efficiency"]))
        if w.get("cons") is not None and g.get("consistency") is not None:
            cons.append((w["cons"], g["consistency"]))
    pband = lambda v: 0 if v < 70 else (1 if v < 85 else 2)
    stats(comp, "replica composite", pband)
    stats(legacy, "legacy 100*asleep/need", pband)
    stats(gscore, "Garmin's own sleep score", pband)
    print("  components (WHOOP vs Garmin):")
    stats(asleep, "time asleep, minutes")
    stats(need, "sleep need, minutes")
    stats(eff, "sleep efficiency, points")
    stats(cons, "sleep consistency, points")
    lo = [(a, b) for a, b in comp if a < 70]
    if lo:
        print(f"  KNOWN WEAKNESS: on WHOOP's {len(lo)} worst nights (<70) the estimate reads "
              f"{st.mean(b for _, b in lo):.0f} against WHOOP's {st.mean(a for a, _ in lo):.0f}.")
    print("  Device disagreement about awake time limits sleep-score agreement.")


if __name__ == "__main__":
    main()

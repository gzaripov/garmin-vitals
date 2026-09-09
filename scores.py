# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WHOOP-style Sleep Score and Day Strain, rebuilt on Garmin data.

    uv run scores.py [YYYY-MM-DD] [--demo]

Two descriptive scores, separate from the recovery signal:

    sleep score  0-100   how well last night covered what the body needed
    day strain   0-21    log-compressed cardiovascular load, sleep onset to sleep onset

Both are REPLICAS. Constants were estimated from an anonymous paired-device sample
(656 scored nights, 671 cycles), then driven by Garmin inputs. Aggregate fit evidence
below describes agreement in that sample, not clinical validation or universal accuracy.
Neither score is an exercise prescription or a clinical safety gate.

--------------------------------------------------------------------------------------
The older sleep formula was exactly 100 * asleep / need (21-night mean absolute error
0.22 points). The later composite combines duration, efficiency and consistency.
`legacy=True` gives the older, simpler formula.
"""
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone

from config import DATA as BASE

# ------------------------------------------------------------------ sleep need
# From WHOOP's own `sleep_needed` breakdown over 656 nights:
#   baseline      7.72 h, flat. (8.36 h during the two-week calibration only.) It falls
#                 ~2 min per YEAR -- an age term, not adaptation to sleep habits.
#   debt          0.362 * SUM_i 0.25^i * shortfall(i nights back), capped at 2.13 h.
#                 rmse 12 min. Decay 0.25 means last night carries ~80% of it, so this
#                 is a short memory: two good nights clear almost all of it.
#   recent strain 0.0536 * EWMA(strain, decay 0.25) - 0.246 h, floored at 0. rmse 10 min.
#                 About 20 min of extra need at strain 12, 35 min at strain 16.
#   nap credit    MINUS the nap sleep, hour for hour. Exact on 25 of 25 nights.
SLEEP_BASELINE_H = 7.72
DEBT_DECAY = 0.25
DEBT_GAIN = 0.362
DEBT_CAP_H = 2.13
STRAIN_NEED_GAIN = 0.0536
STRAIN_NEED_OFFSET = 0.246

# ----------------------------------------------------------------- sleep score
# Fitted to WHOOP's post-5.0 score on WHOOP's OWN inputs, 435 nights: mae 1.79 points.
# That is a faithful recovery of their formula, not a correlation.
#
# The hinge is the interesting part. Below 66% of need the duration term has slope
# ~121 points per unit of duration; above it, ~21. WHOOP's sleep score is therefore
# nearly flat once two thirds of need is met and falls off sharply below it.
SS_HINGE = 0.66
SS_B = (-75.29, 20.94, 100.25, 0.584, 0.272)   # 1, dur, min(dur,hinge), eff, cons

# --------------------------------------------------------------- consistency
# WHOOP compares tonight's clock times with the previous nights'. Fitted against their
# own consistency percentage: using WHOOP's clock times R2 0.63, using Garmin's R2 0.42
# (rmse 10 points) -- so this is the weakest link in the chain. Three prior nights beat
# four and five. Deviations are in hours, wrapped circularly.
CONS_INTERCEPT, CONS_BED, CONS_WAKE = 84.85, 6.04, 4.80
CONS_NIGHTS = 3

# GARMIN'S SLEEP EFFICIENCY IS NEARLY NOISE AGAINST WHOOP'S, and it carries the biggest
# coefficient in the composite (0.584), so it was the single largest source of error.
# Measured over 383 paired nights: r = 0.265, and Garmin reads 2.7 points high on average
# (95.7 vs 92.9) because it counts far less wake-after-sleep-onset (0.34 h vs 0.56 h,
# r = 0.282 between the two awake times). A flat constant beat every Garmin-derived
# efficiency formula outright (mae 2.93 vs 4.06), and using the sleep WINDOW as time in
# bed instead of sleep+awake changed nothing (r 0.292 either way).
#
# Rather than pin a constant and pretend it is a measurement, the reading is regressed
# onto WHOOP's scale. The 0.20 slope IS the finding: it shrinks Garmin's efficiency 80%
# toward its own mean, which is what a predictor this weak is worth. End-to-end that
# takes the sleep score from mae 4.59 to 3.52 and the bias from +2.54 to +0.95
# (5-fold chronological CV: 3.51).
EFF_CAL = (73.80, 0.2003)


def efficiency_pct(asleep_h, in_bed_h, calibrate=True):
    """Sleep efficiency on WHOOP's scale. `calibrate=False` gives Garmin's raw number."""
    if not in_bed_h:
        return None
    raw = 100.0 * asleep_h / in_bed_h
    if not calibrate:
        return raw
    a, b = EFF_CAL
    return max(0.0, min(100.0, a + b * raw))


# --------------------------------------------------------------------- strain
# strain = -3.842 + 1.148*ln(1+training_load) + 1.759*ln(1+active_kcal), clipped 0..21.
# Fitted on 159 cycle-days where BOTH devices were demonstrably worn all day; 5-fold
# chronological block CV gives mae 1.31, median |error| 1.03, r 0.895, and the same
# WHOOP strain band on 76% of days with 100% inside one band.
#
# Garmin's EPOC `activityTrainingLoad` does the work here. Two alternatives were tested
# and rejected: the 2-minute all-day HR series added no predictive value after training
# load (CV MAE 1.563 -> 1.627). Smoothed all-day HR clips workout peaks.
# Time-in-HR-zone with free weights was also worse (CV r 0.58).
STRAIN_B = (-3.842, 1.148, 1.759)


def sleep_need_h(prior_shortfalls_h=(), strain_ewma=None, nap_h=0.0):
    """WHOOP sleep need, in hours. `prior_shortfalls_h` is most-recent-first."""
    debt = DEBT_GAIN * sum(DEBT_DECAY ** i * max(0.0, s)
                           for i, s in enumerate(prior_shortfalls_h))
    debt = min(DEBT_CAP_H, max(0.0, debt))
    st = 0.0 if strain_ewma is None else max(
        0.0, STRAIN_NEED_GAIN * strain_ewma - STRAIN_NEED_OFFSET)
    nap = max(0.0, nap_h or 0.0)
    return {"baseline_h": SLEEP_BASELINE_H, "debt_h": round(debt, 3),
            "strain_h": round(st, 3), "nap_h": -round(nap, 3),
            "need_h": round(SLEEP_BASELINE_H + debt + st - nap, 3)}


def sleep_score(asleep_h, need_h, efficiency_pct=None, consistency_pct=None,
                legacy=False):
    """0-100. `legacy` reproduces WHOOP's pre-5.0 score: 100 * asleep / need."""
    if not need_h or asleep_h is None:
        return None
    dur = asleep_h / need_h
    if legacy:
        return round(max(0.0, min(100.0, 100.0 * dur)), 1)
    if efficiency_pct is None or consistency_pct is None:
        return None
    b = SS_B
    s = (b[0] + b[1] * dur + b[2] * min(dur, SS_HINGE)
         + b[3] * efficiency_pct + b[4] * consistency_pct)
    return round(max(0.0, min(100.0, s)), 1)


def consistency_pct(bedtimes_h, waketimes_h):
    """Sleep consistency 0-100. Both lists are hours-of-day, tonight FIRST."""
    n = min(CONS_NIGHTS, len(bedtimes_h) - 1, len(waketimes_h) - 1)
    if n < 1:
        return None

    def circ(a, b):
        d = abs(a - b) % 24
        return min(d, 24 - d)

    dev_b = sum(circ(bedtimes_h[0], bedtimes_h[i]) for i in range(1, n + 1)) / n
    dev_w = sum(circ(waketimes_h[0], waketimes_h[i]) for i in range(1, n + 1)) / n
    return round(max(0.0, min(100.0, CONS_INTERCEPT - CONS_BED * dev_b
                              - CONS_WAKE * dev_w)), 1)


def strain(training_load, active_kcal):
    """0-21 day strain from Garmin's EPOC training load and daily active calories."""
    if training_load is None or active_kcal is None:
        return None
    b = STRAIN_B
    s = b[0] + b[1] * math.log1p(max(0.0, training_load)) + b[2] * math.log1p(max(0.0, active_kcal))
    return round(max(0.0, min(21.0, s)), 2)


def strain_band(s):
    if s is None:
        return None
    return ("light" if s < 10 else "moderate" if s < 14
            else "strenuous" if s < 18 else "all-out")


# ---------------------------------------------------------------- data plumbing
def _load(name):
    p = BASE / name
    return json.loads(p.read_text()) if p.exists() else {}


def _clock_h(epoch_ms_local):
    """Garmin *TimestampLocal is epoch-ms already in local wall time,
    so it must be rendered as UTC or the offset gets applied twice."""
    t = datetime.fromtimestamp(epoch_ms_local / 1000, tz=timezone.utc)
    return t.hour + t.minute / 60


def _gmt_ms(value):
    """Garmin's zone-less GMT strings denote UTC, never the host's timezone."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


def series(days=None):
    """Compute both scores for every day Garmin has data for. Returns {date: {...}}."""
    gd = _load("garmin_hrv_resp.json")
    gi = _load("garmin_intraday.json")
    ga = _load("garmin_activities.json")
    gh = _load("garmin_healthspan.json")
    coverage = {d for d, r in gh.items() if r.get("activities_synced") is True}
    has_activities = (BASE / "garmin_activities.json").exists()

    onset = {d: r["sleep_start_gmt"] for d, r in gi.items() if r.get("sleep_start_gmt")}
    acts = sorted(((_gmt_ms(a["startTimeGMT"]), a)
                   for a in ga.values() if a.get("startTimeGMT")), key=lambda item: item[0])

    out, dates = {}, sorted(gd)
    for d in dates:
        r = gd[d] or {}
        row = {"date": d}

        # --- strain over the cycle: this sleep onset -> the next one -------------
        # TODAY HAS NO CLOSING ONSET YET, and the first version simply skipped it — so
        # on the evening of a hard run the app showed the previous day's number while
        # WHOOP showed the run. WHOOP accumulates strain live through the day, so the
        # open cycle is closed at `now` instead and flagged partial. Active calories are
        # a running daily total, which is exactly the right partial input.
        nd = (datetime.fromisoformat(d).date() + timedelta(days=1)).isoformat()
        end_ms, partial = onset.get(nd), False
        if end_ms is None and d == date.today().isoformat() and d in onset:
            end_ms, partial = datetime.now().timestamp() * 1000, True
        if d in onset and end_ms and 0 < end_ms - onset[d] < 30 * 3.6e6:
            win = [a for t, a in acts if onset[d] <= t < end_ms]
            # With explicit sync coverage, both calendar dates of a sleep-onset
            # cycle must have been fetched. Legacy full exports keep their math.
            covered = has_activities
            if coverage:
                start_local = gi[d].get("sleep_start_local", onset[d])
                first = datetime.fromtimestamp(start_local / 1000, tz=timezone.utc).date()
                last_local = (gi.get(nd) or {}).get("sleep_start_local")
                last = (datetime.fromtimestamp((last_local - 1) / 1000, tz=timezone.utc).date()
                        if last_local else date.fromisoformat(d))
                covered &= all((first + timedelta(days=k)).isoformat() in coverage
                               for k in range((last - first).days + 1))
                covered &= all(isinstance(a.get("activityTrainingLoad"), (int, float)) for a in win)
            tl = sum(a.get("activityTrainingLoad") or 0 for a in win) if covered else None
            row.update(training_load=round(tl, 1) if tl is not None else None, n_activities=len(win),
                       act_max_hr=max([a.get("maxHR") or 0 for a in win], default=0),
                       strain=strain(tl, r.get("kcal_active")), strain_partial=partial)
            row["strain_band"] = strain_band(row["strain"])

        # --- sleep ---------------------------------------------------------------
        asleep = (r.get("sleep_sec") or 0) / 3600 or None
        awake = (r.get("sleep_awake_sec") or 0) / 3600
        if asleep:
            eff_raw = 100 * asleep / (asleep + awake)
            eff = efficiency_pct(asleep, asleep + awake)
            beds, wakes = [], []
            cur = datetime.fromisoformat(d).date()
            for k in range(0, CONS_NIGHTS + 1):
                g = gi.get((cur - timedelta(days=k)).isoformat()) or {}
                if g.get("sleep_start_local") and g.get("sleep_end_local"):
                    beds.append(_clock_h(g["sleep_start_local"]))
                    wakes.append(_clock_h(g["sleep_end_local"]))
                else:
                    break
            cons = consistency_pct(beds, wakes) if len(beds) > 1 else None

            shortfalls = []
            for k in range(1, 6):
                pk = (cur - timedelta(days=k)).isoformat()
                prev = out.get(pk)
                p = gd.get(pk) or {}
                if prev and prev.get("need_h") and p.get("sleep_sec"):
                    shortfalls.append(max(0.0, prev["need_h"] - p["sleep_sec"] / 3600))
                else:
                    shortfalls.append(0.0)
            sew, wts = [], []
            for k in range(1, 5):
                prev = out.get((cur - timedelta(days=k)).isoformat())
                if prev and prev.get("strain") is not None:
                    sew.append(prev["strain"])
                    wts.append(DEBT_DECAY ** (k - 1))
            ew = sum(s * w for s, w in zip(sew, wts)) / sum(wts) if sew else None
            need = sleep_need_h(shortfalls, ew, (r.get("nap_sec") or 0) / 3600)
            row.update(need, asleep_h=round(asleep, 2), efficiency=round(eff, 1),
                       efficiency_garmin=round(eff_raw, 1),
                       consistency=cons,
                       sleep_score=sleep_score(asleep, need["need_h"], eff, cons),
                       sleep_score_legacy=sleep_score(asleep, need["need_h"], legacy=True),
                       garmin_sleep_score=r.get("sleep_score"))
        out[d] = row
    return {d: out[d] for d in dates[-days:]} if days else out


if __name__ == "__main__":
    want = next((arg for arg in sys.argv[1:] if not arg.startswith("--")), None)
    s = series()
    d = want or max(s, default=date.today().isoformat())
    print(json.dumps(s.get(d, {"date": d, "error": "no data"}), indent=1))

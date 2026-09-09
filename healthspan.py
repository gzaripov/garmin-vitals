# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Experimental wearable trend model, inspired by WHOOP Healthspan / WHOOP Age.

    uv run healthspan.py [--json]

METHOD, which is WHOOP's own stated method and not an invention here. WHOOP describes
WHOOP Age as: estimate a hazard ratio for each input, then convert risk to years with a
log-linear rule where "a 10% increase in mortality risk corresponds to roughly one year
of effective age". That is the Gompertz law of mortality — hazard rises exponentially
with age, doubling every ~8 years in adulthood — so:

    gamma        = ln(1.10) = 0.0953 per year        <- WHOOP's stated conversion
    delta_years  = ln(HR) / gamma
    biological age = chronological age + SUM(delta_years over the nine metrics)

The nine inputs are WHOOP's nine: VO2 max, resting heart rate, steps, hours of sleep,
sleep consistency, time in HR zones 1-3, time in zones 4-5, strength-training minutes,
and lean body mass. Every one of them is available from Garmin.

WHERE THE NUMBERS COME FROM. Each dose-response slope is a published all-cause-mortality
hazard ratio from a large cohort study or meta-analysis, cited on the metric itself
below. The REFERENCE POINT for each — the value at which the contribution is zero — is
taken from publicly described WHOOP targets. These references are model comparison
points, not personalized medical targets.

THREE HONEST LIMITATIONS, none of which WHOOP's version escapes either:

1. THE NINE INPUTS ARE NOT INDEPENDENT. A fit person has a high VO2 max AND a low
   resting heart rate AND high step counts, and those hazard ratios were each estimated
   without adjusting for the others. Multiplying all nine therefore over-counts one
   underlying trait. A configurable global gain changes the scale but does not resolve
   this confounding. It defaults to 1.0; matching another device is not validation.
2. EXTRAPOLATION IS CAPPED. Cohort studies estimate risk inside the range they observed.
   Each metric is capped at CAP_YEARS before shrinkage so that one extreme value cannot
   run away with the total.
3. THIS IS NOT A CLINICAL BIOLOGICAL AGE. It has no blood markers, no methylation, no
   diet, no alcohol, no smoking, no blood pressure, no family history. PhenoAge and
   DunedinPACE measure something this cannot see. Read it as "how do my habits score
   against mortality-cohort evidence", not as a medical estimate.
"""
import json
import statistics as st
import sys
from datetime import date, datetime, timedelta

from config import DATA as BASE, AGE, HEALTH_GAIN, HEALTH_DIR

from scores import consistency_pct, _clock_h, CONS_NIGHTS



GAMMA = 0.0953          # ln(1.10): WHOOP's stated "10% more risk ~= one year"
CAP_YEARS = 3.0         # per-metric cap before shrinkage
# Gain is explicit local configuration, never inferred from a personal record.
SHRINK = HEALTH_GAIN
PACE_SCALE = 0.71       # illustrative scale, not a validated longitudinal aging rate
CHRON_AGE = AGE


def _ln(x):
    import math
    return math.log(x)


# --- the nine metrics -----------------------------------------------------------------
# Each: slope is ln(HR) per unit; ref is the value at which the contribution is zero.
METRICS = [
    dict(key="vo2max", bar=(30, 60), label="VO₂ max", group="Fitness", unit=" ml/kg/min", ref=44.0,
         per=3.5, slope=_ln(0.87), better="higher", fmt="{:.1f}",
         cite="Kodama 2009 JAMA, meta-analysis n=102,980: HR 0.87 per 1-MET (3.5 ml/kg/min) of cardiorespiratory fitness."),
    dict(key="rhr", bar=(40, 80), label="Resting heart rate", group="Fitness", unit=" bpm", ref=60.0,
         per=10.0, slope=_ln(1.09), better="lower", fmt="{:.0f}",
         cite="Zhang 2016 CMAJ, meta-analysis n≈1.25M: HR 1.09 per 10 bpm higher resting heart rate."),
    dict(key="lean_pct", bar=(60, 90), label="Lean body mass", group="Body", unit="%", ref=80.0,
         per=5.0, slope=_ln(0.90), better="higher", fmt="{:.1f}",
         cite="Srikanthan & Karlamangla 2014 Am J Med, NHANES III: highest vs lowest muscle-mass quartile HR ≈ 0.81."),
    # The four volume metrics SATURATE. Their published hazard ratios are whole-range
    # contrasts (inactive vs active), and the curve is steep up to roughly the guideline
    # and near-flat beyond it. sat(x) = 1 - exp(-x/k); the contribution is full_ln_hr
    # * (sat(x) - sat(ref)), so higher volume has diminishing returns.
    dict(key="steps", bar=(0, 14000), label="Steps per day", group="Movement", unit="", ref=8000.0,
         sat_k=4000.0, full=_ln(0.50), better="higher", fmt="{:,.0f}",
         cite="Paluch 2022 Lancet Public Health, n=47,471: ~50% lower mortality highest vs lowest step quartile; risk falls steeply to ~8–10k then plateaus."),
    dict(key="zone13", bar=(0, 5), hm=True, label="Time in HR zones 1–3", group="Movement", unit=" h/wk", ref=2.5,
         sat_k=2.1, full=_ln(0.69), better="higher", fmt="{:.1f}",
         cite="Arem 2015 JAMA Intern Med, n=661,137: HR ≈ 0.69 at 1–2× the 150 min/wk guideline vs inactive, flattening beyond 3×."),
    dict(key="zone45", bar=(0, 150), label="Time in HR zones 4–5", group="Movement", unit=" min/wk", ref=75.0,
         sat_k=75.0, full=_ln(0.87), better="higher", fmt="{:.0f}",
         cite="Arem 2015 / Wang 2021: vigorous minutes add beyond moderate volume, flattening ~150 min/wk."),
    dict(key="strength", bar=(0, 120), label="Strength training", group="Movement", unit=" min/wk", ref=45.0,
         sat_k=30.0, full=_ln(0.83), better="higher", fmt="{:.0f}", jshape=130.0,
         cite="Momma 2022 BJSM, meta-analysis: J-shaped, 10–17% lower all-cause mortality peaking at 30–60 min/wk, benefit fading past ~130."),
    dict(key="sleep_h", bar=(5, 9), hm=True, label="Hours of sleep", group="Sleep", unit=" h", ref=7.0,
         per=1.0, slope=_ln(1.12), better="higher", fmt="{:.1f}", upper_ref=8.5,
         upper_slope=_ln(1.30),
         cite="Cappuccio 2010 SLEEP, meta-analysis n≈1.38M: short sleep HR 1.12, long sleep HR 1.30. U-shaped."),
    dict(key="consistency", bar=(40, 100), label="Sleep consistency", group="Sleep", unit="%", ref=70.0,
         per=10.0, slope=_ln(0.86), better="higher", fmt="{:.0f}", plateau=90.0,
         cite="Windred 2024 SLEEP, UK Biobank n=60,977: top four sleep-regularity quintiles 20–48% lower mortality than the least regular."),
]
BY_KEY = {m["key"]: m for m in METRICS}


def log_hr(m, v):
    """Log hazard ratio for one metric at value v, relative to its reference."""
    if v is None:
        return None
    if m["key"] == "sleep_h":                       # U-shaped, two arms
        short = m["slope"] * max(0.0, m["ref"] - v)
        long_ = m["upper_slope"] * max(0.0, v - m["upper_ref"])
        return short + long_
    if m.get("sat_k"):                              # saturating dose-response
        import math
        sat = lambda t: 1 - math.exp(-max(0.0, t) / m["sat_k"])
        lh = m["full"] * (sat(v) - sat(m["ref"]))
        if m.get("jshape") and v > m["jshape"]:
            lh *= max(0.0, 1 - (v - m["jshape"]) / m["jshape"])
        return lh
    x = v
    if m.get("plateau") is not None:
        x = min(x, m["plateau"]) if m["better"] == "higher" else max(x, m["plateau"])
    # `slope` already carries its own sign: ln(HR) per unit as published, so a
    # protective factor (VO2 max, ln 0.87) is negative and a risk factor (resting HR,
    # ln 1.09) is positive. `better` only drives display and which side plateaus.
    d = (x - m["ref"]) / m["per"]
    lh = m["slope"] * d
    if m.get("jshape") and v > m["jshape"]:          # benefit fades past the J's floor
        lh *= max(0.0, 1 - (v - m["jshape"]) / m["jshape"])
    return lh


def years(m, v, shrink=None):
    lh = log_hr(m, v)
    if lh is None:
        return None
    raw = lh / GAMMA
    raw = max(-CAP_YEARS, min(CAP_YEARS, raw))
    return raw * (SHRINK if shrink is None else shrink)


# --- inputs from Garmin ---------------------------------------------------------------
def _load(n):
    p = BASE / n
    return json.loads(p.read_text()) if p.exists() else {}


def _inputs(window_days=180, as_of=None):
    """Return averages over an inclusive lookback and distinct observed-date count."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    gd, gi, ga, gh = (_load("garmin_hrv_resp.json"), _load("garmin_intraday.json"),
                      _load("garmin_activities.json"), _load("garmin_healthspan.json"))
    end = date.fromisoformat(as_of) if as_of else date.today()
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = end.isoformat()
    days = [d for d in sorted(gd) if lo <= d <= hi]
    observed = set()
    out = {}
    for key, field, scale in (("rhr", "rhr", 1), ("steps", "steps", 1),
                               ("sleep_h", "sleep_sec", 3600)):
        vals = []
        for d in days:
            value = (gd[d] or {}).get(field)
            if isinstance(value, (int, float)) and (field != "sleep_sec" or value > 0):
                vals.append(value / scale)
                observed.add(d)
        out[key] = st.mean(vals) if vals else None

    cons = []
    for d in (days if HEALTH_DIR is not None else sorted(gi)):
        if not lo <= d <= hi:
            continue
        beds, wakes = [], []
        cur = date.fromisoformat(d)
        for k in range(CONS_NIGHTS + 1):
            g = gi.get((cur - timedelta(days=k)).isoformat()) or {}
            if g.get("sleep_start_local") and g.get("sleep_end_local"):
                beds.append(_clock_h(g["sleep_start_local"]))
                wakes.append(_clock_h(g["sleep_end_local"]))
            else:
                break
        c = consistency_pct(beds, wakes) if len(beds) > 1 else None
        if c is not None:
            cons.append(c)
            observed.add(d)
    out["consistency"] = st.mean(cons) if cons else None

    # No activities and no activity coverage are different observations.
    # Only successfully fetched days can count as zero-activity days.
    has_activities = (BASE / "garmin_activities.json").exists()
    covered = {d for d, row in gh.items() if lo <= d <= hi
               and row.get("activities_synced") is True and has_activities}
    # An explicit external integration opts into its existing full-export contract.
    legacy = not covered and HEALTH_DIR is not None and has_activities
    if legacy:
        covered = set(days)
    observed.update(covered)
    z13 = z45 = strength = 0.0
    zone13_known = zone45_known = strength_known = True
    for a in ga.values():
        activity_day = (a.get("startTimeLocal") or "")[:10]
        if not lo <= activity_day <= hi or (not legacy and activity_day not in covered):
            continue
        if not legacy:
            zone13_known &= all(isinstance(a.get(f"hrTimeInZone_{i}"), (int, float)) for i in (1, 2, 3))
            zone45_known &= all(isinstance(a.get(f"hrTimeInZone_{i}"), (int, float)) for i in (4, 5))
            strength_known &= bool(a.get("type"))
        z13 += sum((a.get(f"hrTimeInZone_{i}") or 0) for i in (1, 2, 3)) / 3600
        z45 += sum((a.get(f"hrTimeInZone_{i}") or 0) for i in (4, 5)) / 60
        if (a.get("type") or "") in ("strength_training", "indoor_cardio", "fitness_equipment"):
            if not legacy:
                strength_known &= isinstance(a.get("duration"), (int, float))
            strength += (a.get("duration") or 0) / 60
    weeks = max(1.0, len(covered) / 7)
    out["zone13"] = z13 / weeks if covered and zone13_known else None
    out["zone45"] = z45 / weeks if covered and zone45_known else None
    out["strength"] = strength / weeks if covered and strength_known else None

    # VO2 max updates sparsely and carries forward; body fat uses the observed window.
    vo = [(d, gh[d]["vo2max"]) for d in sorted(gh)
          if d <= hi and isinstance(gh[d].get("vo2max"), (int, float)) and gh[d]["vo2max"] > 0]
    out["vo2max"] = vo[-1][1] if vo else None
    bf = [(d, gh[d]["body_fat_pct"]) for d in sorted(gh)
          if lo <= d <= hi and isinstance(gh[d].get("body_fat_pct"), (int, float))
          and 0 <= gh[d]["body_fat_pct"] <= 100]
    out["lean_pct"] = 100 - st.mean(v for _, v in bf) if bf else None
    observed.update(d for d, _ in vo if lo <= d <= hi)
    observed.update(d for d, _ in bf)
    return out, len(observed)


def inputs(window_days=180, as_of=None):
    """Average available metrics over a trailing calendar window; missing means None."""
    return _inputs(window_days, as_of)[0]


def report(window_days=180, as_of=None, shrink=None):
    vals, observed_days = _inputs(window_days, as_of)
    rows = []
    for m in METRICS:
        v = vals.get(m["key"])
        rows.append({**{k: m[k] for k in ("key", "label", "group", "unit", "ref", "cite", "better", "bar")}, "hm": m.get("hm", False), "fmt": m["fmt"],
                     "value": v, "years": years(m, v, shrink),
                     "display": (m["fmt"].format(v) + m["unit"]) if v is not None else "—"})
    available = sum(r["value"] is not None for r in rows)
    total = sum(r["years"] for r in rows if r["years"] is not None) if available else None
    return {"window_days": window_days, "chronological_age": CHRON_AGE,
            "bio_age": round(CHRON_AGE + total, 1) if CHRON_AGE is not None and total is not None else None,
            "delta_years": round(total, 2) if total is not None else None,
            "available_metrics": available, "observed_days": observed_days, "metrics": rows}


def pace_of_aging(as_of=None, shrink=None):
    """Illustrative -1x..3x comparison of 30-day and 180-day model deltas."""
    d30 = report(30, as_of, shrink)["delta_years"]
    d180 = report(180, as_of, shrink)["delta_years"]
    if d30 is None or d180 is None:
        return None, d30, d180
    return max(-1.0, min(3.0, 1 + (d30 - d180) / PACE_SCALE)), d30, d180


def calibrate(whoop_age, as_of=None):
    """Solve a gain matching another device; unavailable without age or a nonzero delta."""
    if CHRON_AGE is None:
        return None
    raw = report(180, as_of, shrink=1.0)["delta_years"]
    return (whoop_age - CHRON_AGE) / raw if raw else None


if __name__ == "__main__":
    r = report()
    if "--json" in sys.argv:
        print(json.dumps(r, indent=1))
    else:
        delta = r["delta_years"]
        label = "unavailable" if delta is None else f"{delta:+.2f} y"
        print(f"chronological {r['chronological_age']}  ->  experimental age {r['bio_age']}"
              f"  ({label}, {r['available_metrics']}/9 metrics, {r['observed_days']} observed days)")
        for m in sorted(r["metrics"], key=lambda x: (x["years"] is None, x["years"] or 0)):
            y = m["years"]
            print(f"  {m['label']:24s} {m['display']:>14s}  (target {m['ref']:g}) "
                  f"{'   —' if y is None else f'{y:+6.2f} y'}")
        p, d30, d180 = pace_of_aging()
        if p is not None:
            print(f"\nexperimental pace {p:+.2f}x   (30-day Δ {d30:+.2f} y vs 6-month Δ {d180:+.2f} y)")
        else:
            print("\nexperimental pace unavailable")

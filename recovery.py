# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Personal-baseline recovery signal, not a diagnosis or exercise prescription.

z = (ln HRV today - mean ln HRV over prior 30 calendar days) / population SD.
At least 14 baseline readings are required. Symptoms override short sleep;
sleep below 5 hours overrides HRV. A two-day temperature deviation mean of
at least +0.40 C caps a GREEN HRV signal at AMBER, without overriding abstention.
Thresholds are descriptive landmarks, not clinically validated safety gates.
Agreement with another HRV-based device is concordance, not validation.

An explicitly configured health_dir supplies the authoritative implementation.
It is loaded by its exact filename, never by modifying the import search path.
"""
import importlib.util
import json
import math
from datetime import date, datetime, timedelta
from statistics import mean, pstdev

from config import DATA, HEALTH_DIR

BASELINE_DAYS = 30
MIN_BASELINE = 14
GREEN_Z = 0.0
RED_Z = -1.0
TEMP_VETO_C = 0.40
SLEEP_FLOOR_H = 5.0
MODEL_VERSION = "hrv-z30-temp-v2"


def _standalone_readiness(for_date=None, hrv_by_date=None, symptom=False,
                          temp_by_date=None, sleep_by_date=None):
    hrv, temp, sleep = hrv_by_date, temp_by_date, sleep_by_date
    d = str(for_date or date.today())
    sh = sleep.get(d)
    out = {"date": d, "model_version": MODEL_VERSION,
           "as_of": datetime.now().isoformat(timespec="seconds")}
    if d in hrv:
        out["hrv"] = hrv[d]
    if isinstance(sh, (int, float)):
        out["sleep_h"] = round(sh, 2)
    if symptom:
        return {**out, "band": "RED", "z": None, "veto": "symptom",
                "reason": "subjective symptom -- hard veto, overrides the number"}
    if isinstance(sh, (int, float)) and sh < SLEEP_FLOOR_H:
        return {**out, "band": "RED", "z": None, "veto": "short_sleep",
                "sleep_h": round(sh, 2),
                "reason": f"slept {sh:.1f} h -- below the {SLEEP_FLOOR_H:g} h floor"}
    td = temp.get(d)
    cur = datetime.fromisoformat(d).date()
    yst = temp.get((cur - timedelta(days=1)).isoformat())
    temp_2day = ((td + yst) / 2 if isinstance(td, (int, float))
                 and isinstance(yst, (int, float)) else None)
    temp_elevated = temp_2day is not None and temp_2day >= TEMP_VETO_C
    if d not in hrv:
        return {**out, "band": "ABSTAIN", "z": None,
                "reason": "no overnight HRV for this date (non-wear, sync gap, or API failure)"}
    base = [math.log(hrv[x]) for x in hrv
            if 0 < (cur - datetime.fromisoformat(x).date()).days <= BASELINE_DAYS]
    if len(base) < MIN_BASELINE:
        return {**out, "band": "ABSTAIN", "z": None,
                "reason": f"baseline too thin: {len(base)} of {MIN_BASELINE} required days"}
    b, sd = mean(base), pstdev(base)
    if sd == 0:
        return {**out, "band": "ABSTAIN", "z": None, "reason": "zero baseline variance"}
    z = (math.log(hrv[d]) - b) / sd
    band = "GREEN" if z >= GREEN_Z else ("RED" if z < RED_Z else "AMBER")
    capped = temp_elevated and band == "GREEN"
    if capped:
        band = "AMBER"
    res = {**out, "band": band, "sleep_h": round(sh, 2) if isinstance(sh, (int, float)) else None,
           "z": round(z, 2), "hrv": hrv[d], "baseline_n": len(base),
           "baseline_hrv": round(math.exp(b), 1),
           "reason": f"lnHRV {z:+.2f} SD vs {len(base)}-day baseline"}
    if capped:
        res["veto"] = "temperature"
    if isinstance(td, (int, float)):
        res["skin_temp_dev_c"] = td
        if temp_elevated:
            res["skin_temp_2day_c"] = round(temp_2day, 2)
            res["reason"] += (f" · skin temp 2-day mean {temp_2day:+.2f} C sustained"
                              " — capped at AMBER")
    return res


_vote = _standalone_readiness
if HEALTH_DIR is not None:
    source = HEALTH_DIR / "garmin_recovery.py"
    spec = importlib.util.spec_from_file_location("vitals_configured_recovery", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load recovery implementation from {source}")
    external = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(external)
    _vote = external.readiness
    MODEL_VERSION = external.MODEL_VERSION


def readiness(for_date=None, hrv_by_date=None, symptom=False, temp_by_date=None,
              sleep_by_date=None):
    """Read configured data once, then delegate without changing the vote protocol."""
    if hrv_by_date is None or temp_by_date is None or sleep_by_date is None:
        path = DATA / "garmin_hrv_resp.json"
        raw = json.loads(path.read_text()) if path.exists() else {}
        if hrv_by_date is None:
            hrv_by_date = {d: r["hrv_last_night"] for d, r in raw.items()
                           if isinstance(r.get("hrv_last_night"), (int, float)) and r["hrv_last_night"] > 0}
        if temp_by_date is None:
            temp_by_date = {d: r["skin_temp_dev_c"] for d, r in raw.items()
                            if isinstance(r.get("skin_temp_dev_c"), (int, float))}
        if sleep_by_date is None:
            sleep_by_date = {d: r["sleep_sec"] / 3600 for d, r in raw.items()
                             if isinstance(r.get("sleep_sec"), (int, float)) and r["sleep_sec"] > 0}
    return _vote(for_date, hrv_by_date, symptom, temp_by_date, sleep_by_date)


if __name__ == "__main__":
    import sys

    want = next((arg for arg in sys.argv[1:] if not arg.startswith("--")), None)
    print(json.dumps(readiness(want), indent=1))

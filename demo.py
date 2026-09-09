"""Deterministic, entirely synthetic normalized Garmin data for local previews.

No personal source files are read. The seed and smooth synthetic signals provide
varied observations; every displayed score is calculated by the real metric code.
"""
import atexit
import json
import math
import random
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


def create_data(end=None, days=210):
    """Generate a disposable demo ending on `end` (today by default)."""
    directory = tempfile.TemporaryDirectory(prefix="vitals-demo-")
    atexit.register(directory.cleanup)
    base = Path(directory.name)
    end = end or date.today()
    rng = random.Random(73421)
    daily, intraday, activities, health = {}, {}, {}, {}
    for i in range(days):
        day = end - timedelta(days=days - i - 1)
        key = day.isoformat()
        wave = math.sin(i * 0.31)
        sleep_h = 7.35 + 0.65 * math.sin(i * 0.19) + rng.uniform(-0.3, 0.3)
        hrv = 54 + 8 * wave + rng.uniform(-3, 3)
        temperature = 0.08 * math.sin(i * 0.45)
        if i % 41 in (24, 25):
            temperature = 0.55
            hrv += 10
        if i % 31 == 17:
            sleep_h = 4.55
        if i == days - 1:
            sleep_h, hrv, temperature = 7.65, 65.5, 0.06
        bed = datetime.combine(day - timedelta(days=1), time(22, 30)) + timedelta(minutes=25 * math.sin(i * 0.23))
        wake = bed + timedelta(hours=sleep_h + 0.3)
        intraday[key] = {
            "sleep_start_gmt": round(bed.timestamp() * 1000),
            "sleep_end_gmt": round(wake.timestamp() * 1000),
            "sleep_start_local": round(bed.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "sleep_end_local": round(wake.replace(tzinfo=timezone.utc).timestamp() * 1000),
        }
        daily[key] = {
            "hrv_last_night": round(hrv, 1), "skin_temp_dev_c": round(temperature, 2),
            "rhr": round(57 - 2.5 * wave + rng.uniform(-1.5, 1.5)),
            "steps": round(7800 + 2300 * math.sin(i * 0.39) + rng.uniform(-800, 800)),
            "sleep_sec": round(sleep_h * 3600), "sleep_awake_sec": 1080,
            "sleep_score": round(min(98, 78 + 10 * math.sin(i * 0.19))),
            "nap_sec": 1200 if i % 13 == 7 else 0,
            "kcal_active": round(580 + 300 * (1 + math.sin(i * 0.37))),
            "respiration": round(14.2 + 0.7 * math.sin(i * 0.27), 1),
        }
        health[key] = {"activities_synced": True}
        if i % 7 == 0:
            health[key].update(vo2max=round(43.5 + i / 210 + 0.7 * wave, 1),
                               body_fat_pct=round(23.5 - i / 210 + 0.6 * wave, 1))
        if i % 7 in (0, 2, 4, 5):
            strength = i % 7 in (2, 5)
            start = datetime.combine(day, time(10, 0))
            duration = 2400 if strength else 3300
            activities[f"synthetic-{i}"] = {
                "startTimeLocal": start.isoformat(),
                "startTimeGMT": start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
                "type": "strength_training" if strength else "running",
                "duration": duration, "activityTrainingLoad": 48 if strength else 125,
                "maxHR": 146 if strength else 167,
                "hrTimeInZone_1": 900 if strength else 300,
                "hrTimeInZone_2": 900 if strength else 900,
                "hrTimeInZone_3": 400 if strength else 1300,
                "hrTimeInZone_4": 120 if strength else 600,
                "hrTimeInZone_5": 0 if strength else 150,
            }
        # Missing days demonstrate abstention, not fabricated healthy readings.
        if i % 37 == 11 and i != days - 1:
            daily[key] = {}
            intraday.pop(key)
        if i == days - 1:
            daily[key]["kcal_active"] = 540
    for name, records in (("garmin_hrv_resp.json", daily), ("garmin_intraday.json", intraday),
                           ("garmin_activities.json", activities), ("garmin_healthspan.json", health)):
        (base / name).write_text(json.dumps(records, separators=(",", ":")))
    return base

# /// script
# requires-python = ">=3.12"
# dependencies = ["garminconnect==0.3.13"]
# ///
"""Optional local Garmin import: uv run sync.py --days 180.

Only normalized metric inputs are saved, never account profiles, activity names,
GPS tracks or passwords. OAuth tokens still grant account access: keep DATA private.
SDK API reference: https://github.com/cyberjunky/python-garminconnect/tree/0.3.13
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from getpass import getpass
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time


FILES = ("garmin_hrv_resp.json", "garmin_intraday.json",
         "garmin_activities.json", "garmin_healthspan.json")


def number(value, minimum=0):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (minimum is None or value >= minimum))


def fields(payload, names):
    """Drop Garmin's negative missing-data sentinels, but retain measured zeroes."""
    return {dest: payload[src] for dest, src in names.items()
            if number(payload.get(src))}


def object_payload(payload):
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Expected a Garmin object")
    return payload


def normalize_stats(payload):
    row = fields(object_payload(payload), {
        "rhr": "restingHeartRate", "stress_avg": "averageStressLevel",
        "body_battery_high": "bodyBatteryHighestValue",
        "body_battery_low": "bodyBatteryLowestValue", "steps": "totalSteps",
        "kcal_active": "activeKilocalories", "kcal_total": "totalKilocalories",
        "int_mod_min": "moderateIntensityMinutes", "int_vig_min": "vigorousIntensityMinutes",
        "hr_max_day": "maxHeartRate", "hr_min_day": "minHeartRate",
        "stress_high_sec": "highStressDuration", "body_battery_drained": "bodyBatteryDrainedValue",
    })
    return {k: v for k, v in row.items() if k not in ("rhr", "hr_max_day", "hr_min_day") or v > 0}


def normalize_hrv(payload):
    summary = object_payload(object_payload(payload).get("hrvSummary"))
    row = fields(summary, {"hrv_last_night": "lastNightAvg", "hrv_weekly_avg": "weeklyAvg",
                           "hrv_last_night_5min_high": "lastNight5MinHigh"})
    row.update(fields(object_payload(summary.get("baseline")), {
        "hrv_baseline_low": "lowUpper", "hrv_baseline_upper": "balancedUpper"}))
    row = {k: v for k, v in row.items() if v > 0}
    if isinstance(summary.get("status"), str) and row:
        row["hrv_status"] = summary["status"]
    return row


def normalize_respiration(payload):
    row = fields(object_payload(payload), {"resp_sleep_avg": "avgSleepRespirationValue",
        "resp_waking_avg": "avgWakingRespirationValue"})
    return {k: v for k, v in row.items() if v > 0}


def normalize_sleep(payload):
    payload = object_payload(payload)
    dto = object_payload(payload.get("dailySleepDTO"))
    row, clock = {}, {}
    # Zero sleep with no recorded night is not a measurement of zero sleep.
    if number(dto.get("sleepTimeSeconds"), 1):
        row.update(fields(dto, {"sleep_sec": "sleepTimeSeconds", "sleep_deep_sec": "deepSleepSeconds",
            "sleep_rem_sec": "remSleepSeconds", "sleep_awake_sec": "awakeSleepSeconds",
            "nap_sec": "napTimeSeconds", "restless_moments": "restlessMomentsCount",
            "sleep_stress": "avgSleepStress"}))
        overall = object_payload(object_payload(dto.get("sleepScores")).get("overall"))
        row.update(fields(overall, {"sleep_score": "value"}))
        row.update(fields(object_payload(dto.get("sleepNeed")), {
            "sleep_need_min": "actual", "sleep_need_baseline_min": "baseline"}))
        row.update(fields(payload, {"hrv_overnight_avg": "avgOvernightHrv",
            "sleep_rhr": "restingHeartRate", "body_battery_change": "bodyBatteryChange"}))
        for k in ("hrv_overnight_avg", "sleep_rhr"):
            if row.get(k) == 0:
                del row[k]
        clock = fields(dto, {"sleep_start_gmt": "sleepStartTimestampGMT",
            "sleep_end_gmt": "sleepEndTimestampGMT", "sleep_start_local": "sleepStartTimestampLocal",
            "sleep_end_local": "sleepEndTimestampLocal"})
        clock = {k: v for k, v in clock.items() if v > 0}
    if payload.get("skinTempDataExists") and number(payload.get("avgSkinTempDeviationC"), None):
        row["skin_temp_dev_c"] = payload["avgSkinTempDeviationC"]
        row.update(fields(payload, {"skin_temp_cal_days": "skinTempCalibrationDays"}))
    return row, clock


def normalize_activities(payload):
    if not isinstance(payload, list):
        raise ValueError("Expected a Garmin activity list")
    out = {}
    keep = ("duration", "activityTrainingLoad", "averageHR", "maxHR",
            "hrTimeInZone_1", "hrTimeInZone_2", "hrTimeInZone_3", "hrTimeInZone_4", "hrTimeInZone_5")
    for item in payload:
        item = object_payload(item)
        activity_id = item.get("activityId")
        if isinstance(activity_id, bool) or not str(activity_id).isdigit():
            raise ValueError("Activity lacks an ID")
        row = fields(item, {k: k for k in keep})
        row["activityId"] = activity_id
        for k in ("startTimeLocal", "startTimeGMT"):
            timestamp = item.get(k)
            if not isinstance(timestamp, str):
                raise ValueError("Activity lacks a timestamp")
            datetime.fromisoformat(timestamp)
            row[k] = timestamp
        kind = object_payload(item.get("activityType")).get("typeKey")
        if isinstance(kind, str):
            row["type"] = kind
        out[str(activity_id)] = row
    return out


def normalize_vo2(payload):
    if payload is None:
        return {}
    if not isinstance(payload, list):
        raise ValueError("Expected a Garmin max-metrics list")
    for item in payload:
        generic = object_payload(object_payload(item).get("generic"))
        for key in ("vo2MaxPreciseValue", "vo2MaxValue"):
            if number(generic.get(key), 1):
                return {"vo2max": generic[key]}
    return {}


def normalize_body(payload):
    measurements = object_payload(payload).get("dateWeightList") or []
    if not isinstance(measurements, list):
        raise ValueError("Expected a Garmin body-composition list")
    out = {}
    for item in measurements:
        item = object_payload(item)
        day = item.get("calendarDate")
        if not isinstance(day, str):
            raise ValueError("Body composition lacks a date")
        date.fromisoformat(day)
        row = {}
        if number(item.get("bodyFat"), 0.01) and item["bodyFat"] <= 100:
            row["body_fat_pct"] = item["bodyFat"]
        if row:
            out.setdefault(day, {}).update(row)
    return out


def merge_records(old, new):
    """Merge by date/activity ID and field; missing updates never erase history."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError("History must be a record object")
    merged = dict(old)
    for key, row in new.items():
        if not isinstance(row, dict) or (key in old and not isinstance(old[key], dict)):
            raise ValueError("History contains an invalid record")
        valid = {k: v for k, v in row.items() if v is not None}
        if valid:
            merged[key] = {**old.get(key, {}), **valid}
    return merged


def load_records(path):
    if not path.exists():
        return {}
    records = json.loads(path.read_text())
    if not isinstance(records, dict) or not all(isinstance(v, dict) for v in records.values()):
        raise ValueError(f"Invalid history in {path.name}; refusing to overwrite")
    return records


def save_merged(path, new):
    # Re-read at save time so a short sync does not replace older on-disk history.
    old = load_records(path)
    merged = merge_records(old, new)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(merged, stream, indent=1, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return len(old), len(merged)


def status_code(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"(?:API Error|Error|HTTP|\()\s*(\d{3})", str(error))
    return int(match.group(1)) if match else None


class StopSync(Exception):
    """Authentication or rate limiting makes further requests inappropriate."""


class Fetcher:
    def __init__(self, sdk):
        from requests.exceptions import RequestException
        self.network_error = RequestException
        self.sdk = sdk
        self.missing = Counter()
        self.failed = Counter()

    def call(self, label, fn, normalize, *args, optional=False):
        try:
            raw = fn(*args)
            result = normalize(raw)
            if not result or result == ({}, {}):
                self.missing[label] += 1
            return result
        except self.sdk.GarminConnectAuthenticationError:
            self.failed[label] += 1
            raise StopSync("Authentication expired; run sync again to sign in.") from None
        except self.sdk.GarminConnectTooManyRequestsError:
            self.failed[label] += 1
            raise StopSync("Garmin rate limit reached; wait before syncing again.") from None
        except (self.sdk.GarminConnectConnectionError, self.network_error) as error:
            status = 404 if isinstance(error, self.sdk.GarminConnectNotFoundError) else status_code(error)
            if optional and status in (400, 404):
                self.missing[label] += 1
            else:
                self.failed[label] += 1
                detail = f"HTTP {status}" if status else "connection error"
                print(f"  {label}: {detail}; existing data retained.", file=sys.stderr)
        except (ValueError, TypeError, KeyError):
            self.failed[label] += 1
            print(f"  {label}: unexpected response; existing data retained.", file=sys.stderr)
        finally:
            time.sleep(0.4)
        return None


def authenticate(sdk, token_dir):
    # Do not resolve symlinks: the SDK rejects unsafe token path ancestry itself.
    from garminconnect.client import token_file_path
    token_file_path(str(token_dir))
    token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_dir.chmod(0o700)
    client = sdk.Garmin(retry_attempts=0)
    try:
        client.login(str(token_dir))
    except sdk.GarminConnectAuthenticationError:
        if not sys.stdin.isatty():
            raise StopSync("No valid saved session. Run sync in an interactive terminal to sign in.") from None
        print("Sign in to Garmin Connect. Password and MFA code are not saved.")
        email = input("Email: ").strip()
        client = sdk.Garmin(email=email, password=getpass("Password: "),
                            prompt_mfa=lambda: getpass("MFA code: ").strip(), retry_attempts=0)
        client.login(str(token_dir))
    # login() suppresses token-save failures; make a failed save visible to the user.
    client.client.dump(str(token_dir))
    return client


def bounded_days(value):
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("days must be an integer from 1 to 3660") from None
    if not 1 <= days <= 3660:
        raise argparse.ArgumentTypeError("days must be from 1 to 3660")
    return days


def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("end must be YYYY-MM-DD") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=bounded_days, default=180,
                        help="number of calendar days, including end (1–3660; default 180)")
    parser.add_argument("--end", type=iso_date, default=date.today(), help="last day, inclusive (YYYY-MM-DD)")
    parser.add_argument("--token-dir", type=Path, help="private token directory (default DATA/.garminconnect)")
    args = parser.parse_args(argv)
    if args.end > date.today() or args.end.toordinal() < args.days:
        parser.error("date window must be in the past or present and within the calendar")
    from config import DATA
    try:
        import garminconnect as sdk
    except ImportError:
        print("Sync needs its optional dependency: use uv run sync.py --days 180", file=sys.stderr)
        return 1
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    token_dir = (args.token_dir or DATA / ".garminconnect").expanduser()
    pending = {name: {} for name in FILES}
    fetcher = Fetcher(sdk)
    result = 0
    try:
        # Fail closed on malformed existing files before contacting Garmin.
        for name in FILES:
            load_records(DATA / name)
        client = authenticate(sdk, token_dir)
        start = args.end - timedelta(days=args.days - 1)
        print(f"Syncing {args.days} days ({start} through {args.end}); normalized data only.", flush=True)
        body = fetcher.call("body composition", client.get_body_composition, normalize_body,
                            start.isoformat(), args.end.isoformat(), optional=True)
        if body:
            pending[FILES[3]].update(body)
        day = start
        while day <= args.end:
            iso = day.isoformat()
            daily = pending[FILES[0]].setdefault(iso, {})
            for label, method, normalize, optional in (
                ("daily stats", client.get_stats, normalize_stats, False),
                ("HRV", client.get_hrv_data, normalize_hrv, True),
                ("respiration", client.get_respiration_data, normalize_respiration, True),
            ):
                row = fetcher.call(label, method, normalize, iso, optional=optional)
                if row:
                    daily.update(row)
            sleep = fetcher.call("sleep", client.get_sleep_data, normalize_sleep, iso)
            if sleep:
                daily.update(sleep[0])
                pending[FILES[1]][iso] = sleep[1]
            health = pending[FILES[3]].setdefault(iso, {})
            vo2 = fetcher.call("VO2 max", client.get_max_metrics, normalize_vo2, iso, optional=True)
            if vo2:
                health.update(vo2)
            activities = fetcher.call("activities", client.get_activities_by_date,
                                      normalize_activities, iso, iso)
            if activities is not None:
                pending[FILES[2]].update(activities)
                health["activities_synced"] = True
            if (day - start).days % 10 == 0 or day == args.end:
                print(f"  Processed {iso}", flush=True)
            day += timedelta(days=1)
    except KeyboardInterrupt:
        print("Interrupted; saving successfully fetched records.", file=sys.stderr)
        result = 130
    except StopSync as error:
        print(str(error), file=sys.stderr)
        result = 1
    except sdk.GarminConnectAuthenticationError:
        print("Garmin sign-in failed. Check credentials/MFA and try again.", file=sys.stderr)
        result = 1
    except sdk.GarminConnectTooManyRequestsError:
        print("Garmin rate limit reached. Wait before trying again.", file=sys.stderr)
        result = 1
    except (sdk.GarminConnectConnectionError, fetcher.network_error):
        print("Could not reach Garmin. Check your connection and Garmin service status.", file=sys.stderr)
        result = 1
    except (OSError, ValueError, EOFError):
        print("Could not read/write local data, parse existing history, or read sign-in input. Check paths, permissions and JSON files.", file=sys.stderr)
        result = 1
    finally:
        activity_coverage = any(row.get("activities_synced") for row in pending[FILES[3]].values())
        for name, records in pending.items():
            records = {k: row for k, row in records.items() if row}
            if not records and not (name == FILES[2] and activity_coverage):
                continue
            try:
                before, after = save_merged(DATA / name, records)
                print(f"  {name}: {len(records)} fetched records; {before} → {after} stored.")
            except (OSError, ValueError):
                print(f"  {name}: save failed; original file retained.", file=sys.stderr)
                result = 1
    missing = ", ".join(f"{k}: {v}" for k, v in fetcher.missing.items()) or "none"
    failed = ", ".join(f"{k}: {v}" for k, v in fetcher.failed.items()) or "none"
    print(f"Missing sensor data / empty results / unsupported optional requests: {missing}")
    print(f"Failed requests: {failed}")
    return result or int(bool(fetcher.failed))


if __name__ == "__main__":
    # New files/directories, including SDK token files, are private from creation.
    os.umask(0o077)
    raise SystemExit(main())

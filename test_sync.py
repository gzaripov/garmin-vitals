"""Credential-free regression checks: python -m unittest test_sync."""
import json
from pathlib import Path
import tempfile
import unittest

from sync import normalize_activities, normalize_sleep, normalize_stats, save_merged


class SyncRegressionTests(unittest.TestCase):
    def test_short_partial_sync_preserves_history_and_rejects_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            save_merged(path, {"2000-01-01": {"rhr": 55},
                               "2000-01-02": {"rhr": 57, "steps": 8000}})
            save_merged(path, {"2000-01-02": {"rhr": None, "steps": 0},
                               "2000-01-03": {"rhr": 54}})
            self.assertEqual(json.loads(path.read_text()), {
                "2000-01-01": {"rhr": 55}, "2000-01-02": {"rhr": 57, "steps": 0},
                "2000-01-03": {"rhr": 54}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text('{"broken":')
            with self.assertRaises(ValueError):
                save_merged(path, {"2000-01-03": {"rhr": 56}})
            self.assertEqual(path.read_text(), '{"broken":')

    def test_missing_sensor_values_are_not_zero_or_identity_dumps(self):
        self.assertEqual(normalize_stats({"restingHeartRate": 0, "averageStressLevel": -1,
                                          "totalSteps": 0, "activeKilocalories": None}), {"steps": 0})
        daily, clock = normalize_sleep({"dailySleepDTO": {"sleepTimeSeconds": 0},
            "skinTempDataExists": True, "avgSkinTempDeviationC": -0.3})
        self.assertEqual(daily, {"skin_temp_dev_c": -0.3})
        self.assertEqual(clock, {})
        row = normalize_activities([{"activityId": 123, "activityName": "private title",
            "startTimeLocal": "2000-01-01 10:00:00", "startTimeGMT": "2000-01-01 08:00:00",
            "duration": 1800, "hrTimeInZone_1": 0, "hrTimeInZone_2": None,
            "activityTrainingLoad": -1, "startLatitude": 50}])["123"]
        self.assertEqual(row, {"activityId": 123, "duration": 1800, "hrTimeInZone_1": 0,
            "startTimeLocal": "2000-01-01 10:00:00", "startTimeGMT": "2000-01-01 08:00:00"})


if __name__ == "__main__":
    unittest.main()

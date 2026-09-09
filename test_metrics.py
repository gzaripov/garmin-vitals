"""Run with python -m unittest test_metrics; all records here are synthetic."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import healthspan
import recovery
import scores
from demo import create_data


class MetricContracts(unittest.TestCase):
    def test_history_window_keeps_scores_and_gmt_is_utc(self):
        base = create_data(end=date(2000, 3, 1), days=45)
        with patch.object(scores, "BASE", base):
            full = scores.series()
            self.assertEqual(scores.series(days=15), {d: full[d] for d in list(full)[-15:]})
            self.assertTrue(any(r.get("debt_h", 0) > 0 for r in full.values()))
        self.assertEqual(scores._gmt_ms("2000-01-01T12:00:00"),
                         scores._gmt_ms("2000-01-01T12:00:00+00:00"))

    def test_unknown_health_is_not_zero_or_a_personal_age(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(healthspan, "BASE", Path(directory)), \
                patch.object(healthspan, "CHRON_AGE", None), patch.object(healthspan, "HEALTH_DIR", None):
            report = healthspan.report()
            self.assertEqual((report["available_metrics"], report["observed_days"]), (0, 0))
            self.assertIsNone(report["delta_years"])
            self.assertIsNone(report["bio_age"])
            self.assertIsNone(healthspan.pace_of_aging()[0])
            self.assertIsNone(healthspan.calibrate(30))
        base = create_data(end=date(2000, 3, 1), days=45)
        with patch.object(healthspan, "BASE", base), patch.object(healthspan, "CHRON_AGE", None), \
                patch.object(healthspan, "HEALTH_DIR", None):
            report = healthspan.report(as_of="2000-03-01")
            self.assertEqual(report["available_metrics"], 9)
            self.assertIsNone(report["bio_age"])
            self.assertIsNotNone(report["delta_years"])
            (base / "garmin_activities.json").unlink()
            values = healthspan.inputs(as_of="2000-03-01")
            self.assertTrue(all(values[k] is None for k in ("zone13", "zone45", "strength")))

    def test_recovery_veto_precedence_and_abstention(self):
        today = date(2000, 3, 1)
        key = today.isoformat()
        hrv = {(today - timedelta(days=i)).isoformat(): 50 + i % 5 for i in range(1, 31)}
        hrv[key] = 65
        temp = {key: 0.5, (today - timedelta(days=1)).isoformat(): 0.5}
        vote = recovery._standalone_readiness
        self.assertEqual(vote(key, hrv, False, {}, {key: 8})["band"], "GREEN")
        self.assertEqual(vote(key, hrv, False, temp, {key: 8})["band"], "AMBER")
        short = vote(key, hrv, False, temp, {key: 4.5})
        self.assertEqual((short["band"], short["z"], short["veto"]), ("RED", None, "short_sleep"))
        self.assertEqual(vote(key, {}, True, temp, {key: 4.5})["veto"], "symptom")
        self.assertEqual(vote(key, {}, False, temp, {key: 8})["band"], "ABSTAIN")
        self.assertEqual(vote(key, {key: 65}, False, {}, {key: 8})["band"], "ABSTAIN")


if __name__ == "__main__":
    unittest.main()

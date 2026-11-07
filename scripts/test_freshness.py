
"""Offline tests for validate_freshness.py (no BU Dining requests)."""

import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from validate_freshness import (
    check_dataset_freshness,
    check_menu_date_coverage,
    load_current_dataset,
    validate_for_recommendations,
)


NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
DATES = ["2026-11-07", "2026-11-08"]
HALLS = ["West Campus", "Warren Towers", "Marciano Commons"]


def make_dataset(age_hours=3, dates=None):
    dates = DATES if dates is None else dates
    records = [
        {
            "name": "Sample Food",
            "dining_hall": hall,
            "meal": "Lunch",
            "date": day,
            "allergens": None,  # Unknown, never implicitly safe.
        }
        for day in dates for hall in HALLS
    ]
    return {
        "schema_version": "1.0",
        "run_id": "synthetic_test_run",
        "generated_at_utc": (NOW - timedelta(hours=age_hours)).isoformat(),
        "snapshot_date": "2026-11-07",
        "menu_dates": list(dates),
        "dining_halls": list(HALLS),
        "total_records": len(records),
        "validation_status": "passed",
        "allergy_notice": "Check with BU Dining for allergens and cross-contact.",
        "sources": {},
        "records": records,
    }


class FreshnessTests(unittest.TestCase):
    def test_fresh_under_24_hours(self):
        result = check_dataset_freshness(make_dataset(), now=NOW)
        self.assertEqual(result["status"], "fresh")
        self.assertTrue(result["is_fresh"])
        self.assertEqual(result["age_hours"], 3)

    def test_exactly_24_hours_is_fresh(self):
        self.assertEqual(check_dataset_freshness(make_dataset(24), now=NOW)["status"], "fresh")

    def test_more_than_24_hours_is_stale(self):
        dataset = make_dataset(24 + 1 / 3600)
        self.assertEqual(check_dataset_freshness(dataset, now=NOW)["status"], "stale")

    def test_exactly_48_hours_is_stale(self):
        self.assertEqual(check_dataset_freshness(make_dataset(48), now=NOW)["status"], "stale")

    def test_over_48_hours_is_expired(self):
        self.assertEqual(check_dataset_freshness(make_dataset(49), now=NOW)["status"], "expired")

    def test_rejected_publication_status(self):
        dataset = make_dataset()
        dataset["validation_status"] = "failed"
        self.assertEqual(check_dataset_freshness(dataset, now=NOW)["status"], "invalid")
        self.assertFalse(validate_for_recommendations(dataset, DATES, now=NOW)["can_recommend"])

    def test_missing_timestamp(self):
        dataset = make_dataset()
        del dataset["generated_at_utc"]
        result = check_dataset_freshness(dataset, now=NOW)
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["age_hours"])

    def test_malformed_timestamp(self):
        dataset = make_dataset()
        dataset["generated_at_utc"] = "not-a-timestamp"
        self.assertEqual(check_dataset_freshness(dataset, now=NOW)["status"], "invalid")

    def test_naive_timestamp_is_invalid(self):
        dataset = make_dataset()
        dataset["generated_at_utc"] = "2026-11-07T19:00:00"
        self.assertEqual(check_dataset_freshness(dataset, now=NOW)["status"], "invalid")

    def test_future_timestamp_rejected(self):
        dataset = make_dataset(age_hours=-1)
        result = check_dataset_freshness(dataset, now=NOW)
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["age_hours"])

    def test_clock_skew_under_tolerance(self):
        dataset = make_dataset(age_hours=-2 / 60)
        result = check_dataset_freshness(dataset, now=NOW)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["age_hours"], 0)

    def test_invalid_thresholds(self):
        for kwargs in (
            {"max_age_hours": 0}, {"expiry_hours": 24},
            {"max_age_hours": -1}, {"expiry_hours": float("nan")},
            {"max_age_hours": True}, {"clock_skew_tolerance_minutes": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                check_dataset_freshness(make_dataset(), now=NOW, **kwargs)
        outcome = validate_for_recommendations(make_dataset(), DATES, now=NOW, expiry_hours=24)
        self.assertEqual(outcome["status"], "invalid")
        self.assertFalse(outcome["can_recommend"])

    def test_naive_now_is_not_assumed_utc(self):
        with self.assertRaises(ValueError):
            check_dataset_freshness(make_dataset(), now=datetime(2026, 11, 7, 20))

    def test_timezone_offset_and_dst_transition(self):
        dataset = make_dataset()
        # Same local wall time on DST fall-back day; actual elapsed time is 1 hour.
        dataset["generated_at_utc"] = "2026-11-01T01:30:00-04:00"
        local_now = datetime.fromisoformat("2026-11-01T01:30:00-05:00")
        self.assertEqual(check_dataset_freshness(dataset, now=local_now)["age_hours"], 1)
        dataset["generated_at_utc"] = "2026-11-01T05:30:00Z"
        self.assertEqual(check_dataset_freshness(dataset, now=local_now)["age_hours"], 1)


class DateCoverageTests(unittest.TestCase):
    def test_one_requested_date_present(self):
        result = check_menu_date_coverage(make_dataset(), "2026-11-07")
        self.assertTrue(result["all_dates_available"])
        self.assertEqual(result["available_dates"], ["2026-11-07"])
        self.assertEqual(result["meal_periods_by_date"]["2026-11-07"]["West Campus"], ["Lunch"])

    def test_multiple_requested_dates_present_and_normalized(self):
        result = check_menu_date_coverage(make_dataset(), [date(2026, 11, 8), "2026-11-07", "2026-11-07"])
        self.assertTrue(result["all_dates_available"])
        self.assertEqual(result["requested_dates"], DATES)

    def test_missing_requested_date(self):
        result = check_menu_date_coverage(make_dataset(), "2026-11-09")
        self.assertFalse(result["all_dates_available"])
        self.assertEqual(result["missing_dates"], ["2026-11-09"])
        self.assertEqual(validate_for_recommendations(make_dataset(), "2026-11-09", now=NOW)["status"], "missing_dates")

    def test_metadata_date_without_food_records(self):
        dataset = make_dataset()
        dataset["records"] = [r for r in dataset["records"] if r["date"] == DATES[0]]
        dataset["total_records"] = len(dataset["records"])
        result = check_menu_date_coverage(dataset, DATES[1])
        self.assertEqual(result["missing_dates"], [DATES[1]])

    def test_one_hall_missing(self):
        dataset = make_dataset()
        dataset["records"] = [
            r for r in dataset["records"]
            if not (r["date"] == DATES[1] and r["dining_hall"] == "Warren Towers")
        ]
        dataset["total_records"] = len(dataset["records"])
        result = check_menu_date_coverage(dataset, DATES)
        self.assertEqual(result["missing_halls_by_date"], {DATES[1]: ["Warren Towers"]})
        self.assertEqual(result["available_dates"], [DATES[0]])
        self.assertEqual(validate_for_recommendations(dataset, DATES, now=NOW)["status"], "missing_dates")

    def test_strict_iso_date_validation(self):
        for value in ("2026-2-01", "2026-02-30", "20261107", "2026-11-07T00:00:00", [], None):
            with self.subTest(value=value):
                outcome = check_menu_date_coverage(make_dataset(), value)
                self.assertFalse(outcome["all_dates_available"])
                self.assertTrue(outcome["errors"])

    def test_malformed_record_cannot_prove_coverage(self):
        dataset = make_dataset(dates=[DATES[0]])
        dataset["records"][1]["name"] = None
        result = check_menu_date_coverage(dataset, DATES[0])
        self.assertEqual(result["missing_halls_by_date"], {DATES[0]: ["Warren Towers"]})
        self.assertEqual(result["invalid_record_count"], 1)
        self.assertEqual(validate_for_recommendations(dataset, DATES[0], now=NOW)["status"], "invalid")

    def test_invalid_record_even_with_other_coverage_fails_closed(self):
        dataset = make_dataset()
        dataset["records"].append({"bad": "record"})
        dataset["total_records"] += 1
        self.assertEqual(validate_for_recommendations(dataset, DATES, now=NOW)["status"], "invalid")

    def test_metadata_and_records_mismatch_fails_closed(self):
        dataset = make_dataset()
        dataset["menu_dates"] = [DATES[0]]
        self.assertEqual(check_menu_date_coverage(dataset, DATES)["missing_dates"], [DATES[1]])


class IntegrationTests(unittest.TestCase):
    def test_missing_records_array(self):
        dataset = make_dataset()
        del dataset["records"]
        self.assertEqual(validate_for_recommendations(dataset, DATES, now=NOW)["status"], "invalid")

    def test_inconsistent_total_records(self):
        dataset = make_dataset()
        dataset["total_records"] = 400
        result = validate_for_recommendations(dataset, DATES, now=NOW)
        self.assertFalse(result["can_recommend"])
        self.assertTrue(any("total_records" in message for message in result["errors"]))

    def test_empty_dataset(self):
        result = validate_for_recommendations({}, DATES, now=NOW)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["can_recommend"])

    def test_missing_malformed_and_nonobject_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "menus.json"
            with self.assertRaises(FileNotFoundError):
                load_current_dataset(path)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                load_current_dataset(path)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_current_dataset(path)
            path.write_text(json.dumps(make_dataset()), encoding="utf-8")
            self.assertEqual(load_current_dataset(path)["total_records"], 6)

    def test_month_year_boundary(self):
        dataset = make_dataset(dates=["2026-12-31", "2027-01-01"])
        dataset["generated_at_utc"] = "2026-12-31T23:30:00+00:00"
        now = datetime.fromisoformat("2027-01-01T00:30:00+00:00")
        result = validate_for_recommendations(dataset, ["2027-01-01", "2026-12-31"], now=now)
        self.assertTrue(result["can_recommend"])
        self.assertEqual(result["freshness"]["age_hours"], 1)

    def test_input_dataset_not_mutated(self):
        dataset = make_dataset()
        untouched = copy.deepcopy(dataset)
        check_dataset_freshness(dataset, now=NOW)
        check_menu_date_coverage(dataset, DATES)
        outcome = validate_for_recommendations(dataset, DATES, now=NOW)
        self.assertTrue(outcome["can_recommend"])
        self.assertEqual(dataset, untouched)

    def test_missing_allergens_not_automatically_safe(self):
        dataset = make_dataset()
        del dataset["records"][0]["allergens"]
        outcome = validate_for_recommendations(dataset, DATES, now=NOW)
        self.assertTrue(outcome["can_recommend"])
        self.assertNotIn("allergens", dataset["records"][0])
        # Passing dataset checks never claims an item is allergen-free.

    def test_cli_exit_codes(self):
        from validate_freshness import main
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "menus.json"
            path.write_text(json.dumps(make_dataset(age_hours=5000)), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["--path", str(path), "--dates", DATES[0]]), 1)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["--path", str(path) + ".missing", "--dates", DATES[0]]), 2)


if __name__ == "__main__":
    unittest.main()

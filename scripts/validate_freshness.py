
"""Read-only freshness and date-coverage checks for Terrier Trays menus.

This module never refreshes or changes BU Dining menu data. A passing result
is a prerequisite for meal planning, NOT a food-allergy safety guarantee.
"""

import argparse
from datetime import date, datetime, timezone
import json
import math
from numbers import Real
from pathlib import Path
import re
import sys


EXPECTED_HALLS = ("West Campus", "Warren Towers", "Marciano Commons")
DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "current" / "menus.json"
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_current_dataset(path=None):
    """Load the current dataset, raising clear errors for missing/bad JSON."""
    file_path = Path(path) if path is not None else DEFAULT_DATA_PATH
    try:
        with file_path.open("r", encoding="utf-8") as file:
            dataset = json.load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Current menu dataset was not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {file_path} (line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise OSError(f"Could not read current menu dataset {file_path}: {exc}") from exc

    if not isinstance(dataset, dict):
        raise ValueError(f"Expected a JSON object at the root of {file_path}.")
    return dataset


def _iso_date(value):
    """Return YYYY-MM-DD for a strict ISO string or a date (not a datetime)."""
    if isinstance(value, datetime):
        raise ValueError("Use a calendar date, not a datetime, for menu dates.")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not ISO_DATE_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid menu date {value!r}; expected YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date: {value!r}.") from exc
    return parsed.isoformat()


def _requested_date_list(requested_dates):
    if isinstance(requested_dates, (str, date)):
        values = [requested_dates]
    elif isinstance(requested_dates, (list, tuple)):
        values = requested_dates
    else:
        raise ValueError("Provide a date or a list/tuple of dates.")
    if not values:
        raise ValueError("At least one requested menu date is required.")
    return sorted({_iso_date(value) for value in values})


def _positive_number(value, label, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(value) or (value < 0 if allow_zero else value <= 0)):
        boundary = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a finite {boundary} number.")


def check_dataset_freshness(
    dataset,
    now=None,
    max_age_hours=24,
    expiry_hours=48,
    clock_skew_tolerance_minutes=5,
):
    """Classify publication freshness using exact, UTC-aware elapsed hours."""
    _positive_number(max_age_hours, "max_age_hours")
    _positive_number(expiry_hours, "expiry_hours")
    _positive_number(clock_skew_tolerance_minutes, "clock_skew_tolerance_minutes", allow_zero=True)
    if expiry_hours <= max_age_hours:
        raise ValueError("expiry_hours must be greater than max_age_hours.")

    checked_at = datetime.now(timezone.utc) if now is None else now
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")
    checked_at = checked_at.astimezone(timezone.utc)

    result = {
        "status": "invalid",
        "is_fresh": False,
        "age_hours": None,
        "generated_at_utc": None,
        "checked_at_utc": checked_at.isoformat(),
        "message": "Dataset freshness cannot be verified.",
    }
    if not isinstance(dataset, dict):
        result["message"] = "Dataset must be a JSON object."
        return result

    timestamp = dataset.get("generated_at_utc")
    if not isinstance(timestamp, str) or not timestamp.strip():
        result["message"] = "Missing or invalid generated_at_utc timestamp."
        return result

    try:
        generated_at = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError:
        result["message"] = "generated_at_utc is not a valid ISO 8601 timestamp."
        return result
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        result["message"] = "generated_at_utc must include a timezone offset or Z."
        return result

    generated_at = generated_at.astimezone(timezone.utc)
    result["generated_at_utc"] = generated_at.isoformat()
    seconds_old = (checked_at - generated_at).total_seconds()
    if seconds_old < -(clock_skew_tolerance_minutes * 60):
        result["message"] = "Dataset timestamp is too far in the future."
        return result

    # A few seconds of tolerated clock skew cannot make data less than 0 hours old.
    age_hours = max(0.0, seconds_old / 3600)
    result["age_hours"] = round(age_hours, 6)

    if dataset.get("validation_status") != "passed":
        result["message"] = "Dataset publication validation has not passed."
    elif age_hours <= max_age_hours:
        result.update(status="fresh", is_fresh=True, message="Dataset was recently retrieved (fresh).")
    elif age_hours <= expiry_hours:
        result.update(status="stale", message="Menu data is older than the freshness threshold.")
    else:
        result.update(status="expired", message="Menu data has exceeded the expiration threshold.")
    return result


def _record_issue(record):
    """Return a reason if a food record cannot prove menu coverage."""
    if not isinstance(record, dict):
        return "record is not an object"
    for field in ("name", "dining_hall", "meal"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return f"missing or invalid {field}"
    if record["dining_hall"] not in EXPECTED_HALLS:
        return "unrecognized dining_hall"
    try:
        _iso_date(record.get("date"))
    except ValueError:
        return "missing or invalid date"
    # Unknown allergens (None / missing) remain UNKNOWN, never 'allergen-free'.
    if "allergens" in record and record["allergens"] is not None:
        allergens = record["allergens"]
        if not isinstance(allergens, list) or any(
            not isinstance(item, str) or not item.strip() for item in allergens
        ):
            return "invalid allergens field"
    return None


def check_menu_date_coverage(dataset, requested_dates):
    """Check metadata AND valid food records for every requested date/hall."""
    result = {
        "all_dates_available": False,
        "requested_dates": [],
        "available_dates": [],
        "missing_dates": [],
        "missing_halls_by_date": {},
        "meal_periods_by_date": {},
        "invalid_record_count": 0,
        "errors": [],
        "message": "Menu date coverage cannot be verified.",
    }
    try:
        requested = _requested_date_list(requested_dates)
    except ValueError as exc:
        result["errors"].append(str(exc))
        result["message"] = "Invalid requested menu dates."
        return result
    result["requested_dates"] = requested

    if not isinstance(dataset, dict):
        result["errors"].append("Dataset must be a JSON object.")
        return result

    menu_dates = dataset.get("menu_dates")
    if not isinstance(menu_dates, list):
        result["errors"].append("menu_dates must be a list of ISO dates.")
        return result
    try:
        normalized_menu_dates = [_iso_date(item) for item in menu_dates]
    except ValueError as exc:
        result["errors"].append(f"Invalid menu_dates metadata: {exc}")
        return result
    if len(set(normalized_menu_dates)) != len(normalized_menu_dates):
        result["errors"].append("menu_dates contains duplicate dates.")
        return result
    published_dates = set(normalized_menu_dates)

    records = dataset.get("records")
    if not isinstance(records, list):
        result["errors"].append("records must be a list of food objects.")
        return result

    # Store which meal names exist, without assuming all meal periods are served.
    meals_by_date = {day: {hall: set() for hall in EXPECTED_HALLS} for day in requested}
    for record in records:
        issue = _record_issue(record)
        if issue is not None:
            result["invalid_record_count"] += 1
            continue
        day = _iso_date(record["date"])
        if day in meals_by_date:
            meals_by_date[day][record["dining_hall"]].add(record["meal"].strip())

    if result["invalid_record_count"]:
        result["errors"].append(
            f"Dataset contains {result['invalid_record_count']} malformed food record(s)."
        )

    for day in requested:
        halls = meals_by_date[day]
        present = {hall for hall, meals in halls.items() if meals}
        result["meal_periods_by_date"][day] = {
            hall: sorted(meals) for hall, meals in halls.items()
        }
        if day not in published_dates or not present:
            result["missing_dates"].append(day)
        elif present != set(EXPECTED_HALLS):
            result["missing_halls_by_date"][day] = [
                hall for hall in EXPECTED_HALLS if hall not in present
            ]
        else:
            result["available_dates"].append(day)

    result["all_dates_available"] = (
        not result["errors"]
        and not result["missing_dates"]
        and not result["missing_halls_by_date"]
    )
    if result["all_dates_available"]:
        result["message"] = "All requested dates have food records for all three dining halls."
    else:
        result["message"] = "Requested menu dates have missing or invalid coverage."
    return result


def _dataset_structure_errors(dataset):
    """Catch inconsistent published metadata and malformed record structure."""
    if not isinstance(dataset, dict):
        return ["Dataset must be a JSON object."]
    errors = []
    for field in ("schema_version", "run_id", "allergy_notice"):
        if not isinstance(dataset.get(field), str) or not dataset[field].strip():
            errors.append(f"{field} must be a nonempty string.")
    if dataset.get("schema_version") not in (None, "1.0"):
        errors.append("Unsupported schema_version; expected 1.0.")
    try:
        _iso_date(dataset.get("snapshot_date"))
    except ValueError:
        errors.append("snapshot_date must be a valid YYYY-MM-DD date.")
    if not isinstance(dataset.get("sources"), dict):
        errors.append("sources must be a JSON object.")

    halls = dataset.get("dining_halls")
    if not isinstance(halls, list) or len(halls) != len(EXPECTED_HALLS) or set(
        hall for hall in halls if isinstance(hall, str)
    ) != set(EXPECTED_HALLS):
        errors.append("dining_halls must list West Campus, Warren Towers, and Marciano Commons.")

    total = dataset.get("total_records")
    records = dataset.get("records")
    if not isinstance(records, list):
        errors.append("records must be a list of food objects.")
    else:
        if not records:
            errors.append("records cannot be empty.")
        for index, record in enumerate(records):
            issue = _record_issue(record)
            if issue is not None:
                errors.append(f"records[{index}]: {issue}.")
                if sum(item.startswith("records[") for item in errors) == 5:
                    errors.append("Additional malformed records, if any, are omitted.")
                    break
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        errors.append("total_records must be a nonnegative integer.")
    elif isinstance(records, list) and total != len(records):
        errors.append(f"total_records is {total}, but records contains {len(records)} items.")

    menu_dates = dataset.get("menu_dates")
    if not isinstance(menu_dates, list) or not menu_dates:
        errors.append("menu_dates must be a nonempty list of ISO dates.")
    else:
        try:
            normalized = [_iso_date(day) for day in menu_dates]
            if len(normalized) != len(set(normalized)):
                errors.append("menu_dates contains duplicate dates.")
        except ValueError as exc:
            errors.append(f"Invalid menu_dates metadata: {exc}")
    return errors


def validate_for_recommendations(
    dataset,
    requested_dates,
    now=None,
    max_age_hours=24,
    expiry_hours=48,
    clock_skew_tolerance_minutes=5,
):
    """Fail-closed prerequisite for meal planning; never changes dataset."""
    errors = _dataset_structure_errors(dataset)
    coverage = check_menu_date_coverage(dataset, requested_dates)
    errors.extend(coverage["errors"])
    try:
        freshness = check_dataset_freshness(
            dataset,
            now=now,
            max_age_hours=max_age_hours,
            expiry_hours=expiry_hours,
            clock_skew_tolerance_minutes=clock_skew_tolerance_minutes,
        )
    except ValueError as exc:
        errors.append(str(exc))
        freshness = {
            "status": "invalid", "is_fresh": False, "age_hours": None,
            "generated_at_utc": None, "checked_at_utc": None,
            "message": "Freshness could not be checked due to invalid settings.",
        }

    warnings = []
    if freshness["status"] in ("stale", "expired"):
        warnings.append(
            f"Menu information is older than {max_age_hours:g} hours; "
            "automatic recommendations are disabled."
        )
    if coverage["missing_dates"]:
        warnings.append("Menu information is missing for: " + ", ".join(coverage["missing_dates"]) + ".")
    for day, halls in coverage["missing_halls_by_date"].items():
        warnings.append(f"{day} is missing food records for: {', '.join(halls)}.")
    if freshness["status"] == "invalid":
        errors.append(freshness["message"])

    # Priority for a single UI status: invalid > expired > stale > missing_dates > fresh.
    if errors:
        status = "invalid"
    elif freshness["status"] in ("expired", "stale"):
        status = freshness["status"]
    elif not coverage["all_dates_available"]:
        status = "missing_dates"
    else:
        status = "fresh"

    return {
        "can_recommend": status == "fresh",
        "status": status,
        "freshness": freshness,
        "coverage": coverage,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": list(dict.fromkeys(errors)),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check current BU Dining menus before recommending meals.")
    parser.add_argument("--dates", nargs="+", required=True, help="Menu dates (YYYY-MM-DD).")
    parser.add_argument("--path", type=Path, default=None, help="Optional dataset path for local testing.")
    parser.add_argument("--max-age-hours", type=float, default=24, help="Freshness threshold (default: 24).")
    parser.add_argument("--expiry-hours", type=float, default=48, help="Expiry threshold (default: 48).")
    args = parser.parse_args(argv)

    try:
        dataset = load_current_dataset(args.path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    outcome = validate_for_recommendations(
        dataset, args.dates, max_age_hours=args.max_age_hours,
        expiry_hours=args.expiry_hours,
    )
    print(f"Status: {outcome['status']}")
    print(f"Can recommend: {'yes' if outcome['can_recommend'] else 'no'}")
    age = outcome["freshness"]["age_hours"]
    print(f"Dataset age: {age:.2f} hours" if age is not None else "Dataset age: unknown")
    missing = outcome["coverage"]["missing_dates"]
    print("Missing dates: " + (", ".join(missing) if missing else "none"))
    halls = outcome["coverage"]["missing_halls_by_date"]
    print("Missing halls: " + ("; ".join(f"{d}: {', '.join(names)}" for d, names in halls.items()) if halls else "none"))
    print("Warnings: " + ("; ".join(outcome["warnings"]) if outcome["warnings"] else "none"))
    print("Errors: " + ("; ".join(outcome["errors"]) if outcome["errors"] else "none"))
    return 0 if outcome["can_recommend"] else 1


if __name__ == "__main__":
    sys.exit(main())

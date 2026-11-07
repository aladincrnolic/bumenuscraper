
"""Terrier Trays: safely publish validated BU Dining menus.

Install:
    python -m pip install requests beautifulsoup4

Run (reuse a snapshot from today, otherwise download):
    python scripts/scrape_bu_menus.py

Force fresh requests for all three halls:
    python scripts/scrape_bu_menus.py --refresh

Use downloaded snapshots without network requests:
    python scripts/scrape_bu_menus.py --offline --snapshot-date 2026-11-07

The only live dataset is data/current/menus.json. Failed validation never
replaces that file. Each successful run is archived with its raw HTML.
No automatic scheduling or archive deletion is performed in this phase.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from urllib import robotparser
from urllib.parse import urlparse
from uuid import uuid4

import requests
from bs4 import BeautifulSoup, Tag


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CURRENT_DIR = DATA_DIR / "current"
ARCHIVE_DIR = DATA_DIR / "archive"
STAGING_DIR = DATA_DIR / "staging"
LEGACY_RAW_DIR = DATA_DIR / "raw"  # Compatibility with existing snapshots.

DINING_HALL_URLS = {
    "West Campus": "https://www.bu.edu/dining/location/west/",
    "Warren Towers": "https://www.bu.edu/dining/location/warren/",
    "Marciano Commons": "https://www.bu.edu/dining/location/marciano/",
}

HALL_SLUGS = {
    "West Campus": "west",
    "Warren Towers": "warren",
    "Marciano Commons": "marciano",
}

MENU_ITEM_SELECTOR = (
    "ol.js-menu-bydate li.menu-meal-period "
    "ol.menu-dishes > li.menu-item"
)

MEAL_NAMES = {
    "breakfast": "Breakfast",
    "brunch": "Brunch",
    "lunch": "Lunch",
    "dinner": "Dinner",
}

NUTRIENTS = {
    "Calories": ("calories", ""),
    "Total Fat": ("total_fat_g", "g"),
    "Saturated Fat": ("saturated_fat_g", "g"),
    "Trans Fat": ("trans_fat_g", "g"),
    "Cholesterol": ("cholesterol_mg", "mg"),
    "Sodium": ("sodium_mg", "mg"),
    "Total Carbohydrate": ("carbohydrates_g", "g"),
    "Dietary Fiber": ("fiber_g", "g"),
    "Sugars": ("sugars_g", "g"),
    "Protein": ("protein_g", "g"),
}

VITAMINS = {
    "Vitamin D": ("vitamin_d_dv", "%"),
    "Potassium": ("potassium_dv", "%"),
    "Calcium": ("calcium_dv", "%"),
    "Iron": ("iron_dv", "%"),
}

NUMBER_RE = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"

ALLERGEN_UNAVAILABLE_RE = re.compile(
    r"\ballergens?\s+(?:are|is)\s+(?:not\s+available|unavailable|unknown)\b"
    r"|\ballergen\s+information\s+(?:is|are)\s+"
    r"(?:not\s+available|unavailable|unknown|incomplete)\b",
    re.IGNORECASE,
)

NO_ALLERGENS_RE = re.compile(
    r"(?:none(?:\s+listed)?|no\s+(?:declared\s+)?allergens?\s*"
    r"(?:listed|identified|declared)?)",
    re.IGNORECASE,
)

ALLERGY_NOTICE = (
    "BU Dining warns that ingredients may change and cross-contact may occur. "
    "No menu item is guaranteed allergen-free. Ask BU Dining staff about "
    "allergies. allergens=null means unknown; allergens=[] means only that "
    "the source explicitly listed none, not that a dish is guaranteed safe."
)

USER_AGENT = "TerrierTrays/1.0 (educational BU menu research)"
TIMEOUT = (10, 35)
MIN_REQUEST_DELAY_SECONDS = 2.0


# ============================================================
# INDIVIDUAL ITEM EXTRACTION
# ============================================================

def clean_text(tag):
    """Return normalized visible text, or None."""
    return tag.get_text(" ", strip=True) or None if tag is not None else None


def own_matches(item, selector):
    """Restrict selection to descendants of this exact li.menu-item."""
    return [
        element for element in item.select(selector)
        if element.find_parent("li", class_="menu-item") is item
    ]


def own_match(item, selector):
    return next(iter(own_matches(item, selector)), None)


def parse_amount(raw, expected_unit):
    """Only accept a numeric amount with the expected unit; never guess."""
    if raw is None:
        return None

    pattern = rf"\s*({NUMBER_RE})\s*{re.escape(expected_unit)}\s*"

    match = re.fullmatch(
        pattern,
        raw,
        flags=re.IGNORECASE
    )

    if match is None:
        return None

    number = float(match.group(1).replace(",", ""))

    if not (0 <= number < float("inf")):
        return None

    return int(number) if number.is_integer() else number


def read_nutrient_table(nutrition, selector, definitions, value_class):
    """Pair each label with the immediately following value cell."""
    result = {}
    conflicting = set()

    if nutrition is None:
        return result

    table = nutrition.select_one(selector)

    if table is None:
        return result

    for label_cell in table.select("td.nutrition-label-nutrient"):
        label = clean_text(label_cell)

        if label not in definitions:
            continue

        field, unit = definitions[label]
        value_cell = label_cell.find_next_sibling("td")

        parsed = None

        if value_cell is not None and value_class in value_cell.get("class", []):
            parsed = parse_amount(clean_text(value_cell), unit)

        if field in result and result[field] != parsed:
            conflicting.add(field)

        elif field not in result:
            result[field] = parsed

    for field in conflicting:
        result[field] = None

    return result


def iso_menu_date(value):
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None

    try:
        return date.fromisoformat(value).isoformat()

    except ValueError:
        return None


# ============================================================
# ALLERGEN EXTRACTION
# ============================================================

def allergen_status(nutrition):
    """An empty HTML allergen list is NOT evidence of no allergens."""
    if nutrition is None:
        return "missing_nutrition_wrapper"

    if ALLERGEN_UNAVAILABLE_RE.search(clean_text(nutrition) or ""):
        return "explicitly_unavailable"

    section = nutrition.select_one(".nutrition-facts-allergens")

    if section is None:
        return "missing_allergen_section"

    labels = [clean_text(li) for li in section.select("li")]
    labels = [label for label in labels if label]

    if labels:
        none_flags = [
            bool(NO_ALLERGENS_RE.fullmatch(label))
            for label in labels
        ]

        if len(labels) == 1 and none_flags[0]:
            return "explicitly_none_listed"

        return (
            "ambiguous_allergen_labels"
            if any(none_flags)
            else "allergens_listed"
        )

    if NO_ALLERGENS_RE.fullmatch(clean_text(section) or ""):
        return "explicitly_none_listed"

    return "empty_allergen_list"


def extract_allergens(nutrition):
    status = allergen_status(nutrition)

    if status == "explicitly_none_listed":
        return []

    if status != "allergens_listed":
        return None

    section = nutrition.select_one(".nutrition-facts-allergens")

    return list(dict.fromkeys(
        label for li in section.select("li")
        if (label := clean_text(li))
    ))


# ============================================================
# EXTRACT A SINGLE FOOD
# ============================================================

def extract_food_item(item, dining_hall):
    """Extract one occurrence of a BU food, retaining all 24 original fields."""
    if dining_hall not in DINING_HALL_URLS:
        raise ValueError(f"Unknown dining hall: {dining_hall}")

    if (
        not isinstance(item, Tag)
        or item.name != "li"
        or "menu-item" not in item.get("class", [])
    ):
        return None

    name = clean_text(own_match(item, ".menu-item-title"))

    if not name:
        return None

    food = {
        "name": name,
        "dining_hall": dining_hall,
        "station": clean_text(own_match(item, ".js-sortby-station")),
        "meal": None,
        "date": None,
        "dietary_tags": [],
        "allergens": None,
        "serving_size": None,
        "calories": None,
        "total_fat_g": None,
        "saturated_fat_g": None,
        "trans_fat_g": None,
        "cholesterol_mg": None,
        "sodium_mg": None,
        "carbohydrates_g": None,
        "fiber_g": None,
        "sugars_g": None,
        "protein_g": None,
        "vitamin_d_dv": None,
        "potassium_dv": None,
        "calcium_dv": None,
        "iron_dv": None,
        "ingredients": None,
        "source_url": DINING_HALL_URLS[dining_hall],
    }

    # Ascend from THIS food element to its meal and menu-date ancestors.
    meal_parent = item.find_parent("li", class_="menu-meal-period")

    if meal_parent is not None:
        for css_class in meal_parent.get("class", []):
            if css_class.startswith("js-meal-period-"):
                code = css_class.removeprefix("js-meal-period-")
                food["meal"] = MEAL_NAMES.get(code)
                break

    date_parent = item.find_parent("ol", class_="js-menu-bydate")

    if date_parent is not None:
        food["date"] = iso_menu_date(
            date_parent.get("data-menudate")
        )

    tags = [
        clean_text(li)
        for li in own_matches(
            item,
            ".menu-item-dietary-restriction li"
        )
    ]

    food["dietary_tags"] = list(
        dict.fromkeys(x for x in tags if x)
    )

    # Nutrition MUST come from this same li.menu-item.
    nutrition = own_match(item, ".nutrition-wrapper")

    if nutrition is None:
        return food

    food["allergens"] = extract_allergens(nutrition)

    serving = clean_text(
        nutrition.select_one(".nutrition-serving-size")
    )

    if serving:
        food["serving_size"] = re.sub(
            r"^Serving\s+size\s*:?\s*",
            "",
            serving,
            flags=re.IGNORECASE
        ).strip() or None

    ingredients = clean_text(
        nutrition.select_one(".nutrition-facts-ingredients")
    )

    if ingredients:
        food["ingredients"] = re.sub(
            r"^Ingredients\s*:\s*",
            "",
            ingredients,
            flags=re.IGNORECASE
        ).strip() or None

    food.update(read_nutrient_table(
        nutrition,
        "table.nutrition-label",
        NUTRIENTS,
        "nutrition-label-amount"
    ))

    food.update(read_nutrient_table(
        nutrition,
        "table.nutrition-vitamins",
        VITAMINS,
        "nutrition-label-percentage"
    ))

    return food


# ============================================================
# EXTRACT ALL FOODS FROM ONE HALL
# ============================================================

def extract_all_with_diagnostics(soup, dining_hall):
    """Only select dated dining dishes, never WordPress navigation items."""
    items = soup.select(MENU_ITEM_SELECTOR)

    records = []
    failures = []

    missing_wrapper_count = 0
    invalid_count = 0

    statuses = Counter()

    for index, item in enumerate(items, 1):
        try:
            nutrition = own_match(item, ".nutrition-wrapper")

            if nutrition is None:
                missing_wrapper_count += 1

            food = extract_food_item(item, dining_hall)

            if food is None:
                invalid_count += 1
                continue

            # Never deduplicate using the food name.
            records.append(food)

            statuses[allergen_status(nutrition)] += 1

        except Exception as exc:
            failures.append({
                "item_index": index,
                "error": repr(exc),
            })

    missing = Counter(
        field
        for record in records
        for field, value in record.items()
        if value is None
    )

    dates = sorted({
        record["date"]
        for record in records
        if record["date"]
    })

    occurrence_counts = Counter(
        f"{record['date']} | {record['meal']}"
        for record in records
    )

    core_fields = [
        field for field, _ in NUTRIENTS.values()
    ]

    complete_nutrition = sum(
        all(
            record[field] is not None
            for field in core_fields
        )
        for record in records
    )

    report = {
        "dining_hall": dining_hall,
        "source_url": DINING_HALL_URLS[dining_hall],
        "items_seen": len(items),
        "valid_records": len(records),
        "invalid_records": invalid_count,
        "extraction_failures": failures,
        "items_without_nutrition_wrapper": missing_wrapper_count,
        "records_with_complete_core_nutrients": complete_nutrition,
        "missing_field_counts": dict(sorted(missing.items())),
        "allergen_status_counts": dict(sorted(statuses.items())),
        "dates": dates,
        "occurrences_by_date_meal": dict(
            sorted(occurrence_counts.items())
        ),
        "errors": [],
        "warnings": [],
    }

    return records, report


# ============================================================
# VALIDATE A DINING HALL
# ============================================================

def validate_hall(soup, records, report, hall):
    """Reject an empty, mislabeled, or incomplete 7-day hall snapshot."""
    title = clean_text(soup.title) or ""

    headings = [
        clean_text(tag) or ""
        for tag in soup.select("h1")
    ]

    if not any(
        hall.casefold() in text.casefold()
        for text in [title] + headings
    ):
        report["errors"].append(
            "Page title/heading does not match this hall"
        )

    if not report["items_seen"] or not records:
        report["errors"].append(
            "No recognizable food items"
        )

    if report["extraction_failures"]:
        report["errors"].append(
            "At least one food item raised an exception"
        )

    if report["invalid_records"]:
        report["errors"].append(
            "One or more selected dishes lacked a valid name"
        )

    if any(
        row["date"] is None or row["meal"] is None
        for row in records
    ):
        report["errors"].append(
            "Some food records lack date or meal context"
        )

    if any(
        row["source_url"] != DINING_HALL_URLS[hall]
        for row in records
    ):
        report["errors"].append(
            "Food record source URL does not match hall"
        )

    dates = report["dates"]

    if len(dates) != 7:
        report["errors"].append(
            f"Expected 7 menu dates; found {len(dates)}"
        )

    elif [date.fromisoformat(d) for d in dates] != [
        date.fromisoformat(dates[0]) + timedelta(days=n)
        for n in range(7)
    ]:
        report["errors"].append(
            "Menu dates are not consecutive"
        )

    if report["records_with_complete_core_nutrients"] == 0:
        report["errors"].append(
            "No food has complete core nutritional values"
        )

    missing = report["missing_field_counts"]

    for field in ("calories", "allergens"):
        if missing.get(field, 0):
            report["warnings"].append(
                f"{missing[field]} records have unknown "
                f"{field}; values retained as null"
            )

    return not report["errors"]


# ============================================================
# FILE WRITING
# ============================================================

def write_bytes_atomic(path, payload):
    """Write a single file atomically on the destination filesystem."""
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = path.with_name(
        f".{path.name}.{uuid4().hex}.tmp"
    )

    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)

    finally:
        tmp.unlink(missing_ok=True)


def write_json(path, data):
    payload = (
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            allow_nan=False
        ) + "\n"
    ).encode("utf-8")

    write_bytes_atomic(path, payload)


# ============================================================
# CACHED HTML
# ============================================================

def find_cached_html(hall, snapshot_date):
    """Reuse successful archived snapshots or existing legacy data/raw files."""
    slug = HALL_SLUGS[hall]

    successful = sorted(
        ARCHIVE_DIR.glob(
            f"{snapshot_date}_*/{slug}.html"
        ),
        reverse=True
    )

    if successful:
        return successful[0]

    legacy = (
        LEGACY_RAW_DIR
        / f"{slug}_{snapshot_date}.html"
    )

    return legacy if legacy.is_file() else None


# ============================================================
# RESPONSIBLE DOWNLOADING
# ============================================================

def read_robots(session):
    response = session.get(
        "https://www.bu.edu/robots.txt",
        timeout=TIMEOUT
    )

    if response.status_code == 404:
        print(
            "  robots.txt not found; also review "
            "BU's website usage terms"
        )
        return None

    response.raise_for_status()

    parser = robotparser.RobotFileParser()

    parser.parse(
        response.text.splitlines()
    )

    return parser


def download_or_load(
    hall,
    snapshot_date,
    stage,
    session,
    robots_state,
    offline=False,
    refresh=False
):
    """Save one original HTML snapshot in temporary staging."""
    slug = HALL_SLUGS[hall]

    stage_path = stage / f"{slug}.html"

    # ----------------------------------------
    # USE PREVIOUSLY DOWNLOADED HTML
    # ----------------------------------------

    if not refresh:
        cached = find_cached_html(
            hall,
            snapshot_date
        )

        if cached is not None:
            print(
                f"  {hall}: cached HTML from {cached}"
            )

            html = cached.read_bytes()

            write_bytes_atomic(
                stage_path,
                html
            )

            return html

    if offline:
        raise FileNotFoundError(
            f"No cached HTML for {hall} on {snapshot_date}"
        )

    # ----------------------------------------
    # CHECK SCRAPING PERMISSIONS
    # ----------------------------------------

    if not robots_state["checked"]:
        robots_state["parser"] = read_robots(session)
        robots_state["checked"] = True

    robots = robots_state["parser"]
    url = DINING_HALL_URLS[hall]

    if robots is not None and not robots.can_fetch(
        USER_AGENT,
        url
    ):
        raise PermissionError(
            f"robots.txt disallows: {url}"
        )

    # ----------------------------------------
    # RESPECT REQUEST DELAYS
    # ----------------------------------------

    delay = MIN_REQUEST_DELAY_SECONDS

    if robots is not None:
        delay = max(
            delay,
            robots.crawl_delay(USER_AGENT) or 0
        )

    time.sleep(delay)

    print(
        f"  {hall}: downloading {url}"
    )

    response = session.get(
        url,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    # ----------------------------------------
    # VERIFY OFFICIAL URL
    # ----------------------------------------

    final = urlparse(response.url)
    expected = urlparse(url)

    if (
        final.scheme != "https"
        or final.netloc != expected.netloc
        or final.path.rstrip("/") != expected.path.rstrip("/")
    ):
        raise ValueError(
            f"Unexpected redirect: {response.url}"
        )

    if not response.content:
        raise ValueError(
            "Response contained no HTML"
        )

    # ----------------------------------------
    # SAVE HTML TO STAGING
    # ----------------------------------------

    write_bytes_atomic(
        stage_path,
        response.content
    )

    return response.content


# ============================================================
# DATA PROVENANCE
# ============================================================

def source_metadata(html, hall, archive_html_path):
    return {
        "url": DINING_HALL_URLS[hall],
        "raw_html_path": archive_html_path.relative_to(
            PROJECT_ROOT
        ).as_posix(),
        "raw_html_sha256": hashlib.sha256(
            html
        ).hexdigest(),
    }


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--snapshot-date",
        default=date.today().isoformat(),
        help=(
            "Snapshot label YYYY-MM-DD; menu dates "
            "are read from official HTML"
        )
    )

    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use saved HTML; no network calls"
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch new HTML even if cached"
    )

    args = parser.parse_args()

    if args.offline and args.refresh:
        parser.error(
            "--offline and --refresh cannot be used together"
        )

    snapshot_date = iso_menu_date(
        args.snapshot_date
    )

    if snapshot_date is None:
        parser.error(
            "--snapshot-date must be a real YYYY-MM-DD date"
        )

    # ========================================================
    # CREATE TEMPORARY RUN DIRECTORY
    # ========================================================

    run_id = (
        f"{snapshot_date}_"
        f"{datetime.now(timezone.utc):%H%M%S%f}_"
        f"{uuid4().hex[:8]}"
    )

    stage = STAGING_DIR / run_id

    stage.mkdir(
        parents=True,
        exist_ok=False
    )

    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    by_hall = {}
    reports = {}
    sources = {}

    robots_state = {
        "checked": False,
        "parser": None,
    }

    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT
    })

    archive_success = ARCHIVE_DIR / run_id

    print(
        f"\nTerrier Trays run: {run_id}"
    )

    print(
        "Check BU's website usage restrictions "
        "before live scraping."
    )

    # ========================================================
    # PROCESS ALL THREE DINING HALLS
    # ========================================================

    try:

        for hall in DINING_HALL_URLS:

            slug = HALL_SLUGS[hall]

            try:

                # ----------------------------------------
                # LOAD OR DOWNLOAD RAW HTML
                # ----------------------------------------

                html = download_or_load(
                    hall,
                    snapshot_date,
                    stage,
                    session,
                    robots_state,
                    offline=args.offline,
                    refresh=args.refresh,
                )

                # ----------------------------------------
                # PARSE HTML
                # ----------------------------------------

                soup = BeautifulSoup(
                    html,
                    "html.parser"
                )

                # ----------------------------------------
                # EXTRACT FOOD RECORDS
                # ----------------------------------------

                records, report = (
                    extract_all_with_diagnostics(
                        soup,
                        hall
                    )
                )

                # ----------------------------------------
                # VALIDATE HALL DATA
                # ----------------------------------------

                validate_hall(
                    soup,
                    records,
                    report,
                    hall
                )

                reports[hall] = report
                by_hall[hall] = records

                # ----------------------------------------
                # PRESERVE SOURCE INFORMATION
                # ----------------------------------------

                sources[hall] = source_metadata(
                    html,
                    hall,
                    archive_success / f"{slug}.html"
                )

                # ----------------------------------------
                # STAGE HALL JSON
                # ----------------------------------------

                write_json(
                    stage / f"{slug}.json",
                    records
                )

                print(
                    f"  {hall}: {len(records)} foods, "
                    f"{len(report['dates'])} dates, "
                    f"{report['missing_field_counts'].get('allergens', 0)} "
                    f"unknown allergens"
                )

                for error in report["errors"]:
                    print(
                        f"    ERROR: {error}"
                    )

            except Exception as exc:

                # A failed hall must never be published
                # as part of a successful dataset.

                reports[hall] = {
                    "dining_hall": hall,
                    "errors": [
                        f"Processing failed: {exc}"
                    ],
                    "warnings": [],
                }

                print(
                    f"  {hall}: FAILED ({exc})"
                )

    finally:
        session.close()

    # ========================================================
    # VALIDATE THE THREE-HALL DATASET
    # ========================================================

    failures = [
        hall
        for hall, report in reports.items()
        if report["errors"]
    ]

    if not failures:

        date_sets = [
            set(reports[hall]["dates"])
            for hall in DINING_HALL_URLS
        ]

        if not all(
            dates == date_sets[0]
            for dates in date_sets[1:]
        ):

            failures.append(
                "cross_hall_date_mismatch"
            )

            print(
                "ERROR: Halls do not cover "
                "identical menu dates."
            )

    # ========================================================
    # CREATE VALIDATION REPORT
    # ========================================================

    validation = {
        "run_id": run_id,

        "checked_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "status": (
            "failed"
            if failures
            else "passed"
        ),

        "failed_checks": failures,

        "halls": reports,
    }

    write_json(
        stage / "validation.json",
        validation
    )

    # ========================================================
    # FAILURE: DO NOT TOUCH CURRENT DATASET
    # ========================================================

    if failures:

        # Preserve failed snapshots separately
        # so we can debug the exact HTML later.

        failed_archive = (
            ARCHIVE_DIR
            / f"failed_{run_id}"
        )

        shutil.move(
            str(stage),
            str(failed_archive)
        )

        print(
            f"\nValidation FAILED. "
            f"Debug files: {failed_archive}"
        )

        print(
            "Existing data/current/menus.json "
            "was NOT changed."
        )

        return 1

    # ========================================================
    # COMBINE VALIDATED RECORDS
    # ========================================================

    # Never deduplicate foods by name.
    # Each hall, meal, and date occurrence is separate.

    records = [
        food
        for hall in DINING_HALL_URLS
        for food in by_hall[hall]
    ]

    menu_dates = reports[
        "West Campus"
    ]["dates"]

    # ========================================================
    # BUILD THE COMBINED DATASET
    # ========================================================

    combined = {
        "schema_version": "1.0",

        "run_id": run_id,

        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "snapshot_date": snapshot_date,

        "menu_dates": menu_dates,

        "dining_halls": list(
            DINING_HALL_URLS
        ),

        "total_records": len(records),

        "allergy_notice": ALLERGY_NOTICE,

        "validation_status": "passed",

        "sources": sources,

        "records": records,
    }

    # ========================================================
    # STAGE THE COMBINED JSON
    # ========================================================

    write_json(
        stage / "menus.json",
        combined
    )

    # ========================================================
    # BACK UP PREVIOUS CURRENT DATASET
    # ========================================================

    previous = CURRENT_DIR / "menus.json"

    if previous.exists():

        shutil.copy2(
            previous,
            stage / "previous_current_menus.json"
        )

    # ========================================================
    # ARCHIVE VALIDATED SNAPSHOT
    # ========================================================

    # At this point, all validation has succeeded.
    #
    # Move staged files to a permanent dated archive
    # before changing the active dataset.

    shutil.move(
        str(stage),
        str(archive_success)
    )

    # ========================================================
    # ATOMIC PUBLICATION
    # ========================================================

    # Only ONE file is the active dataset:
    #
    #     data/current/menus.json
    #
    # It is replaced using os.replace() inside
    # write_bytes_atomic().
    #
    # Other JSON files remain historical archives.

    try:

        write_bytes_atomic(
            previous,
            (
                archive_success / "menus.json"
            ).read_bytes()
        )

    except OSError as exc:

        print(
            f"Publication FAILED: {exc}"
        )

        print(
            "Validated snapshot is available at "
            f"{archive_success}"
        )

        print(
            "Existing current menus were not "
            "deliberately replaced."
        )

        return 1

    # ========================================================
    # SUCCESS SUMMARY
    # ========================================================

    print(
        "\nPublication succeeded."
    )

    print(
        f"Current menu data: {previous}"
    )

    print(
        f"Archived snapshot: {archive_success}"
    )

    print(
        f"Food occurrences: {len(records)}"
    )

    print(
        f"Menu dates: {menu_dates[0]} "
        f"through {menu_dates[-1]}"
    )

    print(
        "No archive cleanup or scheduling "
        "is performed yet."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

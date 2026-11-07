
"""Download, extract, and validate official BU Dining menus for Terrier Trays.

Install:
    pip install requests beautifulsoup4

Run:
    python scripts/scrape_bu_menus.py

Use cached HTML without downloading:
    python scripts/scrape_bu_menus.py --offline

Download fresh HTML:
    python scripts/scrape_bu_menus.py --refresh

The script uses official public pages only.

Important safety rules:
- Never invent missing nutritional information.
- Never interpret missing allergen data as allergen-free.
- Preserve food occurrences across dates, meals, and halls.
- Preserve original HTML for verification.
- Never publish an incomplete combined dataset as successful.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from urllib import robotparser

import requests
from bs4 import BeautifulSoup, Tag


# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"

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

# Select only real dining menu items.
# This excludes WordPress navigation links that also use
# the class "menu-item".
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

# BU's exact nutrition labels.
# Values are (output dictionary key, expected unit).
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

NUMBER_RE = (
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"
)

ALLERGEN_UNAVAILABLE_RE = re.compile(
    r"\ballergens?\s+(?:are|is)\s+"
    r"(?:not\s+available|unavailable|unknown)\b"
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
    "BU Dining warns that ingredient substitutions, formulation "
    "changes, and kitchen cross-contact can occur. No menu item "
    "is guaranteed allergen-free. Students with allergies should "
    "consult BU Dining staff. A null allergens field means "
    "unknown. An empty allergen list, when explicitly documented, "
    "only means that BU lists no allergens."
)

USER_AGENT = "TerrierTrays/1.0 (educational menu research)"

HTTP_TIMEOUT = (10, 35)

# Minimum delay before each live dining-page request.
REQUEST_DELAY_SECONDS = 2.0


# ============================================================
# TEXT AND HTML HELPERS
# ============================================================

def clean_text(tag):
    """
    Return the stripped text of an HTML element.

    Returns None if the element does not exist or is empty.
    """
    if tag is None:
        return None

    return tag.get_text(" ", strip=True) or None


def own_matches(item, selector):
    """
    Find matching descendants belonging to this exact food item.

    This prevents accidentally taking information from another
    nested li.menu-item element.
    """
    return [
        tag
        for tag in item.select(selector)
        if tag.find_parent(
            "li",
            class_="menu-item"
        ) is item
    ]


def own_match(item, selector):
    """
    Return the first matching descendant belonging to this item.
    """
    return next(
        iter(own_matches(item, selector)),
        None
    )


# ============================================================
# NUMERICAL PARSING
# ============================================================

def parse_amount(raw, expected_unit):
    """
    Convert a nutritional value into an int or float.

    Examples:
        "250"   -> 250
        "8g"    -> 8
        "0.5g"  -> 0.5
        "570mg" -> 570
        "23%"   -> 23

    The supplied unit must match exactly.

    Missing or malformed values return None.
    """
    if raw is None:
        return None

    pattern = (
        rf"\s*({NUMBER_RE})\s*"
        rf"{re.escape(expected_unit)}\s*"
    )

    match = re.fullmatch(
        pattern,
        raw,
        flags=re.IGNORECASE
    )

    if match is None:
        return None

    number = float(
        match.group(1).replace(",", "")
    )

    # Reject negative, infinite, or invalid numbers.
    if not (number >= 0 and number < float("inf")):
        return None

    if number.is_integer():
        return int(number)

    return number


def read_nutrient_table(
    nutrition,
    selector,
    definitions,
    value_class
):
    """
    Extract nutrition values from a specific table.

    Each nutrient label is paired with the amount cell
    immediately following it.

    Handles multiple nutrient/value pairs in one row.
    """
    result = {}
    conflicts = set()

    if nutrition is None:
        return result

    table = nutrition.select_one(selector)

    if table is None:
        return result

    for label_cell in table.select(
        "td.nutrition-label-nutrient"
    ):

        label = clean_text(label_cell)

        if label not in definitions:
            continue

        field, unit = definitions[label]

        value_cell = label_cell.find_next_sibling("td")

        parsed = None

        if (
            value_cell is not None
            and value_class in value_cell.get("class", [])
        ):
            parsed = parse_amount(
                clean_text(value_cell),
                unit
            )

        # If the same nutrient appears with conflicting
        # values, do not arbitrarily choose one.
        if field in result and result[field] != parsed:
            conflicts.add(field)

        elif field not in result:
            result[field] = parsed

    for field in conflicts:
        result[field] = None

    return result


def iso_menu_date(value):
    """
    Validate an ISO-formatted YYYY-MM-DD date.
    """
    if not value:
        return None

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None

    try:
        return date.fromisoformat(value).isoformat()

    except ValueError:
        return None


# ============================================================
# ALLERGEN EXTRACTION AND SAFETY
# ============================================================

def allergen_status(nutrition):
    """
    Determine the evidence status of allergen information.

    This is used for diagnostics and safety.

    An empty HTML allergen list is NOT evidence that
    the food is allergen-free.
    """
    if nutrition is None:
        return "missing_nutrition_wrapper"

    nutrition_text = clean_text(nutrition) or ""

    # BU's explicit warning overrides apparent labels.
    if ALLERGEN_UNAVAILABLE_RE.search(nutrition_text):
        return "explicitly_unavailable"

    section = nutrition.select_one(
        ".nutrition-facts-allergens"
    )

    if section is None:
        return "missing_allergen_section"

    labels = [
        clean_text(li)
        for li in section.select("li")
    ]

    labels = [
        label
        for label in labels
        if label
    ]

    if labels:

        declared_none = [
            bool(NO_ALLERGENS_RE.fullmatch(label))
            for label in labels
        ]

        if len(labels) == 1 and declared_none[0]:
            return "explicitly_none_listed"

        if any(declared_none):
            return "ambiguous_allergen_labels"

        return "allergens_listed"

    section_text = clean_text(section) or ""

    if NO_ALLERGENS_RE.fullmatch(section_text):
        return "explicitly_none_listed"

    # BU uses empty <ul> elements for many items.
    # This is NOT proof that allergens are absent.
    return "empty_allergen_list"


def extract_allergens(nutrition):
    """
    Extract officially documented allergens.

    Returns:
        list -> allergen labels supplied by BU
        []   -> explicitly documented as none listed
        None -> missing, empty, ambiguous, or unavailable

    Never infer that a food is allergy-safe.
    """
    status = allergen_status(nutrition)

    if status == "explicitly_none_listed":
        return []

    if status != "allergens_listed":
        return None

    section = nutrition.select_one(
        ".nutrition-facts-allergens"
    )

    labels = [
        clean_text(li)
        for li in section.select("li")
    ]

    return list(
        dict.fromkeys(
            label
            for label in labels
            if label
        )
    )


# ============================================================
# EXTRACT AN INDIVIDUAL FOOD ITEM
# ============================================================

def extract_food_item(item, dining_hall):
    """
    Extract exactly one BU Dining food occurrence.

    Args:
        item:
            BeautifulSoup Tag representing li.menu-item.

        dining_hall:
            Human-readable dining hall name.

    Returns:
        Dictionary containing all 24 requested fields,
        or None if the element is not a valid food item.

    All missing nutrition values remain None.
    """

    if dining_hall not in DINING_HALL_URLS:
        raise ValueError(
            f"Unsupported dining hall: {dining_hall}"
        )

    if not isinstance(item, Tag):
        return None

    if item.name != "li":
        return None

    if "menu-item" not in item.get("class", []):
        return None

    # ------------------------------------------
    # FOOD NAME
    # ------------------------------------------

    name = clean_text(
        own_match(item, ".menu-item-title")
    )

    if not name:
        return None

    # ------------------------------------------
    # INITIALIZE THE COMPLETE RECORD
    # ------------------------------------------

    food = {
        "name": name,
        "dining_hall": dining_hall,

        "station": clean_text(
            own_match(item, ".js-sortby-station")
        ),

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

    # ------------------------------------------
    # MEAL PERIOD
    # ------------------------------------------

    # Walk upward from this food item to the nearest
    # enclosing meal-period element.
    meal_parent = item.find_parent(
        "li",
        class_="menu-meal-period"
    )

    if meal_parent is not None:

        for css_class in meal_parent.get("class", []):

            if css_class.startswith("js-meal-period-"):

                meal_code = css_class.removeprefix(
                    "js-meal-period-"
                )

                food["meal"] = MEAL_NAMES.get(meal_code)

                break

    # ------------------------------------------
    # DATE
    # ------------------------------------------

    # Locate the date container containing this item.
    date_parent = item.find_parent(
        "ol",
        class_="js-menu-bydate"
    )

    if date_parent is not None:

        food["date"] = iso_menu_date(
            date_parent.get("data-menudate")
        )

    # ------------------------------------------
    # DIETARY TAGS
    # ------------------------------------------

    dietary_labels = [
        clean_text(li)
        for li in own_matches(
            item,
            ".menu-item-dietary-restriction li"
        )
    ]

    food["dietary_tags"] = list(
        dict.fromkeys(
            label
            for label in dietary_labels
            if label
        )
    )

    # ------------------------------------------
    # FIND THIS ITEM'S NUTRITION WRAPPER
    # ------------------------------------------

    nutrition = own_match(
        item,
        ".nutrition-wrapper"
    )

    # Missing nutrition does not invalidate a real
    # food item. Retain it with unknown values.
    if nutrition is None:
        return food

    # ------------------------------------------
    # ALLERGENS
    # ------------------------------------------

    food["allergens"] = extract_allergens(nutrition)

    # ------------------------------------------
    # SERVING SIZE
    # ------------------------------------------

    serving = clean_text(
        nutrition.select_one(
            ".nutrition-serving-size"
        )
    )

    if serving:

        serving = re.sub(
            r"^Serving\s+size\s*:?\s*",
            "",
            serving,
            flags=re.IGNORECASE
        ).strip()

        food["serving_size"] = serving or None

    # ------------------------------------------
    # INGREDIENTS
    # ------------------------------------------

    ingredients = clean_text(
        nutrition.select_one(
            ".nutrition-facts-ingredients"
        )
    )

    if ingredients:

        ingredients = re.sub(
            r"^Ingredients\s*:\s*",
            "",
            ingredients,
            flags=re.IGNORECASE
        ).strip()

        food["ingredients"] = ingredients or None

    # ------------------------------------------
    # MAIN NUTRIENT TABLE
    # ------------------------------------------

    nutrient_values = read_nutrient_table(
        nutrition,
        "table.nutrition-label",
        NUTRIENTS,
        "nutrition-label-amount"
    )

    food.update(nutrient_values)

    # ------------------------------------------
    # VITAMINS / MINERALS
    # ------------------------------------------

    vitamin_values = read_nutrient_table(
        nutrition,
        "table.nutrition-vitamins",
        VITAMINS,
        "nutrition-label-percentage"
    )

    food.update(vitamin_values)

    return food


# ============================================================
# EXTRACT ALL FOOD ITEMS FROM ONE HALL
# ============================================================

def extract_all_with_diagnostics(soup, dining_hall):
    """
    Extract all food occurrences from one dining hall.

    Uses a food-specific selector that excludes navigation
    links and unrelated WordPress menu elements.

    Does not deduplicate foods by name.

    Returns:
        records: list of extracted food dictionaries
        report: diagnostic information
    """

    items = soup.select(MENU_ITEM_SELECTOR)

    records = []
    failures = []

    invalid = 0
    absent_wrappers = 0

    statuses = Counter()

    for index, item in enumerate(items, start=1):

        try:

            nutrition = own_match(
                item,
                ".nutrition-wrapper"
            )

            if nutrition is None:
                absent_wrappers += 1

            food = extract_food_item(
                item,
                dining_hall
            )

            if food is None:
                invalid += 1
                continue

            records.append(food)

            statuses[
                allergen_status(nutrition)
            ] += 1

        except Exception as exc:

            failures.append({
                "item_index": index,
                "error": repr(exc),
            })

    # Count unknown values in each field.
    missing = Counter(
        field
        for record in records
        for field, value in record.items()
        if value is None
    )

    # Count occurrences by date and meal.
    occurrence_counts = Counter(
        f"{record['date']} | {record['meal']}"
        for record in records
    )

    dates = sorted({
        record["date"]
        for record in records
        if record["date"]
    })

    nutrient_fields = [
        field
        for field, unit in NUTRIENTS.values()
    ]

    complete_nutrition = sum(
        all(
            record[field] is not None
            for field in nutrient_fields
        )
        for record in records
    )

    report = {
        "dining_hall": dining_hall,

        "source_url": DINING_HALL_URLS[dining_hall],

        "items_seen": len(items),

        "valid_records": len(records),

        "invalid_records": invalid,

        "extraction_failures": failures,

        "items_without_nutrition_wrapper": absent_wrappers,

        "records_with_complete_core_nutrients": complete_nutrition,

        "missing_field_counts": dict(
            sorted(missing.items())
        ),

        "allergen_status_counts": dict(
            sorted(statuses.items())
        ),

        "dates": dates,

        "occurrences_by_date_meal": dict(
            sorted(occurrence_counts.items())
        ),

        "errors": [],
        "warnings": [],
    }

    return records, report


# ============================================================
# VALIDATE EACH DINING HALL
# ============================================================

def validate_hall(
    soup,
    records,
    report,
    dining_hall
):
    """
    Validate the scraped data for a single dining hall.

    Critical errors prevent publication of the combined
    dataset.

    Missing individual nutrition values cause warnings,
    not deletion of otherwise valid food records.
    """

    # Verify that the HTML identifies the intended hall.
    page_titles = [
        clean_text(element) or ""
        for element in soup.select("h1, title")
    ]

    if not any(
        dining_hall.lower() in title.lower()
        for title in page_titles
    ):

        report["errors"].append(
            "Page title does not identify the intended dining hall."
        )

    # Check that menu items were found.
    if not report["items_seen"] or not records:

        report["errors"].append(
            "No recognizable dining menu items found."
        )

    # Extraction exceptions require investigation.
    if report["extraction_failures"]:

        report["errors"].append(
            "One or more food-item extraction exceptions occurred."
        )

    # Individual invalid elements are reported.
    if report["invalid_records"]:

        report["warnings"].append(
            "Some dining menu elements lacked food names."
        )

    # Every record must have a verified date and meal.
    if any(
        record["date"] is None
        or record["meal"] is None
        for record in records
    ):

        report["errors"].append(
            "At least one record lacks a valid date or meal period."
        )

    # Verify every record's official source.
    if any(
        record["source_url"] != DINING_HALL_URLS[dining_hall]
        for record in records
    ):

        report["errors"].append(
            "At least one source URL does not match its dining hall."
        )

    # Verify seven menu dates.
    dates = report["dates"]

    if len(dates) != 7:

        report["errors"].append(
            f"Expected 7 menu dates, found {len(dates)}: {dates}"
        )

    else:

        first_date = date.fromisoformat(dates[0])

        expected_dates = [
            first_date + timedelta(days=i)
            for i in range(7)
        ]

        actual_dates = [
            date.fromisoformat(value)
            for value in dates
        ]

        if actual_dates != expected_dates:

            report["errors"].append(
                "Menu dates are not seven consecutive days."
            )

    # Missing nutrition is reported, not invented.
    missing = report["missing_field_counts"]

    if missing.get("calories", 0):

        report["warnings"].append(
            f"{missing['calories']} records have unknown calories."
        )

    if missing.get("allergens", 0):

        report["warnings"].append(
            f"{missing['allergens']} records have unknown allergens. "
            "These records must not be treated as allergy-safe."
        )

    return not report["errors"]


# ============================================================
# RESPONSIBLE DOWNLOADING
# ============================================================

def read_robots(session):
    """
    Retrieve and parse BU's robots.txt.

    Stops scraping if a robots.txt request fails,
    except when the file is explicitly not found.
    """
    robots_url = "https://www.bu.edu/robots.txt"

    response = session.get(
        robots_url,
        timeout=HTTP_TIMEOUT
    )

    if response.status_code == 404:

        print(
            "robots.txt returned HTTP 404. "
            "Review BU's website usage restrictions."
        )

        return None

    response.raise_for_status()

    parser = robotparser.RobotFileParser()

    parser.parse(
        response.text.splitlines()
    )

    return parser


def write_bytes_atomically(path, content):
    """
    Save bytes safely using a temporary file.

    Replace the destination only after writing succeeds.
    """
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_bytes(content)

    temporary.replace(path)


def write_json(path, value):
    """
    Write readable, UTF-8 JSON.
    """
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        allow_nan=False
    )

    write_bytes_atomically(
        path,
        (payload + "\n").encode("utf-8")
    )


def read_or_fetch_html(
    hall,
    snapshot_date,
    session,
    offline,
    refresh,
    robot_state
):
    """
    Obtain one dining hall's official HTML.

    Uses cached raw HTML when available.

    --offline:
        Never make HTTP requests.

    --refresh:
        Download new HTML and back up an older snapshot.

    Returns:
        html bytes, snapshot file path
    """

    slug = HALL_SLUGS[hall]

    path = RAW_DIR / f"{slug}_{snapshot_date}.html"

    # Reuse the snapshot if available.
    if path.exists() and not refresh:

        print(
            f"[{hall}] Using cached HTML: {path.name}"
        )

        return path.read_bytes(), path

    if offline:

        raise FileNotFoundError(
            f"No cached HTML for {hall}: {path}"
        )

    # Only check robots.txt once per run.
    if not robot_state["checked"]:

        robot_state["parser"] = read_robots(session)

        robot_state["checked"] = True

    robots = robot_state["parser"]

    url = DINING_HALL_URLS[hall]

    if robots is not None:

        if not robots.can_fetch(USER_AGENT, url):

            raise PermissionError(
                f"robots.txt does not permit fetching {url}"
            )

    # Respect configured minimum request delay
    # and any robots.txt crawl delay.
    delay = REQUEST_DELAY_SECONDS

    if robots is not None:

        delay = max(
            delay,
            robots.crawl_delay(USER_AGENT) or 0
        )

    time.sleep(delay)

    print(f"[{hall}] Downloading {url}")

    response = session.get(
        url,
        timeout=HTTP_TIMEOUT
    )

    response.raise_for_status()

    # Prevent unexpectedly redirected content from
    # being treated as official dining hall data.
    if not response.url.startswith(
        "https://www.bu.edu/dining/location/"
    ):

        raise ValueError(
            f"Unexpected redirect for {hall}: {response.url}"
        )

    html = response.content

    if not html:

        raise ValueError(
            f"Empty HTML response for {hall}"
        )

    # Preserve a previous snapshot during refresh.
    if path.exists():

        suffix = datetime.now(
            timezone.utc
        ).strftime("%H%M%S%f")

        backup = path.with_name(
            f"{path.stem}_previous_{suffix}.html"
        )

        shutil.copy2(path, backup)

        print(
            f"[{hall}] Previous HTML backed up: "
            f"{backup.name}"
        )

    write_bytes_atomically(path, html)

    print(f"[{hall}] HTML saved.")

    return html, path


# ============================================================
# DATA PROVENANCE
# ============================================================

def source_metadata(path, html, hall):
    """
    Record the origin of every hall's downloaded HTML.

    SHA-256 allows future verification that the raw
    HTML has not changed.
    """
    return {
        "url": DINING_HALL_URLS[hall],

        "raw_html_path": path.relative_to(
            PROJECT_ROOT
        ).as_posix(),

        "raw_html_sha256": hashlib.sha256(
            html
        ).hexdigest(),

        "file_saved_at_utc": datetime.fromtimestamp(
            path.stat().st_mtime,
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# PRINT DIAGNOSTICS
# ============================================================

def print_hall_summary(report):
    """
    Print a concise summary for a dining hall.
    """
    print(
        f"\n[{report['dining_hall']}]"
    )

    print(
        "Food records:",
        report["valid_records"]
    )

    print(
        "Selected menu items:",
        report["items_seen"]
    )

    print(
        "Menu dates:",
        len(report["dates"])
    )

    print(
        "Complete core nutrition:",
        report["records_with_complete_core_nutrients"]
    )

    print(
        "Unknown calories:",
        report["missing_field_counts"].get(
            "calories",
            0
        )
    )

    print(
        "Unknown allergens:",
        report["missing_field_counts"].get(
            "allergens",
            0
        )
    )

    print("\nAllergen information status:")

    for status, count in report[
        "allergen_status_counts"
    ].items():

        print(f"  {status}: {count}")

    for error in report.get("errors", []):

        print(f"ERROR: {error}")

    for warning in report.get("warnings", []):

        print(f"NOTE: {warning}")


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    """
    Download or load all three halls, extract food data,
    validate each hall, and generate a combined dataset.
    """

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--snapshot-date",

        default=date.today().isoformat(),

        help=(
            "Cache/output file label (YYYY-MM-DD). "
            "Actual food dates are read from BU HTML."
        )
    )

    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use saved HTML only."
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Download fresh HTML and back up old files."
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
            "--snapshot-date must be a valid YYYY-MM-DD date"
        )

    print("=" * 60)
    print("TERRIER TRAYS — BU DINING MENU SCRAPER")
    print("=" * 60)

    print(
        "\nReview BU's website usage restrictions "
        "before live scraping."
    )

    print(
        "Allergen information is not a guarantee "
        "of allergy safety.\n"
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # Track robots.txt checking across all halls.
    robot_state = {
        "checked": False,
        "parser": None,
    }

    reports = {}
    sources = {}
    by_hall = {}

    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT
    })

    # ------------------------------------------
    # SCRAPE EACH HALL
    # ------------------------------------------

    try:

        for hall in DINING_HALL_URLS:

            print("\n" + "-" * 60)
            print("PROCESSING:", hall)
            print("-" * 60)

            try:

                html, path = read_or_fetch_html(
                    hall,
                    snapshot_date,
                    session,
                    args.offline,
                    args.refresh,
                    robot_state
                )

                soup = BeautifulSoup(
                    html,
                    "html.parser"
                )

                records, report = (
                    extract_all_with_diagnostics(
                        soup,
                        hall
                    )
                )

                validate_hall(
                    soup,
                    records,
                    report,
                    hall
                )

                reports[hall] = report

                sources[hall] = source_metadata(
                    path,
                    html,
                    hall
                )

                by_hall[hall] = records

                print_hall_summary(report)

            except (
                requests.RequestException,
                OSError,
                ValueError
            ) as exc:

                reports[hall] = {
                    "dining_hall": hall,
                    "errors": [
                        f"Fetch or parse failed: {exc}"
                    ],
                    "warnings": [],
                }

                print(
                    f"[{hall}] ERROR: {exc}"
                )

    finally:

        session.close()

    # ------------------------------------------
    # CROSS-HALL VALIDATION
    # ------------------------------------------

    errors = [
        hall
        for hall, report in reports.items()
        if report["errors"]
    ]

    # Verify all three halls contain the same dates.
    if not errors:

        date_sets = [
            set(reports[hall]["dates"])
            for hall in DINING_HALL_URLS
        ]

        first_dates = date_sets[0]

        if not all(
            dates == first_dates
            for dates in date_sets[1:]
        ):

            errors.append(
                "Cross-hall date mismatch"
            )

            print(
                "\nERROR: Dining halls do not "
                "cover the same seven dates."
            )

    # ------------------------------------------
    # SAVE VALIDATION REPORT
    # ------------------------------------------

    validation = {
        "snapshot_date": snapshot_date,

        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "status": (
            "failed"
            if errors
            else "passed"
        ),

        "failed_halls_or_checks": errors,

        "halls": reports,
    }

    validation_path = (
        OUTPUT_DIR
        / f"validation_{snapshot_date}.json"
    )

    write_json(
        validation_path,
        validation
    )

    # Do not publish a new combined dataset
    # if one or more halls fail validation.
    if errors:

        print("\n" + "=" * 60)
        print("VALIDATION FAILED")
        print("=" * 60)

        print(
            "Diagnostics saved to:",
            validation_path
        )

        print(
            "No new combined dataset was published."
        )

        return 1

    # ------------------------------------------
    # COMBINE ALL THREE HALLS
    # ------------------------------------------

    # Do not deduplicate by name.
    # The same food can appear in several halls,
    # meals, or dates.
    all_records = [
        record
        for hall in DINING_HALL_URLS
        for record in by_hall[hall]
    ]

    menu_dates = reports[
        "West Campus"
    ]["dates"]

    # ------------------------------------------
    # SAVE INDIVIDUAL HALL DATASETS
    # ------------------------------------------

    for hall, records in by_hall.items():

        path = (
            OUTPUT_DIR
            / f"{HALL_SLUGS[hall]}_{snapshot_date}.json"
        )

        write_json(
            path,
            records
        )

        print(
            f"\nSaved {len(records)} records "
            f"for {hall}: {path}"
        )

    # ------------------------------------------
    # BUILD COMBINED DATASET
    # ------------------------------------------

    combined = {
        "schema_version": "1.0",

        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "snapshot_date": snapshot_date,

        "menu_dates": menu_dates,

        "dining_halls": list(
            DINING_HALL_URLS
        ),

        "total_records": len(all_records),

        "allergy_notice": ALLERGY_NOTICE,

        "sources": sources,

        "records": all_records,
    }

    # ------------------------------------------
    # SAVE COMBINED JSON
    # ------------------------------------------

    combined_path = (
        OUTPUT_DIR
        / f"terrier_trays_menus_{snapshot_date}.json"
    )

    write_json(
        combined_path,
        combined
    )

    # ------------------------------------------
    # FINAL RESULTS
    # ------------------------------------------

    print("\n" + "=" * 60)
    print("TERRIER TRAYS — EXTRACTION COMPLETE")
    print("=" * 60)

    for hall in DINING_HALL_URLS:

        print(
            f"{hall}: "
            f"{len(by_hall[hall])} records"
        )

    print(
        "\nTotal food occurrences:",
        len(all_records)
    )

    print(
        "Menu dates:",
        menu_dates[0],
        "through",
        menu_dates[-1]
    )

    print(
        "\nCombined dataset:",
        combined_path
    )

    print(
        "Validation report:",
        validation_path
    )

    print(
        "\nReminder: Unknown allergens must never "
        "be treated as safe for allergies."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
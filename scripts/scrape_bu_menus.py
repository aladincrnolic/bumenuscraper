
"""Extract official BU Dining menu items from previously downloaded HTML."""

from collections import Counter
from datetime import date
from pathlib import Path
import math
import re

import requests

from bs4 import BeautifulSoup, Tag


DINING_HALL_URLS = {
    "West Campus": "https://www.bu.edu/dining/location/west/",
    "Warren Towers": "https://www.bu.edu/dining/location/warren/",
    "Marciano Commons": "https://www.bu.edu/dining/location/marciano/",
}

# Exact BU table labels -> (output key, required unit).
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

MEAL_NAMES = {
    "breakfast": "Breakfast",
    "brunch": "Brunch",
    "lunch": "Lunch",
    "dinner": "Dinner",
}

NUMBER_RE = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"

# A warning overrides any apparent allergen list.
# Some BU items display allergen labels while explicitly
# stating that allergen data is unavailable.
ALLERGEN_UNAVAILABLE_RE = re.compile(
    r"\ballergens?\s+(?:are|is)\s+"
    r"(?:not\s+available|unavailable|unknown)\b"
    r"|\ballergen\s+information\s+(?:is|are)\s+"
    r"(?:not\s+available|unavailable|unknown|incomplete)\b",
    re.IGNORECASE,
)

NO_ALLERGENS_RE = re.compile(
    r"\b(?:none\s+listed|no\s+(?:declared\s+)?allergens?\s*"
    r"(?:listed|identified|declared)?)\b",
    re.IGNORECASE,
)


def clean_text(tag):
    """
    Return an element's stripped text.

    Returns None when the element is missing or empty.
    """
    if tag is None:
        return None

    return tag.get_text(" ", strip=True) or None


def own_matches(item, selector):
    """
    Return matching elements belonging to this item.

    Prevents accidentally extracting elements from a
    hypothetically nested menu item.
    """
    return [
        tag
        for tag in item.select(selector)
        if tag.find_parent("li", class_="menu-item") is item
    ]


def own_match(item, selector):
    """
    Return the first matching element belonging to this item.
    """
    return next(iter(own_matches(item, selector)), None)


def parse_amount(raw, expected_unit):
    """
    Parse a number with its expected unit.

    Examples:
        "250"   -> 250       (expected_unit="")
        "8g"    -> 8         (expected_unit="g")
        "0.5g"  -> 0.5       (expected_unit="g")
        "570mg" -> 570       (expected_unit="mg")
        "23%"   -> 23        (expected_unit="%")

    Returns None if the value is missing, malformed,
    or uses the wrong unit.

    Never assumes that a missing value is zero.
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

    number = float(match.group(1).replace(",", ""))

    if not math.isfinite(number):
        return None

    if number.is_integer():
        return int(number)

    return number


def read_nutrient_table(
    nutrition,
    table_selector,
    label_map,
    amount_class
):
    """
    Extract nutrients from one specific nutrition table.

    Each nutrient label is associated with the adjacent
    amount cell in the same table row.

    Handles multiple nutrient/value pairs per row.
    """
    result = {}

    if nutrition is None:
        return result

    table = nutrition.select_one(table_selector)

    if table is None:
        return result

    for label_cell in table.select(
        "td.nutrition-label-nutrient"
    ):
        label = clean_text(label_cell)

        if label not in label_map:
            continue

        output_key, unit = label_map[label]

        value_cell = label_cell.find_next_sibling("td")

        if (
            value_cell is None
            or amount_class not in value_cell.get("class", [])
        ):
            parsed = None
        else:
            parsed = parse_amount(
                clean_text(value_cell),
                unit
            )

        # Conflicting duplicate labels must not produce
        # an arbitrarily selected value.
        if (
            output_key in result
            and result[output_key] != parsed
        ):
            result[output_key] = None
        else:
            result[output_key] = parsed

    return result


def extract_allergens(nutrition):
    """
    Return documented allergens or None.

    Returns:
        list: Documented allergens.
        []: Official source explicitly says none listed.
        None: Allergens are missing or unavailable.

    IMPORTANT:
        None must never be interpreted as allergen-free.
        Even [] does not guarantee protection from
        substitutions or cross-contact.
    """
    if nutrition is None:
        return None

    # An explicit unavailable-data warning overrides
    # apparent allergen labels elsewhere in the wrapper.
    nutrition_text = clean_text(nutrition) or ""

    if ALLERGEN_UNAVAILABLE_RE.search(nutrition_text):
        return None

    section = nutrition.select_one(
        ".nutrition-facts-allergens"
    )

    if section is None:
        return None

    labels = [
        clean_text(tag)
        for tag in section.select("li")
    ]

    # Remove empty labels and repeated labels.
    labels = list(
        dict.fromkeys(
            label for label in labels if label
        )
    )

    if labels:
        # Explicitly documented absence of listed allergens.
        if (
            len(labels) == 1
            and (
                labels[0].lower() == "none"
                or NO_ALLERGENS_RE.fullmatch(labels[0])
            )
        ):
            return []

        # Contradictory information is treated as unknown.
        if any(
            label.lower() == "none"
            or NO_ALLERGENS_RE.fullmatch(label)
            for label in labels
        ):
            return None

        return labels

    # An empty HTML list does not mean allergen-free.
    section_text = clean_text(section) or ""

    if (
        section_text.lower() == "none"
        or NO_ALLERGENS_RE.search(section_text)
    ):
        return []

    return None


def iso_menu_date(raw):
    """
    Validate an ISO-formatted YYYY-MM-DD date.

    Returns None for missing or invalid dates.
    """
    if not raw:
        return None

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None

    try:
        return date.fromisoformat(raw).isoformat()

    except ValueError:
        return None


def extract_food_item(item, dining_hall):
    """
    Extract a single official BU Dining food item.

    Args:
        item:
            BeautifulSoup Tag representing li.menu-item.

        dining_hall:
            Human-readable dining hall name.

    Returns:
        Dictionary containing all available verified food
        information, or None if the element is invalid.

    Notes:
        Unknown values remain None.
        Nutrition is extracted only from this item's wrapper.
        Source URL is obtained from an official fixed mapping.
    """

    # Validate the dining hall.
    if dining_hall not in DINING_HALL_URLS:
        raise ValueError(
            f"Unknown dining hall: {dining_hall!r}"
        )

    # Validate that we received a menu-item element.
    if (
        not isinstance(item, Tag)
        or item.name != "li"
        or "menu-item" not in item.get("class", [])
    ):
        return None

    # Extract the name from this specific item.
    name = clean_text(
        own_match(item, ".menu-item-title")
    )

    if not name:
        return None

    # Initialize every required field.
    # Unknown values remain None.
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

    # -----------------------------------------
    # MEAL PERIOD
    # -----------------------------------------

    # Find the parent meal-period element.
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

    # -----------------------------------------
    # MENU DATE
    # -----------------------------------------

    # Find the enclosing date container.
    date_parent = item.find_parent(
        "ol",
        class_="js-menu-bydate"
    )

    if date_parent is not None:

        food["date"] = iso_menu_date(
            date_parent.get("data-menudate")
        )

    # -----------------------------------------
    # DIETARY TAGS
    # -----------------------------------------

    dietary_labels = [
        clean_text(tag)
        for tag in own_matches(
            item,
            ".menu-item-dietary-restriction li"
        )
    ]

    food["dietary_tags"] = list(
        dict.fromkeys(
            label for label in dietary_labels if label
        )
    )

    # -----------------------------------------
    # NUTRITION WRAPPER
    # -----------------------------------------

    # Locate nutrition ONLY inside this food item.
    nutrition = own_match(
        item,
        ".nutrition-wrapper"
    )

    # Missing nutrition does not invalidate the item.
    if nutrition is None:
        return food

    # -----------------------------------------
    # ALLERGENS
    # -----------------------------------------

    food["allergens"] = extract_allergens(nutrition)

    # -----------------------------------------
    # SERVING SIZE
    # -----------------------------------------

    serving = clean_text(
        nutrition.select_one(
            ".nutrition-serving-size"
        )
    )

    if serving:

        serving = re.sub(
            r"^Serving\s+size\s*:?[ \t]*",
            "",
            serving,
            flags=re.IGNORECASE
        ).strip()

        food["serving_size"] = serving or None

    # -----------------------------------------
    # INGREDIENTS
    # -----------------------------------------

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

    # -----------------------------------------
    # MAIN NUTRIENTS
    # -----------------------------------------

    nutrient_values = read_nutrient_table(
        nutrition,
        "table.nutrition-label",
        NUTRIENTS,
        "nutrition-label-amount"
    )

    food.update(nutrient_values)

    # -----------------------------------------
    # VITAMINS / MINERALS (% DAILY VALUE)
    # -----------------------------------------

    vitamin_values = read_nutrient_table(
        nutrition,
        "table.nutrition-vitamins",
        VITAMINS,
        "nutrition-label-percentage"
    )

    food.update(vitamin_values)

    return food


def extract_all_with_diagnostics(soup, dining_hall):
    """
    Extract all menu-item occurrences from one HTML document.

    Preserves duplicate names across dates and meal periods.

    Returns:
        records: List of valid food dictionaries.
        report: Extraction diagnostics and missing-data counts.
    """

    records = []
    invalid = 0
    failures = []
    missing_wrappers = 0

    items = soup.select("li.menu-item")

    for index, item in enumerate(items, start=1):

        try:
            if own_match(item, ".nutrition-wrapper") is None:
                missing_wrappers += 1

            record = extract_food_item(
                item,
                dining_hall
            )

        except Exception as exc:
            failures.append(
                (index, repr(exc))
            )
            continue

        if record is None:
            invalid += 1

        else:
            # Never deduplicate by food name.
            records.append(record)

    # Count missing fields across all valid records.
    missing = Counter()

    for record in records:

        missing.update(
            key
            for key, value in record.items()
            if value is None
        )

    # Count records by date and meal.
    date_meal_counts = Counter(
        (row["date"], row["meal"])
        for row in records
    )

    report = {
        "items_seen": len(items),
        "valid_records": len(records),
        "invalid_records": invalid,
        "extraction_failures": failures,
        "items_without_nutrition_wrapper": missing_wrappers,
        "missing_field_counts": dict(
            missing.most_common()
        ),
        "occurrences_by_date_meal": dict(
            date_meal_counts
        ),
    }

    return records, report


# =================================================
# MAIN EXECUTION
# =================================================

if __name__ == "__main__":

    # Load an already downloaded official BU HTML snapshot.
    # Keep the original raw HTML for provenance.
    
    # Get the project root regardless of terminal location.
    project_root = Path(__file__).resolve().parent.parent

    # Where we will save the downloaded HTML.
    html_path = project_root / "data" / "raw" / "west_2026-11-07.html"

    # Create directories if they don't exist.
    html_path.parent.mkdir(parents=True, exist_ok=True)

    # Only download if we don't already have the file.
    if not html_path.exists():

        print("Downloading West Campus menu...")

        url = DINING_HALL_URLS["West Campus"]

        response = requests.get(
            url,
            timeout=20,
            headers={
                "User-Agent": "TerrierTrays/1.0 (educational project)"
            }
        )

        response.raise_for_status()

        # Save the original HTML for future use.
        html_path.write_bytes(response.content)

        print("HTML downloaded and saved!")

    else:
        print("Using previously downloaded HTML.")

    # Parse the HTML.
    soup = BeautifulSoup(
        html_path.read_bytes(),
        "html.parser"
    )

    print("HTML successfully loaded!")


    # Extract every food item.
    food_items, diagnostic_report = (
        extract_all_with_diagnostics(
            soup,
            "West Campus"
        )
    )

    # Print diagnostic information.
    for key, value in diagnostic_report.items():
        print(key, ":", value)

    # -----------------------------------------
    # CLASSIC CHEESE PIZZA REGRESSION TEST
    # -----------------------------------------

    # Find the specific pizza occurrence.
    pizza = next(
        (
            food
            for food in food_items
            if food["name"] == "Classic Cheese Pizza"
            and food["date"] == "2026-11-07"
            and food["meal"] == "Lunch"
            and food["station"] == "Brick Oven"
        ),
        None
    )

    assert pizza is not None, (
        "Expected pizza occurrence not found in snapshot"
    )

    expected = {
        "name": "Classic Cheese Pizza",
        "dining_hall": "West Campus",
        "station": "Brick Oven",
        "meal": "Lunch",
        "date": "2026-11-07",

        "dietary_tags": ["Vegetarian", "Halal"],
        "allergens": ["Milk", "Soy", "Wheat"],

        "serving_size": "1/8 cut",
        "calories": 250,

        "total_fat_g": 8,
        "saturated_fat_g": 4,
        "trans_fat_g": 0,
        "cholesterol_mg": 18,
        "sodium_mg": 570,
        "carbohydrates_g": 34,
        "fiber_g": 2,
        "sugars_g": 3,
        "protein_g": 12,

        "vitamin_d_dv": 23,
        "potassium_dv": 5,
        "calcium_dv": 16,
        "iron_dv": 13,
    }

    for key, expected_value in expected.items():

        assert pizza[key] == expected_value, (
            key,
            pizza[key],
            expected_value
        )

    assert pizza["ingredients"], (
        "Ingredient text was not extracted"
    )

    assert pizza["source_url"] == (
        DINING_HALL_URLS["West Campus"]
    )

    print("Pizza regression test passed")


from collections import Counter

allergen_reasons = Counter()

for item in soup.select("li.menu-item"):

    food = extract_food_item(item, "West Campus")

    if food is None:
        continue

    if food["allergens"] is not None:
        continue

    nutrition = own_match(item, ".nutrition-wrapper")

    if nutrition is None:
        reason = "Missing nutrition wrapper"

    elif ALLERGEN_UNAVAILABLE_RE.search(
        clean_text(nutrition) or ""
    ):
        reason = "BU explicitly says allergens unavailable"

    elif nutrition.select_one(
        ".nutrition-facts-allergens"
    ) is None:
        reason = "No allergen section"

    else:
        reason = "Allergen section present but unrecognized"

    allergen_reasons[reason] += 1

print("\nALLERGEN DIAGNOSTICS:")

for reason, count in allergen_reasons.items():
    print(f"{reason}: {count}")


# ==========================================
# INSPECT UNRECOGNIZED ALLERGEN SECTIONS
# ==========================================

from collections import Counter

unrecognized = []

for item in soup.select("li.menu-item"):

    food = extract_food_item(item, "West Campus")

    if food is None or food["allergens"] is not None:
        continue

    nutrition = own_match(item, ".nutrition-wrapper")

    if nutrition is None:
        continue

    if ALLERGEN_UNAVAILABLE_RE.search(
        clean_text(nutrition) or ""
    ):
        continue

    section = nutrition.select_one(
        ".nutrition-facts-allergens"
    )

    if section is None:
        continue

    unrecognized.append({
        "name": food["name"],
        "date": food["date"],
        "meal": food["meal"],
        "text": clean_text(section) or "[EMPTY]",
        "html": str(section)
    })


print("\nTOTAL UNRECOGNIZED:", len(unrecognized))

# Group sections by their displayed text.
text_counts = Counter(
    record["text"]
    for record in unrecognized
)

print("\nMOST COMMON SECTION TEXT:")

for text, count in text_counts.most_common(10):
    print(f"{count} occurrences: {text[:200]}")


# Show examples of different HTML structures.
print("\nHTML EXAMPLES:")

seen = set()

for record in unrecognized:

    signature = record["text"]

    if signature in seen:
        continue

    seen.add(signature)

    print("\n-------------------------")
    print("FOOD:", record["name"])
    print("DATE:", record["date"])
    print("MEAL:", record["meal"])
    print("SECTION HTML:")
    print(record["html"][:1200])

    if len(seen) >= 5:
        break

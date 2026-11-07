import requests
from bs4 import BeautifulSoup
from pathlib import Path

# -----------------------------------
# 1. DOWNLOAD BU DINING HTML
# -----------------------------------

url = "https://www.bu.edu/dining/location/west/"

response = requests.get(
    url,
    headers={"User-Agent": "TerrierTrays/0.1 (student project)"},
    timeout=20
)
response.raise_for_status()

html = response.text

# Save the downloaded HTML
project_folder = Path(__file__).resolve().parent.parent
output_folder = project_folder / "data" / "raw"
output_folder.mkdir(parents=True, exist_ok=True)

file_path = output_folder / "west.html"
file_path.write_text(html, encoding="utf-8")

# Parse HTML
soup = BeautifulSoup(html, "html.parser")


# -----------------------------------
# 2. HELPER FUNCTIONS
# -----------------------------------

def get_text(parent, selector):
    """Extract text from an HTML element."""
    if parent is None:
        return None

    element = parent.select_one(selector)

    return element.get_text(" ", strip=True) if element else None


def to_number(value, unit=""):
    """Convert nutritional strings into numbers."""
    if value is None:
        return None

    value = value.strip()

    # Check and remove the expected unit
    if unit:
        if not value.endswith(unit):
            return None
        value = value.removesuffix(unit).strip()

    try:
        number = float(value)

        # Return integers when possible
        return int(number) if number.is_integer() else number

    except ValueError:
        return None


def get_nutrient(nutrition, name, unit=""):
    """Extract a nutrient from the main nutrition table."""
    if nutrition is None:
        return None

    for row in nutrition.select("table.nutrition-label tr"):
        nutrient = row.select_one(".nutrition-label-nutrient")
        amount = row.select_one(".nutrition-label-amount")

        if nutrient and amount:
            if nutrient.get_text(strip=True) == name:
                return to_number(
                    amount.get_text(strip=True),
                    unit
                )

    return None


def get_vitamin(nutrition, name):
    """Extract vitamin/mineral Daily Value percentages."""
    if nutrition is None:
        return None

    for row in nutrition.select("table.nutrition-vitamins tr"):
        nutrient = row.select_one(".nutrition-label-nutrient")
        percentage = row.select_one(".nutrition-label-percentage")

        if nutrient and percentage:
            if nutrient.get_text(strip=True) == name:
                return to_number(
                    percentage.get_text(strip=True),
                    "%"
                )

    return None


# -----------------------------------
# 3. FIND CLASSIC CHEESE PIZZA
# -----------------------------------

pizza = soup.find(
    "h4",
    class_="menu-item-title",
    string=lambda text: (
        text is not None
        and text.strip() == "Classic Cheese Pizza"
    )
)

facts_title = soup.find(
    "h3",
    class_="nutrition-title",
    string=lambda text: (
        text is not None
        and text.strip() == "Classic Cheese Pizza Nutrition Facts"
    )
)

if pizza is None or facts_title is None:
    raise ValueError("Classic Cheese Pizza not found in BU menu.")

container = pizza.find_parent(
    "div", class_="menu-item-wrapper"
)

nutrition = facts_title.find_parent(
    "div", class_="nutrition-wrapper"
)


# -----------------------------------
# 4. CLEAN SERVING SIZE
# -----------------------------------

serving_size = get_text(
    nutrition, ".nutrition-serving-size"
)

if serving_size:
    serving_size = serving_size.removeprefix(
        "Serving size "
    ).strip()


# -----------------------------------
# 5. CLEAN INGREDIENTS
# -----------------------------------

ingredients = get_text(
    nutrition, ".nutrition-facts-ingredients"
)

if ingredients:
    ingredients = ingredients.removeprefix(
        "Ingredients:"
    ).strip()


# -----------------------------------
# 6. EXTRACT ALL PIZZA DATA
# -----------------------------------

# Missing allergen information must remain unknown
allergen_list = nutrition.select_one(
    ".nutrition-facts-allergens"
)

allergens = (
    [
        li.get_text(strip=True)
        for li in allergen_list.select("li")
    ]
    if allergen_list is not None else None
)

# -----------------------------------
# EXTRACT MEAL PERIOD AND DATE
# -----------------------------------

# Find the meal period containing the pizza
meal_container = container.find_parent(
    "li", class_="menu-meal-period"
)

meal = None

if meal_container:
    for class_name in meal_container.get("class", []):
        if class_name.startswith("js-meal-period-"):
            meal = class_name.removeprefix(
                "js-meal-period-"
            ).capitalize()
            break

# Find the date containing the pizza
date_container = container.find_parent(
    "ol", class_="js-menu-bydate"
)

menu_date = None

if date_container:
    menu_date = date_container.get("data-menudate")

pizza_data = {
    "name": get_text(container, ".menu-item-title"),

    # Location information
    "dining_hall": "West Campus",
    "station": get_text(container, ".js-sortby-station"),

    "meal": meal,
    "date": menu_date,

    # Dietary information
    "dietary_tags": [
        li.get_text(strip=True)
        for li in container.select(
            ".menu-item-dietary-restriction li"
        )
    ],
    "allergens": allergens,

    # Serving size
    "serving_size": serving_size,

    # Macronutrients
    "calories": get_nutrient(nutrition, "Calories"),
    "total_fat_g": get_nutrient(nutrition, "Total Fat", "g"),
    "saturated_fat_g": get_nutrient(
        nutrition, "Saturated Fat", "g"
    ),
    "trans_fat_g": get_nutrient(
        nutrition, "Trans Fat", "g"
    ),
    "cholesterol_mg": get_nutrient(
        nutrition, "Cholesterol", "mg"
    ),
    "sodium_mg": get_nutrient(
        nutrition, "Sodium", "mg"
    ),
    "carbohydrates_g": get_nutrient(
        nutrition, "Total Carbohydrate", "g"
    ),
    "fiber_g": get_nutrient(
        nutrition, "Dietary Fiber", "g"
    ),
    "sugars_g": get_nutrient(
        nutrition, "Sugars", "g"
    ),
    "protein_g": get_nutrient(
        nutrition, "Protein", "g"
    ),

    # Vitamins and minerals (% Daily Value)
    "vitamin_d_dv": get_vitamin(nutrition, "Vitamin D"),
    "potassium_dv": get_vitamin(nutrition, "Potassium"),
    "calcium_dv": get_vitamin(nutrition, "Calcium"),
    "iron_dv": get_vitamin(nutrition, "Iron"),

    # Additional information
    "ingredients": ingredients,
    "source_url": url,
}


# -----------------------------------
# 7. PRINT RESULTS
# -----------------------------------

for key, value in pizza_data.items():
    print(f"{key}: {value}")

food_item = container.find_parent("li", class_="menu-item")

nutrition_inside = food_item.select_one(".nutrition-wrapper")

print("Nutrition inside food item:", nutrition_inside is not None)
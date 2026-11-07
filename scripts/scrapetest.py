
import json
from pathlib import Path

path = Path("data/current/menus.json")

assert path.exists(), "Current dataset does not exist"

with path.open("r", encoding="utf-8") as f:
    data = json.load(f)

assert data["validation_status"] == "passed"
assert len(data["dining_halls"]) == 3
assert len(data["menu_dates"]) == 7
assert data["total_records"] == len(data["records"])

print("Dataset validation passed!")
print("Total food records:", data["total_records"])
print("Menu dates:", data["menu_dates"])

for hall in data["dining_halls"]:
    count = sum(
        food["dining_hall"] == hall
        for food in data["records"]
    )

    print(hall, ":", count)

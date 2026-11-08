# BU Menu Scraper

**A Python pipeline for collecting, validating, and maintaining Boston University dining hall menus.**

BU Menu Scraper powers the menu-data side of **Terrier Trays**, a student project aimed at making campus nutrition information more accessible for meal planning. It extracts information from real BU Dining pages and publishes a structured JSON dataset **only when validation succeeds**.

## Features

- Collects menus for **West Campus, Warren Towers, and Marciano Commons**.
- Extracts food names, meal dates, stations, serving sizes, calories, macronutrients, other nutrition facts, ingredients, dietary tags, and available allergen information.
- Preserves individual food occurrences by dining hall, meal, and date instead of deduplicating by name.
- Validates each dining hall and ensures matching menu-date coverage across all three halls.
- Stages and archives results before atomically replacing `data/current/menus.json`; a failed scrape leaves the current dataset untouched.
- Archives raw HTML with source URLs and SHA-256 hashes for traceability and debugging.
- Provides a separate freshness/date-coverage validator to block recommendations based on stale or missing menus.
- Supports safe archive cleanup with a default **30-day retention period** and **dry-run mode**.

## Data Sources

| Dining hall | Official page |
| --- | --- |
| West Campus | [BU Dining — West](https://www.bu.edu/dining/location/west/) |
| Warren Towers | [BU Dining — Warren](https://www.bu.edu/dining/location/warren/) |
| Marciano Commons | [BU Dining — Marciano](https://www.bu.edu/dining/location/marciano/) |

The scraper uses `requests` and `BeautifulSoup`, checks `robots.txt`, applies request delays, and validates returned URLs. Review BU's website usage restrictions before running automated requests.

## Getting Started

### 1. Clone and install

Python 3.10+ is recommended.

```bash
git clone https://github.com/aladincrnolic/bumenuscraper.git
cd bumenuscraper
python -m pip install requests beautifulsoup4
```

### 2. Run the scraper

```bash
python scripts/scrape_bu_menus.py
```

The default run reuses a saved snapshot from today when available, otherwise it fetches official HTML. To force fresh requests:

```bash
python scripts/scrape_bu_menus.py --refresh
```

To process an already saved HTML snapshot without network requests (replace the date with one you actually have):

```bash
python scripts/scrape_bu_menus.py --offline --snapshot-date 2026-11-07
```

When validation passes, the active dataset is written to **`data/current/menus.json`**. The actual menu dates are extracted from BU's HTML, not inferred from the snapshot label.

## How It Works

```text
Official BU Dining pages
         |
         v
Download / load saved HTML
         |
         v
Extract individual food records
         |
         v
Validate halls and menu dates
         |
   +-----+-----+
   |           |
 Failure     Success
   |           |
   v           v
Save failed   Archive validated
snapshot      data + raw HTML
               |
               v
         Atomically publish
         current/menus.json
               |
               v
         Freshness and coverage
         checks for meal planning
```

**Publication and freshness are different checks:** a previously valid snapshot can become too old to use for recommendations.

## Repository Structure

```text
bumenuscraper/
├── .github/
│   └── workflows/
│       └── update_menus.yml
├── data/
│   ├── archive/           # Successful and failed snapshots
│   ├── current/           # Published menus.json
│   ├── processed/         # Additional processed-data directory
│   ├── raw/               # Legacy raw HTML snapshots
│   └── staging/           # Temporary run directory (created as needed)
└── scripts/
    ├── scrape_bu_menus.py
    ├── validate_freshness.py
    ├── cleanup_archives.py
    ├── scrapetest.py
    ├── test_freshness.py
    └── test_cleanup_archives.py
```

### Python Scripts

| File | Purpose |
| --- | --- |
| `scrape_bu_menus.py` | Downloads/parses menu HTML, validates records, archives runs, and publishes the active JSON. |
| `validate_freshness.py` | Checks dataset integrity, age, requested dates, and coverage of all three halls. |
| `cleanup_archives.py` | Previews or removes expired archived files without modifying the live dataset. |
| `scrapetest.py` | Runs basic assertions against the current published menu data. |
| `test_freshness.py` | Offline unit tests for freshness and date coverage. |
| `test_cleanup_archives.py` | Offline unit tests for archive cleanup. |

## Output Format

The published `menus.json` contains metadata (`schema_version`, `generated_at_utc`, `snapshot_date`, `menu_dates`, `dining_halls`, `total_records`, `sources`, and `validation_status`) as well as a `records` array of food occurrences.

A **shortened illustrative record** looks like this (not an actual scrape):

```json
{
  "name": "Example Food",
  "dining_hall": "West Campus",
  "station": "Example Station",
  "meal": "Lunch",
  "date": "2026-11-07",
  "dietary_tags": ["Vegetarian"],
  "allergens": null,
  "serving_size": "1 serving",
  "calories": 250,
  "total_fat_g": 8,
  "carbohydrates_g": 34,
  "protein_g": 12,
  "ingredients": null,
  "source_url": "https://www.bu.edu/dining/location/west/"
}
```

Additional nutrient fields and source metadata are included in real output. Missing fields are not guessed.

### Allergen Safety

- `"allergens": null` means the source information is missing, unknown, or ambiguous — **not allergen-free**.
- `"allergens": []` means the source explicitly listed no allergens — **not a guarantee of safety**.
- Even listed allergen information cannot rule out ingredient changes or cross-contact.

**Always confirm food allergies with BU Dining staff.** This dataset is not a medical or food-safety guarantee.

## Check Freshness Before Recommendations

Supply the exact menu dates your application needs:

```bash
python scripts/validate_freshness.py --dates 2026-11-07 2026-11-08
```

Replace those example dates with dates present in the current menu. By default, the freshness threshold is **24 hours** and the expiry threshold is **48 hours**. Recommendations are permitted only when dataset integrity, freshness, and requested-date/hall coverage all pass.

Optional thresholds:

```bash
python scripts/validate_freshness.py --dates 2026-11-07 --max-age-hours 24 --expiry-hours 48
```

This validator is read-only; it does not scrape, repair, or replace the data.

## Archive Cleanup

Preview archived files eligible for cleanup (default 30 days):

```bash
python scripts/cleanup_archives.py
```

Actually delete eligible files:

```bash
python scripts/cleanup_archives.py --retention-days 30 --delete
```

The cleanup tool restricts which paths and file types can be deleted and avoids symlink traversal. It does not target `data/current/menus.json`.

## Testing

Run the offline unit tests from the repository root:

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
```

After successfully generating a seven-day dataset, run the additional published-dataset assertions:

```bash
python scripts/scrapetest.py
```

The second command requires `data/current/menus.json` to exist and expects three halls and seven menu dates.

## Automated Daily Updates

The [GitHub Actions workflow](.github/workflows/update_menus.yml) is configured to run **daily at 8:17 AM America/New_York** and can also be launched manually from the Actions tab.

It installs dependencies, runs the Python unit tests, downloads fresh menu data, checks that the newly published dataset covers seven days and passes freshness validation, removes eligible archives older than 30 days, and commits the validated dataset and retained archives to the default branch.

Execution depends on GitHub Actions, repository permissions, and the official BU Dining pages. Check the repository's **Actions** tab to verify successful runs. The scripts can also be run manually as described above.

## About Terrier Trays

This scraper is the **data pipeline**, not the meal-planning algorithm or website. The wider Terrier Trays project aims to help BU students explore real dining options that match nutrition goals, dietary preferences, and restrictions.

## Disclaimer

Independent student project; **not affiliated with or endorsed by Boston University Dining**. Menus, nutrition values, ingredients, and allergen information may change. Refer to [BU Dining](https://www.bu.edu/dining/) for authoritative information.

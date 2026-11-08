# BU Menu Scraper

BU Menu Scraper collects menus from Boston University Dining and publishes a structured JSON dataset for **Terrier Trays**, a student meal-planning project. The scraper preserves each food occurrence by hall, date, meal, and station, including nutrition, ingredients, dietary tags, and allergen information when available.

**Use [`data/current/menus.json`](data/current/menus.json) as the published dataset.** Check freshness and coverage for the dates your app needs before using it to recommend meals.

## Data sources

| Dining hall | Official menu | Cache filename prefix |
| --- | --- | --- |
| West Campus | [BU Dining — West](https://www.bu.edu/dining/location/west/) | `west` |
| Warren Towers | [BU Dining — Warren](https://www.bu.edu/dining/location/warren/) | `warren` |
| Marciano Commons | [BU Dining — Marciano](https://www.bu.edu/dining/location/marciano/) | `marciano` |

For live requests, the scraper checks BU's `robots.txt`, applies request delays, and verifies the final HTTPS host and path for each menu page. Review BU's website usage restrictions before running automated requests.

## Getting started

Use Python 3.12 to match GitHub Actions. Run the commands below from the repository root after cloning.

```bash
git clone https://github.com/aladincrnolic/bumenuscraper.git
cd bumenuscraper
python -m pip install requests beautifulsoup4
```

| Goal | Command |
| --- | --- |
| Reuse cached HTML when available and fetch missing halls from BU | `python scripts/scrape_bu_menus.py` |
| Fetch new HTML for all three halls | `python scripts/scrape_bu_menus.py --refresh` |
| Reprocess a saved snapshot without network requests | `python scripts/scrape_bu_menus.py --offline --snapshot-date 2026-11-07` |

`--snapshot-date` defaults to your machine's local date. It labels the run and selects cached HTML. The scraper reads the actual menu dates from BU's HTML. For each hall, it checks `data/archive/<date>_*/<hall>.html` first, then `data/raw/<hall>_<date>.html`. Offline mode requires a cache hit for every hall. Use `--offline` and `--refresh` separately.

Every successful scrape, including an offline run, replaces the local `data/current/menus.json`. Use a separate checkout if you want to replay an older snapshot while keeping your current dataset.

## Validation and publication

1. The scraper downloads or loads each hall's HTML and stages the raw HTML, parsed JSON, and validation report in `data/staging/<run-id>/`.
2. Each hall must have **exactly seven consecutive menu dates**, and all three halls must cover the same dates. The scraper also checks hall identity, food records, date and meal context, and the presence of at least one record with complete core nutrition per hall.
3. On scrape or validation failure, the scraper saves the available diagnostics in `data/archive/failed_<run-id>/` and leaves the current dataset in place.
4. On success, the scraper saves the combined dataset and, when present, a copy of the previous current file as `previous_current_menus.json`. It moves the staged files into `data/archive/<run-id>/` and atomically replaces the local `data/current/menus.json`.

The scraper validates the source's seven-day window. The freshness validator separately checks the dates an application requests. The workflow runs that check after the local file replacement and before pushing the update to GitHub.

## Dataset format

The JSON includes `schema_version` (currently `"1.0"`), `run_id`, `generated_at_utc`, `snapshot_date`, `menu_dates`, `dining_halls`, `total_records`, `allergy_notice`, `validation_status`, `sources`, and `records`. Each hall's entry in `sources` includes the official URL, the archived HTML path, and a SHA-256 hash of the HTML.

This shortened example uses illustrative values.

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

### Allergen information

- `"allergens": null` means the source information is missing, unavailable, or ambiguous. Treat these records as having unknown allergen status.
- `"allergens": []` means the source explicitly indicated that no allergens were listed.
- A nonempty array contains the allergen labels extracted from the source.

Ingredients and preparation can change, and the published labels cannot rule out cross-contact. Confirm allergy information directly with BU Dining staff.

## Check freshness before recommending meals

Pass the exact dates the app needs, even if some may be missing. Replace the example dates below with the dates for your plan.

```bash
python scripts/validate_freshness.py --dates 2026-11-07 2026-11-08
```

The read-only validator defaults to `data/current/menus.json`. Use `--path` to check a different file. It checks the dataset's structure, publication status, age, and whether every requested date has food records for all three halls. Coverage can pass when a hall has records for only some meal periods.

| Dataset age | Default freshness status |
| --- | --- |
| Up to and including 24 hours | Fresh |
| Over 24 hours through 48 hours | Stale |
| Over 48 hours | Expired |

Allow automatic recommendations only when the command exits with status `0`. Use `--max-age-hours` and `--expiry-hours` to change the thresholds. The expiry threshold must be greater than the freshness threshold.

The age check uses `generated_at_utc`, which records when the JSON was generated. Reprocessing cached HTML gives the JSON a new timestamp even when the source HTML is older. Use `--refresh` when you need a new fetch from BU.

## Automated updates

The [Update BU Dining Menus workflow](.github/workflows/update_menus.yml) is scheduled daily at **8:17 a.m. Boston time** (`America/New_York`). To start it manually, go to **Actions → Update BU Dining Menus → Run workflow** and select the default branch (`main`). GitHub Actions must be enabled, and repository rules must allow the workflow token to push to the default branch. The workflow requests `contents: write`.

The workflow sets up Python 3.12, installs dependencies, runs the offline unit tests, and forces a fresh scrape labeled with the current Boston date. It verifies that the dataset was generated during the run and includes today's Boston menu date. It then checks every date from today through the last available date, capped at seven days.

**A shorter forward window can still publish.** The source snapshot may contain seven consecutive dates with some already in the past. The workflow warns when fewer than seven dates remain starting today. A missing menu for today or a failed freshness or coverage check prevents the push.

After verification, the workflow runs 30-day archive cleanup and pushes changes to the current JSON and tracked archives. Failed scrape diagnostics stay in the runner's checkout because a failed scrape skips the commit step.

## Archive cleanup

Preview eligible files first. The default retention period is 30 days.

```bash
python scripts/cleanup_archives.py
python scripts/cleanup_archives.py --retention-days 30 --delete
```

Cleanup targets regular `.html`, `.htm`, and `.json` files under `data/archive/` by default and skips symlinks. Files must be older than the cutoff by modification time. For recognized run directories, the run timestamp must also be older than the cutoff, and a recent or unsafe entry can keep the whole run from being cleaned up. Cleanup leaves `data/current/menus.json` outside its scope.

GitHub Actions restores tracked archive file modification times from the most recent commit for each file before cleanup. A fresh local clone can show fewer eligible files because checkout resets their modification times. Cleanup removes files from the working tree, and the workflow records those removals in a new commit.

## Testing

Run the same offline unit tests used by GitHub Actions. They cover freshness, requested-date coverage, and archive cleanup.

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
```

Run the separate smoke test from the repository root against an existing `data/current/menus.json`. It checks the validation flag, hall count, menu date count, and record count.

```bash
python scripts/scrapetest.py
```

## Repository layout

| Path | Role |
| --- | --- |
| `scripts/scrape_bu_menus.py` | Fetch, parse, validate, archive, and publish menu data |
| `scripts/validate_freshness.py` | Check dataset age, integrity, and requested-date coverage |
| `scripts/cleanup_archives.py` | Preview and remove eligible archive files |
| `scripts/test_freshness.py`, `scripts/test_cleanup_archives.py` | Offline unit tests |
| `scripts/scrapetest.py` | Smoke test for the published dataset |
| `.github/workflows/update_menus.yml` | Scheduled and manual updates |
| `data/current/menus.json` | Published menu dataset |
| `data/archive/` | Historical snapshots and local failed-run diagnostics |
| `data/staging/` | Per-run working directories created as needed |
| `data/raw/`, `data/processed/` | Legacy HTML and JSON outputs. The scraper can reuse HTML from `data/raw/`. |

## About Terrier Trays

This repository provides menu data for Terrier Trays. The website and meal-planning logic are separate. Terrier Trays is an independent student project. For current menus, ingredients, and dietary information, consult [BU Dining](https://www.bu.edu/dining/).

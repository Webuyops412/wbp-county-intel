"""
run_all_scrapers.py
We Buy Property LLC — Head of Data
Purpose: Master runner — executes all county scrapers in priority sequence,
         combines output into unified CSV per county, logs run stats.
Run:     python run_all_scrapers.py
         python run_all_scrapers.py --county allegheny   (single county)
         python run_all_scrapers.py --type code_violation  (single data type)
         python run_all_scrapers.py --dry-run              (test without writing)
"""

import importlib.util
import subprocess
import sys
import os
import time
import pandas as pd
from datetime import datetime, date

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SCRIPTS_DIR = os.path.dirname(__file__)
OUTPUT_DIR  = os.path.join(SCRIPTS_DIR, "..", "output")
LOG_DIR     = os.path.join(SCRIPTS_DIR, "..", "logs")

# Required output schema — all scrapers must conform
REQUIRED_COLUMNS = [
    "owner_name", "property_address", "city", "zip", "parcel_id",
    "data_type", "source", "county", "date_pulled", "raw_detail"
]

# Scraper registry — priority order
# status: active | manual_url_needed | selenium_required | deferred
# ─── ARV THRESHOLDS BY COUNTY ─────────────────────────────────────────────────
# Allegheny: ARV > $100k (CLR 87.7% → MIN_FMV = 87,700)
# All other counties: ARV > $150k (CLR ≈ 1.0 → MIN_FMV = 150,000)

SCRAPERS = [
    # ── ALLEGHENY COUNTY (ARV > $100k) ──
    {
        "name":     "Allegheny Code Violations (All Types)",
        "script":   "allegheny_code_violations.py",
        "county":   "Allegheny",
        "type":     "code_violation",
        "status":   "active",
        "priority": 1,
        "arv_min":  100_000,
        "notes":    "WPRDC — all violation types, no owner filter, ARV > $100k",
    },
    {
        "name":     "Allegheny Tax Delinquent",
        "script":   "allegheny_tax_delinquent.py",
        "county":   "Allegheny",
        "type":     "tax_delinquent",
        "status":   "active",
        "priority": 2,
        "arv_min":  100_000,
        "notes":    "WPRDC — daily updates, ARV > $100k, all owner types",
    },
    {
        "name":     "Allegheny Water Shutoffs",
        "script":   "allegheny_water_shutoffs.py",
        "county":   "Allegheny",
        "type":     "water_shutoff",
        "status":   "active",
        "priority": 3,
        "arv_min":  100_000,
        "notes":    "WPRDC PWSA — severe financial distress signal, ARV > $100k",
    },
    {
        "name":     "Allegheny Property Assessments",
        "script":   "allegheny_assessments.py",
        "county":   "Allegheny",
        "type":     "property_assessment",
        "status":   "active",
        "priority": 4,
        "arv_min":  100_000,
        "notes":    "WPRDC — enrichment layer for owner info + ARV baseline",
    },
    # ── WASHINGTON COUNTY (ARV > $150k) ──
    {
        "name":     "Washington Tax Delinquent",
        "script":   "washington_tax_delinquent.py",
        "county":   "Washington",
        "type":     "tax_delinquent",
        "status":   "active",
        "priority": 5,
        "arv_min":  150_000,
        "notes":    "County Treasurer PDF — ARV > $150k",
    },
    # ── WESTMORELAND COUNTY (ARV > $150k) ──
    {
        "name":     "Westmoreland Tax Delinquent",
        "script":   "westmoreland_tax_delinquent.py",
        "county":   "Westmoreland",
        "type":     "tax_delinquent",
        "status":   "manual_url_needed",
        "priority": 6,
        "arv_min":  150_000,
        "notes":    "⚠ Check westmoreland county site for current year PDF URL",
    },
    # ── BEAVER, BUTLER, ARMSTRONG (ARV > $150k) — SCRAPERS PENDING ──
    {
        "name":     "Beaver County Tax Delinquent",
        "script":   "beaver_tax_delinquent.py",
        "county":   "Beaver",
        "type":     "tax_delinquent",
        "status":   "deferred",
        "priority": 7,
        "arv_min":  150_000,
        "notes":    "Scraper not yet built — source: beavercountypa.gov/tax-claim",
    },
    {
        "name":     "Butler County Tax Delinquent",
        "script":   "butler_tax_delinquent.py",
        "county":   "Butler",
        "type":     "tax_delinquent",
        "status":   "deferred",
        "priority": 8,
        "arv_min":  150_000,
        "notes":    "Scraper not yet built — source: butlercountypa.gov",
    },
    {
        "name":     "Armstrong County Tax Delinquent",
        "script":   "armstrong_tax_delinquent.py",
        "county":   "Armstrong",
        "type":     "tax_delinquent",
        "status":   "deferred",
        "priority": 9,
        "arv_min":  150_000,
        "notes":    "Scraper not yet built — source: armstrongcounty.com",
    },
    # DEFERRED — Selenium-based (build Phase 2B)
    # {
    #     "name":   "Allegheny Evictions (UJS)",
    #     "script": "ujs_evictions.py",
    #     "status": "selenium_required",
    #     "priority": 6,
    # },
    # {
    #     "name":   "Allegheny Probates",
    #     "script": "allegheny_probates.py",
    #     "status": "selenium_required",
    #     "priority": 7,
    # },
]


def run_scraper(scraper: dict, dry_run: bool = False) -> dict:
    """Run a single scraper script as a subprocess. Returns result dict."""
    script_path = os.path.join(SCRIPTS_DIR, scraper["script"])
    result = {
        "name":     scraper["name"],
        "county":   scraper["county"],
        "type":     scraper["type"],
        "status":   "skipped",
        "records":  0,
        "file":     None,
        "error":    None,
        "elapsed":  0,
    }

    if scraper["status"] in ("selenium_required", "deferred"):
        result["status"] = "deferred"
        print(f"  ⏭  DEFERRED: {scraper['name']} ({scraper['status']})")
        return result

    if scraper["status"] == "manual_url_needed":
        result["status"] = "manual_action"
        print(f"  ⚠  MANUAL URL NEEDED: {scraper['name']}")
        print(f"     {scraper.get('notes', '')}")
        return result

    if not os.path.exists(script_path):
        result["status"] = "error"
        result["error"] = f"Script not found: {script_path}"
        print(f"  ✗ NOT FOUND: {script_path}")
        return result

    if dry_run:
        result["status"] = "dry_run"
        print(f"  🔍 DRY RUN: {scraper['name']} — {script_path}")
        return result

    print(f"\n{'─'*60}")
    print(f"  Running: {scraper['name']}")
    print(f"{'─'*60}")

    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=300
        )
        elapsed = time.time() - start
        result["elapsed"] = round(elapsed, 1)

        print(proc.stdout)
        if proc.stderr:
            print(f"  STDERR: {proc.stderr[:500]}")

        if proc.returncode != 0:
            result["status"] = "error"
            result["error"] = proc.stderr[:200]
            return result

        # Find output file
        today = date.today().strftime("%Y%m%d")
        county_lower = scraper["county"].lower()
        type_lower = scraper["type"].lower()
        candidate_file = os.path.join(OUTPUT_DIR, f"{county_lower}_{type_lower}_{today}.csv")
        if os.path.exists(candidate_file):
            df = pd.read_csv(candidate_file)
            result["records"] = len(df)
            result["file"] = candidate_file
            result["status"] = "success"
        else:
            result["status"] = "success_no_file"

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = "Script exceeded 5 min timeout"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def merge_outputs(results: list) -> dict:
    """Combine all successful CSVs into per-county master files."""
    merged = {}
    for r in results:
        if r["status"] == "success" and r["file"] and os.path.exists(r["file"]):
            county = r["county"]
            df = pd.read_csv(r["file"])
            # Ensure required columns
            for col in REQUIRED_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            df = df[REQUIRED_COLUMNS + [c for c in df.columns if c not in REQUIRED_COLUMNS]]

            if county not in merged:
                merged[county] = []
            merged[county].append(df)
            print(f"  → Merged {len(df)} {r['type']} records for {county}")

    master_files = {}
    for county, dfs in merged.items():
        combined = pd.concat(dfs, ignore_index=True)
        combined = combined.drop_duplicates(subset=["property_address", "data_type"])
        master_file = os.path.join(OUTPUT_DIR, f"{county.lower()}_all_data_{date.today().strftime('%Y%m%d')}.csv")
        combined.to_csv(master_file, index=False)
        master_files[county] = {"file": master_file, "records": len(combined)}
        print(f"  ✓ Master file: {master_file} ({len(combined)} records)")

    return master_files


def write_run_log(results: list, master_files: dict, elapsed_total: float):
    """Write a log file for this run."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"run_log_{date.today().strftime('%Y%m%d_%H%M%S')}.md")

    lines = [
        f"# County Scraper Run Log",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Total elapsed:** {elapsed_total:.1f}s",
        "",
        "## Results",
        "",
        "| Scraper | Status | Records | Elapsed |",
        "|---------|--------|---------|---------|",
    ]
    for r in results:
        lines.append(f"| {r['name']} | {r['status']} | {r['records']:,} | {r['elapsed']}s |")

    lines += ["", "## Master Output Files", ""]
    for county, info in master_files.items():
        lines.append(f"- **{county}**: {info['records']:,} records → `{os.path.basename(info['file'])}`")

    lines += ["", "## Actions Required", ""]
    for r in results:
        if r["status"] == "manual_action":
            lines.append(f"- ⚠ **{r['name']}**: Manual URL needed — see script notes")
        elif r["status"] == "error":
            lines.append(f"- ✗ **{r['name']}**: Error — {r['error']}")

    lines += ["", "## Next Steps", "",
              "1. QA spot-check output CSVs — confirm record counts look right",
              "2. Upload to REI Sift: dedup → organize → skip trace",
              "3. Export skip-traced list to PropStream — run ARV filter ($100k Allegheny / $150k other counties)",
              "4. Load PropStream-filtered list into SMS campaign (Smarter Contact or GHL SMS)",
              "5. SMS replies → GHL contact created automatically (GHL = CRM only, not a data hub)",
              "6. Flag any issues to COO via DATA_outbox.md",
              ]

    with open(log_file, "w") as f:
        f.write("\n".join(lines))
    print(f"\n📋 Run log: {log_file}")
    return log_file


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    county_filter = None
    type_filter = None

    if "--county" in sys.argv:
        idx = sys.argv.index("--county")
        county_filter = sys.argv[idx+1].lower() if idx+1 < len(sys.argv) else None
    if "--type" in sys.argv:
        idx = sys.argv.index("--type")
        type_filter = sys.argv[idx+1].lower() if idx+1 < len(sys.argv) else None

    print(f"\n{'='*60}")
    print(f"WBP County Scraper — Master Runner")
    print(f"{'='*60}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if dry_run: print("MODE: DRY RUN")
    print()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    # Filter scrapers if requested
    scrapers_to_run = [
        s for s in sorted(SCRAPERS, key=lambda x: x["priority"])
        if (county_filter is None or s["county"].lower() == county_filter)
        and (type_filter is None or s["type"].lower() == type_filter)
    ]

    print(f"Running {len(scrapers_to_run)} scrapers:")
    for s in scrapers_to_run:
        status_icon = "✅" if s["status"] == "active" else "⚠" if s["status"] == "manual_url_needed" else "⏭"
        print(f"  {status_icon} [{s['priority']}] {s['name']} ({s['county']}) — {s['status']}")

    print()
    total_start = time.time()
    results = []

    for scraper in scrapers_to_run:
        result = run_scraper(scraper, dry_run=dry_run)
        results.append(result)

    # Merge outputs
    print(f"\n{'='*60}")
    print("Merging outputs...")
    master_files = merge_outputs(results) if not dry_run else {}

    total_elapsed = time.time() - total_start

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    success = [r for r in results if r["status"] == "success"]
    errors  = [r for r in results if r["status"] == "error"]
    manual  = [r for r in results if r["status"] == "manual_action"]
    total_records = sum(r["records"] for r in success)

    print(f"✅ Success: {len(success)}/{len(results)} scrapers")
    print(f"📊 Total records: {total_records:,}")
    print(f"⏱  Total time: {total_elapsed:.1f}s")
    if errors:
        print(f"✗  Errors: {len(errors)}")
    if manual:
        print(f"⚠  Manual actions needed: {len(manual)}")

    # Write log
    log_file = write_run_log(results, master_files, total_elapsed)

    print(f"\n📋 POST
"""
run_all_scrapers.py
We Buy Property LLC — Head of Data
Purpose: Orchestrator for GitHub Actions — runs active county scrapers,
         passes args to each, merges output for dashboard.

Called by .github/workflows/scrape.yml:
    python scraper/run_all_scrapers.py \
        --lookback-days 7 \
        --county all \
        --output-dir data/ \
        --dashboard-dir dashboard/ \
        --json
"""

import argparse
import importlib.util
import os
import sys
import subprocess
import time
from datetime import datetime

# ── Scraper registry ──────────────────────────────────────────────────────────
# Add new scrapers here. county must match --county filter values.
SCRAPERS = [
    {
        "name":     "Allegheny Tax Delinquent",
        "module":   "allegheny_tax_delinquent",
        "county":   "allegheny",
        "status":   "active",
        "priority": 1,
    },
    {
        "name":     "Allegheny Sheriff Sales",
        "module":   "allegheny_sheriff_sales",
        "county":   "allegheny",
        "status":   "active",
        "priority": 2,
    },
    {
        "name":     "Allegheny Foreclosure Filings",
        "module":   "allegheny_foreclosures",
        "county":   "allegheny",
        "status":   "active",
        "priority": 3,
    },
    {
        "name":     "Allegheny Tax Liens",
        "module":   "allegheny_tax_liens",
        "county":   "allegheny",
        "status":   "active",
        "priority": 4,
    },
    {
        "name":     "Allegheny Fire News",
        "module":   "allegheny_fire_news",
        "county":   "allegheny",
        "status":   "active",
        "priority": 5,
    },
    {
        "name":     "Allegheny Estate Sales",
        "module":   "allegheny_estate_sales",
        "county":   "allegheny",
        "status":   "active",
        "priority": 6,
    },
    {
        "name":     "Allegheny Pre-Probate",
        "module":   "allegheny_probate",
        "county":   "allegheny",
        "status":   "active",
        "priority": 7,
    },
    {
        "name":     "Allegheny Jail Roster",
        "module":   "allegheny_jail_roster",
        "county":   "allegheny",
        "status":   "active",
        "priority": 8,
    },
    # Future scrapers — plug in when ready:
    # {"name": "Washington Tax Delinquent",    "module": "washington_tax_delinquent",    "county": "washington",    "status": "deferred", "priority": 9},
    # {"name": "Westmoreland Tax Delinquent",  "module": "westmoreland_tax_delinquent",  "county": "westmoreland",  "status": "deferred", "priority": 10},
]


def run_scraper_module(scraper, args):
    """Import and run a scraper module directly (avoids subprocess overhead)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    module_path = os.path.join(script_dir, scraper["module"] + ".py")

    if not os.path.exists(module_path):
        print(f"  [SKIP] {scraper['name']}: module not found at {module_path}")
        return {"status": "skipped", "records": 0, "name": scraper["name"]}

    try:
        spec = importlib.util.spec_from_file_location(scraper["module"], module_path)
        mod = importlib.util.module_from_spec(spec)

        # Inject the args the module expects via sys.argv
        # Each module has its own argparse — we pass our parsed values through
        saved_argv = sys.argv[:]
        sys.argv = [module_path]
        sys.argv += ["--output-dir", args.output_dir]
        sys.argv += ["--dashboard-dir", args.dashboard_dir]
        if args.json:
            sys.argv.append("--json")
        # Note: --lookback-days is accepted here but not yet used by individual scrapers
        # (WPRDC data is always current snapshot — lookback used for future recorder scraper)

        start = time.time()
        spec.loader.exec_module(mod)
        elapsed = time.time() - start
        sys.argv = saved_argv

        print(f"  [OK] {scraper['name']} completed in {elapsed:.1f}s")
        return {"status": "success", "records": getattr(mod, "_records_written", 0), "name": scraper["name"], "elapsed": elapsed}

    except SystemExit:
        # argparse calls sys.exit(0) on --help; module ran successfully
        sys.argv = saved_argv
        return {"status": "success", "records": 0, "name": scraper["name"]}
    except Exception as e:
        sys.argv = saved_argv
        print(f"  [ERROR] {scraper['name']}: {e}")
        return {"status": "error", "records": 0, "name": scraper["name"], "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="WBP County Lead Scraper Orchestrator")
    parser.add_argument("--lookback-days",  type=int,  default=7,
                        help="Days of history for recorder-style scrapers (default: 7)")
    parser.add_argument("--county",         default="all",
                        help="County filter: allegheny / washington / westmoreland / all (default: all)")
    parser.add_argument("--output-dir",     default="data",
                        help="Directory for CSV + JSON output (default: data/)")
    parser.add_argument("--dashboard-dir",  default="dashboard",
                        help="Directory for dashboard records.json (default: dashboard/)")
    parser.add_argument("--json",           action="store_true",
                        help="Write records.json output for dashboard")
    args = parser.parse_args()

    # Normalise paths
    args.output_dir    = os.path.abspath(args.output_dir)
    args.dashboard_dir = os.path.abspath(args.dashboard_dir)
    os.makedirs(args.output_dir,    exist_ok=True)
    os.makedirs(args.dashboard_dir, exist_ok=True)

    county_filter = args.county.lower().strip()

    print(f"\n{'='*60}")
    print("WBP County Scraper — GitHub Actions Runner")
    print(f"{'='*60}")
    print(f"Date:         {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"County:       {county_filter}")
    print(f"Lookback:     {args.lookback_days}d")
    print(f"Output dir:   {args.output_dir}")
    print(f"Dashboard:    {args.dashboard_dir}")
    print(f"JSON output:  {args.json}")
    print()

    # Select active scrapers matching county filter
    scrapers_to_run = [
        s for s in sorted(SCRAPERS, key=lambda x: x["priority"])
        if s["status"] == "active"
        and (county_filter == "all" or s["county"] == county_filter)
    ]

    if not scrapers_to_run:
        print(f"No active scrapers match county='{county_filter}'. Exiting.")
        sys.exit(0)

    print(f"Running {len(scrapers_to_run)} scraper(s):")
    for s in scrapers_to_run:
        print(f"  [{s['priority']}] {s['name']} ({s['county']})")
    print()

    # Run each scraper
    total_start = time.time()
    results = []
    for scraper in scrapers_to_run:
        print(f"\n--- {scraper['name']} ---")
        result = run_scraper_module(scraper, args)
        results.append(result)

    total_elapsed = time.time() - total_start

    # Summary
    success = [r for r in results if r["status"] == "success"]
    errors  = [r for r in results if r["status"] == "error"]
    skipped = [r for r in results if r["status"] == "skipped"]

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Success:  {len(success)}/{len(results)} scrapers")
    print(f"Total time: {total_elapsed:.1f}s")
    if errors:
        print(f"ERRORS ({len(errors)}):")
        for r in errors:
            print(f"  - {r['name']}: {r.get('error', 'unknown')}")
        sys.exit(1)  # Fail the GitHub Actions step so we get notified
    if skipped:
        print(f"Skipped: {[r['name'] for r in skipped]}")

    print("\nDone.")


if __name__ == "__main__":
    main()

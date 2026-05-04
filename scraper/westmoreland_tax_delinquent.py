"""
westmoreland_tax_delinquent.py
We Buy Property LLC — Head of Data
Purpose: Pull Westmoreland County PA tax delinquent / tax sale list.
         Source: Westmoreland County Tax Claim Bureau
         URL:    https://www.westmorelandcountypa.gov/160/Tax-OfficeTax-Claim-Bureau
Output:  Standardized CSV for REI Sift import
Run:     python westmoreland_tax_delinquent.py
Rate:    1-2 requests — single page + file download

RESEARCH NOTE (2026-04-16 — VERIFIED):
  County moved domain: co.westmoreland.pa.us → westmorelandcountypa.gov
  2025 Upset Sale advertising list confirmed at:
    https://www.westmorelandcountypa.gov/DocumentCenter/View/34033/2025-AD-LIST
  Annual Upset Tax Sale page:
    https://www.westmorelandcountypa.gov/165/Annual-Upset-Tax-Sale
  Judicial Sale Lists:
    https://www.westmorelandcountypa.gov/167/Judicial-Sale-Lists
  Sale date: September 8, 2025 (annual — check for 2026 list when published ~Aug 2026)
  Update SALE_LIST_URL each year when new advertising list is posted.
"""

import requests
import pandas as pd
import pdfplumber
from datetime import datetime, date
from io import BytesIO
import os
import time
import sys
import re

# ─── CONFIG ──────────────────────────────────────────────────────────────────
# 2025 Advertising List — VERIFIED 2026-04-16
# Update each year when new list posted (~August before September sale)
SALE_LIST_URL = "https://www.westmorelandcountypa.gov/DocumentCenter/View/34033/2025-AD-LIST"

# Annual Upset Sale page (check here for 2026 list when published)
ANNUAL_SALE_PAGE = "https://www.westmorelandcountypa.gov/165/Annual-Upset-Tax-Sale"

COUNTY_BASE   = "https://www.westmorelandcountypa.gov"  # Updated domain 2026-04-16
OUTPUT_DIR    = os.path.join(os.path.dirname(__file__), "..", "output")
HEADERS       = {"User-Agent": "Mozilla/5.0 (compatible; WBP-DataBot/1.0; research use)"}


def discover_sale_url() -> str | None:
    """Try to auto-discover the tax sale list URL from county pages."""
    candidate_paths = [
        "/160/Tax-OfficeTax-Claim-Bureau",
        "/165/Annual-Upset-Tax-Sale",
        "/167/Judicial-Sale-Lists",
        "/166/Tax-Sale-Information",
    ]

    for path in candidate_paths:
        url = COUNTY_BASE + path
        try:
            time.sleep(1)
            resp = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                html = resp.text

                # Look for sale list links
                pattern = re.compile(
                    r'href=["\']([^"\']*(?:upset|sale|delinquent|tax.?sale|repository)[^"\']*\.(?:pdf|xlsx?))["\']',
                    re.IGNORECASE
                )
                matches = pattern.findall(html)
                if matches:
                    found = matches[0]
                    if not found.startswith("http"):
                        found = COUNTY_BASE + found
                    print(f"  → Found at {path}: {found}")
                    return found

                # DocumentCenter pattern (Civic Plus sites)
                dc_pattern = re.compile(r'href=["\']([^"\']*DocumentCenter[^"\']*)["\'].*?(?:upset|sale|delinq)',
                                        re.IGNORECASE | re.DOTALL)
                dc_matches = dc_pattern.findall(html)
                if dc_matches:
                    found = dc_matches[0]
                    if not found.startswith("http"):
                        found = COUNTY_BASE + found
                    return found

        except Exception as e:
            print(f"  → {path}: {e}")

    return None


def download_and_parse(url: str) -> pd.DataFrame:
    """Download PDF or Excel and extract tabular data."""
    print(f"  Downloading: {url}")
    time.sleep(1)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        if url.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(BytesIO(resp.content))
            print(f"  → Excel: {len(df)} rows")
            return df

        # PDF extraction
        rows = []
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row and any(cell for cell in row if cell):
                            rows.append([str(c or "").strip() for c in row])

        if not rows:
            # Try text extraction as fallback
            text_rows = []
            with pdfplumber.open(BytesIO(resp.content)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        for line in text.split("\n"):
                            if line.strip() and len(line.strip()) > 5:
                                text_rows.append([line.strip()])
            if text_rows:
                df = pd.DataFrame(text_rows, columns=["raw_line"])
                print(f"  → PDF text-only: {len(df)} lines (parsing may need custom logic)")
                return df
            return pd.DataFrame()

        if rows and any(keyword in str(rows[0]).lower()
                       for keyword in ["owner", "address", "parcel", "name", "amount"]):
            df = pd.DataFrame(rows[1:], columns=rows[0])
        else:
            df = pd.DataFrame(rows)
            df.columns = [f"col_{i}" for i in range(len(df.columns))]

        print(f"  → PDF parsed: {len(df)} rows")
        return df

    except Exception as e:
        print(f"  → Parse error: {e}")
        return pd.DataFrame()


def standardize(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize to WBP schema."""
    # Handle raw_line fallback (text-only PDF)
    if "raw_line" in df.columns:
        output = pd.DataFrame({
            "owner_name":       "",
            "property_address": df["raw_line"],
            "city":             "Westmoreland County",
            "zip":              "",
            "parcel_id":        "",
            "data_type":        "tax_delinquent",
            "source":           "westmoreland_tax_claim_bureau",
            "county":           "Westmoreland",
            "date_pulled":      datetime.today().strftime("%Y-%m-%d"),
            "raw_detail":       df["raw_line"],
        })
        return output

    # Map columns
    col_map = {}
    for col in df.columns:
        c = str(col).lower().replace(" ", "_")
        if any(k in c for k in ["owner", "name"]):           col_map["owner_name"] = col
        elif any(k in c for k in ["address", "street"]) and "mail" not in c: col_map["property_address"] = col
        elif any(k in c for k in ["city", "munic", "muni"]): col_map["city"] = col
        elif "zip" in c:                                      col_map["zip"] = col
        elif any(k in c for k in ["parcel", "parid", "map"]): col_map["parcel_id"] = col
        elif any(k in c for k in ["amount", "balance", "tax", "owed"]): col_map["raw_detail"] = col

    output = pd.DataFrame({
        "owner_name":       df[col_map["owner_name"]].fillna("") if "owner_name" in col_map else "",
        "property_address": df[col_map["property_address"]].fillna("") if "property_address" in col_map else "",
        "city":             df[col_map["city"]].fillna("") if "city" in col_map else "Westmoreland County",
        "zip":              df[col_map["zip"]].fillna("") if "zip" in col_map else "",
        "parcel_id":        df[col_map["parcel_id"]].fillna("") if "parcel_id" in col_map else "",
        "data_type":        "tax_delinquent",
        "source":           "westmoreland_tax_claim_bureau",
        "county":           "Westmoreland",
        "date_pulled":      datetime.today().strftime("%Y-%m-%d"),
        "raw_detail":       df[col_map["raw_detail"]].fillna("").astype(str) if "raw_detail" in col_map else "",
    })
    return output


def save_output(df: pd.DataFrame) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"westmoreland_tax_delinquent_{date.today().strftime('%Y%m%d')}.csv"
    filepath = os.path.join(OUTPUT_DIR, filename)
    df.to_csv(filepath, index=False)
    print(f"  ✓ Saved: {filepath}")
    return filepath


if __name__ == "__main__":
    start = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Westmoreland County tax delinquent records...")

    # Use manual URL if provided via CLI
    manual_url = None
    if "--url" in sys.argv:
        idx = sys.argv.index("--url")
        if idx + 1 < len(sys.argv):
            manual_url = sys.argv[idx + 1]

    sale_url = manual_url or SALE_LIST_URL

    if not sale_url:
        print("  Auto-discovering URL from county website...")
        sale_url = discover_sale_url()

    if not sale_url:
        print("\n⚠ MANUAL ACTION REQUIRED:")
        print("  Westmoreland County site requires manual URL lookup.")
        print("  1. Visit: https://www.co.westmoreland.pa.us/")
        print("  2. Navigate: Departments → Tax Claim Bureau")
        print("  3. Find: Upset Sale List or Repository List (PDF/Excel)")
        print("  4. Run: python westmoreland_tax_delinquent.py --url <URL>")
        print("  5. Then update SALE_LIST_URL in this script for future automation")
        exit(0)

    raw_df = download_and_parse(sale_url)
    if raw_df.empty:
        print("\n⚠ Could not extract data. See script header for troubleshooting.")
        exit(0)

    output_df = standardize(raw_df)
    output_df = output_df[output_df["property_address"].astype(str).str.len() > 3]
    output_df = output_df.drop_duplicates(subset=["property_address"])

    filepath = save_output(output_df)
    print(f"\n✅ DONE — {len(output_df)} records | {filepath}")
    print(f"   Elapsed: {time.time()-start:.1f}s")

"""
washington_tax_delinquent.py
We Buy Property LLC — Head of Data
Purpose: Pull Washington County PA tax delinquent / tax sale list.
         Source: Washington County Tax Claim Bureau
         URL:    https://www.co.washington.pa.us/164/Tax-Claim-Bureau
Output:  Standardized CSV for REI Sift import
Run:     python washington_tax_delinquent.py
Rate:    1 request max — single file download
"""

import requests
import pandas as pd
import pdfplumber
from datetime import datetime, date
from io import BytesIO
import os
import time
import re

# ─── CONFIG ──────────────────────────────────────────────────────────────────
COUNTY_BASE = "https://www.co.washington.pa.us/164/Tax-Claim-Bureau"
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "output")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WBP-DataBot/1.0; research use)"
}

# Known candidate URLs for Washington County tax sale lists (PDF format)
# These need to be verified/updated each cycle — check the Tax Claim Bureau page
CANDIDATE_PDF_URLS = [
    "https://www.co.washington.pa.us/DocumentCenter/View/upset-sale-list",
    "https://www.co.washington.pa.us/DocumentCenter/View/9999",  # placeholder
]


def find_sale_list_url() -> str | None:
    """
    Scrape the Tax Claim Bureau page to find the current tax sale list URL.
    Returns PDF/Excel URL if found, None otherwise.
    """
    print(f"  Fetching Tax Claim Bureau page: {COUNTY_BASE}")
    time.sleep(1)
    try:
        resp = requests.get(COUNTY_BASE, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text

        # Look for PDF links in the page
        # Washington County embeds links as href="/DocumentCenter/View/..."
        pdf_pattern = re.compile(
            r'href=["\']([^"\']*(?:upset|sale|delinquent|tax[-_]claim)[^"\']*\.(?:pdf|xlsx?))["\']',
            re.IGNORECASE
        )
        matches = pdf_pattern.findall(html)

        # Also look for DocumentCenter links (typical Civic Plus government sites)
        dc_pattern = re.compile(
            r'href=["\']([^"\']*DocumentCenter[^"\']*)["\']',
            re.IGNORECASE
        )
        dc_matches = dc_pattern.findall(html)

        # Try to find any link with "upset" or "sale" in anchor text
        anchor_pattern = re.compile(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>[^<]*(?:upset|sale|delinquent)[^<]*</a>',
            re.IGNORECASE
        )
        anchor_matches = anchor_pattern.findall(html)

        all_candidates = matches + anchor_matches
        if all_candidates:
            url = all_candidates[0]
            if not url.startswith("http"):
                url = "https://www.co.washington.pa.us" + url
            print(f"  → Found candidate URL: {url}")
            return url

        print("  → No sale list links found on page (page may require JS rendering)")
        return None

    except Exception as e:
        print(f"  → Error fetching county page: {e}")
        return None


def download_and_parse_pdf(url: str) -> pd.DataFrame:
    """Download a PDF from the county and extract tabular data."""
    print(f"  Downloading PDF: {url}")
    time.sleep(1)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        # Try Excel first
        if url.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(BytesIO(resp.content))
            print(f"  → Excel parsed: {len(df)} rows")
            return df

        # Try PDF
        rows = []
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            for page_num, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row and any(cell for cell in row if cell):
                            rows.append([str(c or "").strip() for c in row])

        if not rows:
            print("  → PDF has no extractable tables — may be scanned/image PDF")
            return pd.DataFrame()

        # Use first row as header if it looks like headers
        if rows and any(keyword in str(rows[0]).lower()
                       for keyword in ["owner", "address", "parcel", "amount", "name"]):
            df = pd.DataFrame(rows[1:], columns=rows[0])
        else:
            df = pd.DataFrame(rows)
            df.columns = [f"col_{i}" for i in range(len(df.columns))]

        print(f"  → PDF parsed: {len(df)} rows, columns: {list(df.columns)[:6]}")
        return df

    except Exception as e:
        print(f"  → Download/parse error: {e}")
        return pd.DataFrame()


def standardize(df: pd.DataFrame) -> pd.DataFrame:
    """Map raw columns to WBP standard schema."""
    print(f"  Columns: {list(df.columns)}")

    col_map = {}
    for col in df.columns:
        c = str(col).lower().replace(" ", "_").replace("-", "_")
        if any(k in c for k in ["owner", "name"]):           col_map["owner_name"] = col
        elif any(k in c for k in ["address", "street", "location"]) and "mail" not in c:
                                                              col_map["property_address"] = col
        elif any(k in c for k in ["city", "munic", "borough", "township"]): col_map["city"] = col
        elif "zip" in c:                                      col_map["zip"] = col
        elif any(k in c for k in ["parcel", "parid", "map_number"]): col_map["parcel_id"] = col
        elif any(k in c for k in ["amount", "owed", "total", "balance", "tax"]): col_map["raw_detail"] = col

    output = pd.DataFrame({
        "owner_name":       df[col_map["owner_name"]].fillna("") if "owner_name" in col_map else "",
        "property_address": df[col_map["property_address"]].fillna("") if "property_address" in col_map else "",
        "city":             df[col_map["city"]].fillna("") if "city" in col_map else "Washington County",
        "zip":              df[col_map["zip"]].fillna("") if "zip" in col_map else "",
        "parcel_id":        df[col_map["parcel_id"]].fillna("") if "parcel_id" in col_map else "",
        "data_type":        "tax_delinquent",
        "source":           "washington_tax_claim_bureau",
        "county":           "Washington",
        "date_pulled":      datetime.today().strftime("%Y-%m-%d"),
        "raw_detail":       df[col_map["raw_detail"]].fillna("").astype(str) if "raw_detail" in col_map else "",
    })
    return output


def save_output(df: pd.DataFrame) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"washington_tax_delinquent_{date.today().strftime('%Y%m%d')}.csv"
    filepath = os.path.join(OUTPUT_DIR, filename)
    df.to_csv(filepath, index=False)
    print(f"  ✓ Saved: {filepath}")
    return filepath


if __name__ == "__main__":
    start = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Washington County tax delinquent records...")

    # Step 1: Find the current sale list URL
    sale_url = find_sale_list_url()

    if not sale_url:
        print("\n⚠ MANUAL ACTION REQUIRED:")
        print("  1. Visit https://www.co.washington.pa.us/164/Tax-Claim-Bureau")
        print("  2. Download the current Upset Sale or Repository List (PDF/Excel)")
        print("  3. Save to: output/washington_tax_delinquent_manual.pdf")
        print("  4. Re-run with: python washington_tax_delinquent.py --manual output/washington_tax_delinquent_manual.pdf")
        exit(0)

    # Step 2: Download and parse
    raw_df = download_and_parse_pdf(sale_url)
    if raw_df.empty:
        print("\n⚠ Could not extract data from PDF. May need OCR or manual processing.")
        exit(0)

    # Step 3: Standardize
    output_df = standardize(raw_df)
    output_df = output_df[output_df["property_address"].astype(str).str.len() > 3]
    output_df = output_df.drop_duplicates(subset=["property_address"])

    filepath = save_output(output_df)
    print(f"\n✅ DONE — {len(output_df)} records | {filepath}")
    print(f"   Elapsed: {time.time()-start:.1f}s")

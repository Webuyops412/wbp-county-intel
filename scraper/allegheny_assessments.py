"""
allegheny_assessments.py
We Buy Property LLC — Head of Data
Purpose: Pull Allegheny County property assessment data from WPRDC.
         Use as: (1) owner name enrichment for other scrapers,
                 (2) standalone absentee owner + equity targeting list
Output:  CSV with owner name, mailing address, property address, assessed value
Run:     python allegheny_assessments.py [--absentee-only]
Rate:    Single large API call — dataset has ~600K records, use filters
"""

import requests
import pandas as pd
from datetime import datetime, date
import os
import time
import sys

# ─── CONFIG ──────────────────────────────────────────────────────────────────
# WPRDC Allegheny County Property Assessments
# Resource ID confirmed from WPRDC data catalog — allegheny-county-property-assessments
RESOURCE_ID = "65855e14-549e-4992-b5be-d629afc676fa"  # VERIFIED 2026-04-16 — 584,896 records live
WPRDC_API   = "https://data.wprdc.org/api/3/action/datastore_search_sql"
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "output")

# WBP target counties — Allegheny municipalities
TARGET_COUNTIES = ["ALLEGHENY"]

# Absentee owner: owner mailing address is NOT at the property address
# (mailing_address city/state differs from property address)


def fetch_assessments(absentee_only: bool = True, limit: int = 50000) -> pd.DataFrame:
    """
    Pull Allegheny County property assessments.

    Args:
        absentee_only: If True, filter to properties where owner mailing ≠ property address
                       (proxy for absentee/landlord ownership)
        limit: Max records to pull
    Returns:
        Standardized DataFrame
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Allegheny County assessments...")
    print(f"  Mode: {'absentee owners only' if absentee_only else 'all properties'}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Core fields from Allegheny County assessment dataset
    # Field names confirmed from WPRDC data dictionary
    fields = (
        '"PARID", "PROPERTYHOUSENUM", "PROPERTYADDRESS", "PROPERTYCITY", '
        '"PROPERTYZIP", "PROPERTYSTATE", '
        '"OWNERDESC", '           # Owner 1 name
        '"CHANGENOTICEADDRESS1", "CHANGENOTICEADDRESS2", '  # Mailing address
        '"CHANGENOTICECITY", "CHANGENOTICESTATE", "CHANGENOTICEZIP", '
        '"CLASSDESC", '           # Property class (RESIDENTIAL, COMMERCIAL, etc.)
        '"LOTAREA", "SALEPRICE", "SALEDATE", '
        '"FAIRMARKETBUILDING", "FAIRMARKETLAND", '
        '"YEARBLT", "BEDROOMS", "FULLBATHS"'
    )

    # Filter: residential properties only (CLASS = R or residential class codes)
    res_filter = "AND (\"CLASSDESC\" LIKE 'RESIDENTIAL%' OR \"CLASSDESC\" LIKE 'SINGLE%')"

    # Absentee filter: mailing city differs from property city (rough proxy)
    # More precise: mailing address != property address
    absentee_filter = ""
    if absentee_only:
        absentee_filter = (
            'AND ("CHANGENOTICECITY" IS NOT NULL '
            'AND UPPER("CHANGENOTICECITY") != UPPER("PROPERTYCITY"))'
        )

    sql = (
        f'SELECT {fields} '
        f'FROM "{RESOURCE_ID}" '
        f'WHERE "PROPERTYSTATE" = \'PA\' '
        f'{res_filter} '
        f'{absentee_filter} '
        f'LIMIT {limit}'
    )

    try:
        resp = requests.get(WPRDC_API, params={"sql": sql}, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise ValueError(f"API error: {data.get('error')}")
        records = data["result"]["records"]
        print(f"  → {len(records)} records retrieved")
    except Exception as e:
        print(f"  ✗ Error: {e}")
        raise

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # ─── STANDARDIZE ─────────────────────────────────────────────────────────
    def clean(col): return df.get(col, pd.Series([""] * len(df))).fillna("").astype(str).str.strip()

    prop_addr = (
        clean("PROPERTYHOUSENUM") + " " + clean("PROPERTYADDRESS")
    ).str.strip()

    mail_addr = (
        clean("CHANGENOTICEADDRESS1") + ", " +
        clean("CHANGENOTICECITY") + ", " +
        clean("CHANGENOTICESTATE") + " " +
        clean("CHANGENOTICEZIP")
    ).str.strip(", ")

    output = pd.DataFrame({
        "owner_name":         clean("OWNERDESC"),
        "property_address":   prop_addr,
        "city":               clean("PROPERTYCITY"),
        "zip":                clean("PROPERTYZIP"),
        "parcel_id":          clean("PARID"),
        "data_type":          "property_assessment",
        "source":             "wprdc_allegheny_assessments",
        "county":             "Allegheny",
        "date_pulled":        datetime.today().strftime("%Y-%m-%d"),
        "raw_detail":         (
            "Class: " + clean("CLASSDESC") +
            " | YearBuilt: " + clean("YEARBLT") +
            " | FMV: $" + clean("FAIRMARKETBUILDING") +
            " | SalePrice: $" + clean("SALEPRICE") +
            " | SaleDate: " + clean("SALEDATE") +
            " | Beds: " + clean("BEDROOMS") +
            " | MailAddr: " + mail_addr
        ),
        "owner_mailing_address": mail_addr,
    })

    output = output[output["property_address"].str.len() > 3].copy()
    output = output.drop_duplicates(subset=["parcel_id"])

    print(f"  → {len(output)} clean records")
    return output


def save_output(df: pd.DataFrame, absentee_only: bool = True) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tag = "absentee" if absentee_only else "all"
    filename = f"allegheny_assessments_{tag}_{date.today().strftime('%Y%m%d')}.csv"
    filepath = os.path.join(OUTPUT_DIR, filename)
    df.to_csv(filepath, index=False)
    print(f"  ✓ Saved: {filepath}")
    return filepath


if __name__ == "__main__":
    absentee_only = "--absentee-only" in sys.argv or True  # default to absentee
    start = time.time()
    df = fetch_assessments(absentee_only=absentee_only)
    if not df.empty:
        filepath = save_output(df, absentee_only)
        print(f"\n✅ DONE — {len(df)} records | {filepath}")
        print(f"   Elapsed: {time.time()-start:.1f}s")
        print(f"\n📋 NEXT STEP: Cross-reference with code_violations CSV by parcel_id to get owner names")
    else:
        print("\n⚠ No data — verify resource ID or check WPRDC API")

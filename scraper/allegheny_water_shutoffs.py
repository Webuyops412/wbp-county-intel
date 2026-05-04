"""
allegheny_water_shutoffs.py
We Buy Property LLC — Head of Data
Purpose: Pull Pittsburgh Water & Sewer Authority (PWSA) water shutoff records
         from WPRDC open data. Water shutoff = severe financial distress signal.
         Join with assessments for ARV filter > $100k.
         No owner filter — LLC, in-state, out-of-state all included.

Source: WPRDC PWSA Water Service Shutoffs
URL:    https://data.wprdc.org/dataset/pwsa-water-service-shutoffs
Updated: 2026-05-04

ARV FILTER:
  MIN_FMV = 87,700 → approx $100k ARV (Allegheny CLR 87.7%)
"""

import requests
import pandas as pd
from datetime import datetime, date
import os, time, json

# ─── CONFIG ──────────────────────────────────────────────────────────────────
WPRDC_SEARCH   = "https://data.wprdc.org/api/3/action/datastore_search"
WPRDC_SQL      = "https://data.wprdc.org/api/3/action/datastore_search_sql"
ASSESSMENTS_ID = "65855e14-549e-4992-b5be-d629afc676fa"
OUTPUT_DIR     = os.path.join(os.path.dirname(__file__), "..", "output")
BATCH_SIZE     = 5000

# ARV filter
MIN_FMV = 87_700
MAX_FMV = 600_000
CLR     = 0.877

# Known PWSA shutoff resource IDs on WPRDC (try both)
SHUTOFF_RESOURCE_IDS = [
    "5e95e9fe-4d07-4f22-a2a3-8b7a47a1f98c",  # PWSA Water Shutoffs (primary)
    "7a6a4f93-7a30-4870-9e8c-7fc2e5d54d7c",  # Fallback ID
]


def find_shutoff_resource() -> str | None:
    """Try known resource IDs to find the active PWSA shutoff dataset."""
    for rid in SHUTOFF_RESOURCE_IDS:
        try:
            r = requests.get(WPRDC_SEARCH, params={
                "resource_id": rid, "limit": 1
            }, timeout=20)
            data = r.json()
            if data.get("success") and data["result"].get("total", 0) > 0:
                print(f"  ✓ Found PWSA shutoff data at resource ID: {rid}")
                return rid
        except Exception:
            continue
    return None


def search_wprdc_for_shutoffs() -> str | None:
    """Search WPRDC package registry for PWSA shutoff dataset."""
    try:
        r = requests.get(
            "https://data.wprdc.org/api/3/action/package_search",
            params={"q": "PWSA water shutoff", "rows": 5},
            timeout=20
        )
        data = r.json()
        if data.get("success"):
            for pkg in data["result"]["results"]:
                for res in pkg.get("resources", []):
                    if any(kw in res.get("name", "").lower() for kw in ["shutoff", "shut-off", "shut off", "service"]):
                        print(f"  Found: {res['name']} → {res['id']}")
                        return res["id"]
    except Exception as e:
        print(f"  ⚠ WPRDC search failed: {e}")
    return None


def fetch_shutoffs(resource_id: str) -> pd.DataFrame:
    """Pull all shutoff records from WPRDC."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching water shutoffs (resource: {resource_id})...")
    all_records = []
    offset = 0
    while True:
        r = requests.get(WPRDC_SEARCH, params={
            "resource_id": resource_id,
            "limit": BATCH_SIZE,
            "offset": offset
        }, timeout=60)
        data = r.json()
        if not data.get("success"):
            print(f"  ✗ API error: {data.get('error')}")
            break
        records = data["result"]["records"]
        total   = data["result"]["total"]
        all_records.extend(records)
        offset += len(records)
        if offset >= total or not records:
            break
        time.sleep(0.4)
    print(f"  → {len(all_records):,} raw shutoff records")
    return pd.DataFrame(all_records) if all_records else pd.DataFrame()


def parse_address(addr):
    if not addr:
        return "", "Pittsburgh", ""
    parts = str(addr).split(",")
    street   = parts[0].strip() if parts else ""
    city     = parts[1].strip() if len(parts) > 1 else "Pittsburgh"
    state_zip = parts[2].strip() if len(parts) > 2 else ""
    zip_code  = state_zip.replace("PA", "").replace("-", "").strip()
    return street, city, zip_code


def fetch_assessments_for_parcels(parcel_ids: list) -> pd.DataFrame:
    results = []
    batch_size = 80
    for i in range(0, len(parcel_ids), batch_size):
        batch = parcel_ids[i:i + batch_size]
        ids_str = ", ".join(f"'{p}'" for p in batch)
        sql = f"""SELECT "PARID", "FAIRMARKETTOTAL", "OWNERDESC",
                         "CHANGENOTICEADDRESS1", "CHANGENOTICEADDRESS3", "CHANGENOTICEADDRESS4"
                  FROM "{ASSESSMENTS_ID}"
                  WHERE "PARID" IN ({ids_str})"""
        try:
            r = requests.get(WPRDC_SQL, params={"sql": sql}, timeout=60)
            data = r.json()
            if data.get("success"):
                results.extend(data["result"]["records"])
        except Exception as e:
            print(f"  ⚠ Assessment batch {i//batch_size+1} error: {e}")
        time.sleep(0.3)
    return pd.DataFrame(results) if results else pd.DataFrame()


def run():
    start = time.time()

    # Find resource ID
    resource_id = find_shutoff_resource() or search_wprdc_for_shutoffs()
    if not resource_id:
        print("⚠ PWSA water shutoff dataset not found on WPRDC.")
        print("  Manual check: https://data.wprdc.org → search 'PWSA shutoff'")
        print("  Once found, add resource ID to SHUTOFF_RESOURCE_IDS in this script.")
        return None

    # Pull shutoffs
    df = fetch_shutoffs(resource_id)
    if df.empty:
        print("⚠ No shutoff records returned")
        return None

    print(f"  Columns: {list(df.columns)}")

    # Detect address column (varies by dataset version)
    addr_col = next((c for c in df.columns if "address" in c.lower()), None)
    parcel_col = next((c for c in df.columns if "parid" in c.lower() or "parcel" in c.lower()), None)

    if addr_col:
        parsed = df[addr_col].apply(parse_address)
        df["property_address"] = [p[0] for p in parsed]
        df["city"]             = [p[1] for p in parsed]
        df["zip"]              = [p[2] for p in parsed]
    else:
        print("⚠ No address column found — cannot parse addresses")
        return None

    df = df[df["property_address"].str.len() > 3].copy()

    # Assessment join for ARV filter
    if parcel_col and df[parcel_col].notna().sum() > 0:
        parcel_ids = df[parcel_col].dropna().unique().tolist()
        adf = fetch_assessments_for_parcels(parcel_ids)
        if not adf.empty:
            adf = adf.rename(columns={"PARID": parcel_col})
            adf["FAIRMARKETTOTAL"] = pd.to_numeric(adf["FAIRMARKETTOTAL"], errors="coerce").fillna(0)
            df = df.merge(adf, on=parcel_col, how="left")
            df["FAIRMARKETTOTAL"] = df["FAIRMARKETTOTAL"].fillna(0)
            before = len(df)
            df = df[(df["FAIRMARKETTOTAL"] >= MIN_FMV) & (df["FAIRMARKETTOTAL"] <= MAX_FMV)]
            print(f"  ARV filter: {before:,} → {len(df):,} records (≈ ${MIN_FMV/CLR:,.0f}–${MAX_FMV/CLR:,.0f} ARV)")
    else:
        df["FAIRMARKETTOTAL"] = 0
        df["OWNERDESC"] = ""

    df["estimated_arv"] = (df.get("FAIRMARKETTOTAL", 0) / CLR).round(0).astype(int)

    # Build output
    shutoff_date_col = next((c for c in df.columns if "date" in c.lower() or "shutoff" in c.lower()), "")
    output = pd.DataFrame({
        "owner_name":       df.get("OWNERDESC", pd.Series([""] * len(df))).fillna(""),
        "property_address": df["property_address"],
        "city":             df["city"],
        "zip":              df["zip"],
        "parcel_id":        df.get(parcel_col, pd.Series([""] * len(df))).fillna("") if parcel_col else "",
        "assessed_fmv":     df.get("FAIRMARKETTOTAL", 0),
        "estimated_arv":    df["estimated_arv"],
        "data_type":        "water_shutoff",
        "source":           "wprdc_pwsa_shutoffs",
        "county":           "Allegheny",
        "date_pulled":      date.today().strftime("%Y-%m-%d"),
        "status":           "Water Service Shutoff",
        "violation_type":   "Water Shutoff",
        "raw_detail":       "Water Shutoff | ARV est: $" + df["estimated_arv"].astype(str),
    })

    output = output.drop_duplicates(subset=["property_address"])
    output = output.reset_index(drop=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"allegheny_water_shutoffs_{date.today().strftime('%Y%m%d')}.csv"
    filepath = os.path.join(OUTPUT_DIR, filename)
    output.to_csv(filepath, index=False)

    elapsed = time.time() - start
    print(f"\n✅ DONE — {len(output):,} water shutoff records | {filename} | {elapsed:.1f}s")
    return filepath


if __name__ == "__main__":
    run()

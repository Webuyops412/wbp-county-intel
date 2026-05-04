"""
allegheny_code_violations.py
We Buy Property LLC — Head of Data
Purpose: Pull ALL open code violation records from WPRDC + join with assessments
         to filter by estimated ARV > $100k (Allegheny CLR 87.7%).
         No type filter — all distress signals included.
         No owner filter — LLC, in-state, out-of-state all included.

ARV FILTER:
  MIN_FMV = 40,000  -> loose floor, real ARV filtering done in PropStream after skip trace
  MAX_FMV = 600,000 -> avoids commercial/luxury outliers

Updated: 2026-05-04 — removed type/owner filters, added assessment join for ARV
"""

import requests
import pandas as pd
from datetime import datetime, date
import os, time, json

VIOLATIONS_ID  = "70c06278-92c5-4040-ab28-17671866f81c"
ASSESSMENTS_ID = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_SEARCH   = "https://data.wprdc.org/api/3/action/datastore_search"
WPRDC_SQL      = "https://data.wprdc.org/api/3/action/datastore_search_sql"
OUTPUT_DIR     = os.path.join(os.path.dirname(__file__), "..", "output")
BATCH_SIZE     = 5000
MIN_FMV        = 40_000
MAX_FMV        = 600_000
CLR            = 0.877
OPEN_STATUSES  = ["In Violation", "In Court", "Clean & Lien", "Under Investigation"]

# Violation types that signal owner distress — not neighbor complaints
# Removed: Curb Cuts, Dumpster (on Street), Refuse or Recycling Violations, Utility Poles and Wires
DISTRESS_TYPES = [
    "Vacant Building",
    "Vacant Buildings",
    "Building Maintenance",
    "Building Maintenance Issues",
    "Fire Safety System Issue",
    "Building Without a Permit",
    "Work or Construction Without Permits",
    "Unpermitted Electrical Work",
    "Sewer Lateral",
    "Zoning Issue",
    "Weeds/Debris",
    "Weeds or Debris",
    "Weeds and Debris",
    "Refuse Violations",
    "Graffiti on Structure",
    "Broken Sidewalk",
]


def parse_address(addr):
    if not addr:
        return "", "Pittsburgh", ""
    parts = str(addr).split(",")
    street    = parts[0].strip() if parts else ""
    city      = parts[1].strip() if len(parts) > 1 else "Pittsburgh"
    state_zip = parts[2].strip() if len(parts) > 2 else ""
    zip_code  = state_zip.replace("PA", "").replace("-", "").strip()
    return street, city, zip_code


def fetch_all_violations():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching ALL Allegheny violations (no type filter)...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    all_records = []
    for status in OPEN_STATUSES:
        offset, count = 0, 0
        while True:
            r = requests.get(WPRDC_SEARCH, params={
                "resource_id": VIOLATIONS_ID,
                "filters": json.dumps({"status": status}),
                "limit": BATCH_SIZE,
                "offset": offset
            }, timeout=60)
            data = r.json()
            if not data.get("success"):
                print(f"  Error on status={status}: {data.get('error')}")
                break
            records = data["result"]["records"]
            total   = data["result"]["total"]
            all_records.extend(records)
            count  += len(records)
            offset += len(records)
            if offset >= total or not records:
                break
            time.sleep(0.4)
        print(f"  '{status}': {count:,} records")
    print(f"  -> {len(all_records):,} total raw violation records")
    return pd.DataFrame(all_records) if all_records else pd.DataFrame()


def fetch_assessments_for_parcels(parcel_ids):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching assessments for {len(parcel_ids):,} parcels...")
    results = []
    for i in range(0, len(parcel_ids), 80):
        batch   = parcel_ids[i:i + 80]
        ids_str = ", ".join(f"'{p}'" for p in batch)
        sql = (
            f'SELECT "PARID", "FAIRMARKETTOTAL", "OWNERDESC", '
            f'"CHANGENOTICEADDRESS1", "CHANGENOTICEADDRESS3", "CHANGENOTICEADDRESS4", '
            f'"CLASSDESC", "USEDESC" '
            f'FROM "{ASSESSMENTS_ID}" '
            f'WHERE "PARID" IN ({ids_str})'
        )
        try:
            r    = requests.get(WPRDC_SQL, params={"sql": sql}, timeout=60)
            data = r.json()
            if data.get("success"):
                results.extend(data["result"]["records"])
        except Exception as e:
            print(f"  Batch {i//80 + 1} failed: {e}")
        time.sleep(0.3)
    print(f"  -> {len(results):,} assessment records matched")
    return pd.DataFrame(results) if results else pd.DataFrame()


def run():
    start = time.time()

    # 1. Pull all violations — no type filter
    vdf = fetch_all_violations()
    if vdf.empty:
        print("No violation records returned")
        return

    # 2. Parse addresses
    parsed = vdf["address"].apply(parse_address)
    vdf["property_address"] = [p[0] for p in parsed]
    vdf["city"]             = [p[1] for p in parsed]
    vdf["zip"]              = [p[2] for p in parsed]
    vdf = vdf[vdf["property_address"].str.len() > 3].copy()

    # Filter to distress types only
    if "case_file_type" in vdf.columns:
        before = len(vdf)
        vdf = vdf[vdf["case_file_type"].isin(DISTRESS_TYPES)].copy()
        print(f"  Type filter: {before:,} -> {len(vdf):,} records")

    # 3. Assessment join for ARV filter
    parcel_col = "parcel_id" if "parcel_id" in vdf.columns else None
    if parcel_col and vdf[parcel_col].notna().sum() > 0:
        parcel_ids = vdf[parcel_col].dropna().unique().tolist()
        adf = fetch_assessments_for_parcels(parcel_ids)
    else:
        print("  No parcel IDs found — skipping ARV filter")
        adf = pd.DataFrame()

    if not adf.empty:
        adf = adf.rename(columns={"PARID": "parcel_id"})
        adf["FAIRMARKETTOTAL"] = pd.to_numeric(adf["FAIRMARKETTOTAL"], errors="coerce").fillna(0)
        keep_cols = ["parcel_id", "FAIRMARKETTOTAL", "OWNERDESC",
                     "CHANGENOTICEADDRESS1", "CHANGENOTICEADDRESS3",
                     "CHANGENOTICEADDRESS4", "CLASSDESC", "USEDESC"]
        adf = adf[[c for c in keep_cols if c in adf.columns]]
        vdf = vdf.merge(adf, on="parcel_id", how="left")
        vdf["FAIRMARKETTOTAL"] = vdf["FAIRMARKETTOTAL"].fillna(0)
        before = len(vdf)
        vdf = vdf[(vdf["FAIRMARKETTOTAL"] >= MIN_FMV) & (vdf["FAIRMARKETTOTAL"] <= MAX_FMV)]
        print(f"  ARV filter (~${int(MIN_FMV/CLR):,}–${int(MAX_FMV/CLR):,}): {before:,} -> {len(vdf):,} records")
    else:
        vdf["FAIRMARKETTOTAL"] = 0
        vdf["OWNERDESC"]       = ""

    vdf["estimated_arv"] = (vdf["FAIRMARKETTOTAL"] / CLR).round(0).astype(int)

    # 4. Build output
    def col(name, default=""):
        return vdf.get(name, pd.Series([default] * len(vdf))).fillna(default)

    output = pd.DataFrame({
        "owner_name":       col("OWNERDESC"),
        "property_address": vdf["property_address"],
        "city":             vdf["city"],
        "zip":              vdf["zip"],
        "parcel_id":        col("parcel_id"),
        "assessed_fmv":     vdf["FAIRMARKETTOTAL"],
        "estimated_arv":    vdf["estimated_arv"],
        "property_class":   col("CLASSDESC"),
        "property_use":     col("USEDESC"),
        "latitude":         col("latitude"),
        "longitude":        col("longitude"),
        "neighborhood":     col("neighborhood"),
        "data_type":        "code_violation",
        "source":           "wprdc_pli_violations",
        "county":           "Allegheny",
        "date_pulled":      date.today().strftime("%Y-%m-%d"),
        "status":           col("status"),
        "violation_type":   col("case_file_type"),
        "raw_detail": (
            "Type: "      + col("case_file_type").astype(str) +
            " | Status: " + col("status").astype(str) +
            " | Case: "   + col("casefile_number").astype(str) +
            " | Date: "   + col("investigation_date").astype(str) +
            " | ARV est: $" + vdf["estimated_arv"].astype(str)
        ),
    })

    output = output.drop_duplicates(subset=["property_address", "parcel_id"])
    output = output.reset_index(drop=True)

    # 5. Save
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"allegheny_code_violations_{date.today().strftime('%Y%m%d')}.csv"
    filepath = os.path.join(OUTPUT_DIR, filename)
    output.to_csv(filepath, index=False)

    elapsed = time.time() - start
    print(f"\nDONE — {len(output):,} records | {filename} | {elapsed:.1f}s")
    print("Violation types pulled:")
    for vtype, cnt in output["violation_type"].value_counts().head(12).items():
        print(f"  {vtype}: {cnt:,}")
    print(f"\nNEXT: enrich_owners.py -> REI Sift dedup -> skip trace")
    return filepath


if __name__ == "__main__":
    run()

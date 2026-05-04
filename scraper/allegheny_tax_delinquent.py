"""
allegheny_tax_delinquent.py
We Buy Property LLC — Head of Data
Purpose: Pull Pittsburgh tax delinquent properties from WPRDC,
         JOIN with Allegheny County assessments to get mailing address + ARV filter,
         output skip-trace-ready CSV for PropStream import.

UPDATE FREQUENCY:
  Tax delinquent (WPRDC):  DAILY   (Pittsburgh Finance Dept push — confirmed 2026-04-21)
  Assessments (WPRDC):     MONTHLY (as of 1st of each month — ASOFDATE field)

ARV FILTER LOGIC:
  FAIRMARKETTOTAL = Allegheny County assessed fair market value
  Allegheny CLR (Common Level Ratio) ≈ 87.7%
  Formula: MIN_FMV = target_ARV × 0.877
    $150k ARV → MIN_FMV = 130,000  (default)
    $200k ARV → MIN_FMV = 175,000
    Adjust MIN_FMV / MAX_FMV below to change targeting.

OWNER NAME NOTE:
  WPRDC assessments do NOT include actual owner first/last name — only type codes
  (e.g. "REGULAR-ETUX OR ET VIR", "CORPORATION"). Owner names are resolved by
  PropStream at import. This script outputs property address + APN + mailing address
  which is sufficient for PropStream skip trace.

SKIP TRACE OUTPUT FORMAT (PropStream + BatchSkipTracing compatible):
  property_address, property_city, property_state, property_zip,
  mailing_address, mailing_city, mailing_state, mailing_zip,
  county, apn, fmv_assessed, total_delinq, bedrooms, year_built, ...

Run:      python allegheny_tax_delinquent.py
Schedule: Daily (WPRDC updates daily — stale data < 24hrs)
"""

import requests
import pandas as pd
import re
from datetime import datetime, date
import os
import time

# ─── CONFIG — ADJUST THESE TO CHANGE TARGETING ───────────────────────────────
MIN_FMV  = 40_000    # Loose floor — real ARV filtering done in PropStream after skip trace
MAX_FMV  = 600_000   # Assessed FMV ceiling → ~$683k ARV  (avoids luxury/commercial)
MIN_BEDS = 0         # No minimum — include all distressed residential
MIN_YEAR = 1900      # Exclude pre-1900 structures

# ─── WPRDC RESOURCE IDs (verified 2026-04-21) ────────────────────────────────
TAX_DELINQUENT_ID = "ed0d1550-c300-4114-865c-82dc7c23235b"   # Daily — 22k+ residential
ASSESSMENTS_ID    = "65855e14-549e-4992-b5be-d629afc676fa"   # Monthly — 584k parcels

WPRDC_BASE  = "https://data.wprdc.org/api/3/action"
WPRDC_SRCH  = f"{WPRDC_BASE}/datastore_search"
WPRDC_SQL   = f"{WPRDC_BASE}/datastore_search_sql"

OUTPUT_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
BATCH_SIZE  = 100    # Parcel IDs per POST request (avoids URL length limit)


# ─── MAILING ADDRESS PARSER ──────────────────────────────────────────────────
def parse_mailing(row) -> dict:
    """
    Parse Allegheny County CHANGENOTICE mailing address fields.
    Format quirk: city + state are in ADDRESS3 combined (e.g. 'PITTSBURGH PA')
    """
    def cl(f): return str(row.get(f,"") or "").strip()
    a1, a2, a3, a4 = cl("CHANGENOTICEADDRESS1"), cl("CHANGENOTICEADDRESS2"), \
                     cl("CHANGENOTICEADDRESS3"), cl("CHANGENOTICEADDRESS4")

    street = (a1 + (" " + a2 if a2 and a2 != a1 else "")).strip()
    city, state = "", "PA"
    if len(a3) >= 3:
        parts = a3.rsplit(None, 1)
        if len(parts) == 2 and len(parts[1]) == 2:
            city, state = parts[0].strip().title(), parts[1].upper()
        else:
            city = a3.strip().title()
    zip_ = re.sub(r"[^\d\-]", "", a4)[:10]
    return {"mailing_address": street, "mailing_city": city,
            "mailing_state": state,   "mailing_zip":  zip_}


# ─── STEP 1: PULL TAX DELINQUENT ─────────────────────────────────────────────
def fetch_tax_delinquent() -> pd.DataFrame:
    """Pull all residential tax delinquent properties (Pittsburgh). Updated daily."""
    print(f"[1/3] Pulling Pittsburgh tax delinquent (WPRDC — daily feed)...")
    r = requests.get(WPRDC_SRCH, params={
        "resource_id": TAX_DELINQUENT_ID,
        "filters":     '{"state_description":"Residential"}',
        "limit":       32000
    }, timeout=60)
    r.raise_for_status()
    records = r.json()["result"]["records"]
    df = pd.DataFrame(records)
    df["pin"] = df["pin"].str.strip()
    total_owed = (df["current_delq_tax"].fillna(0) + df["prior_delq_tax"].fillna(0)).sum()
    print(f"  {len(df):,} residential delinquent | Total pool: ${total_owed:,.0f}")
    return df


# ─── STEP 2: BATCH LOOKUP ASSESSMENTS ────────────────────────────────────────
def fetch_assessments_for_pins(pins: list) -> pd.DataFrame:
    """
    For each delinquent pin, look up its assessment record.
    Uses POST (not GET) to avoid URL length limits.
    Filters to ARV range + residential + min beds in same query.
    """
    print(f"[2/3] Looking up assessments for {len(pins):,} pins (batches of {BATCH_SIZE})...")
    matched = []

    for i in range(0, len(pins), BATCH_SIZE):
        batch = pins[i:i+BATCH_SIZE]
        in_clause = ", ".join(f"'{p}'" for p in batch)
        sql = (
            f'SELECT "PARID","PROPERTYHOUSENUM","PROPERTYADDRESS","PROPERTYCITY",'
            f'"PROPERTYSTATE","PROPERTYZIP","PROPERTYUNIT","CLASSDESC","USEDESC",'
            f'"CHANGENOTICEADDRESS1","CHANGENOTICEADDRESS2","CHANGENOTICEADDRESS3","CHANGENOTICEADDRESS4",'
            f'"FAIRMARKETTOTAL","BEDROOMS","YEARBLT","FINISHEDLIVINGAREA",'
            f'"CONDITIONDESC","HOMESTEADFLAG","SALEDATE","SALEPRICE"'
            f' FROM "{ASSESSMENTS_ID}"'
            f' WHERE "PARID" IN ({in_clause})'
            f' AND "FAIRMARKETTOTAL" >= {MIN_FMV}'
            f' AND "FAIRMARKETTOTAL" <= {MAX_FMV}'
            f' AND ("CLASSDESC" LIKE \'RESIDENTIAL%%\' OR "CLASSDESC" LIKE \'SINGLE%%\')'
        )
        try:
            r = requests.post(WPRDC_SQL, data={"sql": sql}, timeout=30)
            data = r.json()
            if data.get("success"):
                matched.extend(data["result"]["records"])
        except Exception as e:
            print(f"  Batch {i//BATCH_SIZE} error: {e}")

        if i % (BATCH_SIZE * 20) == 0 and i > 0:
            print(f"  {i:,}/{len(pins):,} processed — {len(matched):,} in ARV range so far")

    print(f"  {len(matched):,} properties pass ARV filter (${MIN_FMV:,}–${MAX_FMV:,})")
    return pd.DataFrame(matched) if matched else pd.DataFrame()


# ─── STEP 3: BUILD OUTPUT ─────────────────────────────────────────────────────
def build_output(td_df: pd.DataFrame, assess_df: pd.DataFrame) -> pd.DataFrame:
    """JOIN delinquent + assessments. Build PropStream skip trace CSV."""
    print(f"[3/3] Building skip trace output...")
    assess_df["PARID"] = assess_df["PARID"].str.strip()
    merged = td_df.merge(assess_df, left_on="pin", right_on="PARID", how="inner")
    print(f"  {len(merged):,} records after join")

    mail = merged.apply(parse_mailing, axis=1, result_type="expand")

    output = pd.DataFrame({
        # PropStream import needs property address + APN — it resolves owner name itself
        "property_address": (merged["PROPERTYHOUSENUM"].fillna("").astype(str).str.strip() + " " +
                             merged["PROPERTYADDRESS"].fillna("").astype(str).str.strip()).str.strip(),
        "property_city":    merged["PROPERTYCITY"].fillna("").str.strip().str.title(),
        "property_state":   merged["PROPERTYSTATE"].fillna("PA").str.strip().str.upper(),
        "property_zip":     merged["PROPERTYZIP"].fillna("").astype(str).str.strip().str[:5],
        "property_unit":    merged["PROPERTYUNIT"].fillna("").str.strip(),
        # Owner mailing address (for direct mail + skip trace enrichment)
        "mailing_address":  mail["mailing_address"],
        "mailing_city":     mail["mailing_city"],
        "mailing_state":    mail["mailing_state"],
        "mailing_zip":      mail["mailing_zip"],
        # Property identifiers
        "county":           "Allegheny",
        "apn":              merged["PARID"].str.strip(),
        # Property details (for QA + prioritization)
        "fmv_assessed":     pd.to_numeric(merged["FAIRMARKETTOTAL"], errors="coerce").fillna(0).astype(int),
        "est_arv":          (pd.to_numeric(merged["FAIRMARKETTOTAL"], errors="coerce").fillna(0) / 0.877).astype(int),
        "bedrooms":         pd.to_numeric(merged["BEDROOMS"], errors="coerce").fillna(0).astype(int),
        "year_built":       merged["YEARBLT"].fillna("").astype(str).str[:4],
        "sqft":             pd.to_numeric(merged["FINISHEDLIVINGAREA"], errors="coerce").fillna(0).astype(int),
        "condition":        merged["CONDITIONDESC"].fillna(""),
        "homestead":        merged["HOMESTEADFLAG"].fillna(""),   # Y = owner occupied
        "use_desc":         merged["USEDESC"].fillna(""),
        # Delinquency data
        "data_type":        "tax_delinquent",
        "current_delq":     pd.to_numeric(merged["current_delq_tax"], errors="coerce").fillna(0).round(2),
        "prior_delq":       pd.to_numeric(merged["prior_delq_tax"], errors="coerce").fillna(0).round(2),
        "total_delinq":     (pd.to_numeric(merged["current_delq_tax"], errors="coerce").fillna(0) +
                             pd.to_numeric(merged["prior_delq_tax"], errors="coerce").fillna(0)).round(2),
        "neighborhood":     merged["neighborhood"].fillna(""),
        "billing_city":     merged["billing_city"].fillna(""),   # quick owner location check
        # Pipeline
        "source":           "WPRDC_TaxDelinquent+Assessments",
        "date_pulled":      date.today().strftime("%Y-%m-%d"),
    })

    output = output[output["property_address"].str.len() > 3].drop_duplicates(subset=["apn"])
    return output.sort_values("total_delinq", ascending=False)


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    start = time.time()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print("WBP — Tax Delinquent Skip Trace Pull")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"ARV target: ~${int(MIN_FMV/0.877):,} – ~${int(MAX_FMV/0.877):,}")
    print(f"Assessed FMV filter: ${MIN_FMV:,} – ${MAX_FMV:,}")
    print("=" * 60)

    td_df     = fetch_tax_delinquent()
    assess_df = fetch_assessments_for_pins(td_df["pin"].tolist())

    if assess_df.empty:
        print("\n⚠ No assessment matches found. Check filters.")
        return

    output = build_output(td_df, assess_df)

    fname = f"allegheny_tax_delinquent_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(OUTPUT_DIR, fname)
    output.to_csv(fpath, index=False)

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"✅ {len(output):,} records saved → {fname}")
    print(f"   Est. ARV range: ${output['est_arv'].min():,} – ${output['est_arv'].max():,}")
    print(f"   Avg delinquency: ${output['total_delinq'].mean():,.0f}")
    print(f"   Elapsed: {elapsed:.1f}s")
    print(f"\n📋 NEXT STEPS (per SOP):")
    print(f"   1. QA spot-check {fname} (5–10 rows)")
    print(f"   2. PropStream → Import Properties → 'Tax Delinquent Allegheny {date.today().strftime('%Y-%m')}'")
    print(f"   3. Select all → Skip Trace (free on Pro)")
    print(f"   4. Export → merge phones → GHL import")
    print(f"   5. GHL tag: tax-delinquent-allegheny-{date.today().strftime('%Y%m')}")
    print(f"   6. Update DATA_outbox.md with run summary")
    return output


if __name__ == "__main__":
    main()

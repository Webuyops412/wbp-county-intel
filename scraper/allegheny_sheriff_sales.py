"""
allegheny_sheriff_sales.py
We Buy Property LLC — Head of Data
Purpose: Pull Allegheny County Sheriff Sales (mortgage foreclosures scheduled for auction)
         from WPRDC. These are properties already in the foreclosure pipeline — high distress,
         bank as plaintiff, owner as defendant. Cross-ref assessments for ARV + mailing address.

Fields: SaleNumber, DocketNumber, ID, Plaintiff, Defendant, SaleType, SaleDate, SaleStatus,
        Street, City, State, ZIPCode, Municipality, CostsTaxes, latitude, longitude

UPDATE FREQUENCY: Monthly (new sales posted ~30 days before auction date)
DATA SOURCE: WPRDC — Allegheny County Sheriff Sales (Current Bid List)
Resource ID: 21b22189-2aa4-419f-a9e9-c518bbe25fe0

Run:      python allegheny_sheriff_sales.py
Schedule: Daily via GitHub Actions (catches new postings)
"""

import requests
import pandas as pd
import json
import argparse
from datetime import datetime, date
import os
import time

# ─── CONFIG ──────────────────────────────────────────────────────────────────
MIN_FMV   = 87_700    # ~$100k ARV (Allegheny CLR 87.7%)
MAX_FMV   = 600_000   # ~$683k ARV ceiling

# ─── WPRDC ───────────────────────────────────────────────────────────────────
SHERIFF_RESOURCE_ID  = "21b22189-2aa4-419f-a9e9-c518bbe25fe0"   # Current Bid List
ASSESSMENTS_ID       = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_BASE           = "https://data.wprdc.org/api/3/action"
WPRDC_SRCH           = f"{WPRDC_BASE}/datastore_search"
WPRDC_SQL            = f"{WPRDC_BASE}/datastore_search_sql"
BATCH_SIZE           = 100

# Statuses to EXCLUDE (already resolved)
SKIP_STATUSES = {"SOLD", "WITHDRAWN", "CONTINUED", "STAYED", "BANKRUPTCY"}


def fetch_sheriff_sales() -> pd.DataFrame:
    print("[1/3] Pulling Allegheny County Sheriff Sales (Current Bid List)...")
    all_records, offset = [], 0
    while True:
        r = requests.get(WPRDC_SRCH, params={
            "resource_id": SHERIFF_RESOURCE_ID,
            "limit": 5000,
            "offset": offset
        }, timeout=60)
        r.raise_for_status()
        result = r.json()["result"]
        records = result["records"]
        if not records:
            break
        all_records.extend(records)
        total = result.get("total", 0)
        if offset + len(records) >= total:
            break
        offset += len(records)

    df = pd.DataFrame(all_records)
    print(f"  Raw records: {len(df):,}")

    # Filter out already-resolved sales
    if "SaleStatus" in df.columns:
        df = df[~df["SaleStatus"].str.upper().str.strip().isin(SKIP_STATUSES)]
        print(f"  After status filter (removing sold/withdrawn): {len(df):,}")

    # Keep only mortgage foreclosures (primary interest)
    if "SaleType" in df.columns:
        mort = df[df["SaleType"].str.contains("Mortgage", case=False, na=False)]
        print(f"  Mortgage foreclosures: {len(mort):,} | Other types: {len(df)-len(mort):,}")
        df = df  # keep all types — tax foreclosures are also valuable

    return df


def fetch_assessments_for_ids(assessment_ids: list, cache: dict) -> pd.DataFrame:
    """Fetch assessment data by the WPRDC internal ID field (matches Sheriff Sales 'ID' column)."""
    print(f"[2/3] Assessment lookup for {len(assessment_ids):,} properties...")

    # Filter out already cached
    known = set(str(i) for i in cache.keys())
    new_ids = [i for i in assessment_ids if str(i) not in known]
    print(f"  Cache hit: {len(assessment_ids)-len(new_ids):,} | Net-new: {len(new_ids):,}")

    new_records = []
    for i in range(0, len(new_ids), BATCH_SIZE):
        batch = new_ids[i:i + BATCH_SIZE]
        in_clause = ", ".join(f"'{x}'" for x in batch)
        sql = (
            f'SELECT "PARID","PROPERTYHOUSENUM","PROPERTYADDRESS","PROPERTYCITY",'
            f'"PROPERTYSTATE","PROPERTYZIP","CLASSDESC","USEDESC",'
            f'"CHANGENOTICEADDRESS1","CHANGENOTICEADDRESS2","CHANGENOTICEADDRESS3","CHANGENOTICEADDRESS4",'
            f'"FAIRMARKETTOTAL","BEDROOMS","YEARBLT","FINISHEDLIVINGAREA","OWNERDESC",'
            f'"CONDITIONDESC","HOMESTEADFLAG","_id"'
            f' FROM "{ASSESSMENTS_ID}"'
            f' WHERE "_id" IN ({in_clause})'
            f' AND "FAIRMARKETTOTAL" >= {MIN_FMV}'
            f' AND "FAIRMARKETTOTAL" <= {MAX_FMV}'
        )
        try:
            r = requests.post(WPRDC_SQL, data={"sql": sql}, timeout=30)
            data = r.json()
            if data.get("success"):
                batch_recs = data["result"]["records"]
                new_records.extend(batch_recs)
                for rec in batch_recs:
                    cache[str(rec["_id"])] = rec
        except Exception as e:
            print(f"  Batch {i//BATCH_SIZE} error: {e}")

    # Combine cached + new
    cached_records = [
        v for k, v in cache.items()
        if k in set(str(i) for i in assessment_ids)
        and MIN_FMV <= float(v.get("FAIRMARKETTOTAL", 0) or 0) <= MAX_FMV
    ]
    all_recs = new_records + [r for r in cached_records if r not in new_records]
    print(f"  {len(all_recs):,} assessment records passing ARV filter")
    return pd.DataFrame(all_recs) if all_recs else pd.DataFrame(), cache


def build_output(sheriff_df: pd.DataFrame, assess_df: pd.DataFrame) -> pd.DataFrame:
    print("[3/3] Building output...")

    if assess_df.empty:
        # Fall back: use address data directly from sheriff sales (no ARV enrichment)
        print("  No assessment data — using sheriff sales address data directly")
        output = pd.DataFrame({
            "property_address": sheriff_df["Street"].fillna("").str.strip().str.title(),
            "property_city":    sheriff_df["City"].fillna("").str.strip().str.title(),
            "property_state":   sheriff_df["State"].fillna("PA").str.strip().str.upper(),
            "property_zip":     sheriff_df["ZIPCode"].fillna("").astype(str).str.strip().str[:5],
            "mailing_address":  "",
            "mailing_city":     "",
            "mailing_state":    "",
            "mailing_zip":      "",
            "county":           "Allegheny",
            "apn":              "",
            "fmv_assessed":     0,
            "est_arv":          0,
            "bedrooms":         0,
            "year_built":       "",
            "sqft":             0,
            "condition":        "",
            "homestead":        "",
            "data_type":        "sheriff_sale",
            "plaintiff":        sheriff_df["Plaintiff"].fillna("").str.strip(),
            "defendant":        sheriff_df["Defendant"].fillna("").str.strip(),
            "sale_date":        sheriff_df["SaleDate"].fillna("").astype(str).str[:10],
            "sale_status":      sheriff_df["SaleStatus"].fillna("").str.strip(),
            "sale_number":      sheriff_df["SaleNumber"].fillna("").astype(str),
            "docket":           sheriff_df["DocketNumber"].fillna("").astype(str),
            "municipality":     sheriff_df.get("Municipality", pd.Series([""] * len(sheriff_df))).fillna(""),
            "costs_taxes":      pd.to_numeric(sheriff_df.get("CostsTaxes", 0), errors="coerce").fillna(0).round(2),
            "source":           "WPRDC_SheriffSales",
            "date_pulled":      date.today().isoformat(),
            "score":            80,   # Sheriff sale = already in foreclosure = very high distress
            "flags":            "Sheriff Sale, Mortgage Foreclosure",
        })
    else:
        # Join on ID field
        assess_df["_id_str"] = assess_df["_id"].astype(str)
        sheriff_df["ID_str"] = sheriff_df["ID"].astype(str)
        merged = sheriff_df.merge(assess_df, left_on="ID_str", right_on="_id_str", how="left")
        print(f"  {merged['PARID'].notna().sum():,}/{len(merged):,} matched to assessments")

        def parse_mailing(row):
            def cl(f): return str(row.get(f, "") or "").strip()
            a1, a2, a3, a4 = cl("CHANGENOTICEADDRESS1"), cl("CHANGENOTICEADDRESS2"), cl("CHANGENOTICEADDRESS3"), cl("CHANGENOTICEADDRESS4")
            street = (a1 + (" " + a2 if a2 and a2 != a1 else "")).strip()
            city, state = "", "PA"
            if len(a3) >= 3:
                parts = a3.rsplit(None, 1)
                if len(parts) == 2 and len(parts[1]) == 2:
                    city, state = parts[0].strip().title(), parts[1].upper()
                else:
                    city = a3.strip().title()
            import re
            zip_ = re.sub(r"[^\d\-]", "", a4)[:10]
            return street, city, state, zip_

        mailing = merged.apply(lambda r: parse_mailing(r), axis=1, result_type="expand")
        mailing.columns = ["mailing_address", "mailing_city", "mailing_state", "mailing_zip"]

        fmv = pd.to_numeric(merged.get("FAIRMARKETTOTAL"), errors="coerce").fillna(0)
        output = pd.DataFrame({
            "property_address": merged["Street"].fillna("").str.strip().str.title(),
            "property_city":    merged["City"].fillna("").str.strip().str.title(),
            "property_state":   merged["State"].fillna("PA").str.strip().str.upper(),
            "property_zip":     merged["ZIPCode"].fillna("").astype(str).str.strip().str[:5],
            "mailing_address":  mailing["mailing_address"],
            "mailing_city":     mailing["mailing_city"],
            "mailing_state":    mailing["mailing_state"],
            "mailing_zip":      mailing["mailing_zip"],
            "county":           "Allegheny",
            "apn":              merged.get("PARID", pd.Series([""] * len(merged))).fillna("").astype(str).str.strip(),
            "fmv_assessed":     fmv.astype(int),
            "est_arv":          (fmv / 0.877).astype(int),
            "bedrooms":         pd.to_numeric(merged.get("BEDROOMS"), errors="coerce").fillna(0).astype(int),
            "year_built":       merged.get("YEARBLT", pd.Series([""] * len(merged))).fillna("").astype(str).str[:4],
            "sqft":             pd.to_numeric(merged.get("FINISHEDLIVINGAREA"), errors="coerce").fillna(0).astype(int),
            "condition":        merged.get("CONDITIONDESC", pd.Series([""] * len(merged))).fillna(""),
            "homestead":        merged.get("HOMESTEADFLAG", pd.Series([""] * len(merged))).fillna(""),
            "data_type":        "sheriff_sale",
            "plaintiff":        merged["Plaintiff"].fillna("").str.strip(),
            "defendant":        merged["Defendant"].fillna("").str.strip(),
            "sale_date":        merged["SaleDate"].fillna("").astype(str).str[:10],
            "sale_status":      merged["SaleStatus"].fillna("").str.strip(),
            "sale_number":      merged["SaleNumber"].fillna("").astype(str),
            "docket":           merged["DocketNumber"].fillna("").astype(str),
            "municipality":     merged.get("Municipality", pd.Series([""] * len(merged))).fillna(""),
            "costs_taxes":      pd.to_numeric(merged.get("CostsTaxes", 0), errors="coerce").fillna(0).round(2),
            "source":           "WPRDC_SheriffSales+Assessments",
            "date_pulled":      date.today().isoformat(),
            "score":            0,
            "flags":            "",
        })

        # Scoring: Sheriff sales are very high distress — base 80
        scores, flags_list = [], []
        for _, row in output.iterrows():
            score = 80
            flags = ["Sheriff Sale"]
            sale_type_raw = str(merged.loc[row.name, "SaleType"] if "SaleType" in merged.columns else "")
            if "Mortgage" in sale_type_raw:
                flags.append("Mortgage Foreclosure")
            if row["fmv_assessed"] > 0:
                arv = row["est_arv"]
                if arv > 300_000:
                    score = min(score + 10, 100)
                    flags.append(f"High Equity (${arv:,} ARV)")
                elif arv > 150_000:
                    score = min(score + 5, 100)
                    flags.append(f"Sweet Spot ARV (${arv:,})")
            mail_state = str(row.get("mailing_state", "") or "").strip().upper()
            mail_city  = str(row.get("mailing_city", "") or "").strip().lower()
            prop_city  = str(row.get("property_city", "") or "").strip().lower()
            if mail_state not in ("", "PA") or (mail_city and mail_city != prop_city):
                score = min(score + 5, 100)
                flags.append("Absentee Owner")
            scores.append(score)
            flags_list.append(", ".join(flags))
        output["score"] = scores
        output["flags"] = flags_list

    output = output[output["property_address"].str.len() > 3]
    output = output.drop_duplicates(subset=["sale_number"]) if "sale_number" in output.columns else output
    return output.sort_values("score", ascending=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir",    default=os.path.join(os.path.dirname(__file__), "..", "output"))
    parser.add_argument("--dashboard-dir", default=os.path.join(os.path.dirname(__file__), "..", "dashboard"))
    parser.add_argument("--lookback-days", type=int, default=30)  # accepted, not used (snapshot data)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    cache_path    = os.path.join(os.path.dirname(output_dir), "data", "sheriff_assess_cache.json")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    # Load assessment cache
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f).get("records", {})
            print(f"  Cache loaded: {len(cache):,} records")
        except Exception:
            pass

    start = time.time()
    print("=" * 60)
    print("WBP — Allegheny Sheriff Sales Scraper")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    sheriff_df  = fetch_sheriff_sales()
    assess_ids  = sheriff_df["ID"].dropna().astype(str).tolist() if "ID" in sheriff_df.columns else []
    assess_df, cache = fetch_assessments_for_ids(assess_ids, cache)

    # Save cache
    if cache:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"records": cache}, f)

    output = build_output(sheriff_df, assess_df)

    fname = f"allegheny_sheriff_sales_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json:
        records = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source": "WPRDC Sheriff Sales",
            "total": len(records),
            "records": records,
        }
        for path in [os.path.join(dashboard_dir, "sheriff_sales.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "sheriff_sales.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output):,} sheriff sale properties → {fname}")
    print(f"  Avg score: {output['score'].mean():.1f}")
    print(f"  Elapsed: {elapsed:.1f}s")

    global _records_written
    _records_written = len(output)
    return output


_records_written = 0

if __name__ == "__main__":
    main()

"""
allegheny_foreclosures.py
We Buy Property LLC — Head of Data
Purpose: Pull Allegheny County Mortgage Foreclosure filings (lis pendens) from WPRDC.
         These are properties the moment a lender files for foreclosure — EARLIER signal
         than sheriff sales. Many will never reach auction if owner sells first.

Fields: pin, block_lot, filing_date, case_id, municipality, ward, docket_type, amount,
        plaintiff, last_activity

UPDATE FREQUENCY: Near-real-time (new filings added as recorded at courthouse)
DATA SOURCE: WPRDC — Allegheny County Mortgage Foreclosure Records
Resource ID: 859bccfd-0e12-4161-a348-313d734f25fd

STRATEGY:
  - Pull all records, filter to recent filings (default: last 180 days)
  - Cross-ref assessment data by PARID (pin field) for owner + address + ARV
  - Score based on amount owed, recency, absentee owner status

Run:      python allegheny_foreclosures.py
          python allegheny_foreclosures.py --lookback-days 365
Schedule: Daily via GitHub Actions
"""

import requests
import pandas as pd
import json
import re
import argparse
from datetime import datetime, date, timedelta
import os
import time

# ─── CONFIG ──────────────────────────────────────────────────────────────────
MIN_FMV        = 87_700     # ~$100k ARV
MAX_FMV        = 600_000
DEFAULT_LOOKBACK = 180      # Days of filing history to keep
BATCH_SIZE     = 100

# ─── WPRDC ───────────────────────────────────────────────────────────────────
FORECLOSURE_RESOURCE_ID = "859bccfd-0e12-4161-a348-313d734f25fd"
ASSESSMENTS_ID          = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_BASE              = "https://data.wprdc.org/api/3/action"
WPRDC_SRCH              = f"{WPRDC_BASE}/datastore_search"
WPRDC_SQL               = f"{WPRDC_BASE}/datastore_search_sql"

# Docket types that indicate ACTIVE foreclosure (not resolved)
ACTIVE_DOCKETS = {
    "Praecipe for Writ of Execution",
    "Praecipe to Substitute",
    "Notice of Foreclosure",
    "Complaint",
    "Lis Pendens",
    "Writ of Execution",
}


def normalize_pin(pin: str) -> str:
    """Normalize WPRDC foreclosure pin to match assessment PARID format."""
    # Foreclosure pins: '1222S00215000000' — assessment PARIDs same format
    return str(pin).strip().upper()


def fetch_foreclosures(lookback_days: int) -> pd.DataFrame:
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    print(f"[1/3] Pulling Allegheny foreclosure filings (last {lookback_days} days, since {cutoff})...")

    all_records, offset = [], 0
    while True:
        r = requests.get(WPRDC_SRCH, params={
            "resource_id": FORECLOSURE_RESOURCE_ID,
            "limit": 5000,
            "offset": offset,
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
        if offset % 20000 == 0:
            print(f"  {offset:,}/{total:,} fetched...")

    df = pd.DataFrame(all_records)
    print(f"  Total filings in dataset: {len(df):,}")

    # Filter to lookback window
    if "filing_date" in df.columns:
        df["filing_date"] = pd.to_datetime(df["filing_date"], errors="coerce")
        df = df[df["filing_date"] >= pd.Timestamp(cutoff)]
        print(f"  After date filter (>{cutoff}): {len(df):,}")

    # Deduplicate by pin — keep most recent filing per property
    if "pin" in df.columns:
        df["pin"] = df["pin"].apply(normalize_pin)
        df = df.sort_values("filing_date", ascending=False)
        df = df.drop_duplicates(subset=["pin"], keep="first")
        print(f"  After dedup by pin (most recent filing): {len(df):,}")

    return df


def fetch_assessments_for_pins(pins: list, cache: dict) -> tuple:
    print(f"[2/3] Assessment lookup for {len(pins):,} pins...")
    known = set(cache.keys())
    new_pins = [p for p in pins if p not in known]
    print(f"  Cache hit: {len(pins)-len(new_pins):,} | Net-new: {len(new_pins):,}")

    new_records = []
    for i in range(0, len(new_pins), BATCH_SIZE):
        batch = new_pins[i:i + BATCH_SIZE]
        in_clause = ", ".join(f"'{p}'" for p in batch)
        sql = (
            f'SELECT "PARID","PROPERTYHOUSENUM","PROPERTYADDRESS","PROPERTYCITY",'
            f'"PROPERTYSTATE","PROPERTYZIP","OWNERDESC",'
            f'"CHANGENOTICEADDRESS1","CHANGENOTICEADDRESS2","CHANGENOTICEADDRESS3","CHANGENOTICEADDRESS4",'
            f'"FAIRMARKETTOTAL","BEDROOMS","YEARBLT","FINISHEDLIVINGAREA",'
            f'"CONDITIONDESC","HOMESTEADFLAG","CLASSDESC"'
            f' FROM "{ASSESSMENTS_ID}"'
            f' WHERE "PARID" IN ({in_clause})'
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
                    cache[rec["PARID"].strip()] = rec
        except Exception as e:
            print(f"  Batch {i//BATCH_SIZE} error: {e}")

    cached_recs = [
        v for k, v in cache.items()
        if k in set(pins)
        and MIN_FMV <= float(v.get("FAIRMARKETTOTAL", 0) or 0) <= MAX_FMV
    ]
    all_recs = new_records + [r for r in cached_recs if r not in new_records]
    print(f"  {len(all_recs):,} passing ARV filter")
    return pd.DataFrame(all_recs) if all_recs else pd.DataFrame(), cache


def parse_mailing(row) -> tuple:
    def cl(f): return str(row.get(f, "") or "").strip()
    a1, a2 = cl("CHANGENOTICEADDRESS1"), cl("CHANGENOTICEADDRESS2")
    a3, a4 = cl("CHANGENOTICEADDRESS3"), cl("CHANGENOTICEADDRESS4")
    street = (a1 + (" " + a2 if a2 and a2 != a1 else "")).strip()
    city, state = "", "PA"
    if len(a3) >= 3:
        parts = a3.rsplit(None, 1)
        if len(parts) == 2 and len(parts[1]) == 2:
            city, state = parts[0].strip().title(), parts[1].upper()
        else:
            city = a3.strip().title()
    zip_ = re.sub(r"[^\d\-]", "", a4)[:10]
    return street, city, state, zip_


def build_output(fc_df: pd.DataFrame, assess_df: pd.DataFrame) -> pd.DataFrame:
    print("[3/3] Building scored output...")

    if assess_df.empty:
        print("  WARNING: No assessment data — limited enrichment")
        output = pd.DataFrame({
            "property_address": "",
            "property_city":    fc_df.get("municipality", pd.Series([""] * len(fc_df))).fillna("").str.title(),
            "property_state":   "PA",
            "property_zip":     "",
            "mailing_address":  "",
            "mailing_city":     "",
            "mailing_state":    "",
            "mailing_zip":      "",
            "county":           "Allegheny",
            "apn":              fc_df["pin"].fillna(""),
            "fmv_assessed":     0,
            "est_arv":          0,
            "bedrooms":         0,
            "year_built":       "",
            "sqft":             0,
            "condition":        "",
            "homestead":        "",
            "data_type":        "foreclosure_filing",
            "plaintiff":        fc_df.get("plaintiff", pd.Series([""] * len(fc_df))).fillna(""),
            "case_id":          fc_df.get("case_id", pd.Series([""] * len(fc_df))).fillna(""),
            "docket_type":      fc_df.get("docket_type", pd.Series([""] * len(fc_df))).fillna(""),
            "filing_date":      fc_df.get("filing_date", pd.Series([None] * len(fc_df))).dt.strftime("%Y-%m-%d").fillna(""),
            "amount_owed":      pd.to_numeric(fc_df.get("amount", 0), errors="coerce").fillna(0).round(2),
            "municipality":     fc_df.get("municipality", pd.Series([""] * len(fc_df))).fillna(""),
            "source":           "WPRDC_ForeclosureFilings",
            "date_pulled":      date.today().isoformat(),
            "score":            70,
            "flags":            "Foreclosure Filing",
        })
    else:
        assess_df = assess_df.copy()
        assess_df["PARID"] = assess_df["PARID"].str.strip()
        merged = fc_df.merge(assess_df, left_on="pin", right_on="PARID", how="inner")
        print(f"  {len(merged):,}/{len(fc_df):,} pins matched to assessments")

        mail_data = merged.apply(parse_mailing, axis=1, result_type="expand")
        mail_data.columns = ["mailing_address", "mailing_city", "mailing_state", "mailing_zip"]
        fmv = pd.to_numeric(merged["FAIRMARKETTOTAL"], errors="coerce").fillna(0)

        output = pd.DataFrame({
            "property_address": (merged["PROPERTYHOUSENUM"].fillna("").astype(str).str.strip() + " " +
                                 merged["PROPERTYADDRESS"].fillna("").astype(str).str.strip()).str.strip(),
            "property_city":    merged["PROPERTYCITY"].fillna("").str.strip().str.title(),
            "property_state":   merged["PROPERTYSTATE"].fillna("PA").str.strip().str.upper(),
            "property_zip":     merged["PROPERTYZIP"].fillna("").astype(str).str.strip().str[:5],
            "mailing_address":  mail_data["mailing_address"],
            "mailing_city":     mail_data["mailing_city"],
            "mailing_state":    mail_data["mailing_state"],
            "mailing_zip":      mail_data["mailing_zip"],
            "county":           "Allegheny",
            "apn":              merged["PARID"].str.strip(),
            "fmv_assessed":     fmv.astype(int),
            "est_arv":          (fmv / 0.877).astype(int),
            "bedrooms":         pd.to_numeric(merged.get("BEDROOMS"), errors="coerce").fillna(0).astype(int),
            "year_built":       merged.get("YEARBLT", pd.Series([""] * len(merged))).fillna("").astype(str).str[:4],
            "sqft":             pd.to_numeric(merged.get("FINISHEDLIVINGAREA"), errors="coerce").fillna(0).astype(int),
            "condition":        merged.get("CONDITIONDESC", pd.Series([""] * len(merged))).fillna(""),
            "homestead":        merged.get("HOMESTEADFLAG", pd.Series([""] * len(merged))).fillna(""),
            "data_type":        "foreclosure_filing",
            "plaintiff":        merged.get("plaintiff", pd.Series([""] * len(merged))).fillna(""),
            "case_id":          merged.get("case_id", pd.Series([""] * len(merged))).fillna(""),
            "docket_type":      merged.get("docket_type", pd.Series([""] * len(merged))).fillna(""),
            "filing_date":      merged["filing_date"].dt.strftime("%Y-%m-%d").fillna(""),
            "amount_owed":      pd.to_numeric(merged.get("amount", 0), errors="coerce").fillna(0).round(2),
            "municipality":     merged.get("municipality", pd.Series([""] * len(merged))).fillna(""),
            "source":           "WPRDC_ForeclosureFilings+Assessments",
            "date_pulled":      date.today().isoformat(),
            "score":            0,
            "flags":            "",
        })

        # Scoring: base 70 (foreclosure filing = strong distress signal)
        scores, flags_list = [], []
        for _, row in output.iterrows():
            score = 70
            flags = ["Foreclosure Filing"]

            amount = float(row.get("amount_owed", 0) or 0)
            if amount > 100_000:
                score = min(score + 10, 100)
                flags.append(f"High Debt (${amount:,.0f})")
            elif amount > 50_000:
                score = min(score + 5, 100)
                flags.append(f"Debt >${50_000:,}")

            arv = int(row.get("est_arv", 0) or 0)
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
                score = min(score + 10, 100)
                flags.append("Absentee Owner")

            # Recency bonus — filed in last 30 days
            try:
                fd = pd.to_datetime(row.get("filing_date", ""))
                if (date.today() - fd.date()).days <= 30:
                    score = min(score + 5, 100)
                    flags.append("Recent Filing (<30d)")
            except Exception:
                pass

            scores.append(score)
            flags_list.append(", ".join(flags))

        output["score"] = scores
        output["flags"] = flags_list

    output = output[output["apn"].str.len() > 3]
    output = output.drop_duplicates(subset=["apn"])
    return output.sort_values("score", ascending=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir",    default=os.path.join(os.path.dirname(__file__), "..", "output"))
    parser.add_argument("--dashboard-dir", default=os.path.join(os.path.dirname(__file__), "..", "dashboard"))
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    cache_path    = os.path.join(os.path.dirname(output_dir), "data", "foreclosure_assess_cache.json")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f).get("records", {})
            print(f"  Cache: {len(cache):,} records")
        except Exception:
            pass

    start = time.time()
    print("=" * 60)
    print("WBP — Allegheny Mortgage Foreclosure Scraper")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Lookback: {args.lookback_days} days")
    print("=" * 60)

    fc_df = fetch_foreclosures(args.lookback_days)
    pins  = fc_df["pin"].dropna().tolist() if "pin" in fc_df.columns else []
    assess_df, cache = fetch_assessments_for_pins(pins, cache)

    if cache:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"records": cache}, f)

    output = build_output(fc_df, assess_df)

    fname = f"allegheny_foreclosures_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json:
        records = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source": "WPRDC Foreclosure Filings",
            "total": len(records),
            "records": records,
        }
        for path in [os.path.join(dashboard_dir, "foreclosures.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "foreclosures.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    hot = output[output["score"] >= 70]
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output):,} foreclosure records → {fname}")
    print(f"  Hot leads (score >=70): {len(hot):,}")
    print(f"  Avg score: {output['score'].mean():.1f}")
    print(f"  Elapsed: {elapsed:.1f}s")

    global _records_written
    _records_written = len(output)
    return output


_records_written = 0

if __name__ == "__main__":
    main()

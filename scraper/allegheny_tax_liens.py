"""
allegheny_tax_liens.py
We Buy Property LLC — Head of Data
Purpose: Pull Allegheny County active tax liens from WPRDC.
         Tax liens = government has priority claim on property for unpaid taxes.
         Multiple liens + high amounts = severe financial distress.
         Cross-ref with assessments for full owner + address + ARV enrichment.

Fields: pin, number (lien count), total_amount

UPDATE FREQUENCY: Near-real-time (as filed at courthouse)
DATA SOURCE: WPRDC — Allegheny County Tax Liens (Summary)
Resource ID: d1e80180-5b2e-4dab-8ec3-be621628649e

STRATEGY:
  - Filter: total_amount >= MIN_LIEN_AMOUNT (default $3,000)
  - Multiple liens (number > 1) = escalating distress signal
  - Cross-ref assessments by PARID (pin) for full record enrichment

Run:      python allegheny_tax_liens.py
Schedule: Daily via GitHub Actions
"""

import requests
import pandas as pd
import json
import re
import argparse
from datetime import datetime, date
import os
import time

# ─── CONFIG ──────────────────────────────────────────────────────────────────
MIN_LIEN_AMOUNT = 3_000     # Ignore trivial liens
MIN_FMV         = 87_700    # ~$100k ARV
MAX_FMV         = 600_000
BATCH_SIZE      = 100

# ─── WPRDC ───────────────────────────────────────────────────────────────────
TAX_LIEN_RESOURCE_ID = "d1e80180-5b2e-4dab-8ec3-be621628649e"
ASSESSMENTS_ID       = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_BASE           = "https://data.wprdc.org/api/3/action"
WPRDC_SRCH           = f"{WPRDC_BASE}/datastore_search"
WPRDC_SQL            = f"{WPRDC_BASE}/datastore_search_sql"


def fetch_tax_liens(min_amount: float) -> pd.DataFrame:
    print(f"[1/3] Pulling Allegheny County tax liens (amount >= ${min_amount:,.0f})...")
    all_records, offset = [], 0
    while True:
        r = requests.get(WPRDC_SRCH, params={
            "resource_id": TAX_LIEN_RESOURCE_ID,
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
            print(f"  {offset:,}/{total:,}...")

    df = pd.DataFrame(all_records)
    print(f"  Raw records: {len(df):,}")

    # Normalize and filter
    df["pin"] = df["pin"].astype(str).str.strip()
    df["total_amount"] = pd.to_numeric(df["total_amount"], errors="coerce").fillna(0)
    df["number"] = pd.to_numeric(df["number"], errors="coerce").fillna(0).astype(int)

    df = df[df["total_amount"] >= min_amount]
    print(f"  After amount filter (>=${min_amount:,.0f}): {len(df):,}")

    # Sort by severity — most owed first
    df = df.sort_values("total_amount", ascending=False)
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


def build_output(lien_df: pd.DataFrame, assess_df: pd.DataFrame) -> pd.DataFrame:
    print("[3/3] Building scored output...")

    if assess_df.empty:
        print("  WARNING: No assessment data")
        output = pd.DataFrame({
            "property_address": "",
            "property_city":    "",
            "property_state":   "PA",
            "property_zip":     "",
            "mailing_address":  "",
            "mailing_city":     "",
            "mailing_state":    "",
            "mailing_zip":      "",
            "county":           "Allegheny",
            "apn":              lien_df["pin"],
            "fmv_assessed":     0,
            "est_arv":          0,
            "bedrooms":         0,
            "year_built":       "",
            "sqft":             0,
            "condition":        "",
            "homestead":        "",
            "data_type":        "tax_lien",
            "lien_count":       lien_df["number"],
            "lien_total":       lien_df["total_amount"],
            "source":           "WPRDC_TaxLiens",
            "date_pulled":      date.today().isoformat(),
            "score":            60,
            "flags":            "Tax Lien",
        })
    else:
        assess_df = assess_df.copy()
        assess_df["PARID"] = assess_df["PARID"].str.strip()
        merged = lien_df.merge(assess_df, left_on="pin", right_on="PARID", how="inner")
        print(f"  {len(merged):,}/{len(lien_df):,} pins matched to assessments")

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
            "data_type":        "tax_lien",
            "lien_count":       merged["number"].astype(int),
            "lien_total":       merged["total_amount"].round(2),
            "source":           "WPRDC_TaxLiens+Assessments",
            "date_pulled":      date.today().isoformat(),
            "score":            0,
            "flags":            "",
        })

        # Scoring: base 60 (lien = financial distress but less urgent than foreclosure/sheriff)
        scores, flags_list = [], []
        for _, row in output.iterrows():
            score = 60
            flags = ["Tax Lien"]

            lien_total = float(row.get("lien_total", 0) or 0)
            lien_count = int(row.get("lien_count", 0) or 0)

            if lien_total > 20_000:
                score = min(score + 20, 100)
                flags.append(f"Severe Liens (${lien_total:,.0f})")
            elif lien_total > 10_000:
                score = min(score + 12, 100)
                flags.append(f"High Liens (${lien_total:,.0f})")
            elif lien_total > 5_000:
                score = min(score + 6, 100)
                flags.append(f"Liens >${5_000:,}")

            if lien_count >= 5:
                score = min(score + 10, 100)
                flags.append(f"Multiple Liens ({lien_count})")
            elif lien_count >= 3:
                score = min(score + 5, 100)
                flags.append(f"{lien_count} Liens")

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

            homestead = str(row.get("homestead", "") or "").strip().upper()
            if homestead in ("", "N", "NO", "0"):
                score = min(score + 5, 100)
                flags.append("No Homestead")

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
    parser.add_argument("--min-lien",      type=float, default=MIN_LIEN_AMOUNT)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    cache_path    = os.path.join(os.path.dirname(output_dir), "data", "lien_assess_cache.json")
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
    print("WBP — Allegheny Tax Liens Scraper")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Min lien amount: ${args.min_lien:,.0f}")
    print("=" * 60)

    lien_df   = fetch_tax_liens(args.min_lien)
    pins      = lien_df["pin"].dropna().tolist()
    assess_df, cache = fetch_assessments_for_pins(pins, cache)

    if cache:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"records": cache}, f)

    output = build_output(lien_df, assess_df)

    fname = f"allegheny_tax_liens_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json:
        records = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source": "WPRDC Tax Liens",
            "total": len(records),
            "records": records,
        }
        for path in [os.path.join(dashboard_dir, "tax_liens.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "tax_liens.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    hot = output[output["score"] >= 70]
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output):,} lien records → {fname}")
    print(f"  Hot leads (score >=70): {len(hot):,}")
    print(f"  Avg score: {output['score'].mean():.1f}")
    print(f"  Avg lien total: ${output['lien_total'].mean():,.0f}")
    print(f"  Elapsed: {elapsed:.1f}s")

    global _records_written
    _records_written = len(output)
    return output


_records_written = 0

if __name__ == "__main__":
    main()

"""
allegheny_estate_sales.py
We Buy Property LLC — Head of Data
Purpose: Scrape Pittsburgh-area estate sales from EstateSales.net.
         Estate sales = owner recently died or moved to care facility.
         Address is public, timing is urgent (sale date = cleanup deadline).
         Cross-ref WPRDC assessments for APN + mailing address for heir outreach.

Strategy:
  - Parse EstateSales.net Pittsburgh listings (public HTML)
  - Extract property addresses + sale dates
  - WPRDC assessment lookup for APN, FMV, owner
  - Score: base 72 + upcoming sale urgency + ARV + absentee

DATA SOURCES:
  - https://www.estatesales.net/PA/Pittsburgh
  - https://www.estatesale.com/estate-sales/pa/pittsburgh

Run:      python allegheny_estate_sales.py
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
MIN_FMV    = 87_700
MAX_FMV    = 600_000
BATCH_SIZE = 50

# ─── WPRDC ───────────────────────────────────────────────────────────────────
ASSESSMENTS_ID = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_SQL      = "https://data.wprdc.org/api/3/action/datastore_search_sql"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Allegheny County ZIP codes (used to confirm location)
ALLEGHENY_ZIPS = {
    "15001","15003","15004","15005","15006","15007","15014","15015","15017",
    "15018","15019","15020","15024","15025","15026","15027","15028","15030",
    "15031","15032","15034","15035","15037","15038","15042","15043","15044",
    "15045","15046","15047","15049","15050","15051","15056","15057","15060",
    "15061","15062","15063","15064","15065","15068","15071","15072","15074",
    "15075","15076","15078","15079","15081","15082","15083","15084","15085",
    "15086","15088","15089","15090","15091","15095","15096","15101","15102",
    "15104","15106","15108","15110","15112","15116","15120","15122","15123",
    "15126","15127","15129","15130","15131","15132","15133","15134","15135",
    "15136","15137","15138","15139","15140","15141","15142","15143","15144",
    "15145","15146","15147","15148","15201","15202","15203","15204","15205",
    "15206","15207","15208","15209","15210","15211","15212","15213","15214",
    "15215","15216","15217","15218","15219","15220","15221","15222","15223",
    "15224","15225","15226","15227","15228","15229","15230","15231","15232",
    "15233","15234","15235","15236","15237","15238","15239","15240","15241",
    "15242","15243","15244","15250","15251","15252","15253","15254","15255",
    "15257","15258","15259","15260","15261","15262","15264","15265","15267",
    "15268","15270","15272","15274","15275","15276","15277","15278","15279",
    "15281","15282","15283","15285","15286","15289","15290","15295",
}

STREET_TYPES = (
    r"Street|Avenue|Boulevard|Drive|Road|Lane|Way|Court|Place|Circle|Terrace|"
    r"Alley|Pike|Highway|Run|Hill|Ridge|Glen|Blvd|Ave|Dr|Rd|St|Ln|Ct|Pl|Cir|Ter|Hwy"
)
ADDRESS_RE = re.compile(
    r"\b(\d{1,5})\s+([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})\s+("
    + STREET_TYPES + r")\.?\b",
    re.IGNORECASE,
)


# ─── STEP 1: SCRAPE ESTATE SALES ─────────────────────────────────────────────

def fetch_estatesales_net(max_pages: int = 5) -> list:
    """Scrape EstateSales.net Pittsburgh listings."""
    listings = []
    base_url = "https://www.estatesales.net/PA/Pittsburgh"

    for page in range(1, max_pages + 1):
        url = f"{base_url}/{page}" if page > 1 else base_url
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                break
            r.raise_for_status()

            html = r.text

            # Extract sale blocks — each sale has address + date in structured divs
            # Pattern: city, state zip in listing cards
            # Address pattern in listings
            addr_matches = ADDRESS_RE.findall(html)

            # Extract dates — EstateSales.net format: "May 10 - 11, 2026"
            date_matches = re.findall(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,?\s*\d{4}",
                html, re.IGNORECASE
            )

            # Extract ZIP codes near addresses
            zip_matches = re.findall(r"\b(15\d{3})\b", html)

            for i, (house, street_name, street_type) in enumerate(addr_matches):
                full_addr = f"{house} {street_name} {street_type}".strip()
                zip_code = zip_matches[i] if i < len(zip_matches) else ""
                sale_date = date_matches[i] if i < len(date_matches) else ""

                # Only keep Allegheny County ZIPs
                if zip_code and zip_code not in ALLEGHENY_ZIPS:
                    continue

                listings.append({
                    "extracted_address": full_addr,
                    "house_num":   house.strip(),
                    "street_name": f"{street_name} {street_type}".strip(),
                    "zip_hint":    zip_code,
                    "sale_date_raw": sale_date,
                    "source":      "EstateSales.net",
                    "article_url": url,
                    "pub_date":    date.today().isoformat(),
                })

            time.sleep(1.5)

        except Exception as e:
            print(f"  Page {page} error: {e}")
            break

    return listings


def fetch_estatesale_com(max_pages: int = 3) -> list:
    """Scrape EstateSale.com Pittsburgh listings as backup source."""
    listings = []
    try:
        r = requests.get(
            "https://www.estatesale.com/estate-sales/pa/pittsburgh",
            headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        html = r.text

        addr_matches = ADDRESS_RE.findall(html)
        zip_matches  = re.findall(r"\b(15\d{3})\b", html)

        for i, (house, street_name, street_type) in enumerate(addr_matches):
            zip_code = zip_matches[i] if i < len(zip_matches) else ""
            if zip_code and zip_code not in ALLEGHENY_ZIPS:
                continue
            listings.append({
                "extracted_address": f"{house} {street_name} {street_type}".strip(),
                "house_num":   house.strip(),
                "street_name": f"{street_name} {street_type}".strip(),
                "zip_hint":    zip_code,
                "sale_date_raw": "",
                "source":      "EstateSale.com",
                "article_url": "https://www.estatesale.com/estate-sales/pa/pittsburgh",
                "pub_date":    date.today().isoformat(),
            })
    except Exception as e:
        print(f"  EstateSale.com error: {e}")
    return listings


# ─── STEP 2: ASSESSMENT LOOKUP ───────────────────────────────────────────────

def lookup_assessments(records: list) -> pd.DataFrame:
    print(f"[2/3] Assessment lookup for {len(records)} estate sale addresses...")
    all_results = []

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        conditions = []
        for r in batch:
            sn = re.sub(r"'", "", r["street_name"])[:14].upper()
            conditions.append(
                f'("PROPERTYHOUSENUM" = \'{r["house_num"]}\' AND '
                f'"PROPERTYADDRESS" ILIKE \'%{sn}%\')'
            )
        sql = (
            f'SELECT "PARID","PROPERTYHOUSENUM","PROPERTYADDRESS","PROPERTYCITY",'
            f'"PROPERTYSTATE","PROPERTYZIP","OWNERDESC",'
            f'"CHANGENOTICEADDRESS1","CHANGENOTICEADDRESS2",'
            f'"CHANGENOTICEADDRESS3","CHANGENOTICEADDRESS4",'
            f'"FAIRMARKETTOTAL","BEDROOMS","YEARBLT","FINISHEDLIVINGAREA",'
            f'"CONDITIONDESC","HOMESTEADFLAG","CLASSDESC"'
            f' FROM "{ASSESSMENTS_ID}"'
            f' WHERE ({" OR ".join(conditions)})'
            f' AND "FAIRMARKETTOTAL" >= {MIN_FMV}'
            f' AND "FAIRMARKETTOTAL" <= {MAX_FMV}'
            f' AND "CLASSDESC" ILIKE \'%RESIDENTIAL%\''
        )
        try:
            resp = requests.post(WPRDC_SQL, data={"sql": sql}, timeout=30)
            data = resp.json()
            if data.get("success"):
                for hit in data["result"]["records"]:
                    ph = str(hit.get("PROPERTYHOUSENUM", "")).strip()
                    pa = str(hit.get("PROPERTYADDRESS", "")).upper()
                    for rec in batch:
                        if ph == rec["house_num"] and rec["street_name"][:8].upper() in pa:
                            all_results.append({**hit, **rec})
                            break
        except Exception as e:
            print(f"  Batch {i // BATCH_SIZE} error: {e}")
        time.sleep(0.15)

    print(f"  {len(all_results)} addresses matched to assessments")
    return pd.DataFrame(all_results) if all_results else pd.DataFrame()


# ─── STEP 3: BUILD OUTPUT ────────────────────────────────────────────────────

def parse_mailing(row) -> tuple:
    def cl(f): return str(row.get(f, "") or "").strip()
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
    return street, city, state, zip_


def build_output(records: list, assess_df: pd.DataFrame) -> pd.DataFrame:
    print("[3/3] Building scored output...")

    if assess_df.empty:
        if not records:
            return pd.DataFrame()
        df_raw = pd.DataFrame(records)
        output = pd.DataFrame({
            "property_address": df_raw["extracted_address"],
            "property_city":    "",
            "property_state":   "PA",
            "property_zip":     df_raw.get("zip_hint", ""),
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
            "data_type":        "estate_sale",
            "sale_date_raw":    df_raw.get("sale_date_raw", ""),
            "news_source":      df_raw["source"],
            "article_url":      df_raw["article_url"],
            "source":           df_raw["source"],
            "date_pulled":      date.today().isoformat(),
            "score":            65,
            "flags":            "Estate Sale, Address Unverified",
        })
        return output

    mail_data = assess_df.apply(parse_mailing, axis=1, result_type="expand")
    mail_data.columns = ["mailing_address", "mailing_city", "mailing_state", "mailing_zip"]
    fmv = pd.to_numeric(assess_df["FAIRMARKETTOTAL"], errors="coerce").fillna(0)

    output = pd.DataFrame({
        "property_address": (assess_df["PROPERTYHOUSENUM"].fillna("").astype(str).str.strip() + " " +
                             assess_df["PROPERTYADDRESS"].fillna("").astype(str).str.strip()).str.strip(),
        "property_city":    assess_df["PROPERTYCITY"].fillna("").str.strip().str.title(),
        "property_state":   assess_df["PROPERTYSTATE"].fillna("PA").str.strip().str.upper(),
        "property_zip":     assess_df["PROPERTYZIP"].fillna("").astype(str).str.strip().str[:5],
        "mailing_address":  mail_data["mailing_address"],
        "mailing_city":     mail_data["mailing_city"],
        "mailing_state":    mail_data["mailing_state"],
        "mailing_zip":      mail_data["mailing_zip"],
        "county":           "Allegheny",
        "apn":              assess_df["PARID"].fillna("").astype(str).str.strip(),
        "fmv_assessed":     fmv.astype(int),
        "est_arv":          (fmv / 0.877).astype(int),
        "bedrooms":         pd.to_numeric(assess_df.get("BEDROOMS"), errors="coerce").fillna(0).astype(int),
        "year_built":       assess_df.get("YEARBLT", pd.Series([""] * len(assess_df))).fillna("").astype(str).str[:4],
        "sqft":             pd.to_numeric(assess_df.get("FINISHEDLIVINGAREA"), errors="coerce").fillna(0).astype(int),
        "condition":        assess_df.get("CONDITIONDESC", pd.Series([""] * len(assess_df))).fillna(""),
        "homestead":        assess_df.get("HOMESTEADFLAG", pd.Series([""] * len(assess_df))).fillna(""),
        "data_type":        "estate_sale",
        "sale_date_raw":    assess_df.get("sale_date_raw", pd.Series([""] * len(assess_df))).fillna(""),
        "news_source":      assess_df["source"].fillna(""),
        "article_url":      assess_df["article_url"].fillna(""),
        "source":           assess_df["source"].fillna(""),
        "date_pulled":      date.today().isoformat(),
        "score":            0,
        "flags":            "",
    })

    scores, flags_list = [], []
    for _, row in output.iterrows():
        score = 72    # estate sale = confirmed transition event
        flags = ["Estate Sale"]

        # Sale date urgency
        sale_raw = str(row.get("sale_date_raw", "") or "")
        if sale_raw:
            flags.append(f"Sale: {sale_raw[:20]}")
            # Try to detect upcoming sales (high urgency)
            if any(m in sale_raw for m in ["2026", "2027"]):
                score = min(score + 8, 100)
                flags.append("Upcoming Sale")

        # ARV
        arv = int(row.get("est_arv", 0) or 0)
        if arv > 300_000:
            score = min(score + 10, 100)
            flags.append(f"High Equity (${arv:,} ARV)")
        elif arv > 150_000:
            score = min(score + 5, 100)
            flags.append(f"Sweet Spot ARV (${arv:,})")

        # Absentee owner (heir living elsewhere)
        mail_state = str(row.get("mailing_state", "") or "").strip().upper()
        mail_city  = str(row.get("mailing_city", "") or "").strip().lower()
        prop_city  = str(row.get("property_city", "") or "").strip().lower()
        if mail_state not in ("", "PA") or (mail_city and mail_city != prop_city):
            score = min(score + 10, 100)
            flags.append("Absentee Heir")

        # No homestead = non-primary (landlord/investment property in estate)
        homestead = str(row.get("homestead", "") or "").strip().upper()
        if homestead in ("", "N", "NO", "0"):
            score = min(score + 5, 100)
            flags.append("No Homestead")

        scores.append(score)
        flags_list.append(", ".join(flags))

    output["score"] = scores
    output["flags"] = flags_list
    output = output[output["property_address"].str.len() > 3]
    output = output.drop_duplicates(subset=["apn"])
    return output.sort_values("score", ascending=False)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir",    default=os.path.join(os.path.dirname(__file__), "..", "output"))
    parser.add_argument("--dashboard-dir", default=os.path.join(os.path.dirname(__file__), "..", "dashboard"))
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    start = time.time()
    print("=" * 60)
    print("WBP — Allegheny Estate Sales Scraper")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    print("[1/3] Scraping estate sale listings...")
    records = []
    net = fetch_estatesales_net(max_pages=5)
    print(f"  EstateSales.net: {len(net)} listings")
    records.extend(net)

    com = fetch_estatesale_com()
    print(f"  EstateSale.com: {len(com)} listings")
    records.extend(com)

    # Deduplicate by address
    seen, unique = set(), []
    for r in records:
        key = r["extracted_address"].upper()[:30]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    records = unique
    print(f"  Unique addresses: {len(records)}")

    assess_df = pd.DataFrame()
    if records:
        assess_df = lookup_assessments(records)

    output = build_output(records, assess_df)

    fname = f"allegheny_estate_sales_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json and not output.empty:
        recs_out = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source":     "EstateSales.net + EstateSale.com",
            "total":      len(recs_out),
            "records":    recs_out,
        }
        for path in [os.path.join(dashboard_dir, "estate_sales.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "estate_sales.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    hot = output[output["score"] >= 80] if not output.empty else pd.DataFrame()
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output)} estate sale leads → {fname}")
    if not output.empty:
        print(f"  Hot leads (>=80): {len(hot)}")
        print(f"  Avg score: {output['score'].mean():.1f}")
    print(f"  Elapsed: {elapsed:.1f}s")

    global _records_written
    _records_written = len(output)
    return output


_records_written = 0

if __name__ == "__main__":
    main()

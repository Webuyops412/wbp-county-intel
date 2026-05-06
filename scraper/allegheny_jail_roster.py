"""
allegheny_jail_roster.py
We Buy Property LLC — Head of Data
Purpose: Scrape Allegheny County Jail public roster → cross-ref WPRDC assessments
         by owner name. Incarcerated property owner = vacant property, unpaid taxes
         building, high motivation when contacted. A wholly unique list no other
         investor is working.

Strategy:
  - Pull public jail roster (alleghenycounty.us — Who's In Jail)
  - Extract inmate names + booking date
  - Cross-ref WPRDC assessment OWNERDESC by last name + first name prefix
  - Filter: residential properties in ARV range with homestead flag
    (homestead = primary residence → likely sitting empty)
  - Output: property address + owner in custody + booking date

DATA SOURCE: https://www2.alleghenycounty.us/jailwho/jailwho.aspx
             Allegheny County Jail public inmate search (no login required)

Run:      python allegheny_jail_roster.py
Schedule: Weekly via GitHub Actions (roster changes slowly)
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
BATCH_SIZE = 25     # Keep small — name queries are broad

# ─── WPRDC ───────────────────────────────────────────────────────────────────
ASSESSMENTS_ID = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_SQL      = "https://data.wprdc.org/api/3/action/datastore_search_sql"

# ─── JAIL ROSTER ─────────────────────────────────────────────────────────────
JAIL_ROSTER_URL = "https://www2.alleghenycounty.us/jailwho/jailwho.aspx"
JAIL_SEARCH_URL = "https://www2.alleghenycounty.us/jailwho/jailwho.aspx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": JAIL_ROSTER_URL,
}


# ─── STEP 1: FETCH JAIL ROSTER ───────────────────────────────────────────────

def fetch_jail_roster() -> list:
    """
    Allegheny County Jail 'Who's In Jail' public search.
    ASP.NET form — POST with __VIEWSTATE to get all current inmates.
    Returns list of {last_name, first_name, booking_date, charges}.
    """
    inmates = []

    try:
        # First GET to grab __VIEWSTATE / __EVENTVALIDATION tokens
        r = requests.get(JAIL_ROSTER_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text

        # Extract ASP.NET form tokens
        viewstate = re.search(r'id="__VIEWSTATE"\s+value="([^"]*)"', html)
        evval     = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]*)"', html)
        vsgenerator = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"', html)

        vs  = viewstate.group(1) if viewstate else ""
        ev  = evval.group(1) if evval else ""
        vsg = vsgenerator.group(1) if vsgenerator else ""

        # POST: search all inmates (blank last name = all)
        post_data = {
            "__VIEWSTATE":          vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION":    ev,
            "ctl00$MainContent$txtLastName":  "",
            "ctl00$MainContent$txtFirstName": "",
            "ctl00$MainContent$btnSearch":    "Search",
        }

        r2 = requests.post(JAIL_SEARCH_URL, data=post_data, headers=HEADERS, timeout=45)
        r2.raise_for_status()
        html2 = r2.text

        # Parse table rows — look for pattern: Last, First | Booking Date | Charges
        # Typical row: <td>SMITH</td><td>JOHN</td><td>01/15/2026</td><td>...</td>
        rows = re.findall(
            r"<tr[^>]*>(?:\s*<td[^>]*>(.*?)</td>){3,}",
            html2, re.DOTALL | re.IGNORECASE
        )

        if not rows:
            # Try alternative: parse all td sequences
            tds = re.findall(r"<td[^>]*>([^<]{2,40})</td>", html2)
            # Group into rows of N columns
            n_cols = 5  # typical for jail roster
            for i in range(0, len(tds) - n_cols, n_cols):
                row_cells = [re.sub(r"<[^>]+>", "", tds[j]).strip() for j in range(i, i + n_cols)]
                if len(row_cells) >= 3:
                    last_name  = row_cells[0].strip().upper()
                    first_name = row_cells[1].strip().upper() if len(row_cells) > 1 else ""
                    booking_dt = row_cells[2].strip() if len(row_cells) > 2 else ""

                    # Skip header rows
                    if last_name in ("LAST NAME", "LAST", "NAME", "") or len(last_name) < 2:
                        continue
                    # Validate booking date format
                    if not re.match(r"\d{1,2}/\d{1,2}/\d{4}", booking_dt):
                        booking_dt = ""

                    inmates.append({
                        "last_name":    last_name.title(),
                        "first_name":   first_name.title(),
                        "full_name":    f"{first_name.title()} {last_name.title()}".strip(),
                        "booking_date": booking_dt,
                        "source":       "AlleghenyJailRoster",
                    })
        else:
            for row_html in rows:
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
                cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
                if len(cells) < 3:
                    continue
                last_name  = cells[0].upper()
                first_name = cells[1].upper() if len(cells) > 1 else ""
                booking_dt = cells[2] if len(cells) > 2 else ""

                if last_name in ("LAST NAME", "LAST", "NAME", "") or len(last_name) < 2:
                    continue

                inmates.append({
                    "last_name":    last_name.title(),
                    "first_name":   first_name.title(),
                    "full_name":    f"{first_name.title()} {last_name.title()}".strip(),
                    "booking_date": booking_dt,
                    "source":       "AlleghenyJailRoster",
                })

        print(f"  Jail roster: {len(inmates)} inmates parsed")

    except Exception as e:
        print(f"  Jail roster fetch error: {e}")
        print("  Falling back to PA UJS docket search...")
        inmates = fetch_pa_ujs_fallback()

    # Deduplicate
    seen, unique = set(), []
    for inmate in inmates:
        key = f"{inmate['last_name']}|{inmate['first_name']}"
        if key not in seen and len(inmate["last_name"]) >= 2:
            seen.add(key)
            unique.append(inmate)
    return unique


def fetch_pa_ujs_fallback() -> list:
    """
    Fallback: PA Unified Judicial System public dockets.
    Search for recent Allegheny County criminal filings.
    """
    inmates = []
    try:
        # PA UJS portal — public criminal docket search
        url = "https://ujsportal.pacourts.us/DocketSheets/CP.aspx"
        r = requests.get(url, headers=HEADERS, timeout=20)
        # If accessible, parse for recent Allegheny County criminal cases
        # This is often JS-heavy — return empty if blocked
        if r.status_code == 200 and "pacourts" in r.url:
            print("  PA UJS accessible — but requires JS for full search")
    except Exception as e:
        print(f"  PA UJS fallback error: {e}")
    return inmates


# ─── STEP 2: ASSESSMENT LOOKUP BY NAME ───────────────────────────────────────

def lookup_assessments_by_name(inmates: list) -> pd.DataFrame:
    """
    Match inmate names against WPRDC OWNERDESC.
    Only match homestead properties (primary residence = vacant while incarcerated).
    """
    print(f"[2/3] Matching {len(inmates)} inmates against WPRDC property owners...")
    all_results = []

    # Focus on unique last names to avoid over-querying
    seen_last, unique_last = set(), []
    for inmate in inmates:
        key = inmate["last_name"].upper()
        if key not in seen_last and len(key) >= 3:
            seen_last.add(key)
            unique_last.append(inmate)

    for i in range(0, len(unique_last), BATCH_SIZE):
        batch = unique_last[i:i + BATCH_SIZE]
        conditions = []
        for inmate in batch:
            last = re.sub(r"'", "''", inmate["last_name"].upper())
            conditions.append(f'"OWNERDESC" ILIKE \'{last}%\'')

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
            f' AND "HOMESTEADFLAG" = \'HOM\''
        )
        try:
            resp = requests.post(WPRDC_SQL, data={"sql": sql}, timeout=30)
            data = resp.json()
            if data.get("success"):
                for hit in data["result"]["records"]:
                    owner_desc = str(hit.get("OWNERDESC", "")).upper().strip()
                    for inmate in batch:
                        last = inmate["last_name"].upper()
                        first_3 = inmate["first_name"].upper()[:3]
                        if last in owner_desc and first_3 and first_3 in owner_desc:
                            all_results.append({**hit, **inmate, "_match": "name_confirmed"})
                            break
                        elif last in owner_desc:
                            all_results.append({**hit, **inmate, "_match": "last_name_only"})
                            break
        except Exception as e:
            print(f"  Batch {i // BATCH_SIZE} error: {e}")
        time.sleep(0.2)

    print(f"  {len(all_results)} property matches found")
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


def build_output(inmates: list, assess_df: pd.DataFrame) -> pd.DataFrame:
    print("[3/3] Building scored output...")

    if assess_df.empty:
        print("  No property matches — returning raw roster")
        return pd.DataFrame()  # No point outputting without a property

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
        "data_type":        "jail_roster",
        "inmate_name":      assess_df["full_name"].fillna(""),
        "owner_desc":       assess_df["OWNERDESC"].fillna(""),
        "name_match":       assess_df.get("_match", pd.Series(["unknown"] * len(assess_df))).fillna("unknown"),
        "booking_date":     assess_df.get("booking_date", pd.Series([""] * len(assess_df))).fillna(""),
        "source":           "AlleghenyJailRoster",
        "date_pulled":      date.today().isoformat(),
        "score":            0,
        "flags":            "",
    })

    scores, flags_list = [], []
    for _, row in output.iterrows():
        score = 70    # incarcerated owner = vacant + distressed but non-urgent
        flags = ["Owner Incarcerated", "Vacant Property"]

        # Name match quality
        match = str(row.get("name_match", "") or "")
        if match == "name_confirmed":
            score = min(score + 15, 100)
            flags.append("Name Confirmed")
        else:
            score = min(score + 5, 100)
            flags.append("Last Name Match")

        # ARV
        arv = int(row.get("est_arv", 0) or 0)
        if arv > 300_000:
            score = min(score + 10, 100)
            flags.append(f"High Equity (${arv:,} ARV)")
        elif arv > 150_000:
            score = min(score + 5, 100)
            flags.append(f"Sweet Spot ARV (${arv:,})")

        scores.append(score)
        flags_list.append(", ".join(flags))

    output["score"] = scores
    output["flags"] = flags_list
    output = output[output["apn"].str.len() > 3]
    output = output.drop_duplicates(subset=["apn"])
    return output.sort_values("score", ascending=False)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir",    default=os.path.join(os.path.dirname(__file__), "..", "output"))
    parser.add_argument("--dashboard-dir", default=os.path.join(os.path.dirname(__file__), "..", "dashboard"))
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    start = time.time()
    print("=" * 60)
    print("WBP — Allegheny Jail Roster Scraper")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    print("[1/3] Fetching jail roster...")
    inmates = fetch_jail_roster()
    print(f"  Unique inmates: {len(inmates)}")

    assess_df = pd.DataFrame()
    if inmates:
        assess_df = lookup_assessments_by_name(inmates)

    output = build_output(inmates, assess_df)

    fname = f"allegheny_jail_roster_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json and not output.empty:
        recs_out = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source":     "Allegheny County Jail Public Roster",
            "total":      len(recs_out),
            "records":    recs_out,
        }
        for path in [os.path.join(dashboard_dir, "jail_roster.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "jail_roster.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    hot = output[output["score"] >= 80] if not output.empty else pd.DataFrame()
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output)} jail roster leads → {fname}")
    if not output.empty:
        print(f"  Hot leads (>=80): {len(hot)}")
        print(f"  Avg score: {output['score'].mean():.1f}")
        confirmed = output[output["name_match"] == "name_confirmed"]
        print(f"  Name-confirmed matches: {len(confirmed)}")
    print(f"  Elapsed: {elapsed:.1f}s")

    global _records_written
    _records_written = len(output)
    return output


_records_written = 0

if __name__ == "__main__":
    main()

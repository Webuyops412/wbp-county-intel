"""
allegheny_probate.py
We Buy Property LLC — Head of Data
Purpose: Pre-probate lead scraper — find recently deceased property owners.
         Strategy: obituaries (PG + TribLive via Legacy.com) → extract names →
         cross-ref WPRDC assessments by OWNERDESC → identify properties likely
         entering probate. Heir contact = mailing address on assessment record.

Why it works:
  - Death = sudden motivation to liquidate real estate
  - Estate settling takes 6–18 months — ideal outreach window
  - No one else is marketing to these leads at filing time

Sources:
  - Pittsburgh Post-Gazette obits: https://www.legacy.com/obituaries/pittsburgh-post-gazette/
  - TribLive/Tribune-Review obits: https://www.legacy.com/obituaries/triblive/
  - PA Death Notices RSS (backup): https://www.post-gazette.com/rss/obits

Run:      python allegheny_probate.py
Schedule: Daily via GitHub Actions
"""

import requests
import pandas as pd
import json
import re
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from email.utils import parsedate_to_datetime
import os
import time

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DEFAULT_LOOKBACK_DAYS = 30
MIN_FMV    = 87_700
MAX_FMV    = 600_000
BATCH_SIZE = 30    # Smaller batches — name matching is expensive

# ─── WPRDC ───────────────────────────────────────────────────────────────────
ASSESSMENTS_ID = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_SQL      = "https://data.wprdc.org/api/3/action/datastore_search_sql"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Obituary RSS feeds
OBIT_RSS_FEEDS = [
    {"name": "PG_Obits",     "url": "https://www.post-gazette.com/rss/obits"},
    {"name": "TribLive",     "url": "https://triblive.com/feed/"},
    {"name": "TribLive_local","url": "https://triblive.com/local/feed/"},
]

# Allegheny County / Pittsburgh area cities for geo-filter
PITTSBURGH_AREA_LOWER = [
    "pittsburgh", "allegheny", "penn hills", "mt. lebanon", "bethel park",
    "ross", "hampton", "plum", "monroeville", "carnegie", "dormont",
    "brentwood", "baldwin", "west mifflin", "mckeesport", "duquesne",
    "homestead", "swissvale", "wilkinsburg", "braddock", "turtle creek",
    "verona", "oakmont", "aspinwall", "shaler", "etna", "millvale",
    "north versailles", "whitehall", "castle shannon", "crafton", "ingram",
    "mckees rocks", "moon", "robinson", "north fayette", "south fayette",
    "upper st. clair", "clairton", "glassport", "elizabeth", "munhall",
    "west homestead", "rankin", "edgewood", "brushton", "lawrenceville",
    "bloomfield", "shadyside", "highland park", "east liberty", "garfield",
    "squirrel hill", "greenfield", "hazelwood", "south side", "beechview",
    "brookline", "banksville", "knoxville", "carrick", "point breeze",
]

# Name extraction from obituary text
# Pattern: "John Michael Smith, 72, of Pittsburgh"
NAME_AGE_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}),\s*(\d{2,3}),\s*of\s+([A-Za-z\s\.]+?)(?:,|\.|\s{2})",
)

# Simpler: just grab "Firstname Lastname" at start of obit
NAME_START_RE = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
)


# ─── STEP 1: FETCH OBITUARIES ────────────────────────────────────────────────

def fetch_legacy_com(source_slug: str, lookback_days: int) -> list:
    """
    Fetch Legacy.com obituary listing for a Pittsburgh paper.
    Uses their public search endpoint — returns JSON of recent obits.
    """
    obituaries = []
    cutoff = date.today() - timedelta(days=lookback_days)

    # Legacy.com affiliate obituary search
    url = f"https://www.legacy.com/api/obituary-search"
    params = {
        "affiliateId": source_slug,
        "regionId": "pa",
        "limit": 200,
        "startRecord": 0,
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            data = r.json()
            for obit in data.get("obituaries", []):
                pub_date_str = obit.get("PublishDate", "")[:10]
                try:
                    pub_date = date.fromisoformat(pub_date_str)
                    if pub_date < cutoff:
                        continue
                except Exception:
                    pass

                city = (obit.get("City") or "").lower()
                state = (obit.get("StateCode") or "").upper()
                if state != "PA":
                    continue
                if not any(c in city for c in PITTSBURGH_AREA_LOWER):
                    # Also accept if it's just "Pittsburgh" area
                    if city not in PITTSBURGH_AREA_LOWER:
                        continue

                first = obit.get("FirstName", "").strip()
                last  = obit.get("LastName", "").strip()
                full_name = f"{first} {last}".strip()
                if len(full_name) < 4:
                    continue

                obituaries.append({
                    "full_name":  full_name,
                    "first_name": first,
                    "last_name":  last,
                    "city":       city.title(),
                    "state":      state,
                    "pub_date":   pub_date_str or date.today().isoformat(),
                    "source":     source_slug,
                    "article_url": obit.get("ObituaryURL", ""),
                })
    except Exception as e:
        print(f"  Legacy.com ({source_slug}) error: {e}")

    return obituaries


def fetch_obit_rss(lookback_days: int) -> list:
    """Pull obituary RSS feeds and extract names."""
    obituaries = []
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    for feed in OBIT_RSS_FEEDS:
        try:
            r = requests.get(feed["url"], headers=HEADERS, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            items = root.findall(".//item")

            for item in items:
                title = (item.findtext("title") or "").strip()
                desc  = (item.findtext("description") or "").strip()
                link  = (item.findtext("link") or "").strip()
                pub   = item.findtext("pubDate") or ""

                full_text = f"{title} {desc}".lower()
                # Filter: obit keywords + Pittsburgh area
                is_obit = any(kw in full_text for kw in
                              ["obituary", "passed away", "died", "funeral", "survived by", "memorial"])
                in_area = any(city in full_text for city in PITTSBURGH_AREA_LOWER)

                if not (is_obit and in_area):
                    continue

                pub_dt = None
                if pub:
                    try:
                        pub_dt = parsedate_to_datetime(pub).replace(tzinfo=None)
                    except Exception:
                        pass
                if pub_dt and pub_dt < cutoff:
                    continue

                # Extract name from title (usually "John Smith Obituary" or just "John Smith")
                clean_title = re.sub(r"\s+obituary.*$", "", title, flags=re.IGNORECASE).strip()
                m = NAME_START_RE.match(clean_title)
                if not m:
                    continue
                full_name = m.group(1).strip()
                if len(full_name) < 5 or len(full_name.split()) < 2:
                    continue

                parts = full_name.split()
                obituaries.append({
                    "full_name":  full_name,
                    "first_name": parts[0],
                    "last_name":  parts[-1],
                    "city":       "",
                    "state":      "PA",
                    "pub_date":   pub_dt.date().isoformat() if pub_dt else date.today().isoformat(),
                    "source":     feed["name"],
                    "article_url": link,
                })
        except Exception as e:
            print(f"  {feed['name']} RSS error: {e}")

    return obituaries


# ─── STEP 2: ASSESSMENT LOOKUP BY NAME ───────────────────────────────────────

def lookup_assessments_by_name(obituaries: list) -> pd.DataFrame:
    """
    Search WPRDC assessments by OWNERDESC (stored as LAST FIRST or LAST FIRST MI).
    Match: last name + first 3 chars of first name for fuzzy tolerance.
    """
    print(f"[2/3] Name-matching {len(obituaries)} obituaries against WPRDC assessments...")
    all_results = []

    # Deduplicate by last name to avoid redundant batches
    seen_last, unique = set(), []
    for o in obituaries:
        key = o["last_name"].upper()
        if key not in seen_last and len(key) >= 3:
            seen_last.add(key)
            unique.append(o)

    for i in range(0, len(unique), BATCH_SIZE):
        batch = unique[i:i + BATCH_SIZE]
        conditions = []
        for o in batch:
            last = re.sub(r"'", "''", o["last_name"].upper())
            # WPRDC stores as "SMITH JOHN A" — search by last name prefix
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
            f' AND "HOMESTEADFLAG" = \'HOM\''  # Primary residence = more motivated
        )
        try:
            resp = requests.post(WPRDC_SQL, data={"sql": sql}, timeout=30)
            data = resp.json()
            if data.get("success"):
                hits = data["result"]["records"]
                # For each hit, find matching obituary
                for hit in hits:
                    owner_desc = str(hit.get("OWNERDESC", "")).upper().strip()
                    for o in batch:
                        last = o["last_name"].upper()
                        first_3 = o["first_name"].upper()[:3]
                        if last in owner_desc and first_3 in owner_desc:
                            all_results.append({**hit, **o, "_match": "name_confirmed"})
                            break
                        elif last in owner_desc:
                            all_results.append({**hit, **o, "_match": "last_name_only"})
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


def build_output(obituaries: list, assess_df: pd.DataFrame) -> pd.DataFrame:
    print("[3/3] Building scored output...")

    if assess_df.empty:
        print("  No assessment matches — returning raw obituary list")
        if not obituaries:
            return pd.DataFrame()
        df_raw = pd.DataFrame(obituaries)
        return pd.DataFrame({
            "property_address": "",
            "property_city":    df_raw["city"],
            "property_state":   df_raw["state"],
            "property_zip":     "",
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
            "data_type":        "probate",
            "deceased_name":    df_raw["full_name"],
            "owner_desc":       "",
            "name_match":       "unmatched",
            "obit_date":        df_raw["pub_date"],
            "article_url":      df_raw["article_url"],
            "source":           df_raw["source"],
            "date_pulled":      date.today().isoformat(),
            "score":            55,
            "flags":            "Pre-Probate, No Property Match",
        })

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
        "data_type":        "probate",
        "deceased_name":    assess_df["full_name"].fillna(""),
        "owner_desc":       assess_df["OWNERDESC"].fillna(""),
        "name_match":       assess_df.get("_match", pd.Series(["unknown"] * len(assess_df))).fillna("unknown"),
        "obit_date":        assess_df["pub_date"].fillna(""),
        "article_url":      assess_df["article_url"].fillna(""),
        "source":           assess_df["source"].fillna(""),
        "date_pulled":      date.today().isoformat(),
        "score":            0,
        "flags":            "",
    })

    scores, flags_list = [], []
    for _, row in output.iterrows():
        score = 68    # Pre-probate = high motivation but takes time
        flags = ["Pre-Probate"]

        # Name match quality
        match = str(row.get("name_match", "") or "")
        if match == "name_confirmed":
            score = min(score + 10, 100)
            flags.append("Name Confirmed")
        else:
            flags.append("Last Name Match")

        # Recency
        try:
            days_ago = (date.today() - date.fromisoformat(str(row.get("obit_date", "")))).days
            if days_ago <= 14:
                score = min(score + 10, 100)
                flags.append(f"Recent Passing ({days_ago}d ago)")
            elif days_ago <= 30:
                score = min(score + 5, 100)
                flags.append(f"Passing ({days_ago}d ago)")
        except Exception:
            pass

        # ARV
        arv = int(row.get("est_arv", 0) or 0)
        if arv > 300_000:
            score = min(score + 10, 100)
            flags.append(f"High Equity (${arv:,} ARV)")
        elif arv > 150_000:
            score = min(score + 5, 100)
            flags.append(f"Sweet Spot ARV (${arv:,})")

        # Absentee mailing = heir living elsewhere (classic probate play)
        mail_state = str(row.get("mailing_state", "") or "").strip().upper()
        mail_city  = str(row.get("mailing_city", "") or "").strip().lower()
        prop_city  = str(row.get("property_city", "") or "").strip().lower()
        if mail_state not in ("", "PA") or (mail_city and mail_city != prop_city):
            score = min(score + 10, 100)
            flags.append("Heir Out of Area")

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
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    start = time.time()
    print("=" * 60)
    print("WBP — Allegheny Pre-Probate Scraper")
    print(f"Date:     {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Lookback: {args.lookback_days} days")
    print("=" * 60)

    print("[1/3] Fetching obituaries...")

    obituaries = []

    # Legacy.com feeds (most structured)
    for slug in ["pittsburgh-post-gazette", "triblive"]:
        results = fetch_legacy_com(slug, args.lookback_days)
        print(f"  Legacy.com/{slug}: {len(results)}")
        obituaries.extend(results)

    # RSS feeds (backup)
    rss_results = fetch_obit_rss(args.lookback_days)
    print(f"  RSS feeds: {len(rss_results)}")
    obituaries.extend(rss_results)

    # Deduplicate by name
    seen, unique = set(), []
    for o in obituaries:
        key = o["full_name"].upper()
        if key not in seen:
            seen.add(key)
            unique.append(o)
    obituaries = unique
    print(f"  Unique names: {len(obituaries)}")

    assess_df = pd.DataFrame()
    if obituaries:
        assess_df = lookup_assessments_by_name(obituaries)

    output = build_output(obituaries, assess_df)

    fname = f"allegheny_probate_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json and not output.empty:
        recs_out = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source":     "Pittsburgh Obituaries (PG/TribLive/RSS)",
            "total":      len(recs_out),
            "records":    recs_out,
        }
        for path in [os.path.join(dashboard_dir, "probate.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "probate.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    hot = output[output["score"] >= 80] if not output.empty else pd.DataFrame()
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output)} pre-probate leads → {fname}")
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

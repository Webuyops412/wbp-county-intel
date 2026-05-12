"""
allegheny_fire_news.py
We Buy Property LLC — Head of Data
Purpose: Scrape Pittsburgh news RSS feeds for residential fire stories.
         Fire-damaged homes = motivated sellers, often uninsured or overwhelmed.
         Extract addresses → cross-ref WPRDC assessments for APN + owner contact.

Strategy:
  - Parse WTAE, WPXI, KDKA RSS feeds
  - Filter: fire keywords + residential keywords + Pittsburgh/Allegheny area
  - Extract addresses via regex
  - Lookup APN + owner in WPRDC assessment DB
  - Score: base 75 + recency bonus + ARV bonus + absentee bonus

Run:      python allegheny_fire_news.py
Schedule: Daily (2x/day recommended) via GitHub Actions
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
MIN_FMV   = 87_700
MAX_FMV   = 600_000
BATCH_SIZE = 50

# ─── DATA SOURCE ─────────────────────────────────────────────────────────────
ASSESSMENTS_ID = "65855e14-549e-4992-b5be-d629afc676fa"
WPRDC_SQL      = "https://data.wprdc.org/api/3/action/datastore_search_sql"

# ─── RSS FEEDS ───────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {
        "name": "GoogleNews_fire",
        "url":  "https://news.google.com/rss/search?q=house+fire+pittsburgh+allegheny&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name": "GoogleNews_blaze",
        "url":  "https://news.google.com/rss/search?q=residential+fire+pittsburgh&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name": "PG_local",
        "url":  "https://www.post-gazette.com/rss/news/local",
    },
    {
        "name": "PG_breaking",
        "url":  "https://www.post-gazette.com/rss/breaking",
    },
]

FIRE_KEYWORDS      = ["fire", "blaze", "flames", "burned", "burning", "burnt"]
STRUCTURE_KEYWORDS = ["house", "home", "residence", "residential", "apartment",
                      "duplex", "building", "structure", "dwelling"]

# Pittsburgh-area city/municipality names for geo-filter
PITTSBURGH_AREA = [
    "pittsburgh", "allegheny", "penn hills", "mt. lebanon", "bethel park",
    "ross", "hampton", "plum", "monroeville", "carnegie", "dormont",
    "brentwood", "baldwin", "west mifflin", "mckeesport", "duquesne",
    "homestead", "swissvale", "edgewood", "wilkinsburg", "braddock",
    "turtle creek", "verona", "oakmont", "aspinwall", "shaler", "etna",
    "millvale", "north versailles", "whitehall", "castle shannon",
    "mount oliver", "chartiers", "crafton", "ingram", "mckees rocks",
    "stowe", "kennedy", "moon", "robinson", "north fayette", "south fayette",
    "upper st. clair", "scott", "south park", "finleyville", "clairton",
    "glassport", "elizabeth", "pleasant hills", "whitaker", "munhall",
    "west homestead", "rankin", "edgewood", "swissvale", "brushton",
    "squirrel hill", "lawrenceville", "bloomfield", "shadyside", "highland park",
    "east liberty", "garfield", "morningside", "troy hill", "spring hill",
    "perry", "northside", "northshore", "south side", "mount washington",
    "beechview", "brookline", "banksville", "knoxville", "carrick", "hazelwood",
    "greenfield", "swisshelm", "point breeze", "regent square",
]

# Street type pattern
STREET_TYPES = (
    r"Street|Avenue|Boulevard|Drive|Road|Lane|Way|Court|Place|Circle|Terrace|"
    r"Alley|Pike|Highway|Run|Hill|Ridge|Glen|Hollow|Trail|Blvd|Ave|Dr|Rd|"
    r"St|Ln|Ct|Pl|Cir|Ter|Hwy"
)

ADDRESS_RE = re.compile(
    r"\b(\d{1,5})\s+([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})\s+("
    + STREET_TYPES + r")\.?\b",
    re.IGNORECASE,
)


# ─── STEP 1: RSS FETCH ───────────────────────────────────────────────────────

def fetch_rss_stories(lookback_days: int) -> list:
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    stories = []

    for feed in RSS_FEEDS:
        try:
            r = requests.get(feed["url"], timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; WBP-DataBot/1.0; +https://we-buy-property.net)"
            })
            r.raise_for_status()
            # Skip HTML responses (bot blocks)
            ct = r.headers.get("Content-Type", "")
            if "html" in ct and "xml" not in ct:
                print(f"  {feed['name']}: blocked (HTML), skipping")
                continue
            root = ET.fromstring(r.content)
            items = root.findall(".//item")
            print(f"  {feed['name']}: {len(items)} items")

            for item in items:
                title = (item.findtext("title") or "").strip()
                desc  = (item.findtext("description") or "").strip()
                link  = (item.findtext("link") or "").strip()
                pub   = item.findtext("pubDate") or ""

                full_text = f"{title} {desc}".lower()

                # Date filter
                pub_dt = None
                if pub:
                    try:
                        pub_dt = parsedate_to_datetime(pub).replace(tzinfo=None)
                    except Exception:
                        pass
                if pub_dt and pub_dt < cutoff:
                    continue

                # Must match fire + structure + area
                if not any(kw in full_text for kw in FIRE_KEYWORDS):
                    continue
                if not any(kw in full_text for kw in STRUCTURE_KEYWORDS):
                    continue
                if not any(city in full_text for city in PITTSBURGH_AREA):
                    continue

                stories.append({
                    "source":    feed["name"],
                    "title":     title,
                    "description": desc,
                    "link":      link,
                    "pub_date":  pub_dt.date().isoformat() if pub_dt else date.today().isoformat(),
                    "raw_text":  f"{title}. {desc}",
                })

        except Exception as e:
            print(f"  {feed['name']} RSS error: {e}")

    print(f"  Fire stories found: {len(stories)}")
    return stories


# ─── STEP 2: ADDRESS EXTRACTION ──────────────────────────────────────────────

def extract_addresses(stories: list) -> list:
    records = []
    for story in stories:
        matches = ADDRESS_RE.findall(story["raw_text"])
        if not matches:
            continue
        seen = set()
        for house_num, street_name, street_type in matches:
            full_street = f"{street_name.strip()} {street_type.strip()}"
            key = f"{house_num}|{full_street.upper()[:20]}"
            if key in seen:
                continue
            seen.add(key)
            records.append({
                **story,
                "extracted_address": f"{house_num} {full_street}".strip(),
                "house_num":         house_num.strip(),
                "street_name":       full_street.strip(),
            })
    return records


# ─── STEP 3: ASSESSMENT LOOKUP ───────────────────────────────────────────────

def lookup_assessments(records: list) -> pd.DataFrame:
    print(f"[2/3] Assessment lookup for {len(records)} extracted addresses...")
    all_results = []

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        conditions = " OR ".join(
            f'("PROPERTYHOUSENUM" = \'{r["house_num"]}\' AND '
            f'"PROPERTYADDRESS" ILIKE \'%{re.sub(chr(39), "", r["street_name"])[:14].upper()}%\')'
            for r in batch
        )
        sql = (
            f'SELECT "PARID","PROPERTYHOUSENUM","PROPERTYADDRESS","PROPERTYCITY",'
            f'"PROPERTYSTATE","PROPERTYZIP","OWNERDESC",'
            f'"CHANGENOTICEADDRESS1","CHANGENOTICEADDRESS2",'
            f'"CHANGENOTICEADDRESS3","CHANGENOTICEADDRESS4",'
            f'"FAIRMARKETTOTAL","BEDROOMS","YEARBLT","FINISHEDLIVINGAREA",'
            f'"CONDITIONDESC","HOMESTEADFLAG","CLASSDESC"'
            f' FROM "{ASSESSMENTS_ID}"'
            f' WHERE ({conditions})'
            f' AND "FAIRMARKETTOTAL" >= {MIN_FMV}'
            f' AND "FAIRMARKETTOTAL" <= {MAX_FMV}'
            f' AND "CLASSDESC" ILIKE \'%RESIDENTIAL%\''
        )
        try:
            resp = requests.post(WPRDC_SQL, data={"sql": sql}, timeout=30)
            data = resp.json()
            if data.get("success"):
                for hit in data["result"]["records"]:
                    prop_house = str(hit.get("PROPERTYHOUSENUM", "")).strip()
                    prop_addr  = str(hit.get("PROPERTYADDRESS", "")).upper()
                    for rec in batch:
                        if (prop_house == rec["house_num"] and
                                rec["street_name"][:8].upper() in prop_addr):
                            all_results.append({**hit, **rec})
                            break
        except Exception as e:
            print(f"  Batch {i // BATCH_SIZE} error: {e}")
        time.sleep(0.15)

    print(f"  {len(all_results)} addresses matched to assessments")
    return pd.DataFrame(all_results) if all_results else pd.DataFrame()


# ─── STEP 4: BUILD OUTPUT ────────────────────────────────────────────────────

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
        # No assessment matches — still output raw addresses, lower score
        if not records:
            return pd.DataFrame()
        df_raw = pd.DataFrame(records)
        output = pd.DataFrame({
            "property_address": df_raw["extracted_address"],
            "property_city":    "",
            "property_state":   "PA",
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
            "data_type":        "fire_news",
            "news_source":      df_raw["source"],
            "headline":         df_raw["title"],
            "article_date":     df_raw["pub_date"],
            "article_url":      df_raw["link"],
            "source":           "NewsRSS",
            "date_pulled":      date.today().isoformat(),
            "score":            60,
            "flags":            "Fire Damage, Address Unverified",
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
        "data_type":        "fire_news",
        "news_source":      assess_df["source"].fillna(""),
        "headline":         assess_df["title"].fillna(""),
        "article_date":     assess_df["pub_date"].fillna(""),
        "article_url":      assess_df["link"].fillna(""),
        "source":           "NewsRSS+" + assess_df["source"].fillna(""),
        "date_pulled":      date.today().isoformat(),
        "score":            0,
        "flags":            "",
    })

    scores, flags_list = [], []
    for _, row in output.iterrows():
        score = 75    # fire = confirmed distress
        flags = ["Fire Damage"]

        # Recency bonus
        try:
            days_ago = (date.today() - date.fromisoformat(str(row.get("article_date", "")))).days
            if days_ago <= 7:
                score = min(score + 10, 100)
                flags.append(f"Fresh Fire ({days_ago}d ago)")
            elif days_ago <= 14:
                score = min(score + 5, 100)
                flags.append(f"Fire ({days_ago}d ago)")
        except Exception:
            pass

        # ARV bonus
        arv = int(row.get("est_arv", 0) or 0)
        if arv > 300_000:
            score = min(score + 10, 100)
            flags.append(f"High Equity (${arv:,} ARV)")
        elif arv > 150_000:
            score = min(score + 5, 100)
            flags.append(f"Sweet Spot ARV (${arv:,})")

        # Absentee owner
        mail_state = str(row.get("mailing_state", "") or "").strip().upper()
        mail_city  = str(row.get("mailing_city",  "") or "").strip().lower()
        prop_city  = str(row.get("property_city", "") or "").strip().lower()
        if mail_state not in ("", "PA") or (mail_city and mail_city != prop_city):
            score = min(score + 5, 100)
            flags.append("Absentee Owner")

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
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--json",          action="store_true")
    args = parser.parse_args()

    output_dir    = os.path.abspath(args.output_dir)
    dashboard_dir = os.path.abspath(args.dashboard_dir)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    start = time.time()
    print("=" * 60)
    print("WBP — Allegheny Fire News Scraper")
    print(f"Date:     {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Lookback: {args.lookback_days} days")
    print("=" * 60)

    print("[1/3] Fetching RSS feeds...")
    stories   = fetch_rss_stories(args.lookback_days)
    records   = extract_addresses(stories)
    print(f"  Addresses extracted: {len(records)}")

    assess_df = pd.DataFrame()
    if records:
        assess_df = lookup_assessments(records)

    output = build_output(records, assess_df)

    fname = f"allegheny_fire_news_{date.today().strftime('%Y%m%d')}.csv"
    fpath = os.path.join(output_dir, fname)
    output.to_csv(fpath, index=False)

    if args.json and not output.empty:
        recs_out = output.to_dict("records")
        json_data = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source":     "Pittsburgh News RSS Feeds (WTAE/WPXI/KDKA/PG)",
            "total":      len(recs_out),
            "records":    recs_out,
        }
        for path in [os.path.join(dashboard_dir, "fire_news.json"),
                     os.path.join(os.path.dirname(output_dir), "data", "fire_news.json")]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(json_data, f, indent=2)

    elapsed = time.time() - start
    hot = output[output["score"] >= 80] if not output.empty else pd.DataFrame()
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(output)} fire leads → {fname}")
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

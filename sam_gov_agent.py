#!/usr/bin/env python3
"""
SAM.gov Contract Fetching Agent
================================

Queries the SAM.gov "Get Opportunities" Public API (v2) for ACTIVE federal
contract opportunities that are set aside for the following programs:

    * WOSB    - Women-Owned Small Business
    * EDWOSB  - Economically Disadvantaged Women-Owned Small Business
    * SDVOSB  - Service-Disabled Veteran-Owned Small Business
    * 8(a)    - 8(a) Business Development Program

It filters to opportunities with an estimated / award value greater than a
configurable threshold (default: $10,000), fetches full details, sorts the
results by NAICS category and then by dollar value (descending), and writes a
polished, responsive HTML report (``sam_gov_listings.html``).

Usage
-----
    1. pip install -r requirements.txt
    2. Create a .env file (see .env.example) containing your API key:
           SAM_GOV_API_KEY=your_api_key_here
    3. python sam_gov_agent.py

The API key is NEVER hard-coded. It is read from the SAM_GOV_API_KEY
environment variable (a .env file is loaded automatically if present).

API reference: https://open.gsa.gov/api/get-opportunities-public-api/
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - python-dotenv is optional at runtime
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

API_URL = "https://api.sam.gov/opportunities/v2/search"

# SAM.gov "typeOfSetAside" codes. We include both the competitive/program
# set-aside codes AND the sole-source codes for each program so we surface as
# many relevant opportunities as possible.
SET_ASIDE_CODES = {
    "WOSB": "Women-Owned Small Business (WOSB)",
    "WOSBSS": "WOSB Sole Source",
    "EDWOSB": "Economically Disadvantaged WOSB (EDWOSB)",
    "EDWOSBSS": "EDWOSB Sole Source",
    "SDVOSBC": "Service-Disabled Veteran-Owned Small Business (SDVOSB)",
    "SDVOSBS": "SDVOSB Sole Source",
    "8A": "8(a) Business Development",
    "8AN": "8(a) Sole Source",
}

# Minimum estimated/award value (USD). Opportunities below this are excluded.
MIN_VALUE = float(os.environ.get("SAM_MIN_VALUE", "10000"))

# Many active solicitations do not publish a dollar figure. When True, such
# opportunities are still included (with value shown as "Not specified") so the
# report is not empty; set SAM_INCLUDE_UNKNOWN_VALUE=false to hide them.
INCLUDE_UNKNOWN_VALUE = os.environ.get(
    "SAM_INCLUDE_UNKNOWN_VALUE", "true"
).strip().lower() not in ("false", "0", "no")

# How many days back to search (SAM.gov requires postedFrom/postedTo, max 1yr).
LOOKBACK_DAYS = int(os.environ.get("SAM_LOOKBACK_DAYS", "365"))

# Only include opportunities that are still active (response deadline open).
ACTIVE_ONLY = os.environ.get("SAM_ACTIVE_ONLY", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)

PAGE_SIZE = 1000  # SAM.gov max limit per request
REQUEST_TIMEOUT = 60
OUTPUT_FILE = os.environ.get("SAM_OUTPUT_FILE", "sam_gov_listings.html")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def get_api_key() -> str:
    key = os.environ.get("SAM_GOV_API_KEY", "").strip()
    if not key:
        sys.exit(
            "ERROR: SAM_GOV_API_KEY is not set.\n"
            "Create a .env file (see .env.example) with:\n"
            "    SAM_GOV_API_KEY=your_api_key_here\n"
            "or export it:  export SAM_GOV_API_KEY=your_api_key_here"
        )
    return key


def parse_value(opp: dict):
    """Best-effort extraction of a dollar value from an opportunity record.

    Returns a float value or None if no value could be determined.
    """
    award = opp.get("award") or {}
    amount = award.get("amount")
    if amount is not None:
        try:
            val = float(str(amount).replace(",", "").replace("$", "").strip())
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return None


def opp_naics(opp: dict) -> str:
    code = opp.get("naicsCode")
    if code:
        return str(code)
    naics_list = opp.get("naics") or []
    if isinstance(naics_list, list) and naics_list:
        first = naics_list[0]
        if isinstance(first, dict):
            codes = first.get("code")
            if isinstance(codes, list) and codes:
                return str(codes[0])
            if codes:
                return str(codes)
    return "Uncategorized"


def format_date(value: str) -> str:
    if not value:
        return "N/A"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%b %d, %Y")
        except ValueError:
            continue
    # Try trimming fractional seconds / trailing text
    try:
        return datetime.fromisoformat(value.split("+")[0].split(".")[0]).strftime(
            "%b %d, %Y"
        )
    except Exception:
        return value


def place_of_performance(opp: dict) -> str:
    pop = opp.get("placeOfPerformance") or {}
    if not pop:
        return "N/A"
    parts = []
    city = pop.get("city")
    if isinstance(city, dict):
        city = city.get("name")
    state = pop.get("state")
    if isinstance(state, dict):
        state = state.get("name") or state.get("code")
    country = pop.get("country")
    if isinstance(country, dict):
        country = country.get("name") or country.get("code")
    for p in (city, state, country):
        if p:
            parts.append(str(p))
    return ", ".join(parts) if parts else "N/A"


# --------------------------------------------------------------------------- #
# API fetching
# --------------------------------------------------------------------------- #


def fetch_for_set_aside(api_key: str, code: str, posted_from: str, posted_to: str):
    """Fetch all opportunities for a single set-aside code, handling pagination."""
    results = []
    offset = 0
    while True:
        params = {
            "api_key": api_key,
            "postedFrom": posted_from,
            "postedTo": posted_to,
            "typeOfSetAside": code,
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        try:
            resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  ! Network error for {code} (offset {offset}): {exc}")
            break

        if resp.status_code == 429:
            print("  ! Rate limited (HTTP 429). Waiting 10s and retrying...")
            time.sleep(10)
            continue
        if resp.status_code != 200:
            snippet = resp.text[:300]
            print(f"  ! API error {resp.status_code} for {code}: {snippet}")
            break

        try:
            data = resp.json()
        except ValueError:
            print(f"  ! Could not decode JSON for {code}")
            break

        batch = data.get("opportunitiesData") or []
        total = data.get("totalRecords", 0)
        results.extend(batch)
        print(
            f"  - {code}: fetched {len(results)}/{total} (offset {offset})"
        )

        offset += PAGE_SIZE
        if offset >= total or not batch:
            break
        time.sleep(0.5)  # be polite to the API

    return results


def fetch_all(api_key: str):
    posted_to = datetime.now().strftime("%m/%d/%Y")
    posted_from = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    print(f"Searching SAM.gov opportunities posted {posted_from} - {posted_to}\n")

    seen = {}
    for code, label in SET_ASIDE_CODES.items():
        print(f"Querying set-aside: {code} ({label})")
        for opp in fetch_for_set_aside(api_key, code, posted_from, posted_to):
            notice_id = opp.get("noticeId") or opp.get("solicitationNumber")
            if not notice_id:
                notice_id = id(opp)
            # tag which set-aside matched (keep the friendly label)
            opp["_setAsideLabel"] = label
            opp["_setAsideCode"] = code
            if notice_id not in seen:
                seen[notice_id] = opp
        print()
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Filtering & sorting
# --------------------------------------------------------------------------- #


def process(opportunities):
    kept = []
    now = datetime.now()
    for opp in opportunities:
        # Active-only filter
        if ACTIVE_ONLY:
            active_flag = str(opp.get("active", "")).strip().lower()
            if active_flag == "no":
                continue
            deadline = opp.get("responseDeadLine")
            if deadline:
                try:
                    dl = datetime.fromisoformat(
                        deadline.split("+")[0].split(".")[0]
                    )
                    if dl < now:
                        continue
                except Exception:
                    pass

        value = parse_value(opp)
        opp["_value"] = value
        if value is None:
            if not INCLUDE_UNKNOWN_VALUE:
                continue
        elif value <= MIN_VALUE:
            continue

        opp["_naics"] = opp_naics(opp)
        kept.append(opp)

    # Sort by NAICS category, then by value descending (unknown values last).
    kept.sort(
        key=lambda o: (
            o["_naics"],
            -(o["_value"] if o["_value"] is not None else -1),
        )
    )
    return kept


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #

SET_ASIDE_COLORS = {
    "WOSB": "#c2185b",
    "WOSBSS": "#c2185b",
    "EDWOSB": "#7b1fa2",
    "EDWOSBSS": "#7b1fa2",
    "SDVOSBC": "#00695c",
    "SDVOSBS": "#00695c",
    "8A": "#e65100",
    "8AN": "#e65100",
}


def esc(value) -> str:
    return html.escape(str(value if value is not None else "N/A"))


def money(value) -> str:
    if value is None:
        return "Not specified"
    return "${:,.0f}".format(value)


def build_html(opportunities) -> str:
    generated = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    # Group by NAICS
    groups = defaultdict(list)
    for opp in opportunities:
        groups[opp["_naics"]].append(opp)
    sorted_naics = sorted(groups.keys())

    total = len(opportunities)
    valued = [o for o in opportunities if o["_value"] is not None]
    total_value = sum(o["_value"] for o in valued)

    # ---- Table of contents ----
    toc_items = "".join(
        f'<li><a href="#cat-{esc(code)}">{esc(code)} '
        f'<span class="toc-count">({len(groups[code])})</span></a></li>'
        for code in sorted_naics
    )

    # ---- Category sections ----
    sections = []
    for code in sorted_naics:
        cards = []
        for opp in groups[code]:
            sac = opp.get("_setAsideCode", "")
            color = SET_ASIDE_COLORS.get(sac, "#455a64")
            link = opp.get("uiLink") or opp.get("link") or "#"
            cards.append(
                f"""
        <div class="card">
          <div class="card-head">
            <h3 class="card-title">{esc(opp.get('title'))}</h3>
            <span class="badge" style="background:{color}">{esc(opp.get('_setAsideLabel'))}</span>
          </div>
          <div class="card-grid">
            <div><span class="lbl">Solicitation #</span>{esc(opp.get('solicitationNumber'))}</div>
            <div><span class="lbl">Estimated Value</span><span class="value">{esc(money(opp.get('_value')))}</span></div>
            <div><span class="lbl">NAICS</span>{esc(opp.get('_naics'))}</div>
            <div><span class="lbl">Notice Type</span>{esc(opp.get('type'))}</div>
            <div><span class="lbl">Agency</span>{esc(opp.get('fullParentPathName') or opp.get('organizationName'))}</div>
            <div><span class="lbl">Office</span>{esc(opp.get('officeAddress', {}).get('city') if isinstance(opp.get('officeAddress'), dict) else opp.get('office'))}</div>
            <div><span class="lbl">Posted</span>{esc(format_date(opp.get('postedDate')))}</div>
            <div><span class="lbl">Response Deadline</span>{esc(format_date(opp.get('responseDeadLine')))}</div>
            <div class="wide"><span class="lbl">Place of Performance</span>{esc(place_of_performance(opp))}</div>
          </div>
          <a class="view-link" href="{esc(link)}" target="_blank" rel="noopener">View on SAM.gov &rarr;</a>
        </div>"""
            )
        sections.append(
            f"""
      <section id="cat-{esc(code)}" class="category">
        <h2 class="cat-title">NAICS {esc(code)} <span class="cat-count">{len(groups[code])} opportunit{'y' if len(groups[code]) == 1 else 'ies'}</span></h2>
        {''.join(cards)}
      </section>"""
        )

    set_aside_summary = ", ".join(sorted({c for c in SET_ASIDE_CODES}))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAM.gov Set-Aside Opportunities</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:#f4f6f8; color:#1f2933; }}
  header.top {{ background:linear-gradient(135deg,#0b213f,#12345c); color:#fff; padding:36px 24px; }}
  header.top h1 {{ margin:0 0 6px; font-size:28px; }}
  header.top p {{ margin:2px 0; color:#c7d3e0; font-size:14px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:16px; margin-top:20px; }}
  .stat {{ background:rgba(255,255,255,.1); border:1px solid rgba(255,255,255,.15); border-radius:10px; padding:14px 20px; min-width:150px; }}
  .stat .n {{ font-size:26px; font-weight:700; }}
  .stat .l {{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:#c7d3e0; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:24px; }}
  .toc {{ background:#fff; border:1px solid #e1e7ee; border-radius:12px; padding:20px 24px; margin-bottom:28px; }}
  .toc h2 {{ margin:0 0 12px; font-size:16px; text-transform:uppercase; letter-spacing:.5px; color:#52606d; }}
  .toc ul {{ list-style:none; margin:0; padding:0; display:flex; flex-wrap:wrap; gap:10px; }}
  .toc a {{ display:inline-block; text-decoration:none; background:#eef2f7; color:#12345c; padding:6px 12px; border-radius:20px; font-size:13px; font-weight:600; }}
  .toc a:hover {{ background:#12345c; color:#fff; }}
  .toc-count {{ color:#7b8794; font-weight:400; }}
  .category {{ margin-bottom:36px; }}
  .cat-title {{ font-size:20px; border-bottom:3px solid #12345c; padding-bottom:8px; margin-bottom:16px; }}
  .cat-count {{ font-size:13px; font-weight:400; color:#7b8794; }}
  .card {{ background:#fff; border:1px solid #e1e7ee; border-radius:12px; padding:20px 22px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,.04); }}
  .card-head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:14px; margin-bottom:14px; }}
  .card-title {{ margin:0; font-size:17px; color:#0b213f; flex:1; }}
  .badge {{ color:#fff; font-size:11px; font-weight:700; padding:5px 11px; border-radius:20px; white-space:nowrap; text-transform:uppercase; letter-spacing:.4px; }}
  .card-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px 20px; }}
  .card-grid > div {{ font-size:14px; }}
  .card-grid .wide {{ grid-column:1/-1; }}
  .lbl {{ display:block; font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#7b8794; margin-bottom:2px; }}
  .value {{ font-weight:700; color:#00695c; }}
  .view-link {{ display:inline-block; margin-top:14px; background:#12345c; color:#fff; text-decoration:none; padding:8px 16px; border-radius:8px; font-size:13px; font-weight:600; }}
  .view-link:hover {{ background:#0b213f; }}
  footer {{ text-align:center; padding:24px; color:#7b8794; font-size:12px; }}
</style>
</head>
<body>
<header class="top">
  <h1>SAM.gov Set-Aside Contract Opportunities</h1>
  <p>Generated {generated}</p>
  <p>Set-aside programs: {esc(set_aside_summary)}</p>
  <p>Filter: estimated value &gt; {money(MIN_VALUE)} &bull; Active opportunities{' only' if ACTIVE_ONLY else ''}</p>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">Opportunities</div></div>
    <div class="stat"><div class="n">{len(sorted_naics)}</div><div class="l">NAICS Categories</div></div>
    <div class="stat"><div class="n">{money(total_value)}</div><div class="l">Total Valued</div></div>
  </div>
</header>
<div class="wrap">
  <nav class="toc">
    <h2>Categories</h2>
    <ul>{toc_items or '<li>No results</li>'}</ul>
  </nav>
  {''.join(sections) if sections else '<p style="text-align:center;color:#7b8794;padding:40px;">No matching opportunities were found for the current filters.</p>'}
</div>
<footer>
  Data sourced from the U.S. General Services Administration SAM.gov Opportunities API.
  This report is for informational purposes only &mdash; always verify details on
  <a href="https://sam.gov" target="_blank" rel="noopener">SAM.gov</a>.
</footer>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="sam_gov_agent",
        description=(
            "Fetch active SAM.gov set-aside contract opportunities "
            "(WOSB, EDWOSB, SDVOSB, 8a) and generate an HTML report."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o",
        "--output",
        default=OUTPUT_FILE,
        help="Path of the HTML report to generate.",
    )
    parser.add_argument(
        "-m",
        "--min-value",
        type=float,
        default=MIN_VALUE,
        help="Minimum estimated/award value (USD) to include.",
    )
    parser.add_argument(
        "-d",
        "--days",
        type=int,
        default=LOOKBACK_DAYS,
        help="How many days back to search (max 365).",
    )
    parser.add_argument(
        "-s",
        "--set-aside",
        action="append",
        choices=sorted(SET_ASIDE_CODES.keys()),
        metavar="CODE",
        help=(
            "Limit to specific set-aside code(s); repeatable. "
            "Choices: " + ", ".join(sorted(SET_ASIDE_CODES.keys())) + ". "
            "Default: all of them."
        ),
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Include opportunities regardless of active status.",
    )
    parser.add_argument(
        "--exclude-unknown-value",
        action="store_true",
        help="Exclude opportunities that have no published dollar value.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="SAM.gov API key (overrides SAM_GOV_API_KEY env var). "
        "Prefer the environment variable / .env file for security.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    global MIN_VALUE, LOOKBACK_DAYS, OUTPUT_FILE, ACTIVE_ONLY
    global INCLUDE_UNKNOWN_VALUE, SET_ASIDE_CODES

    args = parse_args(argv)

    MIN_VALUE = args.min_value
    LOOKBACK_DAYS = max(1, min(args.days, 365))
    OUTPUT_FILE = args.output
    if args.include_all:
        ACTIVE_ONLY = False
    if args.exclude_unknown_value:
        INCLUDE_UNKNOWN_VALUE = False
    if args.set_aside:
        SET_ASIDE_CODES = {
            code: SET_ASIDE_CODES[code] for code in args.set_aside
        }
    if args.api_key:
        os.environ["SAM_GOV_API_KEY"] = args.api_key

    print("=" * 60)
    print("  SAM.gov Set-Aside Contract Fetching Agent")
    print("=" * 60 + "\n")

    api_key = get_api_key()

    raw = fetch_all(api_key)
    print(f"Retrieved {len(raw)} unique opportunities across all set-asides.")

    processed = process(raw)
    print(
        f"{len(processed)} opportunities remain after filtering "
        f"(value > {money(MIN_VALUE)}"
        f"{', including unspecified values' if INCLUDE_UNKNOWN_VALUE else ''})."
    )

    report = build_html(processed)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(f"\nReport written to: {os.path.abspath(OUTPUT_FILE)}")
    print("Open it in your web browser to review the opportunities.")


if __name__ == "__main__":
    main()

"""
LEADFORGE — Stage 1: Collect the Leads
========================================

WHAT THIS SCRIPT DOES (high level pipeline):

    1. Query the Overpass API for businesses of a given category, in a given city.
       (Or, if --input-json is given, load a pre-fetched Overpass JSON export
       instead of calling the live API — see "OFFLINE / OUTAGE FALLBACK" below.)
    2. Drop businesses that have no website (per project rule: we need site_text later).
    3. Fetch each business's homepage (and About/Services page if we can find one).
    4. Extract clean, readable text from those pages using Trafilatura.
    5. Deduplicate leads: first by domain (exact), then by fuzzy name matching.
    6. Assign each surviving lead a stable, unique lead_id.
    7. Write everything to data/01_leads.jsonl (one JSON object per line).

USAGE:
    python stages/01_collect.py --category amenity=car_repair --city Lahore --limit 20

    --category    Overpass tag in "key=value" form (e.g. amenity=dentist, shop=car_repair)
    --city        City name, used only for labeling leads and for the bounding box lookup
    --bbox        Optional manual bounding box "south,west,north,east" (overrides city lookup)
    --limit       Max number of RAW businesses to pull from Overpass (for fast test runs)
    --target      How many USABLE leads (with real site_text) we're trying to reach (default 100)
    --input-json  Path to a pre-fetched Overpass JSON export (see below), used INSTEAD of
                  calling the live Overpass API. Everything downstream (parsing, fetching
                  websites, dedup, writing) still runs exactly the same.

OFFLINE / OUTAGE FALLBACK (--input-json):
    Public Overpass servers occasionally go down or rate-limit hard (we hit this during
    development — 406/502/timeout across all three configured mirrors at once). When that
    happens, --input-json lets us keep moving without inventing data:

        1. Go to https://overpass-turbo.eu/
        2. Run the EXACT same query this script would send (see build_overpass_query below,
           or just run the collector with -v to print it).
        3. Click Export -> "raw data" (GeoJSON) or use the "Data" tab and save the raw
           Overpass JSON response to a file, e.g. data/overpass_export_lahore.json.
        4. Run: python stages/01_collect.py --category shop=car_repair --city Lahore
                    --input-json data/overpass_export_lahore.json

    This is still 100% real Overpass data — same query, same source — just fetched through
    the browser instead of a live HTTP call from this script, because the live endpoints
    were down. It should be treated as a temporary, disclosed workaround (tell the team when
    you use it), not a silent replacement for the live query. Switch back to live querying
    once the API is healthy again.

WHY EACH LIBRARY IS USED:
    requests    -> talk to Overpass API and business websites over HTTP
    trafilatura -> extract clean readable text from a webpage's raw HTML
    rapidfuzz   -> fuzzy string matching, to catch near-duplicate business names
    argparse    -> lets us run this from the command line with --limit etc (Requirement 4)
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import urlparse

import requests
import trafilatura
from rapidfuzz import fuzz
from urllib.robotparser import RobotFileParser


# ---------------------------------------------------------------------------
# CONFIG / CONSTANTS
# ---------------------------------------------------------------------------

# Multiple Overpass servers, tried in order. Public Overpass servers get
# overloaded/rate-limited fairly often (we hit this ourselves during
# development), so falling back to a second/third server is a real
# reliability need, not just nice-to-have.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Per project Rule 3: identify our crawler honestly in the User-Agent.
USER_AGENT = "LeadForgeSprintBot/1.0 (student internship project; contact: your-email@example.com)"

# Per project Rule 3: be gentle, ~1 request per second per site.
REQUEST_DELAY_SECONDS = 1.0

# Per project Requirement: site_text is capped at 4,000 characters.
MAX_SITE_TEXT_CHARS = 4000

# HTTP timeout for fetching a business website (seconds). Real sites can be slow/dead.
FETCH_TIMEOUT_SECONDS = 10

# Fuzzy name-matching threshold (0-100). Two names scoring >= this are treated
# as the same business. 90 is deliberately strict-ish to avoid false merges
# (e.g. "Bundu Khan" vs "Bundu khan" merges; "Cafe Costa" vs "Cafe Delice" does not).
FUZZY_MATCH_THRESHOLD = 90

# Rough city bounding boxes (south, west, north, east).
# NOTE: this is an IMPLEMENTATION DECISION, not a project requirement.
# The project only specifies "one city" - it does not mandate how we find its
# coordinates. A geocoding API would be more general, but a small hardcoded
# lookup keeps Stage 1 simple, dependency-free, and fast, per Section 3/14
# ("keep the solution simple", "avoid unnecessary frameworks").
# Add more cities here as needed, or pass --bbox manually to bypass this entirely.
CITY_BBOXES = {
    "lahore": (31.3000, 74.1000, 31.7000, 74.5500),
}


# ---------------------------------------------------------------------------
# STEP 3/4 — BUILD + SEND THE OVERPASS QUERY
# ---------------------------------------------------------------------------

def build_overpass_query(category_key, category_value, bbox, raw_limit):
    """
    Builds an Overpass QL query string for a given tag (key=value) inside a
    bounding box, capped at raw_limit raw results.

    We query BOTH nodes and ways because small businesses are usually mapped
    as a single point (node), while larger footprints (malls, big buildings)
    are sometimes mapped as an outline (way). 'out center tags' gives us a
    single usable lat/lon even for a way.
    """
    south, west, north, east = bbox
    query = f"""
    [out:json][timeout:25];
    (
      node["{category_key}"="{category_value}"]({south},{west},{north},{east});
      way["{category_key}"="{category_value}"]({south},{west},{north},{east});
    );
    out center tags {raw_limit};
    """
    return query


def query_overpass(category_key, category_value, bbox, raw_limit):
    """
    Sends the Overpass query and returns the list of raw 'elements'
    (each one is a dict with 'tags', and either lat/lon or a 'center').

    Tries each server in OVERPASS_ENDPOINTS in order, moving to the next
    one if the current one fails (timeout, 5xx error, bad response, etc).
    This matters in practice: public Overpass servers get overloaded and
    return errors fairly often, and a single-server script would fail
    completely in that situation.

    Handles network errors gracefully (Rule 5: one failure must not crash
    the whole pipeline) - if EVERY server fails, we print a clear message
    and return an empty list rather than crashing.
    """
    query = build_overpass_query(category_key, category_value, bbox, raw_limit)
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}

    for i, endpoint in enumerate(OVERPASS_ENDPOINTS, start=1):
        print(f"[INFO] Trying Overpass server {i}/{len(OVERPASS_ENDPOINTS)}: {endpoint}")
        try:
            response = requests.post(
                endpoint,
                data={"data": query},
                headers=headers,
                timeout=30,
            )
            if response.status_code != 200:
                print(f"[WARN] Server returned HTTP {response.status_code}, trying next server.")
                continue

            data = response.json()
            elements = data.get("elements", [])
            print(f"[INFO] Success. Overpass returned {len(elements)} raw businesses.")
            return elements

        except requests.exceptions.RequestException as e:
            print(f"[WARN] Request to {endpoint} failed: {e}. Trying next server.")
            continue
        except ValueError:
            print(f"[WARN] {endpoint} did not return valid JSON. Trying next server.")
            continue

    print("[ERROR] All Overpass servers failed. Could not fetch data.", file=sys.stderr)
    return []


def load_overpass_export(path, raw_limit):
    """
    OFFLINE / OUTAGE FALLBACK: loads a pre-fetched Overpass JSON export from
    disk instead of calling the live API. Used when --input-json is passed.

    Expects the same raw JSON shape the live API returns, i.e. a dict with
    an "elements" list (this is exactly what Overpass Turbo's "Export ->
    raw data" / "Data" tab gives you, and exactly what query_overpass()
    would have parsed if the live call had succeeded).

    Still real Overpass data pulled with the same query - just fetched
    through the browser because the live endpoints were down. Truncates to
    raw_limit so --limit behaves the same way it would for a live query.

    Handles a malformed/missing file gracefully (Rule 5) - prints a clear
    error and returns an empty list rather than crashing.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] --input-json file not found: {path}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"[ERROR] --input-json file is not valid JSON: {e}", file=sys.stderr)
        return []

    elements = data.get("elements", [])
    if not elements:
        print(f"[WARN] {path} contained no 'elements' - check it's a raw Overpass "
              f"JSON export (Overpass Turbo -> Export -> raw data), not the GeoJSON one.")

    elements = elements[:raw_limit]
    print(f"[INFO] Loaded {len(elements)} raw businesses from offline export: {path}")
    return elements


# ---------------------------------------------------------------------------
# STEP 5 — PARSE BUSINESS INFO FROM A RAW OVERPASS ELEMENT
# ---------------------------------------------------------------------------

def parse_business(element, city, category_value):
    """
    Pulls the fields we care about out of one raw Overpass element.
    Returns None if the business has no usable name (nothing to work with)
    or no website tag at all (project rule: skip businesses with no website).

    IMPORTANT (per Rule 4 - never invent info): every field we can't find
    is left as an empty string, never guessed or fabricated.
    """
    tags = element.get("tags", {})

    # Prefer an English name if present (OSM sometimes stores a local-script
    # name in "name" and an English version in "name:en").
    name = tags.get("name:en") or tags.get("name")
    if not name:
        return None  # unusable - nothing to identify this business by

    # website / contact:website - the actual field we need for scraping.
    # NOTE: we deliberately do NOT fall back to brand:website, because that
    # points to a corporate/global site, not this specific business location.
    website = tags.get("website") or tags.get("contact:website")
    if not website:
        return None  # project rule: ignore businesses without a website

    # Treat social-media "websites" as not usable, since Trafilatura can't
    # meaningfully extract article-style text from Facebook/Instagram pages
    # (they require login and are mostly JS-rendered).
    # FLAGGED IMPLEMENTATION DECISION: not an explicit project rule, but
    # follows from Rule 4/5 - a scraped login-wall would produce garbage
    # site_text, which is worse than skipping the lead.
    #
    # We match on short fragments ("facebo", "instagr", "fb.com") rather than
    # exact domains, because real OSM data contains typos - e.g. we found
    # "m.facebok.com" (missing an 'o') in real Lahore cafe data during testing.
    # An exact "facebook.com" check would have silently let that one through.
    social_fragments = ("facebo", "instagr", "fb.com", "fb.me")
    website_lower = website.lower()
    if any(frag in website_lower for frag in social_fragments):
        return None

    phone = tags.get("phone") or tags.get("contact:phone") or ""

    return {
        "name": name.strip(),
        "website_raw": website.strip(),
        "city": city,
        "category": category_value,
        "phone": phone.strip(),
    }


# ---------------------------------------------------------------------------
# STEP 6 — URL / DOMAIN HANDLING
# ---------------------------------------------------------------------------

def normalize_url(raw_url):
    """
    Makes sure a URL has a scheme (https://) so requests can actually use it.
    OSM data sometimes stores URLs without "http(s)://" at all.
    """
    raw_url = raw_url.strip()
    if not raw_url:
        return None
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    return raw_url


def extract_domain(url):
    """
    Extracts a clean domain from a URL, e.g.:
        "http://www.lalqila.com/lahore/contact-us/" -> "lalqila.com"
    Strips the "www." prefix so "www.abc.com" and "abc.com" are treated as
    the SAME domain during deduplication.
    Returns None if the URL is too malformed to parse.
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain if domain else None
    except Exception:
        return None


def guess_about_url(homepage_url):
    """
    Given a homepage URL, guesses a likely About/Services page URL.
    This is a best-effort heuristic, not a guarantee - if it 404s later,
    Step 7's error handling will simply skip it (Rule 5: don't crash).
    """
    candidates = ["about", "about-us", "services", "our-services"]
    base = homepage_url.rstrip("/")
    return [f"{base}/{path}" for path in candidates]


# ---------------------------------------------------------------------------
# ROBOTS.TXT CHECK (project Rule 3: respect robots.txt)
# ---------------------------------------------------------------------------

def allowed_by_robots(url):
    """
    Checks the site's robots.txt to see if OUR crawler is allowed to fetch
    this specific URL. We download robots.txt ourselves with a short
    timeout (rather than letting RobotFileParser.read() do it, which can
    hang indefinitely on a slow/dead server).

    If robots.txt doesn't exist or can't be fetched, we assume fetching
    is allowed (this is the standard convention - no robots.txt means no
    stated restrictions).
    """
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        response = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=5)

        if response.status_code >= 400:
            return True  # no robots.txt -> no stated restriction

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)

    except Exception:
        # If we can't check robots.txt for any reason, don't block the
        # whole pipeline over it (Rule 5) - default to allowed.
        return True


# ---------------------------------------------------------------------------
# STEP 7 — FETCH WEBSITE CONTENT (SAFELY)
# ---------------------------------------------------------------------------

def fetch_page(url):
    """
    Fetches a single page's raw HTML, handling the many ways a real-world
    website can fail: timeouts, connection errors, bad status codes, etc.

    Returns the HTML text on success, or None on any failure - and NEVER
    raises an exception out of this function, so one bad website can never
    crash the whole script (project Rule 5).
    """
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return None
        return response.text
    except requests.exceptions.RequestException:
        # Covers: connection errors, timeouts, DNS failures, too many redirects, etc.
        return None


# ---------------------------------------------------------------------------
# STEP 8 — EXTRACT READABLE TEXT
# ---------------------------------------------------------------------------

def extract_readable_text(html):
    """
    Uses Trafilatura to pull clean, readable article-style text out of raw
    HTML - stripping nav bars, ads, cookie banners, scripts, etc.

    We never pass raw HTML downstream: raw HTML is full of markup noise that
    would waste space in our 4,000-char budget and confuse later NLP/LLM
    stages that read site_text.

    Returns "" (empty string, never None) if extraction fails or the page
    had no meaningful readable content.
    """
    if not html:
        return ""
    try:
        text = trafilatura.extract(html)
        return text.strip() if text else ""
    except Exception:
        return ""


def collect_site_text(homepage_url):
    """
    Fetches the homepage, then tries one likely About/Services page,
    combines their readable text, and caps the result at MAX_SITE_TEXT_CHARS.

    Rate limiting (Rule 3): we sleep between requests to the SAME site.
    """
    combined_text = ""

    if not allowed_by_robots(homepage_url):
        print(f"[INFO] robots.txt disallows fetching {homepage_url}, skipping.")
        return ""

    homepage_html = fetch_page(homepage_url)
    combined_text += extract_readable_text(homepage_html)

    # Respect rate limiting before hitting a second page on the same site.
    time.sleep(REQUEST_DELAY_SECONDS)

    # Try one plausible About/Services URL. We stop at the first one that
    # actually returns something - we are not trying every guess, to keep
    # the crawl fast and polite.
    for about_url in guess_about_url(homepage_url):
        if not allowed_by_robots(about_url):
            continue
        about_html = fetch_page(about_url)
        about_text = extract_readable_text(about_html)
        if about_text:
            combined_text += "\n\n" + about_text
            break

    return combined_text[:MAX_SITE_TEXT_CHARS]


# ---------------------------------------------------------------------------
# STEP 9 — DEDUPLICATION
# ---------------------------------------------------------------------------

def deduplicate_leads(leads):
    """
    Two-pass deduplication:

    PASS 1 - exact domain match. If two leads have the exact same domain,
    they are almost certainly the same business (e.g. two OSM nodes for the
    same restaurant's two branches, both linking the same homepage). We keep
    the FIRST occurrence and drop the rest.

    PASS 2 - fuzzy name match, only among leads that survived pass 1 (i.e.
    already have distinct domains). This catches cases like "Bundu Khan"
    appearing as several separately-mapped branches under different domains
    (or no domain overlap) but with clearly matching names. We use
    RapidFuzz's token_sort_ratio, which is tolerant of word order/case
    (e.g. "ABC Realty" vs "Realty ABC" would still score high) and a strict
    threshold (90/100) to avoid accidentally merging genuinely different
    businesses that just happen to have similar names.
    """
    # --- Pass 1: exact domain dedup ---
    seen_domains = set()
    domain_deduped = []
    for lead in leads:
        domain = lead["domain"]
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        domain_deduped.append(lead)

    print(f"[INFO] After domain dedup: {len(domain_deduped)} leads "
          f"(removed {len(leads) - len(domain_deduped)} exact domain duplicates).")

    # --- Pass 2: fuzzy name dedup ---
    final_leads = []
    for candidate in domain_deduped:
        is_duplicate = False
        for kept in final_leads:
            score = fuzz.token_sort_ratio(candidate["name"].lower(), kept["name"].lower())
            if score >= FUZZY_MATCH_THRESHOLD:
                is_duplicate = True
                break
        if not is_duplicate:
            final_leads.append(candidate)

    print(f"[INFO] After fuzzy name dedup: {len(final_leads)} leads "
          f"(removed {len(domain_deduped) - len(final_leads)} fuzzy duplicates).")

    return final_leads


# ---------------------------------------------------------------------------
# STEP 10 — LEAD ID GENERATION
# ---------------------------------------------------------------------------

def assign_lead_ids(leads, prefix="sd"):
    """
    Assigns stable, sequential IDs like sd_0001, sd_0002, ...
    Sequential (not random) so reruns produce predictable, debuggable IDs,
    per the project's explicit instruction not to use unstable/random IDs
    without good reason.
    """
    for i, lead in enumerate(leads, start=1):
        lead["lead_id"] = f"{prefix}_{i:04d}"
    return leads


# ---------------------------------------------------------------------------
# STEP 11 — WRITE JSONL
# ---------------------------------------------------------------------------

def write_jsonl(leads, output_path):
    """
    Writes one JSON object per line to output_path.
    Fields are ordered explicitly to match the project's required schema.
    """
    with open(output_path, "w", encoding="utf-8") as f:
        for lead in leads:
            record = {
                "lead_id": lead["lead_id"],
                "name": lead["name"],
                "domain": lead["domain"],
                "city": lead["city"],
                "category": lead["category"],
                "phone": lead["phone"],
                "site_text": lead["site_text"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[INFO] Wrote {len(leads)} leads to {output_path}")


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------

def run_pipeline(category_key, category_value, city, bbox, raw_limit, input_json=None):
    """
    Runs the full Stage 1 pipeline end to end and returns the final list
    of lead dicts (before writing to disk) - useful for testing.

    If input_json is given, raw businesses are loaded from that pre-fetched
    Overpass export instead of calling the live API (see load_overpass_export
    / the OFFLINE FALLBACK note at the top of the file). Everything after
    that point (parsing, fetching, dedup, ID assignment) is identical either
    way - only the source of the raw elements changes.
    """
    # 1. Get raw businesses - live Overpass query, or offline export fallback.
    if input_json:
        raw_elements = load_overpass_export(input_json, raw_limit)
    else:
        raw_elements = query_overpass(category_key, category_value, bbox, raw_limit)

    # 2. Parse + filter (must have name + real, non-social website).
    parsed_leads = []
    for element in raw_elements:
        business = parse_business(element, city, category_value)
        if business is None:
            continue
        parsed_leads.append(business)

    print(f"[INFO] {len(parsed_leads)} businesses have a usable website "
          f"(out of {len(raw_elements)} raw results).")

    if not parsed_leads:
        print("[WARN] No businesses with usable websites were found. "
              "Try a different --category, a larger --bbox, or a different --city.")
        return []

    # 3/6/7/8. Normalize URL, extract domain, fetch site, extract text.
    enriched_leads = []
    for business in parsed_leads:
        url = normalize_url(business["website_raw"])
        domain = extract_domain(url) if url else None
        if not domain:
            # Malformed URL we couldn't parse at all - skip, don't crash.
            continue

        print(f"[INFO] Fetching site text for: {business['name']} ({domain})")
        site_text = collect_site_text(url)

        business["domain"] = domain
        business["site_text"] = site_text
        enriched_leads.append(business)

        # Rate limiting between DIFFERENT businesses/sites (Rule 3).
        time.sleep(REQUEST_DELAY_SECONDS)

    # 9. Deduplicate.
    deduped_leads = deduplicate_leads(enriched_leads)

    # 10. Assign lead IDs.
    final_leads = assign_lead_ids(deduped_leads)

    return final_leads


def resolve_bbox(args):
    """
    Figures out which bounding box to use: an explicit --bbox always wins;
    otherwise we look the city up in our small CITY_BBOXES table.
    """
    if args.bbox:
        try:
            parts = [float(x.strip()) for x in args.bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            return tuple(parts)
        except ValueError:
            print("[ERROR] --bbox must be 4 comma-separated numbers: south,west,north,east",
                  file=sys.stderr)
            sys.exit(1)

    key = args.city.strip().lower()
    if key not in CITY_BBOXES:
        print(f"[ERROR] No known bounding box for city '{args.city}'. "
              f"Known cities: {list(CITY_BBOXES.keys())}. "
              f"Pass --bbox 'south,west,north,east' manually instead.",
              file=sys.stderr)
        sys.exit(1)
    return CITY_BBOXES[key]


def main():
    parser = argparse.ArgumentParser(
        description="LEADFORGE Stage 1 - collect business leads via Overpass API."
    )
    parser.add_argument(
        "--category", required=True,
        help='OSM tag in key=value form, e.g. "amenity=dentist" or "shop=car_repair"'
    )
    parser.add_argument(
        "--city", default="Lahore",
        help='City name (used for labeling leads and bbox lookup). Default: Lahore'
    )
    parser.add_argument(
        "--bbox", default=None,
        help='Optional manual bounding box "south,west,north,east", overrides --city lookup'
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Max RAW businesses to request from Overpass (test runs use a small number)"
    )
    parser.add_argument(
        "--output", default="data/01_leads.jsonl",
        help="Output JSONL path. Default: data/01_leads.jsonl"
    )
    parser.add_argument(
        "--input-json", default=None,
        help="OFFLINE FALLBACK: path to a pre-fetched Overpass JSON export "
             "(from Overpass Turbo's Export -> raw data), used instead of "
             "calling the live Overpass API. Use this only when the live "
             "endpoints are down - see the OFFLINE FALLBACK note at the top "
             "of this file - and say so to the team when you use it."
    )
    parser.add_argument(
        "-v", "--print-query", action="store_true",
        help="Print the exact Overpass QL query this run would send, then continue. "
             "Useful for pasting the same query into Overpass Turbo for --input-json."
    )
    args = parser.parse_args()

    if "=" not in args.category:
        print('[ERROR] --category must be in "key=value" form, e.g. amenity=dentist',
              file=sys.stderr)
        sys.exit(1)
    category_key, category_value = args.category.split("=", 1)

    bbox = resolve_bbox(args)

    if args.print_query:
        print("[INFO] Overpass QL query for this run (paste into overpass-turbo.eu):")
        print(build_overpass_query(category_key, category_value, bbox, args.limit))

    print(f"[INFO] Running LEADFORGE Stage 1")
    print(f"[INFO] Category: {category_key}={category_value} | City: {args.city} | "
          f"BBox: {bbox} | Raw limit: {args.limit}"
          + (f" | Source: OFFLINE export ({args.input_json})" if args.input_json else " | Source: live Overpass API"))

    leads = run_pipeline(category_key, category_value, args.city, bbox, args.limit,
                          input_json=args.input_json)

    write_jsonl(leads, args.output)


if __name__ == "__main__":
    main()

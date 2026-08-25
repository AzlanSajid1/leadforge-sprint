"""
LEADFORGE — Stage 1: Collect the Leads

Pipeline:

    Overpass API
        ↓
    Raw businesses
        ↓
    Keep businesses with websites
        ↓
    Normalize URL / domain
        ↓
    Check robots.txt
        ↓
    Fetch homepage
        ↓
    Try About / Services page
        ↓
    Extract readable text with Trafilatura
        ↓
    Deduplicate by domain
        ↓
    Fuzzy name deduplication
        ↓
    Assign lead_id
        ↓
    Write data/01_leads.jsonl

Example:

    python stages/01_collect.py \
        --category shop=car_repair \
        --city Lahore \
        --limit 20 \
        --target 10

Final example:

    python stages/01_collect.py \
        --category shop=car_repair \
        --city Lahore \
        --limit 120 \
        --target 100
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import trafilatura
from rapidfuzz import fuzz


# ============================================================
# CONFIGURATION
# ============================================================

OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

USER_AGENT = (
    "LeadForgeSprintBot/1.0 "
    "(student internship project; contact: noor.ul.huda@logitrixsolutions.com)"
)

REQUEST_DELAY_SECONDS = 1.0
FETCH_TIMEOUT_SECONDS = 15
OVERPASS_TIMEOUT_SECONDS = 45
MAX_SITE_TEXT_CHARS = 4000

FUZZY_MATCH_THRESHOLD = 90

OUTPUT_DEFAULT = "data/01_leads.jsonl"


# ============================================================
# CITY BOUNDING BOXES
# ============================================================

# These are implementation choices.
# The project only requires one city.
#
# Bounding box format:
# south, west, north, east

CITY_BBOXES = {
    "lahore": (31.3000, 74.1000, 31.7000, 74.5500),
}


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
)


# ============================================================
# OVERPASS
# ============================================================

def build_overpass_query(
    category_key,
    category_value,
    bbox,
    raw_limit,
):
    """
    Build an Overpass QL query.

    We search both nodes and ways because businesses may be mapped
    either as a point or as an area.
    """

    south, west, north, east = bbox

    query = f"""
[out:json][timeout:40];

(
  node["{category_key}"="{category_value}"](
      {south},{west},{north},{east}
  );

  way["{category_key}"="{category_value}"](
      {south},{west},{north},{east}
  );
);

out center tags {raw_limit};
"""

    return query


def query_overpass(
    category_key,
    category_value,
    bbox,
    raw_limit,
):
    """
    Try the configured Overpass endpoints in order.

    If one endpoint fails, try the next one.

    Returns:
        list of raw Overpass elements
    """

    query = build_overpass_query(
        category_key,
        category_value,
        bbox,
        raw_limit,
    )

    for endpoint in OVERPASS_ENDPOINTS:

        print(f"[INFO] Trying Overpass endpoint:")
        print(f"       {endpoint}")

        try:
            response = session.post(
                endpoint,
                data={"data": query},
                timeout=OVERPASS_TIMEOUT_SECONDS,
            )

            print(f"[INFO] HTTP status: {response.status_code}")

            if response.status_code != 200:
                print(
                    f"[WARN] Overpass returned "
                    f"HTTP {response.status_code}"
                )
                print(
                    f"[WARN] Response: "
                    f"{response.text[:250]}"
                )
                continue

            try:
                data = response.json()
            except ValueError:
                print(
                    "[WARN] Overpass returned something "
                    "that was not valid JSON."
                )
                continue

            elements = data.get("elements", [])

            print(
                f"[INFO] Overpass returned "
                f"{len(elements)} raw elements."
            )

            return elements

        except requests.exceptions.Timeout:
            print("[WARN] Overpass request timed out.")
            continue

        except requests.exceptions.RequestException as exc:
            print(
                f"[WARN] Overpass request failed: {exc}"
            )
            continue

    print(
        "[ERROR] All Overpass endpoints failed."
    )

    return []


# ============================================================
# BUSINESS PARSING
# ============================================================

def parse_business(element, city, category_value):
    """
    Convert one raw Overpass element into a simpler business record.

    Businesses without a website are skipped because Stage 1 needs
    website text for downstream processing.
    """

    tags = element.get("tags", {})

    name = tags.get("name:en") or tags.get("name")

    if not name:
        return None

    website = (
        tags.get("website")
        or tags.get("contact:website")
    )

    if not website:
        return None

    website_lower = website.lower()

    # Social-media URLs are generally not useful for website
    # content extraction.
    social_fragments = (
        "facebook.com",
        "fb.com",
        "fb.me",
        "instagram.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
    )

    if any(fragment in website_lower for fragment in social_fragments):
        return None

    phone = (
        tags.get("phone")
        or tags.get("contact:phone")
        or ""
    )

    return {
        "name": name.strip(),
        "website_raw": website.strip(),
        "city": city.strip(),
        "category": category_value.strip(),
        "phone": phone.strip(),
    }


# ============================================================
# URL HANDLING
# ============================================================

def normalize_url(raw_url):
    """
    Convert a raw website value into a usable HTTP/HTTPS URL.
    """

    if not raw_url:
        return None

    raw_url = raw_url.strip()

    if not raw_url:
        return None

    # Remove surrounding punctuation sometimes present in OSM data.
    raw_url = raw_url.strip(" <>\"'")

    if not raw_url.startswith(
        ("http://", "https://")
    ):
        raw_url = "https://" + raw_url

    return raw_url


def extract_domain(url):
    """
    Extract a normalized domain.

    Example:

        https://www.example.com/about

    becomes:

        example.com
    """

    if not url:
        return None

    try:
        parsed = urlparse(url)

        domain = parsed.netloc.lower()

        if not domain:
            return None

        # Remove username/password if malformed URL contains them.
        if "@" in domain:
            domain = domain.split("@")[-1]

        # Remove port.
        domain = domain.split(":")[0]

        if domain.startswith("www."):
            domain = domain[4:]

        # Basic domain validation.
        if "." not in domain:
            return None

        if not re.match(
            r"^[a-z0-9.-]+$",
            domain,
        ):
            return None

        return domain

    except Exception:
        return None


# ============================================================
# ROBOTS.TXT
# ============================================================

def allowed_by_robots(url):
    """
    Check robots.txt before crawling a website.

    If robots.txt cannot be retrieved, we return True rather than
    treating the website as automatically forbidden.

    This keeps the collector practical while still respecting an
    explicit robots.txt disallow rule when we can read it.
    """

    try:
        parsed = urlparse(url)

        robots_url = (
            f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        )

        robot_parser = RobotFileParser()

        robot_parser.set_url(robots_url)

        robot_parser.read()

        return robot_parser.can_fetch(
            USER_AGENT,
            url,
        )

    except Exception:
        # We cannot reliably determine the robots policy.
        # Don't crash the pipeline.
        return True


# ============================================================
# WEBSITE FETCHING
# ============================================================

def fetch_page(url):
    """
    Fetch a webpage.

    Returns:
        HTML text on success
        None on failure
    """

    try:
        response = session.get(
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            allow_redirects=True,
        )

        if response.status_code != 200:
            print(
                f"[WARN] {url} -> "
                f"HTTP {response.status_code}"
            )
            return None

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        # Only process HTML-like pages.
        if (
            "text/html" not in content_type
            and "application/xhtml+xml"
            not in content_type
        ):
            print(
                f"[WARN] {url} does not appear "
                f"to be an HTML page."
            )
            return None

        return response.text

    except requests.exceptions.Timeout:
        print(f"[WARN] Timeout: {url}")
        return None

    except requests.exceptions.RequestException as exc:
        print(
            f"[WARN] Website request failed: "
            f"{url} -> {exc}"
        )
        return None


# ============================================================
# TRAFILATURA
# ============================================================

def extract_readable_text(html):
    """
    Convert raw HTML into readable text using Trafilatura.
    """

    if not html:
        return ""

    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
        )

        if not text:
            return ""

        return text.strip()

    except Exception as exc:
        print(
            f"[WARN] Trafilatura extraction failed: "
            f"{exc}"
        )
        return ""


# ============================================================
# ABOUT / SERVICES PAGE DISCOVERY
# ============================================================

def find_about_or_services_url(
    homepage_url,
    homepage_html,
):
    """
    Try to discover an About or Services page.

    First inspect homepage links.

    If none are found, try a few conventional paths.
    """

    if homepage_html:

        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(
                homepage_html,
                "html.parser",
            )

            keywords = (
                "about",
                "about us",
                "services",
                "our services",
            )

            for link in soup.find_all("a", href=True):

                text = link.get_text(
                    " ",
                    strip=True,
                ).lower()

                href = link.get("href", "").strip()

                combined = f"{text} {href.lower()}"

                if any(
                    keyword in combined
                    for keyword in keywords
                ):

                    candidate = urljoin(
                        homepage_url,
                        href,
                    )

                    parsed = urlparse(candidate)

                    if parsed.scheme in (
                        "http",
                        "https",
                    ):
                        return candidate

        except Exception:
            pass

    # Fallback guesses.
    base = homepage_url.rstrip("/")

    candidates = [
        "about",
        "about-us",
        "services",
        "our-services",
    ]

    for path in candidates:
        return f"{base}/{path}"

    return None


# ============================================================
# SITE TEXT COLLECTION
# ============================================================

def collect_site_text(homepage_url):
    """
    Collect readable text from:

    1. Homepage
    2. One About/Services page if available

    Final text is capped at 4,000 characters.
    """

    if not allowed_by_robots(homepage_url):
        print(
            f"[INFO] robots.txt does not allow crawling: "
            f"{homepage_url}"
        )
        return ""

    homepage_html = fetch_page(homepage_url)

    if not homepage_html:
        return ""

    homepage_text = extract_readable_text(
        homepage_html
    )

    # Respect delay before requesting another page.
    time.sleep(REQUEST_DELAY_SECONDS)

    second_url = find_about_or_services_url(
        homepage_url,
        homepage_html,
    )

    second_text = ""

    if second_url and second_url != homepage_url:

        if allowed_by_robots(second_url):

            second_html = fetch_page(second_url)

            if second_html:
                second_text = extract_readable_text(
                    second_html
                )

    combined = homepage_text

    if second_text:
        combined += "\n\n" + second_text

    combined = combined.strip()

    return combined[:MAX_SITE_TEXT_CHARS]


# ============================================================
# DEDUPLICATION
# ============================================================

def deduplicate_leads(leads):
    """
    Two-stage deduplication.

    Stage 1:
        Exact domain matching.

    Stage 2:
        Conservative fuzzy name matching.
    """

    # --------------------------------------------------------
    # PASS 1 — DOMAIN
    # --------------------------------------------------------

    seen_domains = set()

    domain_deduped = []

    for lead in leads:

        domain = lead.get("domain")

        if not domain:
            continue

        if domain in seen_domains:
            continue

        seen_domains.add(domain)

        domain_deduped.append(lead)

    print(
        f"[INFO] After domain deduplication: "
        f"{len(domain_deduped)}"
    )

    # --------------------------------------------------------
    # PASS 2 — FUZZY NAME
    # --------------------------------------------------------

    final_leads = []

    for candidate in domain_deduped:

        candidate_name = (
            candidate["name"]
            .strip()
            .lower()
        )

        duplicate = False

        for existing in final_leads:

            existing_name = (
                existing["name"]
                .strip()
                .lower()
            )

            score = fuzz.token_sort_ratio(
                candidate_name,
                existing_name,
            )

            if score >= FUZZY_MATCH_THRESHOLD:

                # Only merge when the names are extremely similar.
                duplicate = True
                break

        if not duplicate:
            final_leads.append(candidate)

    print(
        f"[INFO] After fuzzy name deduplication: "
        f"{len(final_leads)}"
    )

    return final_leads


# ============================================================
# LEAD IDS
# ============================================================

def assign_lead_ids(leads):
    """
    Assign predictable sequential IDs.

    Example:

        lf_0001
        lf_0002
        lf_0003
    """

    for index, lead in enumerate(
        leads,
        start=1,
    ):
        lead["lead_id"] = (
            f"lf_{index:04d}"
        )

    return leads


# ============================================================
# JSONL OUTPUT
# ============================================================

def write_jsonl(leads, output_path):
    """
    Write one JSON object per line.
    """

    directory = os.path.dirname(output_path)

    if directory:
        os.makedirs(
            directory,
            exist_ok=True,
        )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:

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

            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                )
                + "\n"
            )

    print(
        f"[INFO] Wrote {len(leads)} leads to "
        f"{output_path}"
    )


# ============================================================
# PIPELINE
# ============================================================

def run_pipeline(
    category_key,
    category_value,
    city,
    bbox,
    raw_limit,
    target,
):
    """
    Execute Stage 1.
    """

    print()
    print("=" * 60)
    print("LEADFORGE — STAGE 1")
    print("=" * 60)
    print(
        f"Category : "
        f"{category_key}={category_value}"
    )
    print(f"City     : {city}")
    print(f"Raw limit: {raw_limit}")
    print(f"Target   : {target}")
    print("=" * 60)
    print()

    # --------------------------------------------------------
    # 1. OVERPASS
    # --------------------------------------------------------

    raw_elements = query_overpass(
        category_key,
        category_value,
        bbox,
        raw_limit,
    )

    if not raw_elements:

        print()
        print(
            "[ERROR] No raw businesses were returned."
        )
        print(
            "[ERROR] The output file will be empty "
            "because there is no real source data."
        )

        return []

    # --------------------------------------------------------
    # 2. PARSE
    # --------------------------------------------------------

    parsed_leads = []

    for element in raw_elements:

        business = parse_business(
            element,
            city,
            category_value,
        )

        if business:
            parsed_leads.append(
                business
            )

    print(
        f"[INFO] Businesses with usable "
        f"website information: "
        f"{len(parsed_leads)}"
    )

    # --------------------------------------------------------
    # 3. WEBSITE + TEXT
    # --------------------------------------------------------

    enriched_leads = []

    for index, business in enumerate(
        parsed_leads,
        start=1,
    ):

        print()
        print(
            f"[INFO] Processing "
            f"{index}/{len(parsed_leads)}: "
            f"{business['name']}"
        )

        url = normalize_url(
            business["website_raw"]
        )

        domain = extract_domain(url)

        if not url or not domain:
            print(
                "[WARN] Invalid website URL. Skipping."
            )
            continue

        site_text = collect_site_text(url)

        if not site_text:
            print(
                "[WARN] No readable site text. "
                "Skipping lead."
            )
            continue

        business["domain"] = domain
        business["site_text"] = site_text

        enriched_leads.append(
            business
        )

        # Delay between different businesses.
        time.sleep(
            REQUEST_DELAY_SECONDS
        )

        # Stop once enough usable leads exist.
        if len(enriched_leads) >= target:
            print(
                f"[INFO] Reached target of "
                f"{target} usable leads."
            )
            break

    # --------------------------------------------------------
    # 4. DEDUPLICATION
    # --------------------------------------------------------

    deduped_leads = deduplicate_leads(
        enriched_leads
    )

    # --------------------------------------------------------
    # 5. IDS
    # --------------------------------------------------------

    final_leads = assign_lead_ids(
        deduped_leads
    )

    print()
    print(
        f"[INFO] Final usable leads: "
        f"{len(final_leads)}"
    )

    return final_leads


# ============================================================
# BOUNDING BOX
# ============================================================

def resolve_bbox(args):
    """
    Resolve the city's bounding box.

    --bbox overrides the built-in city lookup.
    """

    if args.bbox:

        try:

            parts = [
                float(part.strip())
                for part in args.bbox.split(",")
            ]

            if len(parts) != 4:
                raise ValueError

            return tuple(parts)

        except ValueError:

            print(
                "[ERROR] --bbox must be:"
                " south,west,north,east",
                file=sys.stderr,
            )

            sys.exit(1)

    city_key = (
        args.city
        .strip()
        .lower()
    )

    if city_key not in CITY_BBOXES:

        print(
            f"[ERROR] No bounding box configured "
            f"for '{args.city}'.",
            file=sys.stderr,
        )

        print(
            f"[INFO] Available cities: "
            f"{', '.join(CITY_BBOXES.keys())}"
        )

        print(
            "[INFO] You can provide --bbox manually."
        )

        sys.exit(1)

    return CITY_BBOXES[city_key]


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "LEADFORGE Stage 1 — "
            "Collect business leads"
        )
    )

    parser.add_argument(
        "--category",
        required=True,
        help=(
            'OSM tag in key=value format. '
            'Example: shop=car_repair'
        ),
    )

    parser.add_argument(
        "--city",
        default="Lahore",
        help="City name. Default: Lahore",
    )

    parser.add_argument(
        "--bbox",
        default=None,
        help=(
            "Optional bounding box: "
            "south,west,north,east"
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help=(
            "Maximum RAW businesses to request "
            "from Overpass."
        ),
    )

    parser.add_argument(
        "--target",
        type=int,
        default=100,
        help=(
            "Target number of usable leads. "
            "Default: 100."
        ),
    )

    parser.add_argument(
        "--output",
        default=OUTPUT_DEFAULT,
        help=(
            "Output JSONL file. "
            "Default: data/01_leads.jsonl"
        ),
    )

    args = parser.parse_args()

    if "=" not in args.category:

        print(
            '[ERROR] --category must use '
            '"key=value" format.',
            file=sys.stderr,
        )

        sys.exit(1)

    if args.limit <= 0:

        print(
            "[ERROR] --limit must be greater than 0.",
            file=sys.stderr,
        )

        sys.exit(1)

    if args.target <= 0:

        print(
            "[ERROR] --target must be greater than 0.",
            file=sys.stderr,
        )

        sys.exit(1)

    category_key, category_value = (
        args.category.split("=", 1)
    )

    category_key = category_key.strip()
    category_value = category_value.strip()

    bbox = resolve_bbox(args)

    leads = run_pipeline(
        category_key=category_key,
        category_value=category_value,
        city=args.city,
        bbox=bbox,
        raw_limit=args.limit,
        target=args.target,
    )

    write_jsonl(
        leads,
        args.output,
    )

    print()
    print("=" * 60)

    if len(leads) >= args.target:

        print(
            "SUCCESS: Target number of leads reached."
        )

    elif len(leads) > 0:

        print(
            f"PARTIAL: Collected {len(leads)} "
            f"usable leads out of target {args.target}."
        )

    else:

        print(
            "BLOCKED: No usable leads were collected."
        )

    print("=" * 60)


if __name__ == "__main__":
    main()
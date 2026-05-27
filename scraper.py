"""
Kenya Gazette holiday-declaration scraper.

Polls new.kenyalaw.org for Special Issue gazettes, downloads recent ones,
extracts text, and detects "Declaration of a Public Holiday" notices.
Writes a clean holidays.json that the HoliKE Android app consumes.

Designed to run as a GitHub Actions cron job (hourly).

The app handles thumbnail rendering on-demand: when a user taps a holiday's
gazette icon, the app fetches `source_url` directly, extracts the embedded
base64 JPEG from the page HTML (Kenya Law inlines a 1190x1684 page render
inside `<button data-page="1"><img src="data:image/jpeg;base64,...">`), and
caches the result locally with a 24-hour TTL. The scraper does not store
thumbnails — they're fetched lazily by the client.

Output schema (holidays.json):
{
  "generated_at": "2026-05-26T10:00:00Z",
  "source": "https://new.kenyalaw.org/gazettes/",
  "holidays": [
    {
      "date": "2026-05-27",
      "name": "Eid-ul-Adha",
      "lunar": true,
      "source_gazette": "Vol. CXXVIII-No. 90",
      "source_url": "https://new.kenyalaw.org/akn/ke/officialGazette/2026-05-25/90/eng@2026-05-25",
      "declared_on": "2026-05-25",
      "gazette_notice": "7653"
    },
    ...
  ]
}

Run:
    pip install requests beautifulsoup4 pypdf
    python scraper.py

Outputs:
    holidays.json          — main data file (app reads this)
    scraper_state.json     — internal: tracks scanned URLs
"""

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://new.kenyalaw.org"
USER_AGENT = "HoliKEBot/1.0 (+https://github.com/YOUR_USERNAME/holike) - polite hourly poll"
TIMEOUT = 30
OUTPUT = Path("holidays.json")
STATE = Path("scraper_state.json")   # tracks already-scanned gazettes

# Known baseline (static fixed-date holidays + already-confirmed gazette dates).
# The scraper merges its findings into this list. Lunar entries are speculative
# until a gazette confirms them.
BASELINE = [
    # 2026
    {"date": "2026-01-01", "name": "New Year's Day", "lunar": False},
    {"date": "2026-04-03", "name": "Good Friday", "lunar": False},
    {"date": "2026-04-06", "name": "Easter Monday", "lunar": False},
    {"date": "2026-05-01", "name": "Labour Day", "lunar": False},
    {"date": "2026-06-01", "name": "Madaraka Day", "lunar": False},
    {"date": "2026-10-10", "name": "Mazingira Day", "lunar": False},
    {"date": "2026-10-20", "name": "Mashujaa Day", "lunar": False},
    {"date": "2026-12-12", "name": "Jamhuri Day", "lunar": False},
    {"date": "2026-12-25", "name": "Christmas Day", "lunar": False},
    {"date": "2026-12-26", "name": "Boxing Day", "lunar": False},
    # 2027 fixed dates
    {"date": "2027-01-01", "name": "New Year's Day", "lunar": False},
    {"date": "2027-05-01", "name": "Labour Day", "lunar": False},
    {"date": "2027-06-01", "name": "Madaraka Day", "lunar": False},
    {"date": "2027-10-10", "name": "Mazingira Day", "lunar": False},
    {"date": "2027-10-20", "name": "Mashujaa Day", "lunar": False},
    {"date": "2027-12-12", "name": "Jamhuri Day", "lunar": False},
    {"date": "2027-12-25", "name": "Christmas Day", "lunar": False},
]

# How far back to look on each run. Set to e.g. 90 days for normal operation;
# a fresh deployment can crank this up for backfill.
LOOKBACK_DAYS = 60

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"scanned_urls": []}

def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))

# ---------------------------------------------------------------------------
# Listing — find candidate gazettes
# ---------------------------------------------------------------------------

def get(url):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r

def list_gazettes_for_year(year):
    """
    Return a list of {url, number, date, is_special} for every gazette
    in the given year.
    """
    html = get(f"{BASE}/gazettes/{year}").text
    soup = BeautifulSoup(html, "html.parser")

    results = []
    # The listing is a table; rows contain a link, optional "Special Issue" cell,
    # the number, and the date.
    for row in soup.select("table tr"):
        link = row.find("a", href=re.compile(r"/akn/ke/officialGazette/"))
        if not link:
            continue
        href = urljoin(BASE, link["href"])
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        is_special = any("Special Issue" in c for c in cells)

        # Pull the date out of the AKN URL — most reliable source
        m = re.search(r"/officialGazette/(\d{4}-\d{2}-\d{2})/(\d+)/", href)
        if not m:
            continue
        gz_date, gz_number = m.group(1), m.group(2)

        results.append({
            "url": href,
            "number": gz_number,
            "date": gz_date,
            "is_special": is_special,
        })
    return results

# ---------------------------------------------------------------------------
# Detail page — find the PDF link
# ---------------------------------------------------------------------------

def find_pdf_url(gazette_page_url):
    """
    Fetch the gazette detail page and pull the PDF URL out of the
    `data-pdf` attribute. Kenya Law exposes it directly in the HTML.

    Returns the absolute PDF URL, or None if not found.
    """
    html = get(gazette_page_url).text
    soup = BeautifulSoup(html, "html.parser")

    pdf_div = soup.find(attrs={"data-pdf": True})
    if pdf_div:
        return urljoin(BASE, pdf_div["data-pdf"])

    # Fallback: any <a> ending in .pdf
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return urljoin(BASE, a["href"])

    return None

# ---------------------------------------------------------------------------
# Holiday extraction
# ---------------------------------------------------------------------------

# The canonical phrase in every modern declaration. Match generously across
# line breaks and extra whitespace.
DECLARATION_RE = re.compile(
    r"declaration\s+of\s+a\s+public\s+holiday",
    re.IGNORECASE,
)

# Extract the declared date and occasion from text like:
#   "...declares that, Wednesday, the 27th May, 2026, shall be a public
#    holiday to mark Eid-ul-Adha."
DATE_RE = re.compile(
    r"""
    (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?
    [,\s]*
    (?:the\s+)?
    (\d{1,2})                              # day
    (?:st|nd|rd|th)?
    \s+
    (January|February|March|April|May|June|July|
     August|September|October|November|December)
    [,\s]+
    (\d{4})                                # year
    .{0,80}?                               # filler
    (?:public\s+holiday\s+to\s+mark|
       to\s+commemorate|
       public\s+holiday[\s.,]+)
    \s*
    ([A-Z][A-Za-z\-’' ]{2,60}?)            # occasion
    (?:\.|$)
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# Some occasions are lunar / vary year-to-year
LUNAR_KEYWORDS = ("eid", "idd", "iddul", "id-ul")

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June",
     "July","August","September","October","November","December"], start=1)}

def extract_pdf_text(pdf_bytes):
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(p.extract_text() or "" for p in reader.pages)

def parse_holiday_from_text(text):
    """Return (iso_date, name) or None."""
    if not DECLARATION_RE.search(text):
        return None

    m = DATE_RE.search(text)
    if not m:
        return None

    day = int(m.group(1))
    month = MONTHS[m.group(2).lower()]
    year = int(m.group(3))
    name = m.group(4).strip().rstrip(".").strip()

    # Normalise common Eid spellings to a single canonical form
    lower = name.lower()
    if any(k in lower for k in LUNAR_KEYWORDS):
        if "adha" in lower or "azha" in lower:
            name = "Eid-ul-Adha"
        elif "fitr" in lower:
            name = "Eid-ul-Fitr"

    try:
        iso = date(year, month, day).isoformat()
    except ValueError:
        return None

    is_lunar = any(k in name.lower() for k in LUNAR_KEYWORDS)
    return iso, name, is_lunar

def find_gazette_notice_number(text):
    m = re.search(r"GAZETTE\s+NOTICE\s+NO\.?\s+(\d+)", text, re.IGNORECASE)
    return m.group(1) if m else None

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def merge(baseline, found):
    """Found entries override baseline entries with the same date."""
    by_date = {h["date"]: h for h in baseline}
    for f in found:
        by_date[f["date"]] = f
    return sorted(by_date.values(), key=lambda h: h["date"])

def main():
    state = load_state()
    scanned = set(state["scanned_urls"])
    found_holidays = []

    today = date.today()
    years = sorted({today.year, today.year + 1, today.year - 1})

    candidates = []
    for y in years:
        try:
            candidates.extend(list_gazettes_for_year(y))
        except Exception as e:
            print(f"[warn] could not list {y}: {e}", file=sys.stderr)

    # Filter: Special Issues only, recent only, not already scanned
    cutoff = date.today().toordinal() - LOOKBACK_DAYS
    to_scan = [
        c for c in candidates
        if c["is_special"]
        and date.fromisoformat(c["date"]).toordinal() >= cutoff
        and c["url"] not in scanned
    ]

    print(f"[info] {len(candidates)} total gazettes; {len(to_scan)} new special issues to scan")

    for c in to_scan:
        try:
            pdf_url = find_pdf_url(c["url"])
            if not pdf_url:
                print(f"[skip] no PDF for {c['url']}")
                scanned.add(c["url"])
                continue

            # Download PDF for text extraction
            pdf_bytes = get(pdf_url).content
            text = extract_pdf_text(pdf_bytes)
            parsed = parse_holiday_from_text(text)

            if parsed:
                iso, name, lunar = parsed
                notice = find_gazette_notice_number(text)

                entry = {
                    "date": iso,
                    "name": name,
                    "lunar": lunar,
                    "source_gazette": f"Vol. CXXVIII-No. {c['number']}",
                    "source_url": c["url"],
                    "source_pdf": pdf_url,
                    "declared_on": c["date"],
                    "gazette_notice": notice,
                }
                found_holidays.append(entry)
                print(f"[FOUND] {iso} — {name}  (gazette {c['number']})")
            scanned.add(c["url"])

        except Exception as e:
            print(f"[err] {c['url']}: {e}", file=sys.stderr)

    # Merge with previous run's findings + baseline
    existing = []
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text()).get("holidays", [])
        except Exception:
            pass

    merged = merge(BASELINE, existing + found_holidays)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{BASE}/gazettes/",
        "holidays": merged,
    }
    OUTPUT.write_text(json.dumps(output, indent=2))
    save_state({"scanned_urls": sorted(scanned)})

    new_count = len(found_holidays)
    print(f"[done] wrote {OUTPUT} ({len(merged)} holidays, {new_count} new)")

    # Exit code 0 = success. The GitHub Action can read stdout to decide
    # whether to push a notification on `new_count > 0`.
    return 0 if new_count == 0 else 10  # 10 signals "new holiday found"


if __name__ == "__main__":
    sys.exit(main())

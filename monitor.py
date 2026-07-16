#!/usr/bin/env python3
"""
HMI Course Availability Monitor — Daily Report
------------------------------------------------
Checks https://admission.hmidarjeeling.com/online-admission-2026-2027/
once a day and sends an email + push notification EVERY DAY with the
current seat count for course 379 (Men only) — regardless of whether
it changed since yesterday.

Run manually:   python3 monitor.py
Run a dry test: python3 monitor.py --dry-run   (parses & prints, sends nothing)
"""

import os
import re
import sys
import smtplib
import logging
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration (all read from environment variables — see .env.example)
#
# NOTE: GitHub Actions passes an unset repo Variable through as an EMPTY
# STRING, not a missing key — so os.environ.get("X", "default") would
# silently return "" instead of "default" in that case. We use
# `os.environ.get("X") or "default"` everywhere instead, which falls
# back correctly whether the variable is unset OR set-but-blank.
# ---------------------------------------------------------------------------
URL = os.environ.get("TARGET_URL") or "https://admission.hmidarjeeling.com/online-admission-2026-2027/"

LOG_FILE = os.environ.get("LOG_FILE") or os.path.join(os.path.dirname(__file__), "monitor.log")

# Which course(s) to report on, every single day. Matches against the
# course name + category (case-insensitive substring match).
# Default "379" narrows it to course 379 (Men only). To watch every
# course instead, set the COURSE_FILTER repo Variable to "*".
COURSE_FILTER = os.environ.get("COURSE_FILTER") or "379"
if COURSE_FILTER == "*":
    COURSE_FILTER = ""

# Email (SMTP) settings
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# Push notification via ntfy.sh (free, no signup — pick any unique topic name)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CourseAvailabilityMonitor/1.0)"
}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
console = logging.StreamHandler(sys.stdout)
console.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(console)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_table_field(table, header_name: str, apply_link) -> str:
    """
    Reads a course's data table structurally: maps the header row's column
    names to the matching data row's cell values by position, then returns
    the value for `header_name` (e.g. "Date", "Availability").

    This avoids a bug from an earlier version, which searched the whole
    table's concatenated text with a regex — that could match the HEADER
    row's label (e.g. find "Date" then grab whatever text followed it,
    which was actually the *next column's header*, "Age") instead of the
    real data value. Mapping columns by index guarantees we read from the
    data row, not the header row.
    """
    if table is None:
        return ""
    rows = table.find_all("tr")
    if not rows:
        return ""

    headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]

    # The data row is whichever row actually contains the Apply link —
    # more reliable than assuming it's "the last row", in case of extra
    # rows (e.g. a merged notes row).
    data_row = None
    for row in rows:
        if row.find("a", href=re.compile(r"add-to-cart=\d+")) is apply_link or apply_link in row.find_all("a"):
            data_row = row
            break
    if data_row is None and len(rows) > 1:
        data_row = rows[-1]
    if data_row is None:
        return ""

    data_cells = [c.get_text(strip=True) for c in data_row.find_all(["td", "th"])]

    if header_name in headers:
        idx = headers.index(header_name)
        if idx < len(data_cells):
            value = data_cells[idx]
            # Some responsive-table markup repeats the column label inside
            # each data cell (e.g. "Date01 Apr - 28 Apr 2026") for mobile
            # view — strip that duplicate prefix if present.
            if value.lower().startswith(header_name.lower()):
                value = value[len(header_name):].strip()
            return value

    # Fallback: regex within the data row's own text only (never the
    # header row), scoped so it can't grab an adjacent header/label.
    row_text = data_row.get_text(separator="|", strip=True)
    if header_name.lower() == "availability":
        m = re.search(r"(Closed|\d+\s*Available)", row_text)
        return m.group(1) if m else ""
    return ""


def parse_availability(html: str) -> dict:
    """
    Returns a dict keyed by a stable course id -> course info dict:
      {
        "9161": {
            "name": "379 (Men only)",
            "category": "BASIC COURSE",
            "date": "01 Mar - 28 Mar 2027",
            "availability_text": "48 Available",
            "seats_available": 48,
            "closed": False,
            "apply_url": "https://...",
        },
        ...
      }

    Strategy: the page's raw HTML tree order does NOT match its visual
    reading order (Elementor renders sidebar/filter widgets out of
    visual sequence), so walking "backward through the DOM" from a
    table to find its heading is unreliable — it can wander into
    unrelated widgets.

    Instead: the literal text "Course Name :" appears exactly once per
    course, immediately before that course's own number/name, category,
    and data table — and nothing else on the page emits that exact
    phrase. So we split the raw HTML on that marker first. Each
    resulting chunk is a small, self-contained fragment covering ONE
    course only, and everything we search for in it (name, category
    link, table, apply link) is guaranteed to belong to that course —
    no cross-page contamination possible.
    """
    courses = {}

    # Split on the marker text (tolerant of a missing/extra colon or
    # surrounding whitespace, since it may be wrapped in <strong>/<h2> tags).
    chunks = re.split(r"Course\s*Name\s*:?", html, flags=re.IGNORECASE)

    for chunk in chunks[1:]:  # chunks[0] is the preamble before the first course
        frag = BeautifulSoup(chunk, "html.parser")

        apply_link = frag.find("a", href=re.compile(r"add-to-cart=\d+"))
        if apply_link is None:
            # Not a real course chunk (e.g. trailing footer content, or the
            # "Name of the Course" enquiry form field — different text, but
            # just in case) — skip it.
            continue

        m = re.search(r"add-to-cart=(\d+)", apply_link.get("href", ""))
        if not m:
            continue
        course_id = m.group(1)

        # Category: the course-category link, always present right after
        # the course name/number in every course block.
        cat_link = frag.find("a", href=re.compile(r"course-category"))
        category = cat_link.get_text(strip=True) if cat_link else ""

        # Name: all text between the start of this chunk and the category
        # link (i.e. everything before we hit the category link's own
        # text) — this is exactly the course number/name, nothing else,
        # since the chunk starts right after "Course Name :".
        if cat_link is not None:
            name_parts = []
            for node in frag.find_all(string=True):
                if node.parent is cat_link or node in cat_link.find_all(string=True):
                    break
                text = node.strip()
                if text:
                    name_parts.append(text)
            name = " ".join(name_parts).strip()
        else:
            name = frag.get_text(strip=True)[:80]

        # Table holding Duration/Date/Age/Capacity/Course Fee/Availability/Apply
        table = apply_link.find_parent("table")
        availability_text = extract_table_field(table, "Availability", apply_link) or "Unknown"
        date_text = extract_table_field(table, "Date", apply_link) or ""

        seats = None
        closed = availability_text.strip().lower() == "closed"
        if not closed:
            seat_match = re.search(r"(\d+)\s*Available", availability_text)
            if seat_match:
                seats = int(seat_match.group(1))
                # Treat "0 Available" the same as Closed — some sites show
                # a full course as "0 Available" instead of "Closed".
                if seats == 0:
                    closed = True

        courses[course_id] = {
            "name": name or f"Course #{course_id}",
            "category": category,
            "date": date_text,
            "availability_text": availability_text,
            "seats_available": seats,
            "closed": closed,
            "apply_url": apply_link.get("href", ""),
        }

    return courses


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def matches_filter(course: dict) -> bool:
    if not COURSE_FILTER:
        return True
    haystack = f"{course['name']} {course['category']}".lower()
    return COURSE_FILTER.lower() in haystack


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        log.info("Email not configured — skipping email notification.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        log.info("Email notification sent to %s", EMAIL_TO)
    except Exception as e:
        log.error("Failed to send email: %s", e)


def send_push(title: str, body: str) -> None:
    if not NTFY_TOPIC:
        log.info("ntfy topic not configured — skipping push notification.")
        return
    # HTTP headers must be latin-1/ASCII only — strip characters like
    # em-dashes (—) that would otherwise crash requests with a
    # UnicodeEncodeError.
    safe_title = title.encode("ascii", "ignore").decode("ascii")
    try:
        resp = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": safe_title, "Priority": "high"},
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Push notification sent to ntfy topic '%s'", NTFY_TOPIC)
    except Exception as e:
        log.error("Failed to send push notification: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv

    log.info("Checking %s", URL)
    try:
        html = fetch_page(URL)
    except Exception as e:
        log.error("Failed to fetch page: %s", e)
        sys.exit(1)

    courses = parse_availability(html)
    log.info("Parsed %d course entries", len(courses))

    matched = {cid: c for cid, c in courses.items() if matches_filter(c)}

    if dry_run:
        for cid, c in courses.items():
            marker = "  <-- matches filter" if cid in matched else ""
            print(f"[{cid}] {c['name']} | {c['category']} | {c['date']} | {c['availability_text']}{marker}")
        return

    if not matched:
        log.warning(
            "No course matched COURSE_FILTER=%r. Here's every course this "
            "run actually parsed, so we can see what the real name/category "
            "text looks like:",
            COURSE_FILTER,
        )
        for cid, c in courses.items():
            log.warning("  [%s] name=%r category=%r availability=%r", cid, c["name"], c["category"], c["availability_text"])
        return

    lines = []
    for c in matched.values():
        lines.append(
            f"{c['name']} [{c['category']}] — {c['date']}\n"
            f"   Availability: {c['availability_text']}\n"
            f"   Apply: {c['apply_url']}"
        )

    today = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y")
    subject = f"HMI daily check ({today}): " + "; ".join(
        f"{c['name']} — {c['availability_text']}" for c in matched.values()
    )
    body = f"Daily check — {today}\n\n" + "\n\n".join(lines) + f"\n\nSource: {URL}"

    log.info("Sending daily report:\n%s", body)
    send_email(subject, body)
    send_push(subject, "\n".join(lines)[:3800])  # ntfy has a body size limit


if __name__ == "__main__":
    main()

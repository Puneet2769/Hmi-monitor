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
# ---------------------------------------------------------------------------
URL = os.environ.get(
    "TARGET_URL",
    "https://admission.hmidarjeeling.com/online-admission-2026-2027/",
)

LOG_FILE = os.environ.get("LOG_FILE", os.path.join(os.path.dirname(__file__), "monitor.log"))

# Which course(s) to report on, every single day. Matches against the
# course name + category (case-insensitive substring match).
# Default "379" + "Men only" narrows it to exactly course 379 (Men only)
# and won't accidentally match a different course that also has "379"
# somewhere in unrelated text.
COURSE_FILTER = os.environ.get("COURSE_FILTER", "379")

# Email (SMTP) settings
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# Push notification via ntfy.sh (free, no signup — pick any unique topic name)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

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

    Strategy: every course "Apply" button is a link containing
    'add-to-cart=<id>'. That id is a stable per-course-run identifier.
    We find each such link, then look at the table it sits in, and the
    nearest preceding heading for the course name/category.
    """
    soup = BeautifulSoup(html, "html.parser")
    courses = {}

    apply_links = soup.find_all("a", href=re.compile(r"add-to-cart=\d+"))

    for link in apply_links:
        m = re.search(r"add-to-cart=(\d+)", link.get("href", ""))
        if not m:
            continue
        course_id = m.group(1)

        # Find the enclosing table (holds Duration/Date/.../Availability/Apply)
        table = link.find_parent("table")
        if table is None:
            continue

        table_text = table.get_text(separator="|", strip=True)

        # Availability text: look for "Closed" or "N Available" in the table
        avail_match = re.search(r"(Closed|\d+\s*Available)", table_text)
        availability_text = avail_match.group(1) if avail_match else "Unknown"

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

        # Date: text between "Date" label occurrences (best-effort)
        date_match = re.search(r"Date\|?\s*([0-9A-Za-z\s\-–,]+?)\|", table_text)
        date_text = date_match.group(1).strip() if date_match else ""

        # Find course name + category from headings/links before this table
        name = ""
        category = ""
        node = table
        steps = 0
        while node is not None and steps < 40:
            node = node.find_previous(["h1", "h2", "h3", "h4", "strong", "a"])
            steps += 1
            if node is None:
                break
            text = node.get_text(strip=True)
            if not text:
                continue
            # Category links point at /course-category/
            if node.name == "a" and "course-category" in node.get("href", "") and not category:
                category = text
                continue
            # Course name is a short heading/strong, not the "Course Name :" label itself
            if text.lower() not in ("course name :", "course name:") and not name:
                # crude guard against picking up nav/footer text
                if len(text) < 80:
                    name = text
            if name and category:
                break

        courses[course_id] = {
            "name": name or f"Course #{course_id}",
            "category": category,
            "date": date_text,
            "availability_text": availability_text,
            "seats_available": seats,
            "closed": closed,
            "apply_url": link.get("href", ""),
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
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "high"},
            timeout=15,
        )
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
            "No course matched COURSE_FILTER=%r — check the filter text "
            "against the course names printed by --dry-run.",
            COURSE_FILTER,
        )
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

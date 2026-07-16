# HMI Course 379 Daily Availability Report

Every day, this sends you an email + push notification with the current
seat count for **course 379 (Men only, Basic Mountaineering Course)** —
regardless of whether it changed since yesterday. Runs entirely on
GitHub's free servers, nothing to host.

## How it works

- `.github/workflows/monitor.yml` runs `monitor.py` once a day.
- The script downloads the page, finds course 379's row, and reads its
  "Availability" cell (e.g. `48 Available` or `Closed`).
- It always sends you a message with today's number — there's no
  change-detection, no memory of yesterday needed.

## Setup — one-time, about 10 minutes

### 1. Create a new repository

GitHub → **New repository** → name it e.g. `hmi-monitor` → **Private**
→ Create.

### 2. Upload these files, keeping the folder structure

```
hmi-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml
├── monitor.py
├── requirements.txt
└── README.md
```

Repo page → **Add file → Upload files** → drag in everything (including
the `.github` folder) → commit.

### 3. Add your secrets and variables

Repo → **Settings → Secrets and variables → Actions**.

**Secrets** (Secrets tab → New repository secret):

| Name | Value |
|---|---|
| `SMTP_USER` | your email, e.g. `you@gmail.com` |
| `SMTP_PASS` | a Gmail **App Password** (not your normal password — see below) |
| `EMAIL_TO` | where the daily report goes |
| `NTFY_TOPIC` | a unique made-up name, e.g. `hmi-379-alerts-x7q2` |

**Variables** (Variables tab — optional, sensible defaults if skipped):

| Name | Value |
|---|---|
| `TARGET_URL` | `https://admission.hmidarjeeling.com/online-admission-2026-2027/` |
| `COURSE_FILTER` | `379` (already the script's default — only set this if you want to change what it watches) |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `NTFY_SERVER` | `https://ntfy.sh` |

**Gmail App Password:** turn on 2-Step Verification at
myaccount.google.com/security, then create one at
myaccount.google.com/apppasswords. Use that 16-character code as
`SMTP_PASS`.

**Push notifications:** install the free **ntfy** app (iOS/Android), or
open `https://ntfy.sh/<your-topic-name>` in a browser, and Subscribe
using the exact same name as `NTFY_TOPIC`.

### 4. Test it manually

Repo → **Actions** tab → **HMI Course Availability Monitor** → **Run
workflow**. Check the run's logs — you should get today's email/push
for course 379 within a minute or two.

### 5. Done

The `cron: "30 2 * * *"` line makes it run automatically every day
(currently 08:00 AM IST — edit that line for a different time). No
further action needed.

## Changing what it watches

`COURSE_FILTER` matches against the course's name + category text. Set
it as a repo Variable to change it, e.g.:
- `379` — just this course (current default)
- `Basic` — every Basic Mountaineering Course batch
- (blank) — every course on the page, one line each in the daily email

## Checking on it later

Repo → **Actions** tab shows every past run and its logs.

## Notes

- If HMI redesigns the page layout, parsing may need small tweaks —
  come back and I can help adjust it.
- `python3 monitor.py --dry-run` prints every parsed course locally
  (marking which one(s) match your filter) without sending anything —
  useful for testing changes to `COURSE_FILTER`.

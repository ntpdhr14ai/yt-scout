# YT Scout — Project Context

## What it is
A Flask web app that scrapes YouTube search results to discover Thai creator channels. Filters by subscriber range, Thailand origin, upload frequency, and category. Exports results to CSV/Excel.

## GitHub
`https://github.com/ntpdhr14ai/yt-scout.git` — branch `main`, latest commit `37e21fa`

## Stack
- **Python 3.12** — Flask, Playwright (async, Chromium headless), playwright-stealth, openpyxl
- **Frontend** — single-page vanilla JS + CSS, dark theme, no framework

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask server — routes, scrape state machine, CSV/Excel export |
| `scraper.py` | All scraping logic — browser automation, filters, scoring |
| `templates/index.html` | Full UI — search form, progress bar, results table, cooldown banner |
| `requirements.txt` | Python deps |
| `README.md` | Docs with screenshot |
| `assets/screenshot.png` | README screenshot (force-added, overrides `*.png` gitignore) |
| `.gitignore` | Ignores `*.png`, `__pycache__`, `flask_out.txt`, etc. |

## Architecture

### app.py
- `_state` dict (protected by `threading.Lock`) holds all runtime state: `running`, `results`, `cooldown_until`, `batches_done`, `auto_batch`, `stop_requested`
- `_run_scrape(keyword, region, min_subs, max_subs, batch_size, vpm_filter, target_category)` — runs in a daemon thread; multi-batch loop with 10-min (`COOLDOWN_SECONDS=600`) cooldown between batches
- Loop breaks when `not results and not discovery_queue`
- Routes: `GET /` · `POST /start` · `POST /stop` · `GET /status` · `GET /results` · `GET /export/csv` · `GET /export/excel`
- `/status` response includes `discovery_queue_size` (imported from `scraper.py`)
- `_csv_fields()` / `_flatten_row()` — fields: name, url, subscribers, location_badge, category, upload_freq, score, country, avg_video_views, avg_short_views, avg_live_views, emails, links

### scraper.py
**Constants:** `MAX_CHANNELS_PER_BATCH = 10`, `COOLDOWN_SECONDS = 600`

**Module-level state:** `processed_handles: set`, `discovery_queue: list`

**Filter pipeline (per channel, in order):**
1. Stage 1a — handle keyword drop (`_NAME_DROP_KEYWORDS`)
2. Navigate to channel page
3. Stage 1b — page title keyword drop
4. Subscriber count (4-step extraction) → drop if outside `[min_subs, max_subs]`
5. `_scrape_about()` → TH verification → **only drop if non-Thai country positively confirmed** (Rule E); loading failures keep channel with `?` badge
6. Stage 1c — full name keyword drop
7. `scrape_tab_views()` for `/videos` (90s timeout via `_scrape_tab_with_timeout`)
8. Category extraction from videos page (`meta[itemprop="genre"]` → `ytInitialPlayerResponse`) → **only skip if extracted AND mismatches** (Rule A); unextractable → `"Pending Check"`
9. Timestamp extraction → `upload_freq` → **only skip if real number AND outside VPM range** (Rule B); unextractable → `"Pending Check"`
10. `scrape_tab_views()` for `/shorts` (90s timeout)
11. `scrape_tab_views()` for `/streams` (90s timeout)
12. Score → append to results
13. `discover_from_channels_tab()` if target_category matched

**`scrape_tab_views(page, url, handle, tab_name)`** — 3 attempts, domcontentloaded + networkidle + 2× scroll 700px + view keyword match across 4 selectors; returns `avg` int or `None`

**`_scrape_tab_with_timeout()`** — wraps `scrape_tab_views` with `asyncio.wait_for(..., timeout=90)`; logs `TIMEOUT` and returns `None` (Rule D)

**`_scrape_about()`** — navigates to `/about`; extracts name, sub_text, country, emails, links; TH detection via Thai script / "thailand" / "ไทย" / country field

**`discover_from_channels_tab(page, handle)`** — visits `/@{handle}/channels`; scrapes linked channel handles; appends new ones to `discovery_queue`

**Scoring (`_score_channel`):** subs tier (0–25) + video views/subs ratio (0–30) + shorts views/subs ratio (0–20) + upload_freq (0–15) + TH badge (+10) = max 100

**Graceful fallback rules:**
- Rule A: category unextractable → `"Pending Check"`, don't skip
- Rule B: upload_freq None → `"Pending Check"`, don't skip
- Rule C: tab view failure → `None` (renders as `—`), never breaks loop
- Rule D: tab scrape > 90s → abandon, log TIMEOUT, return `None`
- Rule E: about page load failure / no TH evidence → keep with `?` badge; only drop on confirmed non-Thai country

### templates/index.html
**Form inputs:** keyword, region (select), min/max subs, batch size, Videos/Month Range (select: any/0-10/11-20/21-30/31-40/50plus), Target Category (select: Any + 15 YouTube genres)

**Results table columns:** Channel · Subscribers · Location · Videos/mo · Score · Category · Avg Views Videos · Avg Views Shorts · Avg Views Live · Contact

**JS features:** append-only results rendering (`renderedCount`), 2s poll loop, cooldown countdown timer, discovery queue counter badge, Export CSV/Excel buttons

## Running
```bash
python app.py
# serves http://127.0.0.1:8080
```

## Current status (as of 2026-06-03)
- All code committed and pushed to `main`
- Flask running on `127.0.0.1:8080`
- Last test: keyword "Thai vlog", region TH, batch 3 → found 3 channels (Thái vlog 82.9K, Opas Thai-Vlog 4K, ciize channel 248K)
- Screenshot in `assets/screenshot.png` shows those 3 results
- **Next task:** run with a different keyword (user requested, not yet done)
- No open bugs; all 5 refactor changes + graceful fallback rules shipped

## Key decisions / non-obvious things
- `*.png` is gitignored — `assets/screenshot.png` must be force-added with `git add -f`
- `discovery_queue` is a module-level list in `scraper.py`, imported directly into `app.py` for the `/status` endpoint
- `upload_freq` stores either a `float`, `"Pending Check"`, or `None` — `_score_channel` guards with `isinstance(upload_freq, (int, float))`
- Flask runs with `debug=False` on `127.0.0.1:8080`
- Stealth mode applied via `playwright_stealth.Stealth()` to the Playwright page

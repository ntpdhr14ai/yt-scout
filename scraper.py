import re
import sys
import random
import asyncio
from collections import deque
import urllib.parse
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

import library

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_stealth = Stealth()

MAX_CHANNELS_PER_BATCH = 10
COOLDOWN_SECONDS = 600
SEARCH_SCROLL_PASSES = 1        # minimum loads before the new-channel target may stop scrolling
MAX_SEARCH_SCROLL_PASSES = 20   # hard ceiling per keyword (~10–20 cards per load -> ~300 cards)
SEARCH_STALL_PASSES = 3         # at the loader, no new cards this many times -> exhausted
SEARCH_LOAD_WAIT_SECONDS = 8    # how long to wait for the next batch of cards after each load
NEW_CANDIDATES_PER_RESULT = 3   # aim for batch_size x 3 unseen channels before stopping
_YOUTUBE_TITLE_SUFFIX = " - YouTube"

processed_handles: set = set()
discovery_queue: deque = deque()
stop_requested: bool = False
video_context: dict = {}


def reset_session_state() -> None:
    """Clear all cross-batch module state. Call ONCE at the start of a fresh
    run, NEVER between batches — batch-to-batch dedup and loop termination
    depend on processed_handles accumulating across batches within a run.
    Uses .clear() (in-place) so external imports of these objects stay valid."""
    processed_handles.clear()
    discovery_queue.clear()
    video_context.clear()


# Whole-line country names, as shown in the About dialog table (diag 2026-11-03:
# a row reading just "Thailand"). Anchored so a description line can't match.
_COUNTRY_LINE_RE = re.compile(
    r"^(?:Thailand|United States|United Kingdom|Japan|South Korea|Korea|China|India|"
    r"Brazil|Indonesia|Vietnam|Philippines|Malaysia|Singapore|Australia|Germany|"
    r"France|Canada|Russia|Mexico|Spain|Italy|Netherlands|Turkey|Egypt|Nigeria|"
    r"Pakistan|Myanmar \(Burma\)|Myanmar|Cambodia|Laos|Taiwan|New Zealand|Argentina|"
    r"Colombia|Chile|Ukraine|Poland|Portugal|Greece|Hong Kong|ไทย|ประเทศไทย)$",
    re.IGNORECASE,
)


def country_from_lines(lines) -> str:
    """First line that is exactly a country name; '' if none (unknown, not Thai)."""
    for line in lines:
        t = (line or "").strip()
        if _COUNTRY_LINE_RE.match(t):
            return t
    return ""


def external_link(href: str | None) -> str | None:
    """Destination of an About-page link. YouTube wraps external links as
    youtube.com/redirect?...&q=<url>; links back to YouTube/Google are dropped."""
    if not href:
        return None
    parsed = urllib.parse.urlparse(href)
    if parsed.path == "/redirect" and "youtube.com" in parsed.netloc:
        q = urllib.parse.parse_qs(parsed.query).get("q")
        href = q[0] if q else None
        if not href:
            return None
        parsed = urllib.parse.urlparse(href)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower()
    if any(h in host for h in ("youtube.com", "youtu.be", "google.com")):
        return None
    return href


_THAI_DIGIT_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

_TIME_WORDS = {
    "ago", "hour", "hours", "day", "days", "week", "weeks",
    "month", "months", "year", "years",
    "วัน", "เดือน", "ปี", "ชั่วโมง", "สัปดาห์", "ที่แล้ว", "ผ่านมา",
}


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log_start(fn: str, handle: str) -> None:
    print(f"[scraper] START: {fn} -> {handle}")

def _log_ok(field: str, value, handle: str) -> None:
    print(f"[scraper] OK: {field} = {value!r} -> {handle}")

def _log_retry(n: int, field: str, handle: str) -> None:
    print(f"[scraper] RETRY {n}: {field} -> {handle}")

def _log_failed(field: str, handle: str, reason) -> None:
    print(f"[scraper] FAILED: {field} -> {handle} — reason: {reason}")

def _log_dropped(handle: str, reason: str) -> None:
    print(f"[scraper] DROPPED: {handle} — reason: {reason}")

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _rand_delay(lo: float = 5, hi: float = 15) -> float:
    return random.uniform(lo, hi)

def _has_thai_script(text: str) -> bool:
    return bool(re.search(r"[฀-๿]", text or ""))

def is_thailand_channel(declared_country, text_blob):
    if declared_country and 'thailand' in declared_country.lower():
        return True
    if text_blob and re.search(r'[฀-๿]', text_blob):
        return True
    return False

def _parse_sub_count(text: str) -> int | None:
    if not text:
        return None
    text = text.translate(_THAI_DIGIT_MAP).replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([KMB]?)", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2).upper()
    return int(value * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1))

def parse_view_count(text: str) -> int | None:
    return _parse_sub_count(text)

def _days_ago(text: str) -> float | None:
    if not text:
        return None
    text_n = text.translate(_THAI_DIGIT_MAP)
    t = text_n.lower()
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {
            "second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24,
            "day": 1.0, "week": 7.0, "month": 30.0, "year": 365.0,
        }[unit]
    for thai_unit, days in [
        ("ชั่วโมง", 1 / 24), ("สัปดาห์", 7.0),
        ("เดือน", 30.0), ("ปี", 365.0), ("วัน", 1.0),
    ]:
        if thai_unit in text:
            m = re.search(r"(\d+)", text_n)
            if m:
                return int(m.group(1)) * days
    return None

def _months_ago(text: str) -> float | None:
    d = _days_ago(text)
    return d / 30 if d is not None else None

def parse_recency(text: str) -> int:
    if not text: return 999
    strict = age_days_from_line(text)
    if strict is not None:
        return strict
    t = text.lower().strip()
    for prefix in ['streamed','premiered','updated','scheduled','เผยแพร่เมื่อ','สตรีมเมื่อ','อัปเดตเมื่อ']:
        t = t.replace(prefix, '').strip()
    num = lambda s: int(re.search(r'\d+', s).group()) if re.search(r'\d+', s) else 1
    if any(w in t for w in ['just now','moments','เพิ่งอัป','ไม่กี่วินาที','ไม่กี่นาที']): return 0
    if any(w in t for w in ['hour','ชั่วโมง','ชม.']): return 0
    if any(w in t for w in ['day','วัน']): return num(t)
    if any(w in t for w in ['week','สัปดาห์']): return num(t) * 7
    if any(w in t for w in ['month','เดือน']): return num(t) * 30
    if any(w in t for w in ['year','ปี']): return 999
    return 999


# Whole-line patterns only: a title that merely contains "day" or "วัน" must
# never be read as an upload date.
_AGE_EN_RE = re.compile(
    r"^(?:streamed|premiered|updated)?\s*(\d+)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w|months?|mo|years?|yrs?|y)"
    r"\s+ago$",
    re.IGNORECASE,
)
_AGE_TH_RE = re.compile(
    r"^(?:สตรีมเมื่อ|เผยแพร่เมื่อ|อัปเดตเมื่อ)?\s*(\d+)\s*"
    r"(วินาที|นาที|ชั่วโมง|ชม\.|วัน|สัปดาห์|เดือน|ปี)\s*ที่แล้ว$"
)
_UNIT_DAYS = {
    "s": 0, "sec": 0, "second": 0, "m": 0, "min": 0, "minute": 0,
    "h": 0, "hr": 0, "hour": 0, "d": 1, "day": 1, "w": 7, "wk": 7, "week": 7,
    "mo": 30, "month": 30, "y": None, "yr": None, "year": None,
    "วินาที": 0, "นาที": 0, "ชั่วโมง": 0, "ชม.": 0, "วัน": 1, "สัปดาห์": 7,
    "เดือน": 30, "ปี": None,
}
_VIEWS_RE = re.compile(
    r"^([\d.,]+)\s*([KMB])?(?:\s*(?:views?|watching))?$", re.IGNORECASE
)


def age_days_from_line(line: str) -> int | None:
    """'8h ago' -> 0, '1d ago' -> 1, '2 weeks ago' -> 14, '1y ago' -> 999.
    Returns None if the whole line is not an age stamp."""
    t = (line or "").strip().translate(_THAI_DIGIT_MAP)
    m = _AGE_EN_RE.match(t) or _AGE_TH_RE.match(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit not in _UNIT_DAYS:
        unit = unit.rstrip("s")
    per = _UNIT_DAYS.get(unit, None)
    return 999 if per is None else n * per


def views_from_line(line: str) -> int | None:
    """'4.7K' -> 4700, '203' -> 203, '14K views' -> 14000. None otherwise."""
    m = _VIEWS_RE.match((line or "").strip())
    if not m:
        return None
    return _parse_sub_count(m.group(1) + (m.group(2) or ""))


def parse_renderer_lines(lines: list[str]) -> tuple[int | None, int | None]:
    """(age_days, views) for one grid item. Layout seen 2026-11:
    [duration, title, views, age]. Views = nearest view-shaped line above age."""
    for i, line in enumerate(lines):
        age = age_days_from_line(line)
        if age is not None:
            for prev in reversed(lines[:i]):
                v = views_from_line(prev)
                if v is not None:
                    return age, v
            return age, None
    return None, None


def _is_timestamp_text(txt: str) -> bool:
    t = txt.lower()
    return any(w in t for w in _TIME_WORDS)

SUBSCRIBER_TIERS = {
    'under_5k':     (0, 5000),
    '5k_10k':       (5001, 10000),
    '10k_100k':     (10001, 100000),
    '100k_500k':    (100001, 500000),
    '500k_1m':      (500001, 1000000),
    'over_1m':      (1000001, 999999999),
}

def gate_subscribers(channel, tier):
    if not tier or tier == 'any':
        return True, 'ok'
    lo, hi = SUBSCRIBER_TIERS.get(tier, (0, 999999999))
    subs = channel.get('subs')
    if subs is None:
        return False, 'unparseable subscriber count'
    if not (lo <= subs <= hi):
        return False, f'subs {subs:,} outside tier {tier} ({lo:,}–{hi:,})'
    return True, 'ok'

def gate_location(channel, region):
    if region != 'TH':
        return True, 'no region filter'
    declared = (channel.get('declared_country') or '').lower()
    about_text = (channel.get('about_text') or '')
    name = (channel.get('name') or '')
    blob = f'{declared} {about_text} {name}'
    if 'thailand' in declared:
        return True, 'declared country = Thailand'
    if re.search(r'[฀-๿]', blob):
        return True, 'Thai script detected'
    return False, 'no Thailand signal found'

def gate_category(category, target):
    """Fail-closed: with a target set, unknown category is a rejection."""
    if not target:
        return True, 'no category filter'
    if not category:
        return False, f"category unknown, target '{target}'"
    if target.lower() != category.lower():
        return False, f"category '{category}' does not match target '{target}'"
    return True, 'ok'


def evaluate_cached(facts, stage, region, sub_tier, target_category, days_limit):
    """Re-apply today's gates to stored facts.
    Returns ('pass', row) | ('reject', reason) | ('scrape', reason)."""
    ok, reason = gate_subscribers({'subs': facts.get('subs')}, sub_tier)
    if not ok:
        return 'reject', reason
    if stage == library.STAGE_SUBS:
        return 'scrape', 'only subscriber count on file'
    if region == 'TH' and not facts.get('th_signal'):
        return 'reject', 'no Thailand signal found'
    if stage == library.STAGE_LOCATION:
        return 'scrape', 'metrics not on file'
    if facts.get('metrics_days') != days_limit:
        return 'scrape', f"metrics on file are for {facts.get('metrics_days')}d, need {days_limit}d"
    ok, reason = gate_category(facts.get('category') or None, target_category)
    if not ok:
        return 'reject', reason
    row = dict(facts)
    row['score'] = score_from_facts(facts)
    return 'pass', row


def score_from_facts(facts) -> int:
    """Score from stored facts, using the window the metrics were taken over."""
    count, days = facts.get('upload_frequency'), facts.get('metrics_days')
    per_month = count * 30 / days if isinstance(count, int) and days else None
    return _score_channel(facts.get('subs'), facts.get('avg_views_videos'),
                          facts.get('avg_views_shorts'), per_month,
                          facts.get('location_badge'))


_library_ok = True


def _lib(fn, *args):
    """Call a library function; on storage failure warn once and carry on scraping."""
    global _library_ok
    if not _library_ok:
        return None
    try:
        return fn(*args)
    except library.LibraryUnavailable as e:
        _library_ok = False
        print(f'[library] WARNING: disabled for this run — {e}')
        return None


def scroll_decision(passes: int, card_counts: list[int], new_count: int, target: int,
                    has_loader: bool = True):
    """Whether to keep loading search results. Returns (stop, reason).
    card_counts: cards on the page after each load so far.
    has_loader: YouTube's continuation spinner is still on the page."""
    if not has_loader:
        return True, 'end of results (no loader on page)'
    if passes < SEARCH_SCROLL_PASSES:
        return False, 'minimum depth'
    if new_count >= target:
        return True, f'{new_count} new channels (target {target})'
    if passes >= MAX_SEARCH_SCROLL_PASSES:
        return True, f'max {MAX_SEARCH_SCROLL_PASSES} scrolls'
    recent = card_counts[-(SEARCH_STALL_PASSES + 1):]
    if len(recent) == SEARCH_STALL_PASSES + 1 and len(set(recent)) == 1:
        return True, f'no new cards after {SEARCH_STALL_PASSES} loads at the loader (results exhausted)'
    return False, 'need more'


_CARD_HANDLES_JS = """() => [...document.querySelectorAll('ytd-video-renderer')].map(c => {
  const a = c.querySelector('#channel-name a, ytd-channel-name a, a.yt-formatted-string[href*="/@"]');
  const h = a && a.getAttribute('href');
  return h && h.includes('/@') ? h.split('/@')[1].split('/')[0].trim() : null;
})"""


def _score_channel(
    subs,
    avg_video_views: int | None,
    avg_short_views: int | None,
    upload_freq: float | None,
    location_badge: str | None,
) -> int:
    score = 0
    subs_int = subs if isinstance(subs, int) else None

    if subs_int:
        if subs_int >= 1_000_000:
            score += 25
        elif subs_int >= 100_000:
            score += 20
        elif subs_int >= 10_000:
            score += 15
        elif subs_int >= 1_000:
            score += 8
        else:
            score += 3

    if subs_int and avg_video_views:
        ratio = avg_video_views / max(subs_int, 1)
        if ratio >= 0.3:
            score += 30
        elif ratio >= 0.1:
            score += 22
        elif ratio >= 0.05:
            score += 15
        elif ratio >= 0.01:
            score += 8
        else:
            score += 3

    if subs_int and avg_short_views:
        ratio = avg_short_views / max(subs_int, 1)
        if ratio >= 0.5:
            score += 20
        elif ratio >= 0.2:
            score += 14
        elif ratio >= 0.05:
            score += 8
        else:
            score += 3

    if isinstance(upload_freq, (int, float)) and upload_freq:
        if upload_freq >= 8:
            score += 15
        elif upload_freq >= 4:
            score += 11
        elif upload_freq >= 2:
            score += 7
        elif upload_freq >= 0.5:
            score += 3

    if location_badge in ("TH", "TH inferred"):
        score += 10

    return min(score, 100)

# ---------------------------------------------------------------------------
# Subscriber count extraction  (4-step)
# ---------------------------------------------------------------------------

async def _scrape_subs(page, ch_url: str, handle: str) -> int | None:
    _log_start("_scrape_subs", handle)

    print(f"[scraper] START: subs (span scan) -> {handle}")
    try:
        found = False
        for sel in ["yt-formatted-string", "span"]:
            els = await page.query_selector_all(sel)
            for el in els:
                try:
                    txt = (await el.inner_text()).strip()
                    if "subscriber" in txt.lower() and len(txt) < 60:
                        subs = _parse_sub_count(txt)
                        if subs is not None:
                            _log_ok("subs", f"{txt} -> {subs}", handle)
                            return subs
                except Exception:
                    pass
            if found:
                break
        if not found:
            _log_failed("subs", handle, "no matching span found")
    except Exception as e:
        _log_failed("subs", handle, e)

    about_url = ch_url.rstrip("/") + "/about"
    print(f"[scraper] START: subs step 4 (about regex) -> {handle}")
    try:
        await page.goto(about_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3, 5))
        src = await page.content()
        m = re.search(r"([\d][\d,. ]*)\s*subscriber", src, re.IGNORECASE)
        if m:
            subs = _parse_sub_count(m.group(1))
            if subs is not None:
                _log_ok("subs", f"{m.group(0)} -> {subs}", handle)
                return subs
        _log_failed("subs step 4", handle, "regex found nothing")
    except Exception as e:
        _log_failed("subs step 4", handle, e)

    _log_failed("subs", handle, "all 4 steps exhausted — Pending Check")
    return None

# ---------------------------------------------------------------------------
# About tab
# ---------------------------------------------------------------------------

async def _scrape_about(page, ch_url: str, handle: str, region: str = '') -> tuple[dict, bool, str]:
    data: dict = {}
    about_url = ch_url.rstrip("/") + "/about"
    _log_start("_scrape_about", handle)

    try:
        await page.goto(about_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
            _log_ok("networkidle", "reached", handle)
        except Exception as e:
            _log_failed("networkidle", handle, e)

        body_text = ""
        try:
            body_text = await page.inner_text("body")
            _log_ok("body_text", f"{len(body_text)} chars", handle)
        except Exception as e:
            _log_failed("body_text", handle, e)
        data["about_text"] = body_text

        name = ""
        try:
            title = await page.title()
            if title and _YOUTUBE_TITLE_SUFFIX in title:
                name = title.replace(_YOUTUBE_TITLE_SUFFIX, "").strip()
                _log_ok("name (title)", name, handle)
        except Exception as e:
            _log_failed("name (title)", handle, e)

        if not name:
            try:
                og = await page.get_attribute('meta[property="og:title"]', "content")
                if og:
                    name = og.strip()
                    _log_ok("name (og:title)", name, handle)
            except Exception as e:
                _log_failed("name (og:title)", handle, e)

        if not name:
            for sel in [
                "#channel-name yt-formatted-string",
                "ytd-channel-name yt-formatted-string",
                "yt-formatted-string.ytd-channel-name",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        candidate = (await el.inner_text()).strip()
                        if candidate:
                            name = candidate
                            _log_ok(f"name ({sel})", name, handle)
                            break
                except Exception:
                    pass

        if not name:
            _log_failed("name", handle, "all strategies exhausted")
        data["name"] = name

        try:
            sub_el = page.locator("yt-formatted-string#subscriber-count")
            if await sub_el.count() > 0:
                data["sub_text"] = (await sub_el.first.inner_text()).strip()
                _log_ok("sub_text", data["sub_text"], handle)
        except Exception as e:
            _log_failed("sub_text", handle, e)

        # Country is an unlabeled row in the About dialog table ("Thailand")
        country = ""
        try:
            rows = await page.eval_on_selector_all(
                "ytd-about-channel-renderer table tr", "els => els.map(e => e.innerText)")
            country = country_from_lines(
                l for r in rows for l in (r or "").splitlines())
            if country:
                _log_ok("country (about table)", country, handle)
        except Exception as e:
            _log_failed("country (about table)", handle, e)
        data["country"] = country
        data["declared_country"] = country

        is_th = is_thailand_channel(country, body_text)
        if is_th:
            location_badge = 'TH' if (country.strip().lower() in ('th', 'thailand') or 'thailand' in body_text.lower()) else 'TH inferred'
            keep = True
            _log_ok("location", location_badge, handle)
        else:
            location_badge = '?'
            if region == 'TH':
                _log_dropped(handle, 'not Thailand channel')
                keep = False
            else:
                keep = True
                _log_ok("location", "? (not TH, kept)", handle)

        if not keep:
            data.setdefault("emails", [])
            data.setdefault("links", [])
            return data, False, "?"

        # Email reveal button needs a signed-in session; logged out it never
        # appears (4 s timeout on every channel, seen 2026-11). Scan page text only.
        try:
            page_text = await page.content()
            emails = re.findall(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page_text
            )
            data["emails"] = list(
                set(e for e in emails if "youtube" not in e and "google" not in e)
            )
            _log_ok("emails", data["emails"], handle)
        except Exception as e:
            data["emails"] = []
            _log_failed("emails", handle, e)

        try:
            hrefs = await page.eval_on_selector_all(
                "#link-list-container a", "els => els.map(e => e.getAttribute('href'))")
            links = []
            for href in hrefs:
                dest = external_link(href)
                if dest and dest not in links:
                    links.append(dest)
            data["links"] = links[:5]
            _log_ok("links", data["links"], handle)
        except Exception as e:
            data["links"] = []
            _log_failed("links", handle, e)

    except Exception as e:
        # Rule E: loading failure is not a drop reason — keep with ? badge
        _log_failed("_scrape_about", handle, e)
        data.setdefault("name", "")
        data.setdefault("links", [])
        data.setdefault("emails", [])
        return data, True, "?"

    return data, True, location_badge

_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|/shorts/)([\w-]{11})")


def video_id(href: str | None) -> str | None:
    m = _VIDEO_ID_RE.search(href or "")
    return m.group(1) if m else None


def is_fallback_grid(tab: str, first_href: str | None, videos_first_href: str | None) -> bool:
    """True when a shorts/live URL is really showing the videos grid.
    Seen 2026-11: a channel without those tabs served the videos grid at
    /shorts and /streams with no redirect, so all three tabs had equal stats."""
    if tab == 'shorts' and first_href and '/shorts/' not in first_href:
        return True
    vid = video_id(first_href)
    return vid is not None and vid == video_id(videos_first_href)


async def scrape_tab_metrics(page, handle, tab, days_limit, videos_first_href=None):
    '''
    Visit a channel tab and read the grid items.
    Returns {count, avg_views, first_video_url}
      videos/live: count = items whose age stamp is inside days_limit;
                   avg_views = mean views of those items.
                   count None = tab could not be read (unknown, not zero).
      shorts:      the grid shows no dates, so count is None and avg_views is
                   the mean of the newest 20 Shorts (window not applied).
    '''
    TAB_URLS = {
        'videos': f'https://www.youtube.com/@{handle}/videos',
        'shorts': f'https://www.youtube.com/@{handle}/shorts',
        'live':   f'https://www.youtube.com/@{handle}/streams',
    }
    url = TAB_URLS[tab]
    unknown = {'count': None, 'avg_views': None, 'first_video_url': None}
    print(f'[metrics] START: {tab} tab -> @{handle} (last {days_limit}d)')

    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)

        # Redirect check — if tab is empty YouTube may redirect to /videos
        if tab == 'shorts' and '/shorts' not in page.url:
            print(f'[metrics] EMPTY: {tab} tab redirected -> @{handle}')
            return {'count': 0, 'avg_views': None, 'first_video_url': None}
        if tab == 'live' and '/streams' not in page.url:
            print(f'[metrics] EMPTY: {tab} tab redirected -> @{handle}')
            return {'count': 0, 'avg_views': None, 'first_video_url': None}

        try:
            await page.wait_for_selector('ytd-rich-item-renderer', timeout=10000)
        except Exception:
            if tab in ('shorts', 'live'):
                # Seen 2026-11: a channel with no streams shows an empty grid, no redirect
                print(f'[metrics] EMPTY: {tab} tab has no items -> @{handle}')
                return {'count': 0, 'avg_views': None, 'first_video_url': None}
            print(f'[metrics] FAILED: {tab} grid never rendered -> @{handle} (count unknown)')
            return unknown

        await asyncio.sleep(_rand_delay(2, 4))
        for _ in range(3):
            await page.evaluate('window.scrollBy(0, 700)')
            await asyncio.sleep(_rand_delay(1, 2))

        renderers = await page.query_selector_all('ytd-rich-item-renderer')
        print(f'[metrics] Renderers found: {len(renderers)} on {tab} tab')

        first_video_url = None
        if renderers:
            href = await renderers[0].eval_on_selector_all(
                'a[href*="/watch?v="], a[href*="/shorts/"]',
                'els => els.length ? els[0].getAttribute("href") : null')
            if href:
                first_video_url = href if href.startswith('http') else f'https://www.youtube.com{href}'

        if tab in ('shorts', 'live') and is_fallback_grid(tab, first_video_url, videos_first_href):
            print(f'[metrics] EMPTY: {tab} tab is showing the videos grid -> @{handle}')
            return {'count': 0, 'avg_views': None, 'first_video_url': None}

        in_window_views = []
        in_window_count = 0
        undated = 0

        sample = renderers[:20] if tab == 'shorts' else renderers  # all loaded items (~30)
        for renderer in sample:
            try:
                lines = [l.strip() for l in (await renderer.inner_text()).splitlines() if l.strip()]
            except Exception as e:
                print(f'[metrics] Renderer parse error: {e}')
                continue

            if tab == 'shorts':
                v = next((views_from_line(l) for l in lines if views_from_line(l) is not None), None)
                if v is not None:
                    in_window_views.append(v)
                continue

            age_days, views = parse_renderer_lines(lines)
            if age_days is None:
                undated += 1
                continue          # fail-closed: no readable date -> not counted
            if age_days > days_limit:
                continue
            in_window_count += 1
            if views is not None:
                in_window_views.append(views)

        avg = int(sum(in_window_views) / len(in_window_views)) if in_window_views else None
        count = None if tab == 'shorts' else in_window_count
        print(f'[metrics] OK: {tab} -> count={count}, avg_views={avg}, undated={undated}')
        return {'count': count, 'avg_views': avg, 'first_video_url': first_video_url}

    except Exception as e:
        print(f'[metrics] FAILED: {tab} -> @{handle} — {e}')
        return unknown

# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

async def scrape_channels(
    keyword: str,
    region: str,
    sub_tier: str,
    batch_size: int,
    progress_callback=None,
    target_category: str = '',
    activity_filter: str = 'any',
) -> list[dict]:
    global _library_ok
    _library_ok = True   # retry storage each run even if it failed last run
    print(f'[diag] scrape_channels START — processed_handles={len(processed_handles)} discovery_queue={len(discovery_queue)}')
    batch_size = min(batch_size, MAX_CHANNELS_PER_BATCH)

    # Normalize keyword input to a list
    if isinstance(keyword, str):
        keyword_list = [kw.strip() for kw in keyword.split(',') if kw.strip()]
    else:
        keyword_list = [kw.strip() for kw in keyword if kw.strip()]
    if not keyword_list:
        keyword_list = ['']
    print(f'[search] Keyword list: {keyword_list}')

    results: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await _stealth.apply_stealth_async(page)

        # ── PHASE 2: Content-First Video Search ──────────────────────────
        all_candidates = {}  # unified dedup dict across all keywords
        video_context.clear()

        for current_kw in keyword_list:
            print(f'[search] Processing keyword: "{current_kw}"')
            encoded_kw = urllib.parse.quote(current_kw)
            search_url = (
                f'https://www.youtube.com/results'
                f'?search_query={encoded_kw}&gl=TH&hl=th'
            )
            print(f'[search] Navigating to: {search_url}')
            await page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(_rand_delay(3, 5))

            # Scroll until enough channels we have not seen yet (this run or in the
            # library) are on the page, or the page stops loading new cards.
            target_new = batch_size * NEW_CANDIDATES_PER_RESULT
            known_cache: dict = {}

            def _is_new(h):
                if h in processed_handles:
                    return False
                if h not in known_cache:
                    known_cache[h] = _lib(library.lookup, h) is None
                return known_cache[h]

            # Jump to YouTube's loader each time: scrolling 800 px at a time stopped
            # ~400 px short of it and the page never asked for more (diag 2026-11-03).
            card_counts, passes = [], 0
            while True:
                before = await page.evaluate("document.querySelectorAll('ytd-video-renderer').length")
                loader = await page.query_selector('ytd-continuation-item-renderer')
                if loader:
                    await loader.scroll_into_view_if_needed()
                else:
                    await page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
                await asyncio.sleep(_rand_delay(1, 2))
                for _ in range(SEARCH_LOAD_WAIT_SECONDS * 2):
                    if await page.evaluate("document.querySelectorAll('ytd-video-renderer').length") > before:
                        break
                    await asyncio.sleep(0.5)
                passes += 1
                handles = [h for h in await page.evaluate(_CARD_HANDLES_JS) if h]
                card_counts.append(len(handles))
                new_count = len({h for h in handles if _is_new(h)})
                has_loader = await page.query_selector('ytd-continuation-item-renderer') is not None
                stop, why = scroll_decision(passes, card_counts, new_count, target_new, has_loader)
                if stop:
                    print(f'[search] Scroll stop after {passes} passes: {why} ({len(handles)} cards)')
                    break

            # Diagnostic
            body_len = len(await page.inner_text('body'))
            print(f'[search] Body length after scroll: {body_len} chars')

            # Extract video cards — each card gives us: title, video URL, channel handle
            video_cards = await page.query_selector_all('ytd-video-renderer')
            print(f'[search] Video cards found: {len(video_cards)}')

            kw_candidates = {}  # handle → {video_title, video_url}
            for card in video_cards:
                try:
                    # Video title and URL
                    title_el = await card.query_selector('a#video-title')
                    if not title_el:
                        continue
                    video_title = (await title_el.inner_text()).strip()
                    video_href = await title_el.get_attribute('href')
                    if not video_href:
                        continue
                    video_url = f'https://www.youtube.com{video_href}'

                    # Channel handle from video card metadata
                    chan_el = await card.query_selector(
                        '#channel-name a, '
                        'ytd-channel-name a, '
                        'a.yt-formatted-string[href*="/@"]'
                    )
                    if not chan_el:
                        continue
                    chan_href = await chan_el.get_attribute('href')
                    if not chan_href or '/@' not in chan_href:
                        continue
                    handle = chan_href.split('/@')[1].split('/')[0].strip()
                    if not handle:
                        continue

                    # Extract clean channel display name from the card
                    channel_name_text = (await chan_el.inner_text()).strip()

                    # Accept candidate
                    if handle not in kw_candidates and handle not in processed_handles:
                        kw_candidates[handle] = {
                            'video_title': video_title,
                            'video_url': video_url,
                            'channel_name': channel_name_text,
                        }
                        print(f'[search] ACCEPT: @{handle} — ranked by YouTube for "{current_kw}"')

                except Exception as e:
                    print(f'[search] Card parse error: {e}')
                    continue

            for handle, ctx in kw_candidates.items():
                _lib(library.record_sighting, handle, current_kw, ctx['video_title'], ctx['video_url'])

            new_count = 0
            for handle, ctx in kw_candidates.items():
                if handle not in all_candidates and handle not in processed_handles:
                    all_candidates[handle] = ctx
                    video_context[handle] = ctx
                    new_count += 1
            print(f'[search] Keyword "{current_kw}": +{new_count} new candidates (pool now {len(all_candidates)})')
            await asyncio.sleep(_rand_delay(3, 6))

        print(f'[search] Total unique candidates after all keywords: {len(all_candidates)}')
        if not all_candidates:
            print('[search] ZERO candidates — page may be partially rendered')
            return []

        # Convert to list for pipeline processing
        # Library-known channels cost ~0 s, so only new channels count toward the work cap
        known = [h for h in all_candidates if _lib(library.lookup, h) is not None]
        new = [h for h in all_candidates if h not in known][:batch_size * 5]
        handle_list = known + new
        print(f'[search] Candidates: {len(new)} new + {len(known)} from library')
        # ── END PHASE 2 ───────────────────────────────────────────────────

        channel_links = [f'/@{h}' for h in handle_list]

        # Add discovery queue items from prior batches
        disc_added = []
        while discovery_queue and (len(channel_links) + len(disc_added)) < batch_size * 2:
            h = discovery_queue.popleft()
            if h not in processed_handles:
                disc_path = f'/@{h}'
                if disc_path not in channel_links and disc_path not in disc_added:
                    disc_added.append(disc_path)
        channel_links = channel_links + disc_added
        print(f"[scraper] Collected {len(channel_links)} channel links ({len(disc_added)} from discovery queue)")

        days_limit = int(activity_filter) if activity_filter != 'any' else 30  # UI shows 30 for 'any'
        cached_hits = 0   # library matches are shown but do not use up the batch cap

        # Per-channel loop
        for idx, ch_path in enumerate(channel_links):
            if stop_requested:
                print('[pipeline] STOP requested — ending batch')
                break
            handle = ch_path.lstrip("/")        # @Amazing_Thailand
            handle_bare = handle.lstrip("@")    # Amazing_Thailand
            ch_url = f"https://www.youtube.com{ch_path}"

            if progress_callback:
                progress_callback(idx, len(channel_links), f"Checking {handle}")

            print(f'[pipeline] Evaluating {handle} ({idx+1}/{len(channel_links)})')
            # Record before any gate: a rejected channel must not be re-checked next batch
            processed_handles.add(handle_bare)

            cached = _lib(library.lookup, handle_bare)
            if cached:
                facts, stage, checked_at = cached
                verdict, payload = evaluate_cached(facts, stage, region, sub_tier,
                                                   target_category, days_limit)
                if verdict == 'reject':
                    print(f'[library] SKIPPED: {handle} — {payload} (checked {checked_at})')
                    continue
                if verdict == 'pass':
                    ctx = video_context.get(handle_bare, {})
                    payload['video_title'] = ctx.get('video_title', '')
                    payload['video_url'] = ctx.get('video_url', '')
                    payload['cached_at'] = checked_at
                    results.append(payload)
                    cached_hits += 1
                    print(f'[library] HIT: {handle} — from library (checked {checked_at})')
                    continue
                print(f'[library] RESCRAPE: {handle} — {payload}')

            await page.goto(ch_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))

            ch_canonical_url = ch_url  # resolved after _scrape_about below

            subs = await _scrape_subs(page, ch_url, handle)
            passed, reason = gate_subscribers({'subs': subs}, sub_tier)
            if not passed:
                print(f'[scraper] SKIPPED: {handle} — {reason}')
                _lib(library.save, handle_bare, {'handle': handle_bare, 'subs': subs}, library.STAGE_SUBS)
                continue

            channel_data, keep, location_badge = await _scrape_about(page, ch_url, handle, region)

            # Extract UC ID from /about page source (page is reliably on /about after _scrape_about)
            uc_id = None
            try:
                src = await page.content()
                m = re.search(r'"externalId"\s*:\s*"(UC[\w-]+)"', src)
                if m:
                    uc_id = m.group(1)
            except Exception:
                pass
            if not uc_id:
                try:
                    canon = await page.get_attribute('link[rel="canonical"]', "href")
                    if canon:
                        m = re.search(r'/channel/(UC[\w-]+)', canon)
                        if m:
                            uc_id = m.group(1)
                except Exception:
                    pass
            _log_ok("uc_id", uc_id, handle)
            ch_canonical_url = f'https://www.youtube.com/channel/{uc_id}' if uc_id else ch_url

            channel_data["subs"] = subs
            channel_data["location_badge"] = location_badge
            channel_data["th_signal"] = gate_location(channel_data, 'TH')[0]
            channel_data["url"] = ch_canonical_url
            channel_data["channel_url"] = ch_canonical_url
            channel_data["handle"] = handle_bare
            channel_data["uc_id"] = uc_id

            passed, reason = gate_location(channel_data, region)
            if not passed:
                _lib(library.save, handle_bare, channel_data, library.STAGE_LOCATION)
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            await asyncio.sleep(_rand_delay(5, 10))

            vid_metrics = await scrape_tab_metrics(page, handle_bare, 'videos', days_limit)

            # Early exit: if zero uploads in timeframe, skip Shorts and Live entirely
            if vid_metrics['count'] == 0:
                print(f'[pipeline] EARLY EXIT: @{handle_bare} — 0 uploads in last {days_limit}d, skipping Shorts/Live tabs')
                short_metrics = {'count': 0, 'avg_views': None, 'first_video_url': None}
                live_metrics  = {'count': 0, 'avg_views': None, 'first_video_url': None}
            else:
                first = vid_metrics.get('first_video_url')
                short_metrics = await scrape_tab_metrics(page, handle_bare, 'shorts', days_limit, first)
                live_metrics  = await scrape_tab_metrics(page, handle_bare, 'live',   days_limit, first)

            channel_data['upload_frequency'] = vid_metrics['count']
            channel_data['avg_views_videos'] = vid_metrics['avg_views']
            channel_data['avg_views_shorts'] = short_metrics['avg_views']
            channel_data['avg_views_live']   = live_metrics['avg_views']

            # Category: open the first video found on the videos tab (no second /videos load)
            category = None
            first_video = vid_metrics.get('first_video_url')
            if first_video:
                try:
                    await page.goto(first_video, wait_until='domcontentloaded', timeout=30000)
                    await page.wait_for_selector('meta[itemprop="genre"]', state='attached', timeout=10000)
                    el = await page.query_selector('meta[itemprop="genre"]')
                    if el:
                        category = await el.get_attribute('content')
                    _log_ok("category", category, handle)
                except Exception as e:
                    _log_failed("category", handle, e)
            else:
                _log_failed("category", handle, "no first video link found")
            channel_data["category"] = category or ""
            channel_data["metrics_days"] = days_limit
            _lib(library.save, handle_bare, channel_data, library.STAGE_COMPLETE)

            # Category gate — fail-closed (DECISIONS: Fail-Closed Filter Gates)
            passed, reason = gate_category(category, target_category)
            if not passed:
                print(f"[scraper] SKIPPED: {handle} — {reason}")
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            vid_count = vid_metrics['count']
            per_month = vid_count * 30 / days_limit if isinstance(vid_count, int) else None

            channel_data["score"] = _score_channel(
                channel_data.get("subs"),
                channel_data.get("avg_views_videos"),
                channel_data.get("avg_views_shorts"),
                per_month,
                location_badge,
            )
            ctx = video_context.get(handle_bare, {})
            channel_data['video_title'] = ctx.get('video_title', '')
            channel_data['video_url']   = ctx.get('video_url', '')

            results.append(channel_data)
            print(f'[pipeline] ADDED: {handle} — now {len(results)} in results')

            if len(results) - cached_hits >= batch_size:
                print(f'[pipeline] CAP: reached {batch_size} results this batch — stopping scrape')
                break
            _log_ok("channel_complete", channel_data.get("name", handle), handle)
            if progress_callback:
                progress_callback(
                    idx + 1,
                    len(channel_links),
                    f"Done {channel_data.get('name', handle)}",
                )

            if idx < len(channel_links) - 1:
                await asyncio.sleep(_rand_delay(10, 20))

        await browser.close()

    return results

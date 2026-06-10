import re
import sys
import random
import asyncio
from collections import deque
import urllib.parse
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_stealth = Stealth()

MAX_CHANNELS_PER_BATCH = 10
COOLDOWN_SECONDS = 600
SEARCH_SCROLL_PASSES = 8   # search-results page only; ~18 cards at 4 → ~35 at 8
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


_COUNTRY_RE = re.compile(
    r"^[A-Z]{2}$|Thailand|United States|United Kingdom|Japan|South Korea|"
    r"Korea|China|India|Brazil|Indonesia|Vietnam|Philippines|Malaysia|"
    r"Singapore|Australia|Germany|France|Canada|Russia|Mexico|Spain|"
    r"Italy|Netherlands|Turkey|Egypt|Nigeria|Pakistan|Myanmar|Cambodia|"
    r"Laos|Taiwan|New Zealand|Argentina|Colombia|Chile|Ukraine|Poland|"
    r"Portugal|Greece|Hong Kong",
    re.IGNORECASE,
)

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

        country = ""
        m = re.search(r"(?:Location|Country)\s*[:\|]\s*([^\n\r]+)", body_text, re.IGNORECASE)
        if m:
            country = m.group(1).strip()
            _log_ok("country (body label)", country, handle)
        if not country:
            try:
                loc_els = await page.locator(
                    "yt-formatted-string.ytd-channel-about-metadata-renderer"
                ).all()
                for el in loc_els:
                    txt = (await el.inner_text()).strip()
                    if txt and _COUNTRY_RE.search(txt):
                        country = txt
                        _log_ok("country (selector)", country, handle)
                        break
            except Exception as e:
                _log_failed("country (selector)", handle, e)
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

        try:
            await page.click("tp-yt-paper-button#email-reveal-button", timeout=4000)
            await asyncio.sleep(2)
            page_text = await page.content()
            emails = re.findall(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page_text
            )
            data["emails"] = list(
                set(e for e in emails if "youtube" not in e and "google" not in e)
            )
            _log_ok("emails", data["emails"], handle)
        except Exception as e:
            _log_failed("email_button", handle, e)
            try:
                page_text = await page.content()
                emails = re.findall(
                    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page_text
                )
                data["emails"] = list(
                    set(e for e in emails if "youtube" not in e and "google" not in e)
                )
            except Exception:
                data.setdefault("emails", [])

        try:
            link_els = await page.locator("#link-list-container a").all()
            links = []
            for el in link_els:
                href = (await el.get_attribute("href")) or ""
                if href.startswith("http") and "youtube.com" not in href:
                    links.append(href)
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

async def scrape_tab_metrics(page, handle, tab, days_limit):
    '''
    Visit a channel tab, parse ytd-rich-item-renderer line by line.
    Returns: {count: int, avg_views: int|None}
    count = videos published within days_limit
    avg_views = average views of those videos only
    '''
    TAB_URLS = {
        'videos': f'https://www.youtube.com/@{handle}/videos',
        'shorts': f'https://www.youtube.com/@{handle}/shorts',
        'live':   f'https://www.youtube.com/@{handle}/streams',
    }
    url = TAB_URLS[tab]
    print(f'[metrics] START: {tab} tab → @{handle} (last {days_limit}d)')

    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_load_state('networkidle', timeout=20000)

        # Redirect check — if tab is empty YouTube redirects to /videos
        if tab == 'shorts' and '/shorts' not in page.url:
            print(f'[metrics] EMPTY: {tab} tab redirected → @{handle}')
            return {'count': 0, 'avg_views': None}
        if tab == 'live' and '/streams' not in page.url:
            print(f'[metrics] EMPTY: {tab} tab redirected → @{handle}')
            return {'count': 0, 'avg_views': None}

        await asyncio.sleep(_rand_delay(2, 4))
        for _ in range(3):
            await page.evaluate('window.scrollBy(0, 700)')
            await asyncio.sleep(_rand_delay(1, 2))

        renderers = await page.query_selector_all('ytd-rich-item-renderer')
        print(f'[metrics] Renderers found: {len(renderers)} on {tab} tab')

        in_window_views = []
        in_window_count = 0

        for renderer in renderers[:20]:  # sample max 20
            try:
                text = await renderer.inner_text()
                lines = [l.strip() for l in text.splitlines() if l.strip()]

                # Find date line
                date_line = next(
                    (l for l in lines if any(w in l.lower() for w in [
                        'ago', 'hour', 'day', 'week', 'month', 'year',
                        'วัน', 'สัปดาห์', 'เดือน', 'ปี', 'ชั่วโมง',
                        'streamed', 'premiered'
                    ])), None
                )
                if not date_line:
                    continue

                age_days = parse_recency(date_line)
                if age_days > days_limit:
                    continue  # outside timeframe

                in_window_count += 1

                # Find view count line
                view_line = next(
                    (l for l in lines if any(w in l.lower() for w in [
                        'view', 'วิว', 'ครั้ง', 'watching'
                    ])), None
                )
                if view_line:
                    v = parse_view_count(view_line)
                    if v is not None:
                        in_window_views.append(v)

            except Exception as e:
                print(f'[metrics] Renderer parse error: {e}')
                continue

        avg = int(sum(in_window_views) / len(in_window_views)) if in_window_views else None
        print(f'[metrics] OK: {tab} → count={in_window_count}, avg_views={avg}')
        return {'count': in_window_count, 'avg_views': avg}

    except Exception as e:
        print(f'[metrics] FAILED: {tab} → @{handle} — {e}')
        return {'count': 0, 'avg_views': None}

# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

async def scrape_channels(
    keyword: str,
    region: str,
    sub_tier: str,
    batch_size: int,
    progress_callback=None,
    seen_handles: set | None = None,
    scroll_passes: int = 0,
    target_category: str = '',
    activity_filter: str = 'any',
) -> list[dict]:
    print(f'[diag] scrape_channels START — processed_handles={len(processed_handles)} discovery_queue={len(discovery_queue)}')
    if seen_handles is None:
        seen_handles = set()
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

            # Scroll to load more video cards
            for i in range(SEARCH_SCROLL_PASSES):
                await page.evaluate('window.scrollBy(0, 800)')
                await asyncio.sleep(_rand_delay(1, 2))

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

                    # Upstream dual-matching gate — keyword must appear in video title OR channel name
                    kw_lower = current_kw.lower()
                    if kw_lower and kw_lower not in video_title.lower() and kw_lower not in channel_name_text.lower():
                        print(f'[search] SKIP card: keyword not in title or channel name — "{video_title[:40]}" / "{channel_name_text}"')
                        continue

                    # Accept candidate
                    if handle not in kw_candidates and handle not in processed_handles:
                        kw_candidates[handle] = {
                            'video_title': video_title,
                            'video_url': video_url,
                            'channel_name': channel_name_text,
                        }
                        print(f'[search] ACCEPT: @{handle} — matched via "{"title" if kw_lower in video_title.lower() else "channel name"}"')

                except Exception as e:
                    print(f'[search] Card parse error: {e}')
                    continue

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
        handle_list = list(all_candidates.keys())[:batch_size * 5]
        # ── END PHASE 2 ───────────────────────────────────────────────────

        channel_links = [f'/@{h}' for h in handle_list]
        for link in channel_links:
            seen_handles.add(link)

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

        # Per-channel loop
        for idx, ch_path in enumerate(channel_links):
            handle = ch_path.lstrip("/")        # @Amazing_Thailand
            handle_bare = handle.lstrip("@")    # Amazing_Thailand
            ch_url = f"https://www.youtube.com{ch_path}"

            if progress_callback:
                progress_callback(idx, len(channel_links), f"Checking {handle}")

            print(f'[pipeline] Evaluating {handle} ({idx+1}/{len(channel_links)})')

            await page.goto(ch_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))

            ch_canonical_url = ch_url  # resolved after _scrape_about below

            subs = await _scrape_subs(page, ch_url, handle)
            passed, reason = gate_subscribers({'subs': subs}, sub_tier)
            if not passed:
                print(f'[scraper] SKIPPED: {handle} — {reason}')
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

            passed, reason = gate_location(channel_data, region)
            if not passed:
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            channel_data["subs"] = subs
            channel_data["location_badge"] = location_badge

            await asyncio.sleep(_rand_delay(5, 10))

            days_limit = int(activity_filter) if activity_filter != 'any' else 90
            vid_metrics = await scrape_tab_metrics(page, handle_bare, 'videos', days_limit)

            # Early exit: if zero uploads in timeframe, skip Shorts and Live entirely
            if vid_metrics['count'] == 0:
                print(f'[pipeline] EARLY EXIT: @{handle_bare} — 0 uploads in last {days_limit}d, skipping Shorts/Live tabs')
                short_metrics = {'count': 0, 'avg_views': None}
                live_metrics  = {'count': 0, 'avg_views': None}
            else:
                short_metrics = await scrape_tab_metrics(page, handle_bare, 'shorts', days_limit)
                live_metrics  = await scrape_tab_metrics(page, handle_bare, 'live',   days_limit)

            channel_data['upload_frequency'] = vid_metrics['count']
            channel_data['avg_views_videos'] = vid_metrics['avg_views']
            channel_data['avg_views_shorts'] = short_metrics['avg_views']
            channel_data['avg_views_live']   = live_metrics['avg_views']

            # Category extraction — navigate to first video page
            category = None
            videos_url = f'https://www.youtube.com/@{handle_bare}/videos'
            try:
                await page.goto(videos_url, wait_until='domcontentloaded', timeout=20000)
                first_video_href = None
                for v_sel in ['a#video-title-link', 'ytd-rich-item-renderer a#thumbnail', 'a[href*="/watch?v="]']:
                    el = await page.query_selector(v_sel)
                    if el:
                        href = await el.get_attribute('href')
                        if href and '/watch' in href:
                            first_video_href = href
                            break
                if first_video_href:
                    if first_video_href.startswith('/'):
                        first_video_href = f'https://www.youtube.com{first_video_href}'
                    await page.goto(first_video_href, wait_until='networkidle', timeout=30000)
                    el = await page.query_selector('meta[itemprop="genre"]')
                    if el:
                        category = await el.get_attribute('content')
                    if not category:
                        el = await page.query_selector('meta[property="og:video:tag"]')
                        if el:
                            category = await el.get_attribute('content')
                    _log_ok("category", category, handle)
                else:
                    _log_failed("category", handle, "no first video link found")
            except Exception as e:
                _log_failed("category", handle, e)
            channel_data["category"] = category or ""

            # Category filter (Rule A) — only skip if category extracted AND doesn't match
            if target_category:
                if not category:
                    channel_data["category"] = "Pending Check"
                    print(f"[scraper] PENDING: category unextractable → {handle}, continuing")
                elif target_category.lower() not in category.lower():
                    print(f"[scraper] SKIPPED: {handle} — category '{category}' does not match target '{target_category}'")
                    await asyncio.sleep(_rand_delay(2, 5))
                    continue

            channel_data["score"] = _score_channel(
                channel_data.get("subs"),
                channel_data.get("avg_views_videos"),
                channel_data.get("avg_views_shorts"),
                None,
                location_badge,
            )
            channel_data["url"] = ch_canonical_url
            channel_data["channel_url"] = ch_canonical_url
            channel_data["handle"] = handle_bare
            channel_data["uc_id"] = uc_id

            ctx = video_context.get(handle_bare, {})
            channel_data['video_title'] = ctx.get('video_title', '')
            channel_data['video_url']   = ctx.get('video_url', '')

            results.append(channel_data)
            print(f'[pipeline] ADDED: {handle} — now {len(results)} in results')

            processed_handles.add(handle_bare)
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

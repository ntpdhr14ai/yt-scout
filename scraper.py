import re
import sys
import random
import asyncio
from collections import deque
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_stealth = Stealth()

MAX_CHANNELS_PER_BATCH = 10
COOLDOWN_SECONDS = 600
_YOUTUBE_TITLE_SUFFIX = " - YouTube"

processed_handles: set = set()
discovery_queue: deque = deque()

# ---------------------------------------------------------------------------
# Stage 1 — name-based drop keywords
# ---------------------------------------------------------------------------
_NAME_DROP_KEYWORDS = [
    "global", "north america", "south america", "latin",
    "brazil", "brasil", "india", "vietnam", "viet",
    "indonesia", "malaysia", "philippines", "esports official",
    "singapore", "europe", "middle east", "africa", "japan", "korea",
]

# ---------------------------------------------------------------------------
# Stage 2 — country data
# ---------------------------------------------------------------------------
_NON_THAI_COUNTRIES = {
    "brazil", "india", "vietnam", "indonesia", "philippines",
    "united states", "united kingdom", "malaysia", "singapore",
    "japan", "south korea", "north korea", "korea", "china",
    "australia", "germany", "france", "canada", "russia",
    "mexico", "spain", "italy", "netherlands", "turkey", "egypt",
    "nigeria", "pakistan", "bangladesh", "myanmar", "cambodia",
    "laos", "taiwan", "hong kong", "new zealand", "argentina",
    "colombia", "chile", "ukraine", "poland", "portugal", "greece",
    "us", "gb", "jp", "kr", "cn", "au", "de", "fr", "ca", "ru",
    "br", "in", "vn", "id", "ph", "my", "sg", "mx", "es", "it",
    "nl", "tr", "eg", "ng", "pk", "mm", "kh", "la", "tw", "hk",
    "nz", "ar", "co", "cl", "ua", "pl", "pt", "gr", "uk",
}

_NON_THAI_LONG = frozenset(n for n in _NON_THAI_COUNTRIES if len(n) > 2)

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

def parse_recency(text: str) -> int | None:
    t = text.lower()
    if 'hour' in t or 'minute' in t or 'just now' in t:
        return 0
    if 'day' in t:
        n = int(''.join(c for c in t if c.isdigit()) or 1)
        return n
    if 'week' in t:
        n = int(''.join(c for c in t if c.isdigit()) or 1)
        return n * 7
    if 'month' in t:
        n = int(''.join(c for c in t if c.isdigit()) or 1)
        return n * 30
    if 'year' in t:
        return 999
    if 'วัน' in t:
        n = int(''.join(c for c in t if c.isdigit()) or 1)
        return n
    if 'สัปดาห์' in t:
        n = int(''.join(c for c in t if c.isdigit()) or 1)
        return n * 7
    if 'เดือน' in t:
        n = int(''.join(c for c in t if c.isdigit()) or 1)
        return n * 30
    if 'ปี' in t:
        return 999
    return None

def _name_should_drop(name: str, handle: str) -> bool:
    combined = (name + " " + handle).lower()
    return any(kw in combined for kw in _NAME_DROP_KEYWORDS)

def _is_non_thai_country(country: str) -> bool:
    if not country:
        return False
    c = country.strip().lower()
    if c in ("th", "thailand"):
        return False
    if c in _NON_THAI_COUNTRIES:
        return True
    if len(c) == 2 and c.isalpha():
        return True
    return any(n in c for n in _NON_THAI_LONG)

def _is_timestamp_text(txt: str) -> bool:
    t = txt.lower()
    return any(w in t for w in _TIME_WORDS)

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

    print(f"[scraper] START: subs step 1 (yt-formatted-string#subscriber-count) -> {handle}")
    try:
        el = await page.query_selector("yt-formatted-string#subscriber-count")
        if el:
            txt = (await el.inner_text()).strip()
            subs = _parse_sub_count(txt)
            if subs is not None:
                _log_ok("subs", f"{txt} -> {subs}", handle)
                return subs
        _log_failed("subs step 1", handle, "element not found")
    except Exception as e:
        _log_failed("subs step 1", handle, e)

    print(f"[scraper] START: subs step 2 (#subscriber-count) -> {handle}")
    try:
        el = await page.query_selector("#subscriber-count")
        if el:
            txt = (await el.inner_text()).strip()
            subs = _parse_sub_count(txt)
            if subs is not None:
                _log_ok("subs", f"{txt} -> {subs}", handle)
                return subs
        _log_failed("subs step 2", handle, "element not found")
    except Exception as e:
        _log_failed("subs step 2", handle, e)

    print(f"[scraper] START: subs step 3 (span scan) -> {handle}")
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
            _log_failed("subs step 3", handle, "no matching span found")
    except Exception as e:
        _log_failed("subs step 3", handle, e)

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

async def _scrape_about(page, ch_url: str, handle: str) -> tuple[dict, bool, str]:
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

        body_lower = body_text.lower()
        confirmed_th = (
            "thailand" in body_lower
            or "ไทย" in body_text
            or country.strip().lower() in ("th", "thailand")
        )
        has_thai = _has_thai_script(body_text)
        is_non_thai = _is_non_thai_country(country) if country else False

        if confirmed_th:
            location_badge = "TH"
            keep = True
            _log_ok("location", "confirmed TH", handle)
        elif has_thai:
            location_badge = "TH inferred"
            keep = True
            _log_ok("location", "TH inferred (Thai script detected)", handle)
        elif is_non_thai:
            _log_dropped(handle, f"declared non-Thai country: {country}")
            keep = False
            location_badge = "?"
        else:
            # Rule E: empty page or no TH evidence is not a confirmed non-Thai — keep with ?
            location_badge = "?"
            keep = True
            reason = "about page empty" if not body_text.strip() else "no TH evidence but not confirmed non-Thai"
            _log_ok("location", f"? ({reason})", handle)

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

async def _scrape_tab_views(page, url, handle, tab_name):
    for attempt in range(1, 4):
        try:
            _log_start(f"{tab_name} views (attempt {attempt})", handle)
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            if tab_name == "shorts" and "/shorts" not in page.url:
                print(f"[scraper] EMPTY: shorts tab redirected → {handle}")
                return None
            if tab_name == "live" and "/streams" not in page.url:
                print(f"[scraper] EMPTY: live tab redirected → {handle}")
                return None
            await asyncio.sleep(random.uniform(3, 5))
            await page.evaluate('window.scrollBy(0, 1400)')
            await asyncio.sleep(2)
            selectors = ['span.inline-metadata-item', '#metadata-line span', 'ytd-video-meta-block span', '#metadata span']
            items = []
            for sel in selectors:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    text = (await el.inner_text()).strip()
                    if any(w in text.lower() for w in ['view', 'วิว', 'ครั้ง', 'watch']):
                        items.append(text)
                if items:
                    break
            if not items:
                _log_retry(attempt, f"{tab_name} views empty", handle)
                await asyncio.sleep(3)
                continue
            numbers = []
            for t in items[:8]:
                n = parse_view_count(t)
                if n:
                    numbers.append(n)
            avg = int(sum(numbers) / len(numbers)) if numbers else None
            _log_ok(f"{tab_name}_avg_views", avg, handle)
            return avg
        except Exception as e:
            _log_failed(f"{tab_name} views", handle, e)
            await asyncio.sleep(3)
    return None

# Rule D — 90-second hard timeout wrapper for tab scraping
async def _scrape_tab_with_timeout(page, url, handle, tab_name):
    try:
        return await asyncio.wait_for(
            _scrape_tab_views(page, url, handle, tab_name), timeout=90
        )
    except asyncio.TimeoutError:
        print(f'[scraper] TIMEOUT: {tab_name} → {handle} — exceeded 90s limit')
        return None
    except Exception as e:
        print(f'[scraper] FAILED: {tab_name} views → {handle} — reason: {e}')
        return None

# ---------------------------------------------------------------------------
# Channels tab discovery
# ---------------------------------------------------------------------------

async def discover_from_channels_tab(page, handle):
    url = f'https://www.youtube.com/@{handle}/channels'
    print(f'[scraper] START: channels tab discovery → {handle}')
    await page.goto(url, wait_until='domcontentloaded', timeout=20000)
    await page.wait_for_load_state('networkidle', timeout=20000)
    await asyncio.sleep(random.uniform(2, 4))
    links = await page.query_selector_all('a#channel-name, a.channel-link, ytd-channel-renderer a')
    new_handles = []
    for link in links:
        href = await link.get_attribute('href')
        if href and '/@' in href:
            h = href.split('/@')[-1].split('/')[0].strip()
            if h and h not in processed_handles and h not in discovery_queue:
                new_handles.append(h)
                discovery_queue.append(h)
    print(f'[scraper] DISCOVERED: {len(new_handles)} new channels from {handle} channels tab')
    return new_handles

# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

async def scrape_channels(
    keyword: str,
    region: str,
    min_subs: int,
    max_subs: int,
    batch_size: int,
    progress_callback=None,
    seen_handles: set | None = None,
    scroll_passes: int = 0,
    target_category: str = '',
) -> list[dict]:
    if seen_handles is None:
        seen_handles = set()
    batch_size = min(batch_size, MAX_CHANNELS_PER_BATCH)
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

        # Search
        search_url = (
            f"https://www.youtube.com/results?search_query={quote_plus(keyword)}"
            f"&sp=EgIQAg%253D%253D&gl={region}"
        )
        print(f"[scraper] Navigating to search: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(_rand_delay(3, 6))
        print(f"[scraper] Search page title: {await page.title()!r}")

        try:
            accept_btn = page.locator("button", has_text="Accept all")
            if await accept_btn.count() > 0:
                await accept_btn.first.click()
                await asyncio.sleep(2)
                print("[scraper] Cookie dialog dismissed")
        except Exception:
            pass

        for _ in range(scroll_passes):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(_rand_delay(2, 4))

        # Collect channel links from search
        channel_links: list[str] = []
        attempts = 0
        while len(channel_links) < batch_size and attempts < 8:
            anchors = await page.locator("a#main-link.channel-link").all()
            print(f"[scraper] Link collection attempt {attempts + 1}: {len(anchors)} anchors visible")
            for a in anchors:
                href = await a.get_attribute("href")
                if (
                    href
                    and "/@" in href
                    and href not in channel_links
                    and href not in seen_handles
                ):
                    channel_links.append(href)
            if len(channel_links) < batch_size:
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(_rand_delay(2, 4))
            attempts += 1

        channel_links = channel_links[:batch_size]
        for link in channel_links:
            seen_handles.add(link)

        # Add discovery queue items from prior batches (Change 5)
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

            if _name_should_drop("", handle):
                _log_dropped(handle, "handle matches Stage-1 keyword")
                await asyncio.sleep(_rand_delay(1, 3))
                continue

            await page.goto(ch_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))

            try:
                early_title = await page.title()
                if early_title and _YOUTUBE_TITLE_SUFFIX in early_title:
                    early_name = early_title.replace(_YOUTUBE_TITLE_SUFFIX, "").strip()
                    if _name_should_drop(early_name, handle):
                        _log_dropped(handle, f"name '{early_name}' matches Stage-1 keyword")
                        await asyncio.sleep(_rand_delay(1, 3))
                        continue
            except Exception:
                pass

            subs = await _scrape_subs(page, ch_url, handle)
            sub_pending = subs is None

            if not sub_pending:
                if subs < min_subs or subs > max_subs:
                    _log_dropped(handle, f"subs {subs} outside range [{min_subs}, {max_subs}]")
                    await asyncio.sleep(_rand_delay(2, 5))
                    continue

            channel_data, keep, location_badge = await _scrape_about(page, ch_url, handle)

            if not keep:
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            full_name = channel_data.get("name", "")
            if _name_should_drop(full_name, handle):
                _log_dropped(handle, f"name '{full_name}' matches Stage-1 keyword (post-about)")
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            if not sub_pending:
                channel_data["subs"] = subs
            else:
                about_subs = _parse_sub_count(channel_data.get("sub_text", ""))
                channel_data["subs"] = about_subs if about_subs is not None else "Pending Check"

            channel_data["location_badge"] = location_badge

            await asyncio.sleep(_rand_delay(5, 10))

            # Videos tab (Rule C/D — timeout-wrapped, never raises)
            videos_url = ch_url.rstrip("/") + "/videos"
            avg_video_views = await _scrape_tab_with_timeout(page, videos_url, handle, "videos")
            channel_data["avg_video_views"] = avg_video_views

            # 30/60/90d recency counters — from already-loaded videos page
            videos_30d = videos_60d = videos_90d = None
            try:
                date_texts = []
                for sel in ['span.inline-metadata-item', '#metadata-line span']:
                    candidates = await page.query_selector_all(sel)
                    for el in candidates:
                        try:
                            txt = (await el.inner_text()).strip()
                            if _is_timestamp_text(txt):
                                date_texts.append(txt)
                        except Exception:
                            pass
                    if date_texts:
                        break
                if date_texts:
                    v30 = v60 = v90 = 0
                    for txt in date_texts[:20]:
                        days = parse_recency(txt)
                        if days is not None:
                            if days <= 30:
                                v30 += 1
                            if days <= 60:
                                v60 += 1
                            if days <= 90:
                                v90 += 1
                    videos_30d, videos_60d, videos_90d = v30, v60, v90
                _log_ok("videos_30/60/90d", f"{videos_30d}/{videos_60d}/{videos_90d}", handle)
            except Exception as e:
                _log_failed("videos_30/60/90d", handle, e)
            channel_data["videos_30d"] = videos_30d
            channel_data["videos_60d"] = videos_60d
            channel_data["videos_90d"] = videos_90d

            # Category extraction — navigate to first video page
            category = None
            try:
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
                    await asyncio.sleep(random.uniform(3, 5))
                    await page.goto(videos_url, wait_until='domcontentloaded', timeout=20000)
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

            await asyncio.sleep(_rand_delay(5, 10))

            # Shorts tab (Rule C/D)
            shorts_url = ch_url.rstrip("/") + "/shorts"
            avg_short_views = await _scrape_tab_with_timeout(page, shorts_url, handle, "shorts")
            channel_data["avg_short_views"] = avg_short_views

            await asyncio.sleep(_rand_delay(5, 10))

            # Live tab (Rule C/D)
            live_url = ch_url.rstrip("/") + "/streams"
            avg_live_views = await _scrape_tab_with_timeout(page, live_url, handle, "live")
            channel_data["avg_live_views"] = avg_live_views

            channel_data["score"] = _score_channel(
                channel_data.get("subs"),
                channel_data.get("avg_video_views"),
                channel_data.get("avg_short_views"),
                None,
                location_badge,
            )
            channel_data["url"] = ch_url
            results.append(channel_data)

            processed_handles.add(handle_bare)
            _log_ok("channel_complete", channel_data.get("name", handle), handle)
            if progress_callback:
                progress_callback(
                    idx + 1,
                    len(channel_links),
                    f"Done {channel_data.get('name', handle)}",
                )

            # Channel discovery — only when category filter matches
            if target_category and channel_data.get("category"):
                if target_category.lower() in channel_data["category"].lower():
                    try:
                        await discover_from_channels_tab(page, handle_bare)
                    except Exception as e:
                        _log_failed("channels_tab_discovery", handle, e)

            if idx < len(channel_links) - 1:
                await asyncio.sleep(_rand_delay(10, 20))

        await browser.close()

    return results

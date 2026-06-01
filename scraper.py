import re
import random
import asyncio
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

_stealth = Stealth()

MAX_CHANNELS_PER_BATCH = 10
COOLDOWN_SECONDS = 600

# ---------------------------------------------------------------------------
# Stage 1 — name-based drop keywords (case-insensitive substring match)
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

_COUNTRY_RE = re.compile(
    r"^[A-Z]{2}$|Thailand|United States|United Kingdom|Japan|South Korea|"
    r"Korea|China|India|Brazil|Indonesia|Vietnam|Philippines|Malaysia|"
    r"Singapore|Australia|Germany|France|Canada|Russia|Mexico|Spain|"
    r"Italy|Netherlands|Turkey|Egypt|Nigeria|Pakistan|Myanmar|Cambodia|"
    r"Laos|Taiwan|New Zealand|Argentina|Colombia|Chile|Ukraine|Poland|"
    r"Portugal|Greece|Hong Kong",
    re.IGNORECASE,
)

# Thai digit → Arabic digit translation table
_THAI_DIGIT_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# Words that indicate an upload-timestamp span
_TIME_WORDS = {
    "ago", "hour", "hours", "day", "days", "week", "weeks",
    "month", "months", "year", "years",
    "วัน", "เดือน", "ปี", "ชั่วโมง", "สัปดาห์", "ที่แล้ว", "ผ่านมา",
}

# Fallback selectors tried if span.inline-metadata-item is empty
_VIEW_FALLBACK_SELECTORS = [
    "#metadata-line span",
    "ytd-video-meta-block span",
    "#metadata span",
]

# ---------------------------------------------------------------------------
# Logging helpers  (Fix 4)
# ---------------------------------------------------------------------------

def _log_start(fn: str, handle: str) -> None:
    print(f"[scraper] START: {fn} → {handle}")

def _log_ok(field: str, value, handle: str) -> None:
    print(f"[scraper] OK: {field} = {value!r} → {handle}")

def _log_retry(n: int, field: str, handle: str) -> None:
    print(f"[scraper] RETRY {n}: {field} → {handle}")

def _log_failed(field: str, handle: str, reason) -> None:
    print(f"[scraper] FAILED: {field} → {handle} — reason: {reason}")

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

def _parse_view_count(text: str) -> int | None:
    """Parse a view/watching count from span text; handles Thai digits."""
    if not text:
        return None
    text = text.translate(_THAI_DIGIT_MAP).replace(",", "").strip()
    # number + optional K/M/B + keyword
    m = re.search(r"([\d.]+)\s*([KMB]?)\s*(?:view|watching|ดู)", text, re.IGNORECASE)
    if m:
        value = float(m.group(1))
        suffix = m.group(2).upper()
        return int(value * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1))
    # keyword before number: "ดู 1.2M"
    if any(kw in text.lower() for kw in ("view", "watching", "ดู")):
        m = re.search(r"([\d.]+)\s*([KMB]?)", text, re.IGNORECASE)
        if m:
            value = float(m.group(1))
            suffix = m.group(2).upper()
            return int(value * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1))
    return None

def _days_ago(text: str) -> float | None:
    """Approximate days from a relative timestamp string (English or Thai)."""
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

def _determine_active_status(timestamps: list[str]) -> str:
    """Return 'Active' / 'Slow' / 'Inactive' from upload timestamp strings."""
    if not timestamps:
        return "Inactive"
    days_list = [d for t in timestamps if (d := _days_ago(t)) is not None]
    if not days_list:
        return "Inactive"
    most_recent = min(days_list)
    if most_recent <= 60:
        return "Active"
    if most_recent <= 180:
        return "Slow"
    return "Inactive"

def _calc_upload_frequency(timestamps: list[str]) -> float | None:
    if len(timestamps) < 2:
        return None
    months = [v for t in timestamps if (v := _months_ago(t)) is not None]
    if len(months) < 2:
        return None
    span = max(months) - min(months)
    if span <= 0:
        return None
    return round(len(months) / max(span, 0.5), 2)

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
    if re.match(r"^[a-z]{2}$", c):
        return True
    return any(len(n) > 2 and n in c for n in _NON_THAI_COUNTRIES)

def _is_view_text(txt: str, view_keywords: list[str]) -> bool:
    t = txt.lower()
    return any(kw in t for kw in view_keywords) or bool(re.search(r"[๐-๙]", txt))

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
        if subs_int >= 1_000_000:   score += 25
        elif subs_int >= 100_000:   score += 20
        elif subs_int >= 10_000:    score += 15
        elif subs_int >= 1_000:     score += 8
        else:                       score += 3

    if subs_int and avg_video_views:
        ratio = avg_video_views / max(subs_int, 1)
        if ratio >= 0.3:    score += 30
        elif ratio >= 0.1:  score += 22
        elif ratio >= 0.05: score += 15
        elif ratio >= 0.01: score += 8
        else:               score += 3

    if subs_int and avg_short_views:
        ratio = avg_short_views / max(subs_int, 1)
        if ratio >= 0.5:    score += 20
        elif ratio >= 0.2:  score += 14
        elif ratio >= 0.05: score += 8
        else:               score += 3

    if upload_freq:
        if upload_freq >= 8:    score += 15
        elif upload_freq >= 4:  score += 11
        elif upload_freq >= 2:  score += 7
        elif upload_freq >= 0.5: score += 3

    if location_badge in ("TH", "TH inferred"):
        score += 10

    return min(score, 100)

# ---------------------------------------------------------------------------
# Subscriber count extraction  (4-step)
# ---------------------------------------------------------------------------

async def _scrape_subs(page, ch_url: str, handle: str) -> int | None:
    _log_start("_scrape_subs", handle)

    # Step 1
    print(f"[scraper] START: subs step 1 (yt-formatted-string#subscriber-count) → {handle}")
    try:
        el = await page.query_selector("yt-formatted-string#subscriber-count")
        if el:
            txt = (await el.inner_text()).strip()
            subs = _parse_sub_count(txt)
            if subs is not None:
                _log_ok("subs", f"{txt} → {subs}", handle)
                return subs
        _log_failed("subs step 1", handle, "element not found")
    except Exception as e:
        _log_failed("subs step 1", handle, e)

    # Step 2
    print(f"[scraper] START: subs step 2 (#subscriber-count) → {handle}")
    try:
        el = await page.query_selector("#subscriber-count")
        if el:
            txt = (await el.inner_text()).strip()
            subs = _parse_sub_count(txt)
            if subs is not None:
                _log_ok("subs", f"{txt} → {subs}", handle)
                return subs
        _log_failed("subs step 2", handle, "element not found")
    except Exception as e:
        _log_failed("subs step 2", handle, e)

    # Step 3 — scan every span/yt-formatted-string for "subscriber"
    print(f"[scraper] START: subs step 3 (span scan) → {handle}")
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
                            _log_ok("subs", f"{txt} → {subs}", handle)
                            return subs
                except Exception:
                    pass
            if found:
                break
        if not found:
            _log_failed("subs step 3", handle, "no matching span found")
    except Exception as e:
        _log_failed("subs step 3", handle, e)

    # Step 4 — about page source regex
    about_url = ch_url.rstrip("/") + "/about"
    print(f"[scraper] START: subs step 4 (about regex) → {handle}")
    try:
        await page.goto(about_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3, 5))
        src = await page.content()
        m = re.search(r"([\d][\d,. ]*)\s*subscriber", src, re.IGNORECASE)
        if m:
            subs = _parse_sub_count(m.group(1))
            if subs is not None:
                _log_ok("subs", f"{m.group(0)} → {subs}", handle)
                return subs
        _log_failed("subs step 4", handle, "regex found nothing")
    except Exception as e:
        _log_failed("subs step 4", handle, e)

    _log_failed("subs", handle, "all 4 steps exhausted — Pending Check")
    return None

# ---------------------------------------------------------------------------
# About tab  (data collection + Stage 2 TH verification)
# ---------------------------------------------------------------------------

async def _scrape_about(page, ch_url: str, handle: str) -> tuple[dict, bool, str]:
    """
    Returns (data_dict, keep, location_badge).
    keep=False → caller must skip this channel.
    """
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

        # Full body text — primary source for TH detection and country field
        body_text = ""
        try:
            body_text = await page.inner_text("body")
            _log_ok("body_text", f"{len(body_text)} chars", handle)
        except Exception as e:
            _log_failed("body_text", handle, e)

        # --- Channel name ---
        name = ""
        try:
            title = await page.title()
            if title and " - YouTube" in title:
                name = title.replace(" - YouTube", "").strip()
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

        # --- Subscriber count (about page backup) ---
        try:
            sub_el = page.locator("yt-formatted-string#subscriber-count")
            if await sub_el.count() > 0:
                data["sub_text"] = (await sub_el.first.inner_text()).strip()
                _log_ok("sub_text", data["sub_text"], handle)
        except Exception as e:
            _log_failed("sub_text", handle, e)

        # --- Country field ---
        country = ""
        # First try: "Location:" or "Country:" label in body text
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

        # --- Stage 2: Thailand verification ---
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
        elif not body_text.strip():
            _log_dropped(handle, "about page empty — no Thai evidence")
            keep = False
            location_badge = "?"
        else:
            _log_dropped(handle, "no Thai evidence in about page")
            keep = False
            location_badge = "?"

        if not keep:
            data.setdefault("emails", [])
            data.setdefault("links", [])
            return data, False, "?"

        # --- Email reveal ---
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

        # --- External links ---
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
        _log_failed("_scrape_about", handle, e)
        data.setdefault("name", "")
        data.setdefault("links", [])
        data.setdefault("emails", [])
        return data, False, "?"

    return data, True, location_badge

# ---------------------------------------------------------------------------
# Shared tab-views scraper with hard waits + scroll + retry  (Fix 1)
# ---------------------------------------------------------------------------

async def _scrape_tab_views(
    page,
    tab_url: str,
    tab_name: str,
    handle: str,
    view_keywords: list[str],
) -> tuple[list[int], list[str]]:
    """
    Navigates to tab_url and extracts view counts + upload timestamps.
    Retries the full navigate→load→scroll→extract sequence up to 3 times.
    Returns (view_values[0..7], timestamps).
    """
    _log_start(f"_scrape_tab_views({tab_name})", handle)

    for attempt in range(1, 4):
        if attempt > 1:
            _log_retry(attempt - 1, f"views_{tab_name}", handle)
            await asyncio.sleep(3)

        try:
            # 1. Navigate
            await page.goto(tab_url, wait_until="domcontentloaded")

            # 2. networkidle
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as e:
                _log_failed(f"networkidle_{tab_name}", handle, e)

            # 3. Post-nav sleep
            await asyncio.sleep(random.uniform(3, 5))

            # 4. Scroll twice
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(2)
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(2)

            # 5. Wait for primary selector
            selector_found = True
            try:
                await page.wait_for_selector("span.inline-metadata-item", timeout=15000)
            except Exception as e:
                _log_failed(f"wait_for_selector_{tab_name} attempt {attempt}", handle, e)
                selector_found = False

            # 6. Extract from primary + fallback selectors
            view_values: list[int] = []
            timestamps: list[str] = []

            all_selectors = (
                ["span.inline-metadata-item"] if selector_found else []
            ) + _VIEW_FALLBACK_SELECTORS

            for sel in all_selectors:
                if view_values:
                    break
                try:
                    els = await page.locator(sel).all()
                    for el in els:
                        try:
                            txt = (await el.inner_text()).strip()
                            if _is_view_text(txt, view_keywords):
                                v = _parse_view_count(txt)
                                if v is not None:
                                    view_values.append(v)
                            elif _is_timestamp_text(txt):
                                timestamps.append(txt)
                            if len(view_values) >= 8:
                                break
                        except Exception:
                            pass
                except Exception as e:
                    _log_failed(f"selector({sel})_{tab_name}", handle, e)

            # 7. Return if we got something
            if view_values:
                sample = view_values[:8]
                avg = int(sum(sample) / len(sample))
                _log_ok(f"views_{tab_name}", f"sample={sample} avg={avg}", handle)
                return sample, timestamps

            _log_failed(
                f"views_{tab_name}",
                handle,
                f"attempt {attempt}: all selectors returned empty",
            )

        except Exception as e:
            _log_failed(f"views_{tab_name}", handle, f"attempt {attempt} outer: {e}")

    # All 3 attempts exhausted
    print(
        f"[scraper] FAILED views on {tab_name} for {handle}"
        f" — selector not found after 3 attempts"
    )
    return [], []

# ---------------------------------------------------------------------------
# Per-tab wrappers
# ---------------------------------------------------------------------------

async def _scrape_videos_tab(page, ch_url: str, handle: str) -> dict:
    _log_start("_scrape_videos_tab", handle)
    data: dict = {"avg_video_views": None, "upload_freq": None, "active": "Inactive"}

    view_values, timestamps = await _scrape_tab_views(
        page,
        ch_url.rstrip("/") + "/videos",
        "videos",
        handle,
        ["view", "ดู"],
    )

    if view_values:
        data["avg_video_views"] = int(sum(view_values) / len(view_values))
        _log_ok("avg_video_views", data["avg_video_views"], handle)

    data["active"] = _determine_active_status(timestamps)
    _log_ok("active", data["active"], handle)

    if len(timestamps) >= 2:
        data["upload_freq"] = _calc_upload_frequency(timestamps)
        _log_ok("upload_freq", data["upload_freq"], handle)

    return data


async def _scrape_shorts_tab(page, ch_url: str, handle: str) -> dict:
    _log_start("_scrape_shorts_tab", handle)
    data: dict = {"avg_short_views": None}

    view_values, _ = await _scrape_tab_views(
        page,
        ch_url.rstrip("/") + "/shorts",
        "shorts",
        handle,
        ["view", "ดู"],
    )

    if view_values:
        data["avg_short_views"] = int(sum(view_values) / len(view_values))
        _log_ok("avg_short_views", data["avg_short_views"], handle)

    return data


async def _scrape_live_tab(page, ch_url: str, handle: str) -> dict:
    _log_start("_scrape_live_tab", handle)
    data: dict = {"avg_live_views": None}

    view_values, _ = await _scrape_tab_views(
        page,
        ch_url.rstrip("/") + "/streams",
        "live",
        handle,
        ["view", "watching", "ดู", "กำลังดู"],
    )

    if view_values:
        data["avg_live_views"] = int(sum(view_values) / len(view_values))
        _log_ok("avg_live_views", data["avg_live_views"], handle)

    return data

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

        # ── Search ──────────────────────────────────────────────────────────
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

        # ── Collect channel links ────────────────────────────────────────────
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
        print(f"[scraper] Collected {len(channel_links)} channel links")

        # ── Per-channel loop ─────────────────────────────────────────────────
        for idx, ch_path in enumerate(channel_links):
            handle = ch_path.lstrip("/")   # e.g. "@Amazing_Thailand"
            ch_url = f"https://www.youtube.com{ch_path}"

            if progress_callback:
                progress_callback(idx, len(channel_links), f"Checking {handle}")

            # Stage 1a — handle keyword check (instant, no network)
            if _name_should_drop("", handle):
                _log_dropped(handle, "handle matches Stage-1 keyword")
                await asyncio.sleep(_rand_delay(1, 3))
                continue

            # Navigate to main channel page
            await page.goto(ch_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))

            # Stage 1b — name check from page title (before about page)
            try:
                early_title = await page.title()
                if early_title and " - YouTube" in early_title:
                    early_name = early_title.replace(" - YouTube", "").strip()
                    if _name_should_drop(early_name, handle):
                        _log_dropped(handle, f"name '{early_name}' matches Stage-1 keyword")
                        await asyncio.sleep(_rand_delay(1, 3))
                        continue
            except Exception:
                pass

            # Subscriber count (4-step)
            subs = await _scrape_subs(page, ch_url, handle)
            sub_pending = subs is None

            if not sub_pending and subs is not None:
                if subs < min_subs or subs > max_subs:
                    _log_dropped(handle, f"subs {subs} outside range [{min_subs}, {max_subs}]")
                    await asyncio.sleep(_rand_delay(2, 5))
                    continue

            # About tab — Stage 2 TH verification + data collection
            channel_data, keep, location_badge = await _scrape_about(page, ch_url, handle)

            if not keep:
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            # Stage 1c — full name check after about scrape
            full_name = channel_data.get("name", "")
            if _name_should_drop(full_name, handle):
                _log_dropped(handle, f"name '{full_name}' matches Stage-1 keyword (post-about)")
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            # Resolve subscriber count
            if not sub_pending:
                channel_data["subs"] = subs
            else:
                about_subs = _parse_sub_count(channel_data.get("sub_text", ""))
                channel_data["subs"] = about_subs if about_subs is not None else "Pending Check"

            channel_data["location_badge"] = location_badge

            await asyncio.sleep(_rand_delay(5, 10))

            # Videos tab
            video_data = await _scrape_videos_tab(page, ch_url, handle)
            channel_data.update(video_data)

            await asyncio.sleep(_rand_delay(5, 10))

            # Shorts tab
            shorts_data = await _scrape_shorts_tab(page, ch_url, handle)
            channel_data.update(shorts_data)

            await asyncio.sleep(_rand_delay(5, 10))

            # Live tab
            live_data = await _scrape_live_tab(page, ch_url, handle)
            channel_data.update(live_data)

            # Score
            channel_data["score"] = _score_channel(
                channel_data.get("subs"),
                channel_data.get("avg_video_views"),
                channel_data.get("avg_short_views"),
                channel_data.get("upload_freq"),
                location_badge,
            )
            channel_data["url"] = ch_url
            results.append(channel_data)

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

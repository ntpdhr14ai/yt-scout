import re
import random
import asyncio
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

_stealth = Stealth()

MAX_CHANNELS_PER_BATCH = 10
COOLDOWN_SECONDS = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Country data for Fix 3
# ---------------------------------------------------------------------------

_NON_THAI_COUNTRIES = {
    # Full names
    "brazil", "india", "vietnam", "indonesia", "philippines",
    "united states", "united kingdom", "malaysia", "singapore",
    "japan", "south korea", "north korea", "korea", "china",
    "australia", "germany", "france", "canada", "russia",
    "mexico", "spain", "italy", "netherlands", "turkey", "egypt",
    "nigeria", "pakistan", "bangladesh", "myanmar", "cambodia",
    "laos", "taiwan", "hong kong", "new zealand", "argentina",
    "colombia", "chile", "ukraine", "poland", "portugal", "greece",
    # ISO-2 codes (lower)
    "us", "gb", "jp", "kr", "cn", "au", "de", "fr", "ca", "ru",
    "br", "in", "vn", "id", "ph", "my", "sg", "mx", "es", "it",
    "nl", "tr", "eg", "ng", "pk", "mm", "kh", "la", "tw", "hk",
    "nz", "ar", "co", "cl", "ua", "pl", "pt", "gr", "uk",
}

_COUNTRY_RE = re.compile(
    r'^[A-Z]{2}$|Thailand|United States|United Kingdom|Japan|South Korea|'
    r'Korea|China|India|Brazil|Indonesia|Vietnam|Philippines|Malaysia|'
    r'Singapore|Australia|Germany|France|Canada|Russia|Mexico|Spain|'
    r'Italy|Netherlands|Turkey|Egypt|Nigeria|Pakistan|Myanmar|Cambodia|'
    r'Laos|Taiwan|New Zealand|Argentina|Colombia|Chile|Ukraine|Poland|'
    r'Portugal|Greece|Hong Kong',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rand_delay(lo=5, hi=15):
    return random.uniform(lo, hi)

def _nav_delay():
    """3–5 second post-navigation sleep, applied before any selector work."""
    return random.uniform(3, 5)

def _is_non_thai_country(country: str) -> bool:
    if not country:
        return False
    c = country.strip().lower()
    if c in ("th", "thailand"):
        return False
    if c in _NON_THAI_COUNTRIES:
        return True
    # Any unrecognised 2-letter ISO code that isn't TH
    if re.match(r'^[a-z]{2}$', c):
        return True
    # Partial match for multi-word country names
    for name in _NON_THAI_COUNTRIES:
        if len(name) > 2 and name in c:
            return True
    return False

def _has_thai_script(text: str) -> bool:
    return bool(re.search(r'[฀-๿]', text or ''))

def _parse_sub_count(text: str) -> int | None:
    """Convert '1.23M subscribers', '456K', '789' etc. to an int."""
    if not text:
        return None
    text = text.replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([KMB]?)", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2).upper()
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(value * multipliers.get(suffix, 1))

def _parse_view_count(text: str) -> int | None:
    if not text:
        return None
    text = text.replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([KMB]?)\s*view", text, re.IGNORECASE)
    if m:
        value = float(m.group(1))
        suffix = m.group(2).upper()
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        return int(value * multipliers.get(suffix, 1))
    return None

def _months_ago(relative_text: str) -> float | None:
    """Return approximate months from a 'X days/weeks/months/years ago' string."""
    if not relative_text:
        return None
    t = relative_text.lower()
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year)", t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    mapping = {
        "second": 1 / (30 * 24 * 3600),
        "minute": 1 / (30 * 24 * 60),
        "hour": 1 / (30 * 24),
        "day": 1 / 30,
        "week": 1 / 4.33,
        "month": 1.0,
        "year": 12.0,
    }
    return n * mapping[unit]

def _calc_upload_frequency(timestamps: list[str]) -> float | None:
    """Videos per month based on sampled upload timestamps."""
    if len(timestamps) < 2:
        return None
    months = [v for v in (_months_ago(t) for t in timestamps) if v is not None]
    if len(months) < 2:
        return None
    span = max(months) - min(months)
    if span <= 0:
        return None
    return round(len(months) / max(span, 0.5), 2)

def _score_channel(subs, avg_video_views: int | None,
                   avg_short_views: int | None, upload_freq: float | None,
                   location_badge: str | None) -> int:
    score = 0
    subs_int = subs if isinstance(subs, int) else None

    # Subscriber score (0-25)
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

    # Engagement – avg video views relative to subs (0-30)
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

    # Shorts engagement (0-20)
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

    # Upload frequency (0-15)
    if upload_freq:
        if upload_freq >= 8:
            score += 15
        elif upload_freq >= 4:
            score += 11
        elif upload_freq >= 2:
            score += 7
        elif upload_freq >= 0.5:
            score += 3

    # Thailand match bonus (0-10)
    if location_badge in ("TH", "TH inferred"):
        score += 10

    return min(score, 100)

# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

async def scrape_channels(keyword: str, region: str, min_subs: int, max_subs: int,
                           batch_size: int, progress_callback=None,
                           seen_handles: set | None = None,
                           scroll_passes: int = 0) -> list[dict]:
    """
    seen_handles: set of /@handle paths already processed; mutated in-place.
    scroll_passes: number of extra full-page scrolls before collecting links
                   (used by auto-batch to reach deeper search results).
    """
    if seen_handles is None:
        seen_handles = set()
    batch_size = min(batch_size, MAX_CHANNELS_PER_BATCH)
    results = []

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

        # --- Search YouTube for channels ---
        search_url = (
            f"https://www.youtube.com/results?search_query={quote_plus(keyword)}"
            f"&sp=EgIQAg%253D%253D"  # filter: channels
            f"&gl={region}"
        )
        print(f"[scraper] Navigating to search: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(_rand_delay(3, 6))
        print(f"[scraper] Search page title: {await page.title()!r}  url: {page.url!r}")

        # Accept cookie dialog if present
        try:
            accept_btn = page.locator("button", has_text="Accept all")
            if await accept_btn.count() > 0:
                await accept_btn.first.click()
                await asyncio.sleep(2)
                print("[scraper] Cookie dialog dismissed")
        except Exception:
            pass

        # Extra scrolling for subsequent batches to reach deeper results
        for _ in range(scroll_passes):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(_rand_delay(2, 4))

        # Collect channel links, skipping already-seen handles
        channel_links = []
        attempts = 0
        while len(channel_links) < batch_size and attempts < 8:
            anchors = await page.locator("a#main-link.channel-link").all()
            print(f"[scraper] Attempt {attempts+1}: found {len(anchors)} channel anchors on page")
            for a in anchors:
                href = await a.get_attribute("href")
                if href and "/@" in href and href not in channel_links and href not in seen_handles:
                    channel_links.append(href)
            print(f"[scraper] Attempt {attempts+1}: {len(channel_links)} new links collected so far")
            if len(channel_links) < batch_size:
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(_rand_delay(2, 4))
            attempts += 1

        channel_links = channel_links[:batch_size]
        # Mark all collected links as seen now so next batch never revisits them
        for link in channel_links:
            seen_handles.add(link)

        for idx, ch_path in enumerate(channel_links):
            if progress_callback:
                progress_callback(idx, len(channel_links), f"Checking {ch_path}")

            ch_url = f"https://www.youtube.com{ch_path}"

            # Navigate to channel main page
            await page.goto(ch_url, wait_until="domcontentloaded")
            await asyncio.sleep(_nav_delay())

            subs = None
            sub_text = ""

            # ---------------------------------------------------------------
            # Fix 1 — 4-step subscriber count extraction
            # ---------------------------------------------------------------

            # Step 1: yt-formatted-string#subscriber-count
            print(f"[scraper] Attempting: subscriber count step 1 (yt-formatted-string#subscriber-count) on {ch_url}")
            try:
                el = await page.query_selector('yt-formatted-string#subscriber-count')
                if el:
                    sub_text = (await el.inner_text()).strip()
                    subs = _parse_sub_count(sub_text)
                    print(f"[scraper] Result: step 1 = {sub_text!r} → {subs}")
                else:
                    print(f"[scraper] FAILED: step 1 — element not found")
            except Exception as e:
                print(f"[scraper] FAILED: step 1 — {e}")

            # Step 2: #subscriber-count (generic)
            if subs is None:
                print(f"[scraper] Attempting: subscriber count step 2 (#subscriber-count) on {ch_url}")
                try:
                    el = await page.query_selector('#subscriber-count')
                    if el:
                        sub_text = (await el.inner_text()).strip()
                        subs = _parse_sub_count(sub_text)
                        print(f"[scraper] Result: step 2 = {sub_text!r} → {subs}")
                    else:
                        print(f"[scraper] FAILED: step 2 — element not found")
                except Exception as e:
                    print(f"[scraper] FAILED: step 2 — {e}")

            # Step 3: scan all span/yt-formatted-string for text containing 'subscriber'
            if subs is None:
                print(f"[scraper] Attempting: subscriber count step 3 (scan for 'subscriber' text) on {ch_url}")
                try:
                    found_step3 = False
                    for sel in ['yt-formatted-string', 'span']:
                        els = await page.query_selector_all(sel)
                        for el in els:
                            try:
                                txt = (await el.inner_text()).strip()
                                if 'subscriber' in txt.lower() and len(txt) < 60:
                                    candidate = _parse_sub_count(txt)
                                    if candidate is not None:
                                        subs = candidate
                                        sub_text = txt
                                        found_step3 = True
                                        print(f"[scraper] Result: step 3 ({sel}) = {txt!r} → {subs}")
                                        break
                            except Exception:
                                pass
                        if found_step3:
                            break
                    if not found_step3:
                        print(f"[scraper] FAILED: step 3 — no matching element found")
                except Exception as e:
                    print(f"[scraper] FAILED: step 3 — {e}")

            # Step 4: navigate to /about and regex-scan page source
            if subs is None:
                about_url_check = ch_url.rstrip("/") + "/about"
                print(f"[scraper] Attempting: subscriber count step 4 (about page regex) on {about_url_check}")
                try:
                    await page.goto(about_url_check, wait_until="domcontentloaded")
                    await asyncio.sleep(_nav_delay())
                    src = await page.content()
                    m = re.search(r'([\d][\d,. ]*)\s*subscriber', src, re.IGNORECASE)
                    if m:
                        sub_text = m.group(0)
                        subs = _parse_sub_count(m.group(1))
                        print(f"[scraper] Result: step 4 = {sub_text!r} → {subs}")
                    else:
                        print(f"[scraper] FAILED: step 4 — regex found nothing")
                except Exception as e:
                    print(f"[scraper] FAILED: step 4 — {e}")

            sub_pending = subs is None
            if sub_pending:
                print(f"[scraper] All subscriber count attempts failed for {ch_path} — marking as Pending Check")

            # Skip only if subs are confirmed and outside target range
            if not sub_pending and subs is not None:
                if subs < min_subs or subs > max_subs:
                    if progress_callback:
                        progress_callback(idx, len(channel_links),
                                          f"Skipping {ch_path} (subs out of range)")
                    await asyncio.sleep(_rand_delay(2, 5))
                    continue

            # --- About tab ---
            channel_data = await _scrape_about(page, ch_url)
            about_text = channel_data.pop("about_text", "")

            # Resolve subscriber count: prefer freshly scraped, fall back to about page, then Pending
            if not sub_pending:
                channel_data["subs"] = subs
            else:
                about_subs = _parse_sub_count(channel_data.get("sub_text", ""))
                channel_data["subs"] = about_subs if about_subs is not None else "Pending Check"

            # ---------------------------------------------------------------
            # Fix 3 — Thailand location filtering
            # ---------------------------------------------------------------
            country = channel_data.get("country", "")

            if _is_non_thai_country(country):
                print(f"[scraper] DROPPED: {ch_path} — declared country: {country}")
                await asyncio.sleep(_rand_delay(2, 5))
                continue

            c_norm = (country or "").strip().lower()
            if c_norm in ("th", "thailand"):
                location_badge = "TH"
            elif _has_thai_script(about_text):
                location_badge = "TH inferred"
            else:
                location_badge = "?"

            channel_data["location_badge"] = location_badge
            print(f"[scraper] Result: location badge = {location_badge!r} for {ch_path}")

            await asyncio.sleep(_rand_delay(5, 10))

            # --- Videos tab ---
            video_data = await _scrape_videos_tab(page, ch_url)
            channel_data.update(video_data)

            await asyncio.sleep(_rand_delay(5, 10))

            # --- Shorts tab ---
            shorts_data = await _scrape_shorts_tab(page, ch_url)
            channel_data.update(shorts_data)

            await asyncio.sleep(_rand_delay(5, 10))

            # --- Live tab ---
            live_data = await _scrape_live_tab(page, ch_url)
            channel_data.update(live_data)

            # Scoring
            channel_data["score"] = _score_channel(
                channel_data.get("subs"),
                channel_data.get("avg_video_views"),
                channel_data.get("avg_short_views"),
                channel_data.get("upload_freq"),
                location_badge,
            )
            channel_data["url"] = ch_url
            results.append(channel_data)

            if progress_callback:
                progress_callback(idx + 1, len(channel_links),
                                  f"Done {channel_data.get('name', ch_path)}")

            # Inter-channel gap
            if idx < len(channel_links) - 1:
                await asyncio.sleep(_rand_delay(10, 20))

        await browser.close()

    return results


async def _scrape_about(page, ch_url: str) -> dict:
    data: dict = {}
    about_url = ch_url.rstrip("/") + "/about"
    try:
        await page.goto(about_url, wait_until="domcontentloaded")
        await asyncio.sleep(_nav_delay())

        print(f"[scraper] Attempting: networkidle wait on {about_url}")
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
            print(f"[scraper] Result: networkidle reached on {about_url}")
        except Exception as e:
            print(f"[scraper] FAILED: networkidle wait — {e}")

        # Capture full page text for Thai script detection (Fix 3)
        print(f"[scraper] Attempting: full body text capture on {about_url}")
        try:
            data["about_text"] = await page.inner_text("body")
            print(f"[scraper] Result: about_text captured ({len(data['about_text'])} chars)")
        except Exception as e:
            data["about_text"] = ""
            print(f"[scraper] FAILED: full body text capture — {e}")

        # Channel name — try page title, og:title, then DOM selectors
        print(f"[scraper] Attempting: channel name on {about_url}")
        name = ""
        try:
            title = await page.title()
            if title and " - YouTube" in title:
                name = title.replace(" - YouTube", "").strip()
                print(f"[scraper] Result: channel name (title) = {name!r}")
        except Exception as e:
            print(f"[scraper] FAILED: channel name via title — {e}")

        if not name:
            try:
                og = await page.get_attribute('meta[property="og:title"]', "content")
                if og:
                    name = og.strip()
                    print(f"[scraper] Result: channel name (og:title) = {name!r}")
            except Exception as e:
                print(f"[scraper] FAILED: channel name via og:title — {e}")

        if not name:
            for sel in ["#channel-name yt-formatted-string",
                        "ytd-channel-name yt-formatted-string",
                        "yt-formatted-string.ytd-channel-name"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        candidate = (await el.inner_text()).strip()
                        if candidate:
                            name = candidate
                            print(f"[scraper] Result: channel name ({sel}) = {name!r}")
                            break
                except Exception:
                    pass
            if not name:
                print(f"[scraper] FAILED: channel name — all strategies exhausted")

        data["name"] = name

        # Subscriber count (may appear on about page too)
        print(f"[scraper] Attempting: subscriber count on {about_url}")
        try:
            sub_el = page.locator("yt-formatted-string#subscriber-count")
            if await sub_el.count() > 0:
                data["sub_text"] = await sub_el.first.inner_text()
                print(f"[scraper] Result: subscriber count = {data['sub_text']!r}")
            else:
                print(f"[scraper] Result: subscriber count — element not found")
        except Exception as e:
            print(f"[scraper] FAILED: subscriber count — {e}")

        # Country (Fix 3 — broader pattern)
        print(f"[scraper] Attempting: country on {about_url}")
        try:
            loc_els = await page.locator("yt-formatted-string.ytd-channel-about-metadata-renderer").all()
            found_country = False
            for el in loc_els:
                txt = (await el.inner_text()).strip()
                if txt and _COUNTRY_RE.search(txt):
                    data["country"] = txt
                    found_country = True
                    print(f"[scraper] Result: country = {txt!r}")
                    break
            if not found_country:
                print(f"[scraper] Result: country — not found")
        except Exception as e:
            print(f"[scraper] FAILED: country — {e}")

        # Email reveal button
        print(f"[scraper] Attempting: email reveal button on {about_url}")
        try:
            await page.click("tp-yt-paper-button#email-reveal-button", timeout=4000)
            await asyncio.sleep(2)
            page_text = await page.content()
            emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page_text)
            data["emails"] = list(set(e for e in emails if "youtube" not in e and "google" not in e))
            print(f"[scraper] Result: emails after reveal = {data['emails']}")
        except Exception as e:
            print(f"[scraper] FAILED: email reveal button — {e}")
            # Fallback: scrape emails directly from page source
            try:
                page_text = await page.content()
                emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page_text)
                data["emails"] = list(set(e for e in emails if "youtube" not in e and "google" not in e))
            except Exception:
                data.setdefault("emails", [])

        # Links inside #link-list-container
        print(f"[scraper] Attempting: links in #link-list-container on {about_url}")
        try:
            link_els = await page.locator("#link-list-container a").all()
            links = []
            for el in link_els:
                href = await el.get_attribute("href") or ""
                if href.startswith("http") and "youtube.com" not in href:
                    links.append(href)
            data["links"] = links[:5]
            print(f"[scraper] Result: links = {data['links']}")
        except Exception as e:
            data["links"] = []
            print(f"[scraper] FAILED: links in #link-list-container — {e}")

    except Exception:
        data.setdefault("name", "")
        data.setdefault("links", [])
        data.setdefault("emails", [])
        data.setdefault("about_text", "")

    return data


async def _scrape_videos_tab(page, ch_url: str) -> dict:
    data = {"avg_video_views": None, "upload_freq": None, "active": False}
    videos_url = ch_url.rstrip("/") + "/videos"
    try:
        await page.goto(videos_url, wait_until="domcontentloaded")
        await asyncio.sleep(_nav_delay())

        print(f"[scraper] Attempting: grid renderer on {videos_url}")
        try:
            await page.wait_for_selector("ytd-rich-grid-renderer, ytd-grid-renderer", timeout=20000)
            print(f"[scraper] Result: grid renderer found on {videos_url}")
        except Exception as e:
            print(f"[scraper] FAILED: grid renderer — {e}")
            return data

        view_texts = []
        timestamps = []

        print(f"[scraper] Attempting: video view counts (inline-metadata-item) on {videos_url}")
        try:
            span_els = await page.locator("span.inline-metadata-item").all()
            for span in span_els[:30]:
                txt = (await span.inner_text()).strip()
                if "view" in txt.lower():
                    v = _parse_view_count(txt)
                    if v is not None:
                        view_texts.append(v)
                elif "ago" in txt.lower():
                    timestamps.append(txt)
                if len(view_texts) >= 10:
                    break
            print(f"[scraper] Result: video views sample = {view_texts[:10]}")
        except Exception as e:
            print(f"[scraper] FAILED: video view counts (inline-metadata-item) — {e}")

        # Fallback to metadata-line approach
        if not view_texts:
            print(f"[scraper] Attempting: video view counts fallback (metadata-line) on {videos_url}")
            try:
                video_items = await page.locator("ytd-rich-item-renderer").all()
                for item in video_items[:10]:
                    meta = await item.locator("#metadata-line span").all_inner_texts()
                    for txt in meta:
                        if "view" in txt.lower():
                            v = _parse_view_count(txt)
                            if v is not None:
                                view_texts.append(v)
                        elif "ago" in txt.lower():
                            timestamps.append(txt)
                print(f"[scraper] Result: video views fallback = {view_texts[:10]}")
            except Exception as e:
                print(f"[scraper] FAILED: video view counts fallback — {e}")

        if view_texts:
            data["avg_video_views"] = int(sum(view_texts) / len(view_texts))
            data["active"] = True
        if timestamps:
            data["upload_freq"] = _calc_upload_frequency(timestamps)

    except Exception:
        pass
    return data


async def _scrape_shorts_tab(page, ch_url: str) -> dict:
    data = {"avg_short_views": None}
    shorts_url = ch_url.rstrip("/") + "/shorts"
    try:
        await page.goto(shorts_url, wait_until="domcontentloaded")
        await asyncio.sleep(_nav_delay())

        print(f"[scraper] Attempting: grid renderer on {shorts_url}")
        try:
            await page.wait_for_selector("ytd-rich-grid-renderer, ytd-grid-renderer", timeout=20000)
            print(f"[scraper] Result: grid renderer found on {shorts_url}")
        except Exception as e:
            print(f"[scraper] FAILED: grid renderer — {e}")
            return data

        view_texts = []

        print(f"[scraper] Attempting: shorts view counts (inline-metadata-item) on {shorts_url}")
        try:
            span_els = await page.locator("span.inline-metadata-item").all()
            for span in span_els[:30]:
                txt = (await span.inner_text()).strip()
                if "view" in txt.lower():
                    v = _parse_view_count(txt)
                    if v is not None:
                        view_texts.append(v)
                if len(view_texts) >= 10:
                    break
            print(f"[scraper] Result: shorts views sample = {view_texts[:10]}")
        except Exception as e:
            print(f"[scraper] FAILED: shorts view counts (inline-metadata-item) — {e}")

        # Fallback: metadata-line + yt-formatted-string spans
        if not view_texts:
            print(f"[scraper] Attempting: shorts view counts fallback on {shorts_url}")
            try:
                items = await page.locator("ytd-rich-item-renderer").all()
                for item in items[:10]:
                    meta = await item.locator(
                        "#metadata-line span, .ytd-thumbnail-overlay-time-status-renderer"
                    ).all_inner_texts()
                    for txt in meta:
                        if "view" in txt.lower():
                            v = _parse_view_count(txt)
                            if v is not None:
                                view_texts.append(v)
                spans = await page.locator("span.yt-formatted-string").all_inner_texts()
                for txt in spans:
                    if "view" in txt.lower():
                        v = _parse_view_count(txt)
                        if v is not None:
                            view_texts.append(v)
                print(f"[scraper] Result: shorts views fallback = {view_texts[:10]}")
            except Exception as e:
                print(f"[scraper] FAILED: shorts view counts fallback — {e}")

        if view_texts:
            data["avg_short_views"] = int(sum(view_texts) / len(view_texts))

    except Exception:
        pass
    return data


async def _scrape_live_tab(page, ch_url: str) -> dict:
    data = {"avg_live_views": None}
    live_url = ch_url.rstrip("/") + "/streams"
    try:
        await page.goto(live_url, wait_until="domcontentloaded")
        await asyncio.sleep(_nav_delay())

        print(f"[scraper] Attempting: grid renderer on {live_url}")
        try:
            await page.wait_for_selector("ytd-rich-grid-renderer, ytd-grid-renderer", timeout=20000)
            print(f"[scraper] Result: grid renderer found on {live_url}")
        except Exception as e:
            print(f"[scraper] FAILED: grid renderer — {e}")
            return data

        view_texts = []

        print(f"[scraper] Attempting: live view counts (inline-metadata-item) on {live_url}")
        try:
            span_els = await page.locator("span.inline-metadata-item").all()
            for span in span_els[:30]:
                txt = (await span.inner_text()).strip()
                if "view" in txt.lower() or "watching" in txt.lower():
                    v = _parse_view_count(txt)
                    if v is not None:
                        view_texts.append(v)
                if len(view_texts) >= 10:
                    break
            print(f"[scraper] Result: live views sample = {view_texts[:10]}")
        except Exception as e:
            print(f"[scraper] FAILED: live view counts (inline-metadata-item) — {e}")

        # Fallback: metadata-line approach
        if not view_texts:
            print(f"[scraper] Attempting: live view counts fallback (metadata-line) on {live_url}")
            try:
                items = await page.locator("ytd-rich-item-renderer").all()
                for item in items[:10]:
                    meta = await item.locator("#metadata-line span").all_inner_texts()
                    for txt in meta:
                        if "view" in txt.lower() or "watching" in txt.lower():
                            v = _parse_view_count(txt)
                            if v is not None:
                                view_texts.append(v)
                print(f"[scraper] Result: live views fallback = {view_texts[:10]}")
            except Exception as e:
                print(f"[scraper] FAILED: live view counts fallback — {e}")

        if view_texts:
            data["avg_live_views"] = int(sum(view_texts) / len(view_texts))

    except Exception:
        pass
    return data

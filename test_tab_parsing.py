"""Fixtures copied from diag_output.txt (2026-11-03, @bywiyo videos tab)."""
import pytest
from scraper import age_days_from_line, views_from_line, parse_renderer_lines, parse_recency, _score_channel

REAL_ITEMS = [
    (['9:34', 'The World Is Stunned: Foreigners Flock to the "Naka Cave," ...', '203', '8h ago'], (0, 203)),
    (['10:33', 'Comments Explode! Indonesia Stunned by "Thai Rice Harvester" ...', '4.7K', '1d ago'], (1, 4700)),
    (['9:47', 'When foreigners hit Thailand to deep-dive ...', '3.4K', '2d ago'], (2, 3400)),
    (['10:23', 'When a British mother brings her baby to Thailand ...', '26K', '3d ago'], (3, 26000)),
    (['8:38', 'Vietnamese Amazed by Thailand’s "4-Level Expressway," ...', '7K', '4d ago'], (4, 7000)),
]


@pytest.mark.parametrize("lines,expected", REAL_ITEMS)
def test_real_youtube_grid_items(lines, expected):
    assert parse_renderer_lines(lines) == expected


@pytest.mark.parametrize("line,days", [
    ("8h ago", 0), ("1d ago", 1), ("2w ago", 14), ("3mo ago", 90), ("1y ago", 999),
    ("2 weeks ago", 14), ("Streamed 3 days ago", 3), ("5 minutes ago", 0),
    ("2 วันที่แล้ว", 2), ("๓ สัปดาห์ที่แล้ว", 21),
])
def test_age_lines(line, days):
    assert age_days_from_line(line) == days


@pytest.mark.parametrize("title", [
    "เที่ยว 3 วัน 2 คืน", "Sunday vlog", "10 years in Thailand", "1 day in Bangkok",
])
def test_titles_with_time_words_are_not_dates(title):
    """The old parser read these as upload dates."""
    assert age_days_from_line(title) is None
    assert parse_renderer_lines(["12:00", title, "5K"]) == (None, None)


@pytest.mark.parametrize("line,views", [
    ("203", 203), ("4.7K", 4700), ("26K", 26000), ("1.2M", 1_200_000),
    ("14K views", 14000), ("1,234 watching", 1234), ("9:34", None), ("No views", None),
])
def test_view_lines(line, views):
    assert views_from_line(line) == views


def test_parse_recency_accepts_compact_form():
    assert parse_recency("1d ago") == 1
    assert parse_recency("3 days ago") == 3        # old long form still works


def test_upload_frequency_now_scores():
    # 10 videos / 30 days -> 10 per month -> +15 (was always 0 before)
    assert _score_channel(20_000, None, None, 10.0, None) == 15 + 15
    assert _score_channel(20_000, None, None, None, None) == 15


# --- fallback grid (2026-11-03 run: one channel had identical stats on all 3 tabs) ---
from scraper import video_id, is_fallback_grid

V = "https://www.youtube.com/watch?v=_x9bqjT2YVQ"


def test_video_id():
    assert video_id("/watch?v=_x9bqjT2YVQ") == "_x9bqjT2YVQ"
    assert video_id("/watch?v=_x9bqjT2YVQ&t=5s") == "_x9bqjT2YVQ"
    assert video_id("/shorts/abcdefghijk") == "abcdefghijk"
    assert video_id(None) is None


def test_live_tab_showing_videos_grid_is_detected():
    assert is_fallback_grid("live", "/watch?v=_x9bqjT2YVQ", V) is True


def test_shorts_tab_with_watch_links_is_detected():
    assert is_fallback_grid("shorts", "/watch?v=bFqHr7JkLik", V) is True


def test_real_tabs_are_kept():
    assert is_fallback_grid("shorts", "/shorts/abcdefghijk", V) is False
    assert is_fallback_grid("live", "/watch?v=LIVEstream01", V) is False
    assert is_fallback_grid("live", None, V) is False


# --- adaptive search loading (phase 3, fixed after diag_scroll 2026-11-03) ---------
from scraper import scroll_decision, MAX_SEARCH_SCROLL_PASSES, SEARCH_STALL_PASSES


def test_stops_after_first_load_when_enough_new():
    stop, why = scroll_decision(1, [28], 12, 9)
    assert stop and 'new channels' in why


def test_keeps_loading_when_everything_is_already_known():
    assert scroll_decision(1, [28], 0, 9)[0] is False


def test_growth_means_not_exhausted():
    """Counts from the real diag run (B1..B4): steady growth must not stop."""
    assert scroll_decision(4, [56, 76, 88, 98], 2, 9)[0] is False


def test_stall_at_loader_means_exhausted():
    counts = [56, 76, 98] + [98] * SEARCH_STALL_PASSES
    stop, why = scroll_decision(len(counts), counts, 2, 9)
    assert stop and 'exhausted' in why


def test_no_loader_means_end_of_results():
    stop, why = scroll_decision(3, [40, 60, 70], 0, 9, has_loader=False)
    assert stop and 'end of results' in why


def test_hard_ceiling():
    counts = list(range(10, 10 * (MAX_SEARCH_SCROLL_PASSES + 1), 10))
    stop, why = scroll_decision(MAX_SEARCH_SCROLL_PASSES, counts, 0, 9)
    assert stop and 'max' in why

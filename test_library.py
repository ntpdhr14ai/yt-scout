"""Channel library: storage, freshness, and re-gating stored facts."""
from datetime import datetime, timedelta, timezone

import pytest

import library
import scraper

NOW = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)

COMPLETE = {
    "name": "Go Went Go", "handle": "GoWentGo", "subs": 1_980_000,
    "location_badge": "TH", "th_signal": True, "category": "Travel & Events",
    "upload_frequency": 10, "avg_views_videos": 50_000, "avg_views_shorts": None,
    "avg_views_live": None, "metrics_days": 30, "emails": [], "links": [],
}


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_SCOUT_DB", str(tmp_path / "test.db"))


# --- storage -----------------------------------------------------------------

def test_save_then_lookup_roundtrip():
    library.save("GoWentGo", COMPLETE, library.STAGE_COMPLETE, now=NOW)
    facts, stage, checked = library.lookup("GoWentGo", now=NOW)
    assert stage == "complete"
    assert facts["subs"] == 1_980_000 and facts["category"] == "Travel & Events"


def test_large_fields_are_not_stored():
    library.save("x", {**COMPLETE, "about_text": "A" * 50_000}, library.STAGE_COMPLETE, now=NOW)
    facts, _, _ = library.lookup("x", now=NOW)
    assert "about_text" not in facts


def test_fresh_window_is_14_days():
    library.save("old", COMPLETE, library.STAGE_COMPLETE, now=NOW - timedelta(days=15))
    library.save("new", COMPLETE, library.STAGE_COMPLETE, now=NOW - timedelta(days=13))
    assert library.lookup("old", now=NOW) is None
    assert library.lookup("new", now=NOW) is not None


def test_resave_overwrites():
    library.save("x", {"subs": 1}, library.STAGE_SUBS, now=NOW)
    library.save("x", COMPLETE, library.STAGE_COMPLETE, now=NOW)
    assert library.lookup("x", now=NOW)[1] == "complete"


def test_sightings_are_per_keyword_and_deduplicated():
    library.record_sighting("x", "เที่ยวไทย", "t1", "u1", now=NOW)
    library.record_sighting("x", "เที่ยวไทย", "t2", "u2", now=NOW)
    library.record_sighting("x", "thai vlog", "t3", "u3", now=NOW)
    assert library.keywords_for("x") == ["เที่ยวไทย", "thai vlog"]


def test_unwritable_location_raises_library_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_SCOUT_DB", str(tmp_path / "no_such_dir" / "x.db"))
    with pytest.raises(library.LibraryUnavailable):
        library.save("x", COMPLETE, library.STAGE_COMPLETE)


def test_scraper_keeps_running_when_library_breaks(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YT_SCOUT_DB", str(tmp_path / "no_such_dir" / "x.db"))
    scraper._library_ok = True
    assert scraper._lib(library.lookup, "x") is None          # miss, not crash
    assert scraper._library_ok is False                       # disabled for the run
    assert scraper._lib(library.lookup, "x") is None          # no second attempt
    assert capsys.readouterr().out.count("WARNING") == 1


# --- re-gating stored facts with today's filters -------------------------------

def ev(facts, stage, region="TH", tier="any", cat="", days=30):
    return scraper.evaluate_cached(facts, stage, region, tier, cat, days)


def test_complete_match_passes_with_recomputed_score():
    verdict, row = ev(COMPLETE, library.STAGE_COMPLETE)
    assert verdict == "pass"
    # subs 25 + ratio 50k/1.98M=0.025 -> 8 + upload 10/mo -> 15 + TH 10
    assert row["score"] == 58


def test_subs_rejection_depends_on_todays_tier():
    """Rejected last time under 10k_100k; today the user asks for over_1m."""
    facts = {"subs": 1_980_000}
    assert ev(facts, library.STAGE_SUBS, tier="10k_100k")[0] == "reject"
    assert ev(facts, library.STAGE_SUBS, tier="over_1m") == ("scrape", "only subscriber count on file")


def test_non_thai_channel_stays_rejected_without_scraping():
    facts = {"subs": 5_000, "th_signal": False}
    assert ev(facts, library.STAGE_LOCATION)[0] == "reject"


def test_non_thai_channel_rescraped_for_other_region():
    facts = {"subs": 5_000, "th_signal": False}
    assert ev(facts, library.STAGE_LOCATION, region="US")[0] == "scrape"


def test_metrics_for_other_window_need_rescrape():
    assert ev(COMPLETE, library.STAGE_COMPLETE, days=90)[0] == "scrape"


def test_category_filter_applied_to_stored_category():
    assert ev(COMPLETE, library.STAGE_COMPLETE, cat="Gaming")[0] == "reject"
    assert ev(COMPLETE, library.STAGE_COMPLETE, cat="travel & events")[0] == "pass"


def test_unknown_stored_category_rejected_when_target_set():
    facts = {**COMPLETE, "category": ""}
    assert ev(facts, library.STAGE_COMPLETE, cat="Travel & Events")[0] == "reject"

import pytest
from scraper import gate_subscribers, gate_location, parse_recency


# ---------------------------------------------------------------------------
# gate_subscribers
# ---------------------------------------------------------------------------

class TestGateSubscribers:
    def test_any_tier_always_passes(self):
        passed, _ = gate_subscribers({'subs': 50_000}, 'any')
        assert passed is True

    def test_inside_5k_10k_tier(self):
        passed, _ = gate_subscribers({'subs': 8_780}, '5k_10k')
        assert passed is True

    def test_below_5k_10k_lower_bound(self):
        # 5k_10k lower bound is 5001; subs=5000 is outside
        passed, _ = gate_subscribers({'subs': 5_000}, '5k_10k')
        assert passed is False

    def test_above_10k_100k_upper_bound(self):
        # 10k_100k upper bound is 100000; subs=100001 is outside
        passed, _ = gate_subscribers({'subs': 100_001}, '10k_100k')
        assert passed is False

    def test_none_subs_unparseable(self):
        passed, reason = gate_subscribers({'subs': None}, 'under_5k')
        assert passed is False
        assert 'unparseable' in reason


# ---------------------------------------------------------------------------
# gate_location
# ---------------------------------------------------------------------------

class TestGateLocation:
    def test_declared_thailand_passes(self):
        passed, _ = gate_location({'declared_country': 'Thailand'}, 'TH')
        assert passed is True

    def test_thai_script_in_about_text_passes(self):
        passed, _ = gate_location({'about_text': 'สวัสดีครับ', 'declared_country': ''}, 'TH')
        assert passed is True

    def test_no_thailand_signal_fails(self):
        ch = {'declared_country': 'United States', 'about_text': 'hello', 'name': 'Bob'}
        passed, _ = gate_location(ch, 'TH')
        assert passed is False

    def test_non_th_region_always_passes(self):
        passed, _ = gate_location({'declared_country': 'United States'}, 'US')
        assert passed is True


# ---------------------------------------------------------------------------
# parse_recency  (unit tests replacing the removed gate_recency cases)
# ---------------------------------------------------------------------------

class TestParseRecency:
    def test_days(self):
        assert parse_recency('3 days ago') == 3

    def test_weeks(self):
        assert parse_recency('2 weeks ago') == 14

    def test_months(self):
        assert parse_recency('1 month ago') == 30

    def test_year_maps_to_sentinel(self):
        assert parse_recency('1 year ago') == 999

    def test_thai_days(self):
        assert parse_recency('2 วันที่แล้ว') == 2

    def test_empty_maps_to_sentinel(self):
        assert parse_recency('') == 999

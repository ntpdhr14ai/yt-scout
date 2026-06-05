import pytest
from scraper import gate_subscribers, gate_location, gate_recency


# ---------------------------------------------------------------------------
# gate_subscribers
# ---------------------------------------------------------------------------

class TestGateSubscribers:
    def test_passes_within_range(self):
        passed, reason = gate_subscribers(50_000, 1_000, 100_000)
        assert passed is True
        assert reason == ""

    def test_passes_at_lower_bound(self):
        passed, _ = gate_subscribers(1_000, 1_000, 100_000)
        assert passed is True

    def test_passes_at_upper_bound(self):
        passed, _ = gate_subscribers(100_000, 1_000, 100_000)
        assert passed is True

    def test_fails_below_range(self):
        passed, reason = gate_subscribers(500, 1_000, 100_000)
        assert passed is False
        assert "500" in reason
        assert "1000" in reason

    def test_fails_above_range(self):
        passed, reason = gate_subscribers(200_000, 1_000, 100_000)
        assert passed is False
        assert "200000" in reason

    def test_fails_none(self):
        passed, reason = gate_subscribers(None, 1_000, 100_000)
        assert passed is False
        assert "unparseable" in reason


# ---------------------------------------------------------------------------
# gate_location
# ---------------------------------------------------------------------------

class TestGateLocation:
    def test_passes_when_keep_true(self):
        passed, reason = gate_location(True)
        assert passed is True
        assert reason == ""

    def test_fails_when_keep_false(self):
        passed, reason = gate_location(False)
        assert passed is False
        assert reason != ""


# ---------------------------------------------------------------------------
# gate_recency
# ---------------------------------------------------------------------------

class TestGateRecency:
    def test_passes_any_filter(self):
        # activity_filter='any' always passes regardless of counts
        passed, reason = gate_recency('any', 0, 0, 0)
        assert passed is True
        assert reason == ""

    def test_passes_any_filter_ignores_counts(self):
        passed, _ = gate_recency('any', 0, 0, 0)
        assert passed is True

    def test_passes_30d_with_enough_uploads(self):
        passed, reason = gate_recency('30', 2, 5, 8)
        assert passed is True
        assert reason == ""

    def test_passes_60d_with_enough_uploads(self):
        passed, reason = gate_recency('60', 0, 3, 7)
        assert passed is True

    def test_passes_90d_with_enough_uploads(self):
        passed, reason = gate_recency('90', 0, 0, 2)
        assert passed is True

    def test_fails_30d_zero_uploads(self):
        passed, reason = gate_recency('30', 0, 4, 8)
        assert passed is False
        assert "30d" in reason
        assert "0" in reason

    def test_fails_30d_one_upload(self):
        passed, reason = gate_recency('30', 1, 4, 8)
        assert passed is False
        assert "1" in reason
        assert "minimum is 2" in reason

    def test_fails_60d_insufficient(self):
        passed, reason = gate_recency('60', 0, 1, 5)
        assert passed is False
        assert "60d" in reason

    def test_fails_90d_insufficient(self):
        passed, reason = gate_recency('90', 0, 0, 1)
        assert passed is False
        assert "90d" in reason

    def test_passes_at_threshold(self):
        # exactly 2 uploads meets the minimum
        passed, _ = gate_recency('30', 2, 2, 2)
        assert passed is True

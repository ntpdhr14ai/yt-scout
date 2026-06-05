import pytest
from scraper import keyword_match


class TestKeywordMatch:
    # ------------------------------------------------------------------
    # Empty / falsy keyword → always pass
    # ------------------------------------------------------------------

    def test_empty_keyword_passes(self):
        assert keyword_match("", "Travel Thailand", "about text", ["video 1"]) is True

    def test_whitespace_keyword_passes(self):
        # empty string after Python falsy check — "  " is truthy so it will search
        # just verifying empty string specifically
        assert keyword_match("", "", "", []) is True

    # ------------------------------------------------------------------
    # Match in channel_name
    # ------------------------------------------------------------------

    def test_match_in_channel_name(self):
        assert keyword_match("travel", "Travel Thailand", "", []) is True

    def test_match_in_channel_name_case_insensitive(self):
        assert keyword_match("TRAVEL", "travel thailand", "", []) is True

    # ------------------------------------------------------------------
    # Match in about_text
    # ------------------------------------------------------------------

    def test_match_in_about_text(self):
        assert keyword_match("cooking", "", "We make cooking videos", []) is True

    def test_match_in_about_text_case_insensitive(self):
        assert keyword_match("cooking", "", "COOKING CHANNEL", []) is True

    # ------------------------------------------------------------------
    # Match in recent_titles
    # ------------------------------------------------------------------

    def test_match_in_first_title(self):
        assert keyword_match("recipe", "", "", ["Easy recipe for beginners", "Vlog #2"]) is True

    def test_match_in_last_title(self):
        assert keyword_match("recipe", "", "", ["Vlog #1", "Vlog #2", "Best recipe ever"]) is True

    def test_match_in_middle_title(self):
        titles = ["Vlog #1", "Thai street food recipe", "Vlog #3"]
        assert keyword_match("recipe", "", "", titles) is True

    def test_match_case_insensitive_in_title(self):
        assert keyword_match("RECIPE", "", "", ["Easy Recipe For Beginners"]) is True

    # ------------------------------------------------------------------
    # No match → False
    # ------------------------------------------------------------------

    def test_no_match_anywhere(self):
        assert keyword_match("recipe", "Travel Thailand", "About travel", ["Vlog #1", "Trip to Phuket"]) is False

    def test_no_match_empty_all_fields(self):
        assert keyword_match("recipe", "", "", []) is False

    def test_no_match_empty_titles(self):
        assert keyword_match("recipe", "Travel Thailand", "About travel", []) is False

    # ------------------------------------------------------------------
    # Partial / substring match
    # ------------------------------------------------------------------

    def test_partial_match_in_title(self):
        # "cook" is a substring of "cooking"
        assert keyword_match("cook", "", "", ["Cooking with Grandma"]) is True

    def test_keyword_longer_than_any_field(self):
        assert keyword_match("superlongkeywordthatexistsnowhere", "Travel", "About", ["Vlog"]) is False

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_all_fields_empty(self):
        assert keyword_match("travel", "", "", []) is False

    def test_keyword_matches_channel_name_exactly(self):
        assert keyword_match("ThaiVlogger", "ThaiVlogger", "", []) is True

    def test_keyword_in_channel_name_overrides_empty_titles(self):
        assert keyword_match("food", "Thai Food Channel", "", []) is True

    def test_none_filtered_from_haystack(self):
        # filter(None, ...) drops empty strings — keyword must still match present fields
        assert keyword_match("food", "", "", ["street food"]) is True

    def test_match_across_field_boundary_not_required(self):
        # "namefood" would only match if it appears in one field; here it doesn't
        assert keyword_match("namefood", "name", "", ["food"]) is False

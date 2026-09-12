"""Failing tests for global CleaningStats accumulator."""

from __future__ import annotations


def test_cleaning_stats_initial_zeros():
    from api.stats import CleaningStats

    stats = CleaningStats()
    assert stats.rows_in == 0
    assert stats.rows_out == 0
    assert stats.rows_error == 0
    assert stats.normalized_per_column == {}
    assert stats.blank_per_column == {}


def test_cleaning_stats_to_dict_has_five_global_fields():
    from api.stats import CleaningStats

    stats = CleaningStats()
    payload = stats.to_dict()
    assert set(payload.keys()) == {
        "normalized_per_column",
        "blank_per_column",
        "rows_in",
        "rows_out",
        "rows_error",
    }


def test_record_helpers_accumulate_not_reset():
    from api.stats import CleaningStats

    stats = CleaningStats()
    stats.record_batch(rows_in=10, rows_out=7, rows_error=3)
    stats.record_batch(rows_in=5, rows_out=4, rows_error=1)
    stats.bump_normalized("language", 2)
    stats.bump_normalized("language", 1)
    stats.bump_blank("contact", 3)

    assert stats.rows_in == 15
    assert stats.rows_out == 11
    assert stats.rows_error == 4
    assert stats.normalized_per_column["language"] == 3
    assert stats.blank_per_column["contact"] == 3

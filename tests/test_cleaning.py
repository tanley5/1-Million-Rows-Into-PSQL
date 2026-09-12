"""Failing tests for chunked Polars cleaning / normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import EXPECTED_CSV_HEADERS, NORMALIZED_COLUMNS


def _row(
    *,
    start: str = "2024-01-01T10:00:00Z",
    end: str = "2024-01-01T11:00:00Z",
    language: str = "EN",
    interaction_id: str = "abcdef123456",
    contact: str = "Yes",
) -> dict[str, str]:
    return {
        EXPECTED_CSV_HEADERS[0]: start,
        EXPECTED_CSV_HEADERS[1]: end,
        EXPECTED_CSV_HEADERS[2]: language,
        EXPECTED_CSV_HEADERS[3]: interaction_id,
        EXPECTED_CSV_HEADERS[4]: contact,
    }


def test_clean_csv_soft_normalize_increments_normalized(write_csv):
    from api.cleaning import clean_csv
    from api.stats import CleaningStats

    path = write_csv(
        [
            _row(language=" en ", contact=" no "),
            _row(interaction_id="fedcba654321", language="sp", contact="YES"),
        ]
    )
    stats = CleaningStats()
    frames = list(clean_csv(path, chunk_size=2, stats=stats))

    assert len(frames) == 1
    out = frames[0]
    assert out.columns == NORMALIZED_COLUMNS
    assert out.height == 2
    assert set(out["language"].to_list()) == {"EN", "SP"}
    assert out["contact"].to_list() == [False, True]
    assert stats.rows_in == 2
    assert stats.rows_out == 2
    assert stats.rows_error == 0
    assert stats.normalized_per_column.get("language", 0) >= 1
    assert stats.normalized_per_column.get("contact", 0) >= 1


def test_clean_csv_counts_blanks_per_column(write_csv):
    from api.cleaning import clean_csv
    from api.stats import CleaningStats

    path = write_csv(
        [
            _row(language="", contact="Yes", interaction_id="aaaaaaaaaaaa"),
            _row(language="EN", contact="", interaction_id="bbbbbbbbbbbb"),
        ]
    )
    stats = CleaningStats()
    frames = list(clean_csv(path, chunk_size=10, stats=stats))

    # blank language / contact are hard errors for allow-list / Yes-No, but blanks
    # must still be counted on the raw column before rejection.
    assert stats.blank_per_column.get("language", 0) >= 1
    assert stats.blank_per_column.get("contact", 0) >= 1
    assert stats.rows_error >= 1
    assert sum(f.height for f in frames) == stats.rows_out


def test_clean_csv_hard_errors_excluded(write_csv):
    from api.cleaning import clean_csv
    from api.stats import CleaningStats

    path = write_csv(
        [
            _row(interaction_id="goodid000001", language="EN"),
            _row(interaction_id="bad", language="EN"),  # bad id length
            _row(interaction_id="goodid000002", language="XX"),  # bad language
            _row(
                interaction_id="goodid000003",
                start="2024-01-01T12:00:00Z",
                end="2024-01-01T11:00:00Z",
            ),  # end < start
        ]
    )
    stats = CleaningStats()
    frames = list(clean_csv(path, chunk_size=2, stats=stats))

    assert stats.rows_in == 4
    assert stats.rows_error == 3
    assert stats.rows_out == 1
    assert sum(f.height for f in frames) == 1
    assert frames[0]["interaction_id"].to_list() == ["goodid000001"]


def test_clean_csv_duplicate_id_across_chunks(write_csv):
    from api.cleaning import clean_csv
    from api.stats import CleaningStats

    dup = "dupdupdup001"
    path = write_csv(
        [
            _row(interaction_id=dup, language="EN"),
            _row(interaction_id="unique000002", language="FR"),
            _row(interaction_id=dup, language="SP"),  # duplicate across chunk boundary
        ]
    )
    stats = CleaningStats()
    frames = list(clean_csv(path, chunk_size=2, stats=stats))

    assert stats.rows_in == 3
    assert stats.rows_error == 1
    assert stats.rows_out == 2
    ids = []
    for frame in frames:
        ids.extend(frame["interaction_id"].to_list())
    assert ids.count(dup) == 1
    assert "unique000002" in ids


def test_clean_csv_global_totals_across_chunks(write_csv):
    from api.cleaning import clean_csv
    from api.stats import CleaningStats

    path = write_csv(
        [
            _row(interaction_id="aaaaaaaaaaaa"),
            _row(interaction_id="bbbbbbbbbbbb"),
            _row(interaction_id="cccccccccccc"),
            _row(interaction_id="bad"),  # error in later chunk
        ]
    )
    stats = CleaningStats()
    frames = list(clean_csv(path, chunk_size=2, stats=stats))

    assert len(frames) >= 2
    assert stats.rows_in == 4
    assert stats.rows_out == 3
    assert stats.rows_error == 1
    assert sum(f.height for f in frames) == 3

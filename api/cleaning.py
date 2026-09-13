"""Chunked Polars CSV cleaning and normalization."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

import polars as pl

from api.stats import CleaningStats

PathLike = Union[str, Path]

RAW_TO_NORMALIZED = {
    "Start Datetime": "start_datetime",
    "End Datetime": "end_datetime",
    "Language Random (EN,SP,FR,ZM,ID)": "language",
    "Interaction ID Random (12 char)": "interaction_id",
    "Contact: (Yes/No)": "contact",
}

NORMALIZED_COLUMNS = [
    "start_datetime",
    "end_datetime",
    "language",
    "interaction_id",
    "contact",
]

ALLOWED_LANGUAGES = frozenset({"EN", "SP", "FR", "ZM", "ID"})


def _as_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _is_blank(value: object) -> bool:
    return _as_str(value).strip() == ""


def _parse_utc(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_chunk(
    chunk: pl.DataFrame,
    stats: CleaningStats,
    seen_ids: set[str],
) -> pl.DataFrame:
    renamed = chunk.rename(RAW_TO_NORMALIZED)
    rows_out: list[dict] = []
    rows_in = renamed.height
    rows_error = 0

    for row in renamed.iter_rows(named=True):
        for col in NORMALIZED_COLUMNS:
            if _is_blank(row.get(col)):
                stats.bump_blank(col)

        start_raw = _as_str(row.get("start_datetime"))
        end_raw = _as_str(row.get("end_datetime"))
        language_raw = _as_str(row.get("language"))
        id_raw = _as_str(row.get("interaction_id"))
        contact_raw = _as_str(row.get("contact"))

        start_s = start_raw.strip()
        end_s = end_raw.strip()
        language_s = language_raw.strip()
        id_s = id_raw.strip()
        contact_s = contact_raw.strip()

        language = language_s.upper()
        contact_key = contact_s.lower()

        if language_s != language_raw or language != language_s:
            stats.bump_normalized("language")
        if contact_s != contact_raw or contact_s != contact_key:
            # trim and/or case change on contact
            stats.bump_normalized("contact")
        elif contact_key in {"yes", "no"}:
            # Yes/No → bool is a soft normalize even when already canonical casing
            stats.bump_normalized("contact")

        error = False
        if language not in ALLOWED_LANGUAGES:
            error = True
        if len(id_s) != 12:
            error = True
        if contact_key not in {"yes", "no"}:
            error = True

        start_dt = _parse_utc(start_s)
        end_dt = _parse_utc(end_s)
        if start_dt is None or end_dt is None:
            error = True
        elif end_dt < start_dt:
            error = True

        if error:
            rows_error += 1
            continue

        if id_s in seen_ids:
            rows_error += 1
            continue
        seen_ids.add(id_s)

        rows_out.append(
            {
                "start_datetime": start_dt,
                "end_datetime": end_dt,
                "language": language,
                "interaction_id": id_s,
                "contact": contact_key == "yes",
            }
        )

    stats.record_batch(rows_in=rows_in, rows_out=len(rows_out), rows_error=rows_error)

    if not rows_out:
        return pl.DataFrame(
            schema={
                "start_datetime": pl.Datetime(time_zone="UTC"),
                "end_datetime": pl.Datetime(time_zone="UTC"),
                "language": pl.Utf8,
                "interaction_id": pl.Utf8,
                "contact": pl.Boolean,
            }
        )

    return pl.DataFrame(rows_out).select(NORMALIZED_COLUMNS)


def clean_csv(
    path: PathLike,
    chunk_size: int,
    stats: CleaningStats,
) -> Iterator[pl.DataFrame]:
    """Yield cleaned Polars frames for each CSV chunk, updating ``stats`` globally."""
    path = Path(path)
    seen_ids: set[str] = set()
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    # BatchedCsvReader may return larger frames than batch_size on small files;
    # re-slice to honor the caller's chunk_size (tests force 2–10).
    reader = pl.read_csv_batched(
        path,
        batch_size=max(chunk_size, 10_000),
        infer_schema_length=0,
        schema_overrides={name: pl.Utf8 for name in RAW_TO_NORMALIZED},
    )
    leftover: pl.DataFrame | None = None

    def _emit(frame: pl.DataFrame) -> Iterator[pl.DataFrame]:
        nonlocal leftover
        if leftover is not None:
            frame = pl.concat([leftover, frame], how="vertical_relaxed")
            leftover = None
        offset = 0
        while offset + chunk_size <= frame.height:
            part = frame.slice(offset, chunk_size)
            cleaned = _clean_chunk(part, stats, seen_ids)
            if cleaned.height > 0:
                yield cleaned
            offset += chunk_size
        if offset < frame.height:
            leftover = frame.slice(offset)

    while True:
        batches = reader.next_batches(1)
        if not batches:
            break
        yield from _emit(batches[0])

    if leftover is not None and leftover.height > 0:
        cleaned = _clean_chunk(leftover, stats, seen_ids)
        if cleaned.height > 0:
            yield cleaned

"""Shared fixtures for CSV → Postgres pipeline tests."""

from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

EXPECTED_CSV_HEADERS = [
    "Start Datetime",
    "End Datetime",
    "Language Random (EN,SP,FR,ZM,ID)",
    "Interaction ID Random (12 char)",
    "Contact: (Yes/No)",
]

NORMALIZED_COLUMNS = [
    "start_datetime",
    "end_datetime",
    "language",
    "interaction_id",
    "contact",
]

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/csv_pipeline_test",
)


@pytest.fixture
def expected_csv_headers() -> list[str]:
    return list(EXPECTED_CSV_HEADERS)


@pytest.fixture
def tmp_csv_path(tmp_path: Path) -> Path:
    return tmp_path / "sample.csv"


@pytest.fixture
def write_csv(tmp_csv_path: Path):
    """Write a CSV with exact production headers and given row dicts."""

    def _write(rows: list[dict[str, str]]) -> Path:
        with tmp_csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXPECTED_CSV_HEADERS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return tmp_csv_path

    return _write


@pytest.fixture
def database_url() -> str:
    return TEST_DATABASE_URL


@pytest.fixture
def db_conn(database_url: str):
    """Real Postgres connection; skip if unreachable."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(database_url, autocommit=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available at {database_url}: {exc}")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ensure_interactions_table(db_conn) -> Iterator[None]:
    """Ensure prod interactions table exists; truncate between tests."""
    db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interactions (
            start_datetime timestamptz NOT NULL,
            end_datetime timestamptz NOT NULL,
            language text NOT NULL,
            interaction_id text PRIMARY KEY,
            contact boolean NOT NULL
        )
        """
    )
    db_conn.execute("TRUNCATE TABLE interactions")
    yield
    db_conn.execute("TRUNCATE TABLE interactions")


@pytest.fixture
def cleanup_staging(db_conn) -> Iterator[None]:
    """Drop any leftover staging_* tables after each API/DB test."""
    yield
    rows = db_conn.execute(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public' AND tablename LIKE 'staging_%'
        """
    ).fetchall()
    for (name,) in rows:
        db_conn.execute(f'DROP TABLE IF EXISTS "{name}"')

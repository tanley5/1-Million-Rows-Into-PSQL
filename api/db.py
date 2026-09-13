"""Postgres helpers for UNLOGGED staging and COPY."""

from __future__ import annotations

import os
import re
from typing import Any

import polars as pl
import psycopg

from api.cleaning import NORMALIZED_COLUMNS

COLUMN_SQL = {
    "start_datetime": "timestamptz NOT NULL",
    "end_datetime": "timestamptz NOT NULL",
    "language": "text NOT NULL",
    "interaction_id": "text NOT NULL",
    "contact": "boolean NOT NULL",
}

COLUMN_DATATYPES = [
    {"name": "start_datetime", "datatype": "timestamptz"},
    {"name": "end_datetime", "datatype": "timestamptz"},
    {"name": "language", "datatype": "text"},
    {"name": "interaction_id", "datatype": "text"},
    {"name": "contact", "datatype": "boolean"},
]

_SAFE_ID = re.compile(r"^[0-9a-fA-F]+$")


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def connect() -> psycopg.Connection:
    return psycopg.connect(get_database_url(), autocommit=True)


def staging_table_name(upload_id: str) -> str:
    if not _SAFE_ID.match(upload_id):
        raise ValueError(f"invalid upload_id: {upload_id!r}")
    return f"staging_{upload_id}"


def ensure_interactions_table(conn: psycopg.Connection) -> None:
    cols = ",\n            ".join(
        f"{name} {COLUMN_SQL[name]}" for name in NORMALIZED_COLUMNS
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS interactions (
            {cols},
            PRIMARY KEY (interaction_id)
        )
        """
    )


def create_unlogged_staging(conn: psycopg.Connection, upload_id: str) -> str:
    table = staging_table_name(upload_id)
    cols = ",\n            ".join(
        f"{name} {COLUMN_SQL[name]}" for name in NORMALIZED_COLUMNS
    )
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(
        f"""
        CREATE UNLOGGED TABLE "{table}" (
            {cols},
            UNIQUE (interaction_id)
        )
        """
    )
    return table


def copy_frame_to_staging(
    conn: psycopg.Connection,
    table: str,
    frame: pl.DataFrame,
) -> None:
    if frame.height == 0:
        return
    ordered = frame.select(NORMALIZED_COLUMNS)
    col_list = ", ".join(NORMALIZED_COLUMNS)
    with conn.cursor() as cur:
        with cur.copy(f'COPY "{table}" ({col_list}) FROM STDIN') as copy:
            for row in ordered.iter_rows():
                copy.write_row(row)


def fetch_sample_rows(
    conn: psycopg.Connection,
    table: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    col_list = ", ".join(NORMALIZED_COLUMNS)
    rows = conn.execute(
        f'SELECT {col_list} FROM "{table}" LIMIT %s',
        (limit,),
    ).fetchall()
    sample: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for name, value in zip(NORMALIZED_COLUMNS, row):
            if hasattr(value, "isoformat"):
                item[name] = value.isoformat()
            else:
                item[name] = value
        sample.append(item)
    return sample


def staging_exists(conn: psycopg.Connection, upload_id: str) -> bool:
    table = staging_table_name(upload_id)
    row = conn.execute(
        """
        SELECT 1
        FROM pg_tables
        WHERE schemaname = 'public' AND tablename = %s
        """,
        (table,),
    ).fetchone()
    return row is not None

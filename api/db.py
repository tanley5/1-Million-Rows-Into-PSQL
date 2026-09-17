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
BUCKET_COUNT = 256
BUCKET_EXPRESSION = "get_byte(decode(md5(interaction_id), 'hex'), 0)"


class ApprovalInProgressError(RuntimeError):
    """Raised when another request holds the upload approval lock."""


class ApprovalBucketError(RuntimeError):
    """Raised when one approval bucket fails and can be retried."""

    def __init__(self, bucket: int, message: str):
        self.bucket = bucket
        super().__init__(message)


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


def create_staging_bucket_index(conn: psycopg.Connection, table: str) -> None:
    """Create the expression index used by deterministic approval buckets."""
    index = f"{table}_approval_bucket_idx"
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "{index}" '
        f'ON "{table}" (({BUCKET_EXPRESSION}))'
    )


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


def ensure_approval_progress_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_approval_progress (
            upload_id text NOT NULL,
            bucket smallint NOT NULL CHECK (bucket BETWEEN 0 AND 255),
            status text NOT NULL
                CHECK (status IN ('pending', 'running', 'completed', 'failed')),
            attempts integer NOT NULL DEFAULT 0,
            rows_affected bigint NOT NULL DEFAULT 0,
            last_error text,
            completed_at timestamptz,
            PRIMARY KEY (upload_id, bucket)
        )
        """
    )


def _try_upload_lock(conn: psycopg.Connection, upload_id: str) -> bool:
    return bool(
        conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            (upload_id,),
        ).fetchone()[0]
    )


def _unlock_upload(conn: psycopg.Connection, upload_id: str) -> None:
    conn.execute(
        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
        (upload_id,),
    )


def _initialize_approval_progress(
    conn: psycopg.Connection,
    upload_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO upload_approval_progress (upload_id, bucket, status)
        SELECT %s, bucket, 'pending'
        FROM generate_series(0, %s) AS bucket
        ON CONFLICT (upload_id, bucket) DO NOTHING
        """,
        (upload_id, BUCKET_COUNT - 1),
    )


def _upsert_bucket(
    conn: psycopg.Connection,
    table: str,
    bucket: int,
) -> int:
    col_list = ", ".join(NORMALIZED_COLUMNS)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in NORMALIZED_COLUMNS
        if column != "interaction_id"
    )
    compared_columns = [
        column for column in NORMALIZED_COLUMNS if column != "interaction_id"
    ]
    current_values = ", ".join(
        f"interactions.{column}" for column in compared_columns
    )
    excluded_values = ", ".join(
        f"EXCLUDED.{column}" for column in compared_columns
    )
    result = conn.execute(
        f"""
        INSERT INTO interactions ({col_list})
        SELECT {col_list}
        FROM "{table}"
        WHERE {BUCKET_EXPRESSION} = %s
        ON CONFLICT (interaction_id) DO UPDATE SET {updates}
        WHERE ({current_values}) IS DISTINCT FROM ({excluded_values})
        """,
        (bucket,),
    )
    return result.rowcount


def approve_staging(conn: psycopg.Connection, upload_id: str) -> int | None:
    """Resume bucketed upserts and clean up after every bucket completes."""
    table = staging_table_name(upload_id)
    if not _try_upload_lock(conn, upload_id):
        raise ApprovalInProgressError(upload_id)

    try:
        if not staging_exists(conn, upload_id):
            return None
        ensure_interactions_table(conn)
        ensure_approval_progress_table(conn)
        create_staging_bucket_index(conn, table)
        conn.execute(f'ANALYZE "{table}"')
        _initialize_approval_progress(conn, upload_id)

        buckets = conn.execute(
            """
            SELECT bucket
            FROM upload_approval_progress
            WHERE upload_id = %s AND status <> 'completed'
            ORDER BY bucket
            """,
            (upload_id,),
        ).fetchall()

        for (bucket,) in buckets:
            try:
                with conn.transaction():
                    conn.execute(
                        """
                        UPDATE upload_approval_progress
                        SET status = 'running',
                            attempts = attempts + 1,
                            last_error = NULL,
                            completed_at = NULL
                        WHERE upload_id = %s AND bucket = %s
                        """,
                        (upload_id, bucket),
                    )
                    affected = _upsert_bucket(conn, table, bucket)
                    conn.execute(
                        """
                        UPDATE upload_approval_progress
                        SET status = 'completed',
                            rows_affected = %s,
                            completed_at = now()
                        WHERE upload_id = %s AND bucket = %s
                        """,
                        (affected, upload_id, bucket),
                    )
            except Exception as exc:
                with conn.transaction():
                    conn.execute(
                        """
                        UPDATE upload_approval_progress
                        SET status = 'failed',
                            attempts = attempts + 1,
                            last_error = %s,
                            completed_at = NULL
                        WHERE upload_id = %s AND bucket = %s
                        """,
                        (str(exc)[:2000], upload_id, bucket),
                    )
                raise ApprovalBucketError(bucket, str(exc)) from exc

        total_affected = conn.execute(
            """
            SELECT COALESCE(SUM(rows_affected), 0)
            FROM upload_approval_progress
            WHERE upload_id = %s
            """,
            (upload_id,),
        ).fetchone()[0]
        incomplete = conn.execute(
            """
            SELECT COUNT(*)
            FROM upload_approval_progress
            WHERE upload_id = %s AND status <> 'completed'
            """,
            (upload_id,),
        ).fetchone()[0]
        if incomplete:
            raise ApprovalBucketError(-1, "approval has unfinished buckets")

        with conn.transaction():
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(
                "DELETE FROM upload_approval_progress WHERE upload_id = %s",
                (upload_id,),
            )
        return int(total_affected)
    finally:
        _unlock_upload(conn, upload_id)


def deny_staging(conn: psycopg.Connection, upload_id: str) -> bool:
    """Drop staging and approval progress without changing production."""
    table = staging_table_name(upload_id)
    if not _try_upload_lock(conn, upload_id):
        raise ApprovalInProgressError(upload_id)
    try:
        if not staging_exists(conn, upload_id):
            return False
        with conn.transaction():
            conn.execute(f'DROP TABLE "{table}"')
            if conn.execute(
                "SELECT to_regclass('public.upload_approval_progress')"
            ).fetchone()[0] is not None:
                conn.execute(
                    "DELETE FROM upload_approval_progress WHERE upload_id = %s",
                    (upload_id,),
                )
        return True
    finally:
        _unlock_upload(conn, upload_id)

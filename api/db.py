"""Postgres helpers for UNLOGGED staging and size-capped section upserts."""

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
DEFAULT_SECTION_MAX_ROWS = 100_000


class ApprovalInProgressError(RuntimeError):
    """Raised when another request holds the upload approval lock."""


class ApprovalSectionError(RuntimeError):
    """Raised when a single-section approve call fails."""

    def __init__(self, section_id: int, message: str):
        self.section_id = section_id
        super().__init__(message)


def section_max_rows() -> int:
    raw = os.environ.get("SECTION_MAX_ROWS")
    if raw is None:
        return DEFAULT_SECTION_MAX_ROWS
    value = int(raw)
    if value < 1:
        raise ValueError("SECTION_MAX_ROWS must be >= 1")
    return value


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
            section_id integer NOT NULL,
            UNIQUE (interaction_id)
        )
        """
    )
    return table


def create_staging_section_index(conn: psycopg.Connection, table: str) -> None:
    """Index section_id for per-section approve upserts."""
    index = f"{table}_section_id_idx"
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}" (section_id)'
    )


def copy_frame_to_staging(
    conn: psycopg.Connection,
    table: str,
    frame: pl.DataFrame,
    section_id: int,
) -> None:
    if frame.height == 0:
        return
    ordered = frame.select(NORMALIZED_COLUMNS)
    col_list = ", ".join(NORMALIZED_COLUMNS + ["section_id"])
    with conn.cursor() as cur:
        with cur.copy(f'COPY "{table}" ({col_list}) FROM STDIN') as copy:
            for row in ordered.iter_rows():
                copy.write_row((*row, section_id))


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
    columns = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'upload_approval_progress'
        """
    ).fetchall()
    column_names = {name for (name,) in columns}
    if column_names and "bucket" in column_names and "section_id" not in column_names:
        conn.execute("DROP TABLE upload_approval_progress")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_approval_progress (
            upload_id text NOT NULL,
            section_id integer NOT NULL CHECK (section_id >= 0),
            status text NOT NULL
                CHECK (status IN ('pending', 'running', 'completed', 'failed')),
            attempts integer NOT NULL DEFAULT 0,
            rows_affected bigint NOT NULL DEFAULT 0,
            last_error text,
            completed_at timestamptz,
            PRIMARY KEY (upload_id, section_id)
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
    table: str,
) -> None:
    conn.execute(
        f"""
        INSERT INTO upload_approval_progress (upload_id, section_id, status)
        SELECT %s, section_id, 'pending'
        FROM (
            SELECT DISTINCT section_id
            FROM "{table}"
        ) AS sections
        ON CONFLICT (upload_id, section_id) DO NOTHING
        """,
        (upload_id,),
    )


def _upsert_section(
    conn: psycopg.Connection,
    table: str,
    section_id: int,
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
        WHERE section_id = %s
        ON CONFLICT (interaction_id) DO UPDATE SET {updates}
        WHERE ({current_values}) IS DISTINCT FROM ({excluded_values})
        """,
        (section_id,),
    )
    return result.rowcount


def _section_rows(
    conn: psycopg.Connection,
    upload_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT section_id, status, attempts, rows_affected, last_error, completed_at
        FROM upload_approval_progress
        WHERE upload_id = %s
        ORDER BY section_id
        """,
        (upload_id,),
    ).fetchall()
    sections: list[dict[str, Any]] = []
    for section_id, status, attempts, rows_affected, last_error, completed_at in rows:
        sections.append(
            {
                "section_id": section_id,
                "status": status,
                "attempts": attempts,
                "rows_affected": rows_affected,
                "last_error": last_error,
                "completed_at": (
                    completed_at.isoformat() if completed_at is not None else None
                ),
            }
        )
    return sections


def _summary_from_sections(sections: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"completed": 0, "failed": 0, "pending": 0, "running": 0}
    for section in sections:
        status = section["status"]
        if status in summary:
            summary[status] += 1
    return summary


def _approval_result(
    upload_id: str,
    sections: list[dict[str, Any]],
    staging_dropped: bool,
) -> dict[str, Any]:
    summary = _summary_from_sections(sections)
    rows_upserted = sum(section["rows_affected"] for section in sections)
    status = "approved" if summary["failed"] == 0 and summary["pending"] == 0 and summary["running"] == 0 and sections else "partial"
    if not sections:
        status = "approved" if staging_dropped else "partial"
    return {
        "upload_id": upload_id,
        "status": status,
        "rows_upserted": rows_upserted,
        "sections": sections,
        "summary": summary,
        "staging_dropped": staging_dropped,
    }


def _drop_staging_and_progress(
    conn: psycopg.Connection,
    upload_id: str,
    table: str,
) -> None:
    with conn.transaction():
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(
            "DELETE FROM upload_approval_progress WHERE upload_id = %s",
            (upload_id,),
        )


def _maybe_finalize(
    conn: psycopg.Connection,
    upload_id: str,
    table: str,
) -> tuple[list[dict[str, Any]], bool]:
    incomplete = conn.execute(
        """
        SELECT COUNT(*)
        FROM upload_approval_progress
        WHERE upload_id = %s AND status <> 'completed'
        """,
        (upload_id,),
    ).fetchone()[0]
    if incomplete:
        return _section_rows(conn, upload_id), False
    sections = _section_rows(conn, upload_id)
    _drop_staging_and_progress(conn, upload_id, table)
    return sections, True


def _run_section(
    conn: psycopg.Connection,
    upload_id: str,
    table: str,
    section_id: int,
) -> None:
    try:
        with conn.transaction():
            conn.execute(
                """
                UPDATE upload_approval_progress
                SET status = 'running',
                    attempts = attempts + 1,
                    last_error = NULL,
                    completed_at = NULL
                WHERE upload_id = %s AND section_id = %s
                """,
                (upload_id, section_id),
            )
            affected = _upsert_section(conn, table, section_id)
            conn.execute(
                """
                UPDATE upload_approval_progress
                SET status = 'completed',
                    rows_affected = %s,
                    completed_at = now()
                WHERE upload_id = %s AND section_id = %s
                """,
                (affected, upload_id, section_id),
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
                WHERE upload_id = %s AND section_id = %s
                """,
                (str(exc)[:2000], upload_id, section_id),
            )
        raise


def get_upload_status(conn: psycopg.Connection, upload_id: str) -> dict[str, Any] | None:
    ensure_approval_progress_table(conn)
    present = staging_exists(conn, upload_id)
    if present:
        _initialize_approval_progress(conn, upload_id, staging_table_name(upload_id))

    sections = _section_rows(conn, upload_id)
    if not present and not sections:
        return None
    return {
        "upload_id": upload_id,
        "sections": sections,
        "summary": _summary_from_sections(sections),
        "staging_present": present,
    }


def approve_staging(conn: psycopg.Connection, upload_id: str) -> dict[str, Any] | None:
    """Process all unfinished sections; continue after failures; drop only when all complete."""
    table = staging_table_name(upload_id)
    if not _try_upload_lock(conn, upload_id):
        raise ApprovalInProgressError(upload_id)

    try:
        if not staging_exists(conn, upload_id):
            return None
        ensure_interactions_table(conn)
        ensure_approval_progress_table(conn)
        create_staging_section_index(conn, table)
        conn.execute(f'ANALYZE "{table}"')
        _initialize_approval_progress(conn, upload_id, table)

        sections_to_run = conn.execute(
            """
            SELECT section_id
            FROM upload_approval_progress
            WHERE upload_id = %s AND status <> 'completed'
            ORDER BY section_id
            """,
            (upload_id,),
        ).fetchall()

        for (section_id,) in sections_to_run:
            try:
                _run_section(conn, upload_id, table, section_id)
            except Exception:
                # Continue remaining sections after a failure.
                continue

        sections, dropped = _maybe_finalize(conn, upload_id, table)
        if dropped:
            # Progress deleted; reconstruct summary from last in-memory state.
            # Re-read is empty after drop — capture before drop already in _maybe_finalize.
            pass
        if dropped:
            # sections captured before delete still have completed statuses
            result = _approval_result(upload_id, sections, True)
            result["status"] = "approved"
            return result
        return _approval_result(upload_id, sections, False)
    finally:
        _unlock_upload(conn, upload_id)


def approve_section(
    conn: psycopg.Connection,
    upload_id: str,
    section_id: int,
) -> dict[str, Any] | None:
    """Approve a single section; drop staging only if this completes the upload."""
    table = staging_table_name(upload_id)
    if not _try_upload_lock(conn, upload_id):
        raise ApprovalInProgressError(upload_id)

    try:
        if not staging_exists(conn, upload_id):
            return None
        ensure_interactions_table(conn)
        ensure_approval_progress_table(conn)
        create_staging_section_index(conn, table)
        _initialize_approval_progress(conn, upload_id, table)

        exists = conn.execute(
            """
            SELECT 1
            FROM upload_approval_progress
            WHERE upload_id = %s AND section_id = %s
            """,
            (upload_id, section_id),
        ).fetchone()
        if not exists:
            return None

        try:
            _run_section(conn, upload_id, table, section_id)
        except Exception as exc:
            sections = _section_rows(conn, upload_id)
            raise ApprovalSectionError(section_id, str(exc)) from exc

        sections, dropped = _maybe_finalize(conn, upload_id, table)
        return _approval_result(upload_id, sections, dropped)
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
        ensure_approval_progress_table(conn)
        _drop_staging_and_progress(conn, upload_id, table)
        return True
    finally:
        _unlock_upload(conn, upload_id)

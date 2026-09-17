"""Failing tests for FastAPI upload / preview / approve / deny against real Postgres."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from tests.conftest import EXPECTED_CSV_HEADERS, NORMALIZED_COLUMNS


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    import csv

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPECTED_CSV_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _valid_row(interaction_id: str, language: str = "EN") -> dict[str, str]:
    return {
        EXPECTED_CSV_HEADERS[0]: "2024-06-01T09:00:00Z",
        EXPECTED_CSV_HEADERS[1]: "2024-06-01T09:30:00Z",
        EXPECTED_CSV_HEADERS[2]: language,
        EXPECTED_CSV_HEADERS[3]: interaction_id,
        EXPECTED_CSV_HEADERS[4]: "Yes",
    }


def _id_for_bucket(bucket: int) -> str:
    for value in range(100_000):
        candidate = f"{value:012x}"
        if hashlib.md5(candidate.encode()).digest()[0] == bucket:
            return candidate
    raise AssertionError(f"could not find an ID for bucket {bucket}")


def test_api_app_importable():
    from api.main import app

    assert app is not None


@pytest.fixture
def client(database_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_post_uploads_creates_staging_and_preview(client, db_conn):
    payload = _csv_bytes(
        [_valid_row(f"{i:012x}") for i in range(12)]
    )
    response = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()

    assert "upload_id" in body
    assert isinstance(body["sample"], list)
    assert 1 <= len(body["sample"]) <= 100

    stats = body["stats"]
    for key in (
        "normalized_per_column",
        "blank_per_column",
        "rows_in",
        "rows_out",
        "rows_error",
    ):
        assert key in stats
    assert stats["rows_in"] == 12
    assert stats["rows_out"] == 12
    assert stats["rows_error"] == 0

    columns = body["columns"]
    assert [c["name"] for c in columns] == NORMALIZED_COLUMNS
    assert all("datatype" in c for c in columns)

    staging = f"staging_{body['upload_id']}"
    relkind = db_conn.execute(
        "SELECT relpersistence FROM pg_class WHERE relname = %s",
        (staging,),
    ).fetchone()
    assert relkind is not None
    assert relkind[0] == "u"  # UNLOGGED
    bucket_index = db_conn.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
          AND indexdef LIKE '%%get_byte%%'
        """,
        (staging,),
    ).fetchone()
    assert bucket_index is not None


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_approve_upserts_then_drops_staging(client, db_conn):
    payload = _csv_bytes(
        [
            _valid_row("a00000000001"),
            _valid_row("b00000000002", language="FR"),
        ]
    )
    upload = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    ).json()
    upload_id = upload["upload_id"]

    response = client.post(f"/uploads/{upload_id}/approve")
    assert response.status_code == 200

    count = db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    assert count == 2

    staging = f"staging_{upload_id}"
    exists = db_conn.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
        (staging,),
    ).fetchone()
    assert exists is None


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_deny_drops_staging_without_prod_change(client, db_conn):
    db_conn.execute(
        """
        INSERT INTO interactions
            (start_datetime, end_datetime, language, interaction_id, contact)
        VALUES
            ('2024-01-01T00:00:00Z', '2024-01-01T01:00:00Z', 'EN', 'existing0001', true)
        """
    )
    before = db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]

    payload = _csv_bytes([_valid_row("denydeny0001")])
    upload = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    ).json()
    upload_id = upload["upload_id"]

    response = client.post(f"/uploads/{upload_id}/deny")
    assert response.status_code == 200

    after = db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    assert after == before

    staging = f"staging_{upload_id}"
    exists = db_conn.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
        (staging,),
    ).fetchone()
    assert exists is None


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_approve_or_deny_missing_upload_returns_404(client):
    assert client.post("/uploads/missing-id/approve").status_code == 404
    assert client.post("/uploads/missing-id/deny").status_code == 404


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_approve_retries_only_unfinished_buckets(
    client,
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
):
    import api.db as db

    lower_id = _id_for_bucket(10)
    higher_id = _id_for_bucket(200)
    payload = _csv_bytes([_valid_row(lower_id), _valid_row(higher_id)])
    upload = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    ).json()
    upload_id = upload["upload_id"]

    original_upsert = db._upsert_bucket

    def fail_bucket(conn, table, bucket):
        if bucket == 128:
            raise RuntimeError("injected bucket failure")
        return original_upsert(conn, table, bucket)

    monkeypatch.setattr(db, "_upsert_bucket", fail_bucket)
    failed = client.post(f"/uploads/{upload_id}/approve")
    assert failed.status_code == 500

    assert db_conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE interaction_id = %s",
        (lower_id,),
    ).fetchone()[0] == 1
    assert db_conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE interaction_id = %s",
        (higher_id,),
    ).fetchone()[0] == 0
    statuses = dict(
        db_conn.execute(
            """
            SELECT bucket, status
            FROM upload_approval_progress
            WHERE upload_id = %s AND bucket IN (10, 128, 200)
            """,
            (upload_id,),
        ).fetchall()
    )
    assert statuses == {10: "completed", 128: "failed", 200: "pending"}

    retried_buckets = []

    def track_bucket(conn, table, bucket):
        retried_buckets.append(bucket)
        return original_upsert(conn, table, bucket)

    monkeypatch.setattr(db, "_upsert_bucket", track_bucket)
    retried = client.post(f"/uploads/{upload_id}/approve")
    assert retried.status_code == 200
    assert retried.json()["rows_upserted"] == 2
    assert 10 not in retried_buckets
    assert 128 in retried_buckets
    assert 200 in retried_buckets

    ids = db_conn.execute(
        "SELECT interaction_id FROM interactions ORDER BY interaction_id"
    ).fetchall()
    assert [row[0] for row in ids] == sorted([lower_id, higher_id])
    assert db_conn.execute(
        "SELECT COUNT(*) FROM upload_approval_progress WHERE upload_id = %s",
        (upload_id,),
    ).fetchone()[0] == 0


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_approve_does_not_rewrite_unchanged_conflicts(client):
    interaction_id = _id_for_bucket(42)
    payload = _csv_bytes([_valid_row(interaction_id)])

    first = client.post(
        "/uploads",
        files={"file": ("first.csv", payload, "text/csv")},
    ).json()
    assert client.post(f"/uploads/{first['upload_id']}/approve").status_code == 200

    second = client.post(
        "/uploads",
        files={"file": ("second.csv", payload, "text/csv")},
    ).json()
    response = client.post(f"/uploads/{second['upload_id']}/approve")
    assert response.status_code == 200
    assert response.json()["rows_upserted"] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_concurrent_approve_returns_409(client, db_conn):
    payload = _csv_bytes([_valid_row(_id_for_bucket(7))])
    upload = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    ).json()
    upload_id = upload["upload_id"]

    db_conn.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
        (upload_id,),
    )
    try:
        response = client.post(f"/uploads/{upload_id}/approve")
        assert response.status_code == 409
    finally:
        db_conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (upload_id,),
        )

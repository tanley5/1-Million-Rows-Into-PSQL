"""API upload / preview / sectioned approve / deny against real Postgres."""

from __future__ import annotations

import io

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


def test_api_app_importable():
    from api.main import app

    assert app is not None


@pytest.fixture
def client(database_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SECTION_MAX_ROWS", "2")
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_post_uploads_creates_staging_and_preview(client, db_conn):
    payload = _csv_bytes([_valid_row(f"{i:012x}") for i in range(12)])
    response = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()

    assert "upload_id" in body
    assert isinstance(body["sample"], list)
    assert 1 <= len(body["sample"]) <= 100
    assert body["section_count"] == 6
    assert body["section_max_rows"] == 2

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
    section_index = db_conn.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
          AND indexdef LIKE '%%section_id%%'
        """,
        (staging,),
    ).fetchone()
    assert section_index is not None
    section_ids = {
        row[0]
        for row in db_conn.execute(
            f'SELECT DISTINCT section_id FROM "{staging}"'
        ).fetchall()
    }
    assert section_ids == {0, 1, 2, 3, 4, 5}


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
    body = response.json()
    assert body["status"] == "approved"
    assert body["staging_dropped"] is True
    assert body["summary"]["completed"] == 1

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
    assert client.get("/uploads/missing-id/status").status_code == 404
    assert client.post("/uploads/missing-id/sections/0/approve").status_code == 404


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_approve_continues_after_section_failure_and_status_reports(
    client,
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
):
    import api.db as db

    payload = _csv_bytes(
        [
            _valid_row("aaaaaaaaaaaa"),
            _valid_row("bbbbbbbbbbbb"),
            _valid_row("cccccccccccc"),
            _valid_row("dddddddddddd"),
        ]
    )
    upload = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    ).json()
    upload_id = upload["upload_id"]
    assert upload["section_count"] == 2

    original_upsert = db._upsert_section

    def fail_section(conn, table, section_id):
        if section_id == 0:
            raise RuntimeError("injected section failure")
        return original_upsert(conn, table, section_id)

    monkeypatch.setattr(db, "_upsert_section", fail_section)
    failed = client.post(f"/uploads/{upload_id}/approve")
    assert failed.status_code == 207
    body = failed.json()
    assert body["status"] == "partial"
    assert body["staging_dropped"] is False
    assert body["summary"]["failed"] == 1
    assert body["summary"]["completed"] == 1

    assert (
        db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 2
    )

    status = client.get(f"/uploads/{upload_id}/status")
    assert status.status_code == 200
    statuses = {
        section["section_id"]: section["status"]
        for section in status.json()["sections"]
    }
    assert statuses == {0: "failed", 1: "completed"}

    monkeypatch.setattr(db, "_upsert_section", original_upsert)
    section_retry = client.post(f"/uploads/{upload_id}/sections/0/approve")
    assert section_retry.status_code == 200
    assert section_retry.json()["status"] == "approved"
    assert section_retry.json()["staging_dropped"] is True
    assert db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 4
    assert (
        db_conn.execute(
            "SELECT COUNT(*) FROM upload_approval_progress WHERE upload_id = %s",
            (upload_id,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_full_approve_retries_only_unfinished_sections(
    client,
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
):
    import api.db as db

    payload = _csv_bytes(
        [
            _valid_row("aaaaaaaaaaaa"),
            _valid_row("bbbbbbbbbbbb"),
            _valid_row("cccccccccccc"),
            _valid_row("dddddddddddd"),
        ]
    )
    upload = client.post(
        "/uploads",
        files={"file": ("sample.csv", payload, "text/csv")},
    ).json()
    upload_id = upload["upload_id"]

    original_upsert = db._upsert_section

    def fail_section_zero(conn, table, section_id):
        if section_id == 0:
            raise RuntimeError("injected section failure")
        return original_upsert(conn, table, section_id)

    monkeypatch.setattr(db, "_upsert_section", fail_section_zero)
    assert client.post(f"/uploads/{upload_id}/approve").status_code == 207

    retried = []

    def track_section(conn, table, section_id):
        retried.append(section_id)
        return original_upsert(conn, table, section_id)

    monkeypatch.setattr(db, "_upsert_section", track_section)
    response = client.post(f"/uploads/{upload_id}/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert 0 in retried
    assert 1 not in retried
    assert db_conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 4


@pytest.mark.usefixtures("ensure_interactions_table", "cleanup_staging")
def test_approve_does_not_rewrite_unchanged_conflicts(client, db_conn):
    interaction_id = "eeeeeeeeeeee"
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
    payload = _csv_bytes([_valid_row("ffffffff0001")])
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

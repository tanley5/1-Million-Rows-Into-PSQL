"""FastAPI app: CSV upload → clean → UNLOGGED staging → sectioned approve."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Response, UploadFile

from api.cleaning import clean_csv
from api.db import (
    ApprovalInProgressError,
    ApprovalSectionError,
    COLUMN_DATATYPES,
    approve_section,
    approve_staging,
    connect,
    copy_frame_to_staging,
    create_staging_section_index,
    create_unlogged_staging,
    deny_staging,
    ensure_interactions_table,
    fetch_sample_rows,
    get_upload_status,
    section_max_rows,
)
from api.stats import CleaningStats

app = FastAPI(title="CSV → Postgres pipeline")


@app.post("/uploads")
async def create_upload(file: UploadFile = File(...)) -> dict:
    upload_id = uuid.uuid4().hex
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"
    chunk_size = section_max_rows()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    try:
        stats = CleaningStats()
        with connect() as conn:
            ensure_interactions_table(conn)
            table = create_unlogged_staging(conn, upload_id)
            section_id = 0
            for frame in clean_csv(tmp_path, chunk_size=chunk_size, stats=stats):
                copy_frame_to_staging(conn, table, frame, section_id=section_id)
                section_id += 1
            create_staging_section_index(conn, table)
            sample = fetch_sample_rows(conn, table, limit=100)
    finally:
        tmp_path.unlink(missing_ok=True)

    return {
        "upload_id": upload_id,
        "sample": sample,
        "stats": stats.to_dict(),
        "columns": list(COLUMN_DATATYPES),
        "section_count": section_id,
        "section_max_rows": chunk_size,
    }


def _approve_http_status(payload: dict) -> int:
    if payload.get("status") == "approved" and payload.get("staging_dropped"):
        return 200
    return 207


@app.post("/uploads/{upload_id}/approve")
def approve_upload(upload_id: str, response: Response) -> dict:
    try:
        with connect() as conn:
            result = approve_staging(conn, upload_id)
    except ValueError:
        result = None
    except ApprovalInProgressError:
        raise HTTPException(
            status_code=409,
            detail="approval already in progress",
        )

    if result is None:
        raise HTTPException(status_code=404, detail="upload not found")
    response.status_code = _approve_http_status(result)
    return result


@app.get("/uploads/{upload_id}/status")
def upload_status(upload_id: str) -> dict:
    try:
        with connect() as conn:
            result = get_upload_status(conn, upload_id)
    except ValueError:
        result = None

    if result is None:
        raise HTTPException(status_code=404, detail="upload not found")
    return result


@app.post("/uploads/{upload_id}/sections/{section_id}/approve")
def approve_upload_section(
    upload_id: str,
    section_id: int,
    response: Response,
) -> dict:
    try:
        with connect() as conn:
            result = approve_section(conn, upload_id, section_id)
    except ValueError:
        result = None
    except ApprovalInProgressError:
        raise HTTPException(
            status_code=409,
            detail="approval already in progress",
        )
    except ApprovalSectionError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "section approve failed; retry this section",
                "section_id": exc.section_id,
            },
        )

    if result is None:
        raise HTTPException(status_code=404, detail="upload or section not found")
    response.status_code = _approve_http_status(result)
    return result


@app.post("/uploads/{upload_id}/deny")
def deny_upload(upload_id: str) -> dict:
    try:
        with connect() as conn:
            dropped = deny_staging(conn, upload_id)
    except ValueError:
        dropped = False
    except ApprovalInProgressError:
        raise HTTPException(
            status_code=409,
            detail="approval already in progress",
        )

    if not dropped:
        raise HTTPException(status_code=404, detail="upload not found")
    return {"upload_id": upload_id, "status": "denied"}

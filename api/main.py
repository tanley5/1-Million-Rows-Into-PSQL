"""FastAPI app: CSV upload → clean → UNLOGGED staging → preview."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from api.cleaning import clean_csv
from api.db import (
    ApprovalBucketError,
    ApprovalInProgressError,
    COLUMN_DATATYPES,
    approve_staging,
    connect,
    copy_frame_to_staging,
    create_staging_bucket_index,
    create_unlogged_staging,
    deny_staging,
    ensure_interactions_table,
    fetch_sample_rows,
)
from api.stats import CleaningStats

DEFAULT_CHUNK_SIZE = 100_000

app = FastAPI(title="CSV → Postgres pipeline")


@app.post("/uploads")
async def create_upload(file: UploadFile = File(...)) -> dict:
    upload_id = uuid.uuid4().hex
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"

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
            for frame in clean_csv(tmp_path, chunk_size=DEFAULT_CHUNK_SIZE, stats=stats):
                copy_frame_to_staging(conn, table, frame)
            create_staging_bucket_index(conn, table)
            sample = fetch_sample_rows(conn, table, limit=100)
    finally:
        tmp_path.unlink(missing_ok=True)

    return {
        "upload_id": upload_id,
        "sample": sample,
        "stats": stats.to_dict(),
        "columns": list(COLUMN_DATATYPES),
    }


@app.post("/uploads/{upload_id}/approve")
def approve_upload(upload_id: str) -> dict:
    try:
        with connect() as conn:
            affected = approve_staging(conn, upload_id)
    except ValueError:
        affected = None
    except ApprovalInProgressError:
        raise HTTPException(
            status_code=409,
            detail="approval already in progress",
        )
    except ApprovalBucketError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "approval bucket failed; retry this upload",
                "bucket": exc.bucket,
            },
        )

    if affected is None:
        raise HTTPException(status_code=404, detail="upload not found")
    return {"upload_id": upload_id, "status": "approved", "rows_upserted": affected}


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

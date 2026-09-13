"""FastAPI app: CSV upload → clean → UNLOGGED staging → preview."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile

from api.cleaning import clean_csv
from api.db import (
    COLUMN_DATATYPES,
    connect,
    copy_frame_to_staging,
    create_unlogged_staging,
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
            sample = fetch_sample_rows(conn, table, limit=100)
    finally:
        tmp_path.unlink(missing_ok=True)

    return {
        "upload_id": upload_id,
        "sample": sample,
        "stats": stats.to_dict(),
        "columns": list(COLUMN_DATATYPES),
    }

# 1-Million-Rows-Into-PSQL

Load large CSV uploads into Postgres efficiently: generate sample data → clean in
Polars chunks → `COPY` into UNLOGGED staging (≤100k-row sections) → preview →
approve (continue-on-failure + per-section retry) or deny.

## Prerequisites

- Docker Desktop (Compose)
- Python 3.9+ with a local venv (for pytest / sample generation)

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## 1. Start the stack

From repo root on `main`:

```bash
chmod +x scripts/compose_validate.sh
./scripts/compose_validate.sh
```

Or manually:

```bash
docker compose up --build -d
curl -fsS http://localhost:8000/healthz
curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:5678/healthz
```

| Service  | URL |
|----------|-----|
| FastAPI  | http://localhost:8000/docs |
| n8n      | http://localhost:5678 |
| Postgres | `localhost:5432` (`csv_pipeline` / `csv_pipeline_test`) |

## 2. Run tests

```bash
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/csv_pipeline_test \
  .venv/bin/pytest -q
```

## 3. Generate a sample CSV

```bash
# smoke
.venv/bin/python scripts/generate_sample_csv.py --rows 20 --out data/sample_20.csv

# 1M (happy-path scale)
.venv/bin/python scripts/generate_sample_csv.py --rows 1000000 --out data/sample_1m.csv
```

## 4. Import and activate n8n forms

Detailed steps: [n8n/workflows/README.md](n8n/workflows/README.md).

1. Open http://localhost:5678
2. **Import from File**:
   - `n8n/workflows/upload-preview.draft.json`
   - `n8n/workflows/approve-deny.draft.json`
3. **Activate** both workflows (required for production form URLs).
4. Use **Production** URLs only (not Test / `form-test`):
   - Upload: http://localhost:5678/form/csv-upload
   - Decide: http://localhost:5678/form/csv-decide?upload_id=`<id>`

Test vs production URLs are different paths. If the upload completion link 404s, you are on a Test URL or the decide workflow is inactive.

Env already set on the n8n Compose service: `CSV_PIPELINE_API_URL=http://api:8000`, `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.

### Form size limit

n8n rejects very large multipart uploads (default form-file cap ~200 MiB) with HTTP **413** before the API runs. For huge files (e.g. 10M rows), either raise `N8N_FORMDATA_FILE_SIZE_MAX` / `N8N_PAYLOAD_SIZE_MAX` on the n8n service, or bypass the form:

```bash
curl -F "file=@data/sample_1m.csv" http://localhost:8000/uploads
```

Then open the decide form with the returned `upload_id`.

## 5. Happy path

1. Upload a CSV at `/form/csv-upload`.
2. Review HTML preview (stats, columns, sample) → **Continue to Approve / Deny**.
3. Choose **Approve All**, **Deny**, or **Retry Section**.
4. Confirm result status (`approved` / `partial` / `denied`).

## API map

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/uploads` | Multipart `file` → staging + preview |
| `GET` | `/uploads/{id}/status` | Per-section progress |
| `POST` | `/uploads/{id}/approve` | All unfinished sections; **200** = done + staging dropped; **207** = partial (retry) |
| `POST` | `/uploads/{id}/sections/{section_id}/approve` | Retry one failed section |
| `POST` | `/uploads/{id}/deny` | Drop staging; production unchanged |
| `GET` | `/healthz` | API health |

Also: **404** unknown upload, **409** approve already in progress.

## Ops

```bash
# stop containers, keep volumes
docker compose stop

# stop and remove containers (volumes kept unless -v)
docker compose down

# wipe Compose volumes (destructive)
docker compose down -v
```

Clear production rows and leftover staging during demos:

```bash
docker compose exec -T postgres psql -U postgres -d csv_pipeline <<'SQL'
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename LIKE 'staging_%'
  LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename);
  END LOOP;
END $$;
TRUNCATE TABLE interactions;
TRUNCATE TABLE upload_approval_progress;  -- ignore if missing
SQL
```

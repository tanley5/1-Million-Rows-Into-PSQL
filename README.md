# 1-Million-Rows-Into-PSQL

Load large CSV uploads into Postgres efficiently: generate sample data → clean in
Polars chunks → `COPY` into UNLOGGED staging (≤100k-row sections) → preview →
approve (continue-on-failure + per-section retry) or deny.

## Local stack (Compose)

```bash
# from repo root, on branch compose-stack
chmod +x scripts/compose_validate.sh
./scripts/compose_validate.sh
```

Or manually:

```bash
docker compose up --build -d
curl -fsS http://localhost:8000/healthz
curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:5678/healthz
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/csv_pipeline_test \
  .venv/bin/pytest -q
```

Services:

| Service  | URL |
|----------|-----|
| FastAPI  | http://localhost:8000/docs |
| n8n      | http://localhost:5678 |
| Postgres | `localhost:5432` (`csv_pipeline` / `csv_pipeline_test`) |

Stop without deleting volumes: `docker compose stop`.

## Sample CSV

```bash
python scripts/generate_sample_csv.py --rows 1000000 --out data/sample_1m.csv
```

## n8n forms

Import skeletons from `n8n/workflows/` after the stack is healthy. Do not build
Form nodes until Compose validation is green (see `n8n/workflows/README.md`).

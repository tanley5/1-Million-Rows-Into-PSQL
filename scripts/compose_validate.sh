#!/usr/bin/env bash
# Validate the local Compose stack + Postgres integration tests.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="${HOME}/.docker/bin:/usr/local/bin:/opt/homebrew/bin:${PATH}"

wait_healthy() {
  local service="$1"
  local tries=60
  echo -n "    waiting for ${service}"
  for _ in $(seq 1 "$tries"); do
    local health
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(docker compose ps -q "$service")" 2>/dev/null || true)"
    if [[ "$health" == "healthy" || "$health" == "running" ]]; then
      if [[ "$health" == "healthy" ]]; then
        echo " -> healthy"
        return 0
      fi
    fi
    echo -n "."
    sleep 2
  done
  echo
  echo "ERROR: ${service} did not become healthy in time" >&2
  docker compose ps "$service" || true
  docker compose logs --tail=80 "$service" || true
  return 1
}

echo "==> Compose config"
docker compose config -q

echo "==> Build and start stack"
docker compose up --build -d

echo "==> Wait for healthy services"
wait_healthy postgres
wait_healthy api
wait_healthy n8n

echo "==> Smoke checks"
curl -fsS "http://localhost:8000/healthz"
echo
curl -fsS -o /dev/null -w "n8n healthz HTTP %{http_code}\n" "http://localhost:5678/healthz"

echo "==> Pytest against csv_pipeline_test"
if [[ -x .venv/bin/pytest ]]; then
  PYTEST=.venv/bin/pytest
else
  PYTEST=pytest
fi
TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/csv_pipeline_test" \
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/csv_pipeline_test" \
"$PYTEST" -q

echo "==> Done. Stack left running. Stop with: docker compose stop"

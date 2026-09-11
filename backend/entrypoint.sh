#!/usr/bin/env bash
# Container entrypoint: migrate, then serve.
#
# `set -e` matters here. If the migration fails, the container must die loudly
# rather than start an app against a schema it does not match — a half-migrated
# database serving traffic is worse than a deploy that visibly failed.
set -euo pipefail

echo "Running database migrations…"
alembic upgrade head

echo "Starting linx on port ${PORT:-8000}…"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers

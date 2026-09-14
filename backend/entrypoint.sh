#!/usr/bin/env bash
# Container entrypoint: check the environment, wait for the database, migrate,
# then serve.
#
# `set -e` matters here. If any step fails, the container must die loudly rather
# than start an app against a schema it does not match — a half-migrated
# database serving traffic is worse than a deploy that visibly failed.
set -euo pipefail

# Names the variable that is wrong, and rides out a database that is not
# accepting connections yet. Without it, both of those arrive as a raw
# traceback, which on a hosted deploy is the only thing anyone gets to see.
python -m app.preflight

echo "Running database migrations…"
alembic upgrade head

echo "Starting linx on port ${PORT:-8000}…"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers

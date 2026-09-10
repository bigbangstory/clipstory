#!/usr/bin/env bash
# Local test runner. Starts nothing; expects Postgres to be reachable.
# Set TEST_DATABASE_URL to enable the integration tests.
set -a
DATABASE_URL="${DATABASE_URL:-postgresql://clipstory:clipstory@localhost:5432/clipstory}"
TEST_DATABASE_URL="${TEST_DATABASE_URL:-$DATABASE_URL}"
SECRET_KEY="${SECRET_KEY:-local-development-secret-key-not-for-production-use}"
ADMIN_EMAILS="${ADMIN_EMAILS:-admin@example.com}"
DATA_DIR="${DATA_DIR:-./.test-data}"
BASE_URL="${BASE_URL:-http://testserver}"
set +a
rm -rf "$DATA_DIR"
exec python3 -m pytest "$@"

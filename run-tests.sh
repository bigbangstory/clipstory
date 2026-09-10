#!/usr/bin/env bash
# Local test runner. Expects Postgres to be reachable at DATABASE_URL.
# Set TEST_DATABASE_URL to enable the integration tests.
#
# Both model providers are disabled here on purpose: the suite must never
# download Whisper weights or call a language model. Tests that need a
# transcript or suggestions inject fake providers.
set -a
DATABASE_URL="${DATABASE_URL:-postgresql://clipstory:clipstory@localhost:5432/clipstory}"
TEST_DATABASE_URL="${TEST_DATABASE_URL:-$DATABASE_URL}"
SECRET_KEY="${SECRET_KEY:-local-development-secret-key-not-for-production-use}"
ADMIN_EMAILS="${ADMIN_EMAILS:-admin@example.com}"
DATA_DIR="${DATA_DIR:-./.test-data}"
BASE_URL="${BASE_URL:-http://testserver}"
TRANSCRIPTION_PROVIDER=disabled
SUGGEST_PROVIDER=disabled
set +a
rm -rf "$DATA_DIR"
exec python3 -m pytest "$@"

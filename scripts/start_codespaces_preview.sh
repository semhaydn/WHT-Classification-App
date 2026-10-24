#!/usr/bin/env bash
# Development-only preview server for GitHub Codespaces.
# Does not create, migrate, seed, reset, or copy any database.
set -euo pipefail

PORT=8001

if [[ "${CODESPACES:-}" != "true" ]]; then
    echo "Error: this script runs only in GitHub Codespaces (CODESPACES is not 'true')." >&2
    exit 1
fi
if [[ -z "${CODESPACE_NAME:-}" || -z "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]]; then
    echo "Error: CODESPACE_NAME and GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN must be set." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f manage.py ]]; then
    echo "Error: manage.py not found in ${REPO_ROOT}." >&2
    exit 1
fi

if ss -ltn "sport = :${PORT}" | grep -q LISTEN; then
    echo "Error: port ${PORT} is already in use. Stop the existing server (Ctrl+C in its terminal) and retry." >&2
    exit 1
fi

if [[ -x .venv/bin/python ]]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python"
fi

FORWARDED_HOST="${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"

export DJANGO_ENVIRONMENT=development
export DJANGO_DEBUG=true
export DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost,${FORWARDED_HOST}"
export DJANGO_CSRF_TRUSTED_ORIGINS="https://${FORWARDED_HOST},https://localhost:${PORT}"

echo "Starting development preview at https://${FORWARDED_HOST}/"
echo "Keep port ${PORT} set to Private. Stop with Ctrl+C."
exec "$PYTHON" manage.py runserver "0.0.0.0:${PORT}"

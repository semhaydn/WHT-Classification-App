#!/usr/bin/env bash
set -euo pipefail

demo_mode="${HEROKU_DEMO_MODE:-false}"
if [[ "${demo_mode,,}" == "true" ]]; then
    python manage.py bootstrap_heroku_demo
fi

exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 1

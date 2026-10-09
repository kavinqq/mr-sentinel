#!/bin/sh
# Start the review-history dashboard on http://127.0.0.1:8765 (this machine only).
# First run creates the venv and Django's own db; then: ./run.sh createsuperuser
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    if command -v uv >/dev/null 2>&1; then uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
    else python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; fi
fi
.venv/bin/python manage.py migrate -v 0
if [ "$1" = "createsuperuser" ]; then exec .venv/bin/python manage.py createsuperuser; fi
# --insecure only serves the admin's static files; nothing listens beyond 127.0.0.1
exec .venv/bin/python manage.py runserver 127.0.0.1:8765 --insecure

#!/bin/sh
set -e

# Start the background scheduler in the background if SCHEDULE_HOURS is set
if [ "${SCHEDULE_HOURS:-0}" != "0" ]; then
  python scheduler.py &
fi

# Start the WebUI
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

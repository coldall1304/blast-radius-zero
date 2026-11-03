#!/bin/bash
# Keep the public demo answering.
#
# The app unit already restarts on crash, but not when the process is alive and
# not serving (wedged event loop, saturated worker, broken bind). This probe
# checks the HTTP surface every ten minutes and restarts the unit when it fails.
set -u

URL="http://127.0.0.1:${BRZ_PORT:-8000}/api/health"

if curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; then
  logger -t brz-health "ok"
  exit 0
fi

logger -t brz-health "health check FAILED ($URL) — restarting ${BRZ_SERVICE:-brz}"
systemctl restart "${BRZ_SERVICE:-brz}"
sleep 8

if curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; then
  logger -t brz-health "recovered after restart"
  exit 0
fi

logger -t brz-health "still unhealthy after restart"
exit 1

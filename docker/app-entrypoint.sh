#!/bin/sh
set -eu

case "${1:-web}" in
  web)
    alembic upgrade head
    exec uvicorn --factory app.main:build_app \
      --host 0.0.0.0 --port 8000 \
      --proxy-headers --forwarded-allow-ips "*" \
      --no-server-header --log-level warning
    ;;
  worker)
    exec python -m app.worker.main
    ;;
  *)
    exec "$@"
    ;;
esac

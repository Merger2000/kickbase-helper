#!/bin/sh
# Reads the IONOS API token from the Docker secret (never from the environment file)
# and hands it to Caddy for the ACME DNS-01 challenge.
set -eu

secret_file=/run/secrets/ionos_token
if [ ! -r "$secret_file" ]; then
  echo "Docker secret ionos_token fehlt oder ist nicht lesbar" >&2
  exit 1
fi

IONOS_API_TOKEN="$(tr -d ' \t\r\n' < "$secret_file")"
case "$IONOS_API_TOKEN" in
  ?*.?*) ;;
  *)
    echo "Docker secret ionos_token ist leer oder nicht im Format prefix.secret" >&2
    exit 1
    ;;
esac
export IONOS_API_TOKEN

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile

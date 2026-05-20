#!/bin/bash
# PMKID crack server launcher. Sleeps forever when disabled so supervisord
# doesn't keep flapping the program.
set -euo pipefail

CONFIG="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/config.json"
STATE_DIR="${HELPER_STATE_DIR:-/var/lib/vasili-helper}"
CRACK_CONF="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/crack-server.json"

ENABLED=$(jq -r '.crack.enabled // false' "$CONFIG")
DOMAIN=$(jq  -r '.crack.domain // ""'    "$CONFIG")
SECRET=$(jq  -r '.crack.secret // ""'    "$CONFIG")
WORDLIST=$(jq -r '.crack.wordlist // ""' "$CONFIG")

if [[ "$ENABLED" != "true" ]] || [[ -z "$DOMAIN" ]] || [[ -z "$SECRET" ]]; then
    echo "[crack-server] disabled (enabled=$ENABLED domain='$DOMAIN' secret-set=$([ -n "$SECRET" ] && echo y || echo n))"
    exec sleep infinity
fi

cat > "$CRACK_CONF" <<JSON
{
  "listen": "127.0.0.1",
  "port": 5353,
  "domain": "$DOMAIN",
  "secret": "$SECRET",
  "wordlist": "$WORDLIST"
}
JSON

mkdir -p /etc/vasili "$STATE_DIR"
ln -sf "$CRACK_CONF" /etc/vasili/crack-server.json
# crack-server.py hard-codes /etc/vasili/crack-jobs.db; symlink it into
# the persistent state dir so jobs survive container restarts.
if [[ ! -e /etc/vasili/crack-jobs.db ]]; then
    touch "$STATE_DIR/crack-jobs.db"
    ln -sf "$STATE_DIR/crack-jobs.db" /etc/vasili/crack-jobs.db
fi

exec /usr/bin/python3 /opt/vasili/server/vasili-crack-server.py

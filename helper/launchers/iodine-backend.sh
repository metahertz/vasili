#!/bin/bash
# iodine launcher — listens on 127.0.0.1:5354 so the dns-proxy can route
# *.iodine_domain queries to it. Idle-sleeps when disabled.
set -euo pipefail

CONFIG="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/config.json"

ENABLED=$(jq  -r '.iodine.enabled  // false' "$CONFIG")
DOMAIN=$(jq   -r '.iodine.domain   // ""'    "$CONFIG")
PASSWORD=$(jq -r '.iodine.password // ""'    "$CONFIG")
SUBNET=$(jq   -r '.iodine.subnet   // "10.53.53.1/24"' "$CONFIG")

if [[ "$ENABLED" != "true" ]] || [[ -z "$DOMAIN" ]] || [[ -z "$PASSWORD" ]]; then
    echo "[iodine-backend] disabled (enabled=$ENABLED domain='$DOMAIN' password-set=$([ -n "$PASSWORD" ] && echo y || echo n))"
    exec sleep infinity
fi

echo "[iodine-backend] starting iodined on 127.0.0.1:5354 for $DOMAIN"
exec /usr/sbin/iodined -f -c -P "$PASSWORD" \
     -p 5354 -l 127.0.0.1 "$SUBNET" "$DOMAIN"

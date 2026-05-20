#!/bin/bash
# DNS proxy launcher — UDP/53 multiplexer in front of crack, iodine, and
# WireGuard. Always runs (whether or not anything is behind it); the
# proxy itself drops packets whose backend isn't configured.
set -euo pipefail

CONFIG="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/config.json"
PROXY_CONF="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/dns-proxy.json"

CRACK_DOMAIN=$(jq -r '.crack.domain  // ""' "$CONFIG")
IODINE_DOMAIN=$(jq -r '.iodine.domain // ""' "$CONFIG")

cat > "$PROXY_CONF" <<JSON
{
  "listen": "0.0.0.0",
  "port": 53,
  "crack_backend":     "127.0.0.1:5353",
  "iodine_backend":    "127.0.0.1:5354",
  "wireguard_backend": "127.0.0.1:5355",
  "crack_domain":  "$CRACK_DOMAIN",
  "iodine_domain": "$IODINE_DOMAIN"
}
JSON

mkdir -p /etc/vasili
ln -sf "$PROXY_CONF" /etc/vasili/dns-proxy.json

exec /usr/bin/python3 /opt/vasili/server/vasili-dns-proxy.py

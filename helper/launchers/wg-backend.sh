#!/bin/bash
# WireGuard launcher — listens on 127.0.0.1:5355 so the dns-proxy can
# route WG-shaped packets here. The proxy classifies by packet
# signature (type byte + length), not domain, so WG and iodine share
# UDP/53 cleanly.
set -euo pipefail

CONFIG="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/config.json"
STATE_DIR="${HELPER_STATE_DIR:-/var/lib/vasili-helper}"

ENABLED=$(jq -r '.wireguard.enabled // false' "$CONFIG")

if [[ "$ENABLED" != "true" ]]; then
    echo "[wg-backend] disabled"
    exec sleep infinity
fi

SUBNET=$(jq -r '.wireguard.subnet // "10.53.1.0/24"' "$CONFIG")
CLIENT_PUB=$(jq -r '.wireguard.client_pubkey // ""' "$CONFIG")
SRV_PRIV_FILE="$STATE_DIR/wg_server_private"
SRV_PUB_FILE="$STATE_DIR/wg_server_public"
if [[ ! -f "$SRV_PRIV_FILE" ]]; then
    umask 077
    wg genkey | tee "$SRV_PRIV_FILE" | wg pubkey > "$SRV_PUB_FILE"
fi
SRV_PRIV=$(cat "$SRV_PRIV_FILE")

WG_CONF=/etc/wireguard/wg-vasili.conf
SERVER_IP=$(echo "$SUBNET" | sed 's|/.*$||' | awk -F. '{print $1"."$2"."$3".1"}')
{
    echo "[Interface]"
    echo "PrivateKey = $SRV_PRIV"
    echo "Address = $SERVER_IP/24"
    # Bound to 5355 so dns-proxy fronts WG on UDP/53.
    echo "ListenPort = 5355"
    if [[ -n "$CLIENT_PUB" ]]; then
        CLIENT_IP=$(echo "$SUBNET" | sed 's|/.*$||' | awk -F. '{print $1"."$2"."$3".2"}')
        echo ""
        echo "[Peer]"
        echo "PublicKey = $CLIENT_PUB"
        echo "AllowedIPs = $CLIENT_IP/32"
    fi
} > "$WG_CONF"
chmod 600 "$WG_CONF"

echo "[wg-backend] starting wireguard on 127.0.0.1:5355"
wg-quick down wg-vasili 2>/dev/null || true
wg-quick up wg-vasili
# wg-quick is one-shot; keep supervisord happy until something stops us.
exec sleep infinity

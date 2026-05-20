#!/bin/bash
# vasili-helper container entrypoint.
#
# First-boot housekeeping, then exec supervisord. Re-runnable.
set -euo pipefail

CONFIG_DIR="${HELPER_CONFIG_DIR:-/etc/vasili-helper}"
STATE_DIR="${HELPER_STATE_DIR:-/var/lib/vasili-helper}"
CONFIG_FILE="$CONFIG_DIR/config.json"

mkdir -p "$CONFIG_DIR" "$STATE_DIR" /var/log/vasili-helper

# Enable IP forwarding so the tunnel backends can NAT outbound traffic.
# Fails silently when running unprivileged (e.g. UI smoke test) — the
# tunnel programs will refuse to forward but the UI is still usable.
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

# Generate a token on first boot if one wasn't supplied via env.
if [[ -z "${HELPER_TOKEN:-}" ]]; then
    HELPER_TOKEN="$(openssl rand -hex 16)"
    echo "[entrypoint] generated HELPER_TOKEN=$HELPER_TOKEN"
fi
export HELPER_TOKEN

# Bootstrap config.json on first run.
if [[ ! -f "$CONFIG_FILE" ]]; then
    cat > "$CONFIG_FILE" <<JSON
{
  "public_ip": "${PUBLIC_IP:-auto}",
  "auth_token": "$HELPER_TOKEN",
  "ssh":       { "enabled": false },
  "iodine":    { "enabled": false, "domain": "", "password": "", "subnet": "10.53.53.1/24" },
  "wireguard": { "enabled": false, "subnet": "10.53.1.0/24", "client_pubkey": "" },
  "crack":     { "enabled": false, "domain": "", "secret": "",
                 "wordlist": "$STATE_DIR/rockyou.txt" }
}
JSON
    echo "[entrypoint] wrote initial $CONFIG_FILE"
else
    # Keep auth_token in sync with the env var so operators can rotate
    # by restarting the container with a new HELPER_TOKEN.
    if command -v jq >/dev/null 2>&1; then
        tmp=$(mktemp)
        jq --arg t "$HELPER_TOKEN" '.auth_token = $t' "$CONFIG_FILE" > "$tmp" && mv "$tmp" "$CONFIG_FILE"
    fi
fi

echo "[entrypoint] helper UI will be on :8080  (token: $HELPER_TOKEN)"
echo "[entrypoint] config at $CONFIG_FILE  state at $STATE_DIR"

exec "$@"

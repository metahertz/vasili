#!/bin/bash
# SSH-on-TCP-53 launcher. Runs a dedicated sshd with a config that listens
# on :53 and only accepts the helper-managed client keypair.
set -euo pipefail

CONFIG="${HELPER_CONFIG_DIR:-/etc/vasili-helper}/config.json"
STATE_DIR="${HELPER_STATE_DIR:-/var/lib/vasili-helper}"

ENABLED=$(jq -r '.ssh.enabled // false' "$CONFIG")

if [[ "$ENABLED" != "true" ]]; then
    echo "[ssh-tunnel] disabled"
    exec sleep infinity
fi

CLIENT_KEY="$STATE_DIR/ssh_client_key"
if [[ ! -f "$CLIENT_KEY" ]]; then
    ssh-keygen -t ed25519 -f "$CLIENT_KEY" -N "" -C "vasili-client" >/dev/null
fi
mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
PUBKEY=$(cat "$CLIENT_KEY.pub")
if ! grep -qF "$PUBKEY" /root/.ssh/authorized_keys; then
    echo "$PUBKEY" >> /root/.ssh/authorized_keys
fi

SSHD_CONF=/etc/ssh/vasili-sshd-53.conf
cat > "$SSHD_CONF" <<'EOF'
Port 53
PermitTunnel yes
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
AllowTcpForwarding yes
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/vasili-sshd-53.pid
EOF

mkdir -p /run/sshd
echo "[ssh-tunnel] starting sshd on TCP/53"
exec /usr/sbin/sshd -D -e -f "$SSHD_CONF"

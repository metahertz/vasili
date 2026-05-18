#!/usr/bin/env bash
# ============================================================================
# Vasili Server Setup
# Configure tunnel endpoints so Vasili clients can reach the internet
# through captive portals via port 53 (DNS).
#
# Architecture:
#   TCP/53 — SSH tun-mode VPN (always installed)
#   UDP/53 — choose ONE of: iodine (DNS tunnel) or WireGuard (VPN)
#
# Run as root on an Ubuntu server with a public IP.
# Re-runnable and idempotent.
# ============================================================================
set -euo pipefail

VASILI_DIR="/etc/vasili"
VASILI_CONF="$VASILI_DIR/server.conf"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ============================================================================
# Helpers
# ============================================================================

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}=== $* ===${NC}\n"; }

ensure_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This script must be run as root (use sudo)"
        exit 1
    fi
}

prompt_with_default() {
    local prompt="$1" default="$2" var
    read -rp "$(echo -e "${CYAN}$prompt${NC} [${default}]: ")" var
    echo "${var:-$default}"
}

detect_public_ip() {
    local ip
    ip=$(curl -s --connect-timeout 5 ifconfig.me 2>/dev/null) || \
    ip=$(curl -s --connect-timeout 5 icanhazip.com 2>/dev/null) || \
    ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}') || \
    ip="<YOUR_SERVER_IP>"
    echo "$ip"
}

ensure_ip_forwarding() {
    local current
    current=$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)
    if [[ "$current" != "1" ]]; then
        log "Enabling IPv4 forwarding"
        sysctl -w net.ipv4.ip_forward=1 >/dev/null
    fi
    if [[ ! -f /etc/sysctl.d/99-vasili.conf ]] || ! grep -q ip_forward /etc/sysctl.d/99-vasili.conf 2>/dev/null; then
        echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-vasili.conf
        log "Persisted ip_forward in /etc/sysctl.d/99-vasili.conf"
    fi
}

# Add NAT masquerade for a given source subnet on the default outbound interface
ensure_nat() {
    local subnet="$1"
    local out_iface
    out_iface=$(ip -4 route show default | awk '{print $5; exit}')
    if [[ -z "$out_iface" ]]; then
        warn "Could not detect default route interface — NAT not configured"
        return 1
    fi
    # Check if rule already exists
    if ! iptables -t nat -C POSTROUTING -s "$subnet" -o "$out_iface" -j MASQUERADE 2>/dev/null; then
        iptables -t nat -A POSTROUTING -s "$subnet" -o "$out_iface" -j MASQUERADE
        log "NAT masquerade: $subnet -> $out_iface"
    fi
    # Forward rules
    if ! iptables -C FORWARD -s "$subnet" -j ACCEPT 2>/dev/null; then
        iptables -A FORWARD -s "$subnet" -j ACCEPT
    fi
    if ! iptables -C FORWARD -d "$subnet" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; then
        iptables -A FORWARD -d "$subnet" -m state --state RELATED,ESTABLISHED -j ACCEPT
    fi
    # Persist
    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save 2>/dev/null || true
    elif command -v iptables-save &>/dev/null; then
        iptables-save > /etc/iptables.rules 2>/dev/null || true
    fi
}

check_port53_conflict() {
    local proto="$1"  # tcp or udp
    local conflict
    conflict=$(ss -lnp "${proto:0:1}" 2>/dev/null | grep ":53 " | head -1) || true
    if [[ -n "$conflict" ]]; then
        warn "Something is already listening on ${proto^^}/53:"
        echo "  $conflict"
        if echo "$conflict" | grep -q "systemd-resolve"; then
            warn "systemd-resolved uses port 53. Disable stub listener?"
            local ans
            ans=$(prompt_with_default "Disable systemd-resolved stub listener? (y/n)" "y")
            if [[ "$ans" == "y" ]]; then
                mkdir -p /etc/systemd/resolved.conf.d
                cat > /etc/systemd/resolved.conf.d/vasili-no-stub.conf <<'RESOLVEDEOF'
[Resolve]
DNSStubListener=no
RESOLVEDEOF
                systemctl restart systemd-resolved
                log "systemd-resolved stub listener disabled"
                return 0
            fi
        fi
        return 1
    fi
    return 0
}

save_conf() {
    local key="$1" value="$2"
    mkdir -p "$VASILI_DIR"
    touch "$VASILI_CONF"
    # Remove existing key, then append
    sed -i "/^${key}=/d" "$VASILI_CONF"
    echo "${key}=${value}" >> "$VASILI_CONF"
}

load_conf() {
    local key="$1" default="${2:-}"
    if [[ -f "$VASILI_CONF" ]]; then
        local val
        val=$(grep "^${key}=" "$VASILI_CONF" 2>/dev/null | tail -1 | cut -d= -f2-)
        echo "${val:-$default}"
    else
        echo "$default"
    fi
}

# ============================================================================
# SSH on TCP/53
# ============================================================================

setup_ssh() {
    hdr "SSH Tunnel Server (TCP/53)"

    # Install openssh-server if needed
    if ! dpkg -l openssh-server &>/dev/null; then
        log "Installing openssh-server..."
        apt-get update -qq && apt-get install -y -qq openssh-server
    fi

    local public_ip
    public_ip=$(detect_public_ip)
    log "Detected public IP: $public_ip"
    save_conf "PUBLIC_IP" "$public_ip"

    # Check TCP/53 conflict
    if ! check_port53_conflict "tcp"; then
        warn "TCP/53 conflict detected — SSH setup may fail"
    fi

    # Write dedicated sshd config for port 53
    log "Writing /etc/ssh/vasili-sshd-53.conf"
    cat > /etc/ssh/vasili-sshd-53.conf <<'SSHDEOF'
# Vasili SSH tunnel server — TCP port 53
Port 53
PermitTunnel yes
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
AllowTcpForwarding yes
Subsystem sftp /usr/lib/openssh/sftp-server
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/vasili-sshd-53.pid
SSHDEOF

    # Generate client keypair
    mkdir -p "$VASILI_DIR"
    if [[ ! -f "$VASILI_DIR/ssh_client_key" ]]; then
        log "Generating SSH client keypair"
        ssh-keygen -t ed25519 -f "$VASILI_DIR/ssh_client_key" -N "" -C "vasili-client"
        chmod 600 "$VASILI_DIR/ssh_client_key"
    else
        log "SSH client keypair already exists"
    fi

    # Add client public key to authorized_keys
    local pubkey
    pubkey=$(cat "$VASILI_DIR/ssh_client_key.pub")
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    touch /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    if ! grep -qF "$pubkey" /root/.ssh/authorized_keys 2>/dev/null; then
        echo "$pubkey" >> /root/.ssh/authorized_keys
        log "Added vasili client key to /root/.ssh/authorized_keys"
    fi

    # Write tun-up script for server side
    cat > "$VASILI_DIR/ssh-tun-up.sh" <<'TUNEOF'
#!/bin/bash
# Called after SSH tun53 device appears — configure server endpoint
TUN=tun53
ip addr add 10.53.0.1/32 peer 10.53.0.2 dev $TUN 2>/dev/null || true
ip link set $TUN up 2>/dev/null || true
TUNEOF
    chmod +x "$VASILI_DIR/ssh-tun-up.sh"

    # Create systemd service
    cat > /etc/systemd/system/vasili-sshd-53.service <<'SVCEOF'
[Unit]
Description=Vasili SSH tunnel server (TCP/53)
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/sshd -D -f /etc/ssh/vasili-sshd-53.conf
ExecStartPost=/etc/vasili/ssh-tun-up.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable vasili-sshd-53.service
    systemctl restart vasili-sshd-53.service
    log "vasili-sshd-53 service started"

    ensure_ip_forwarding
    ensure_nat "10.53.0.0/24"

    save_conf "SSH_ENABLED" "true"
    save_conf "SSH_USER" "root"
    save_conf "SSH_KEY" "$VASILI_DIR/ssh_client_key"

    log "SSH tunnel server ready on TCP/53"
    echo ""
    warn "Copy the private key to your Vasili Pi:"
    echo "  scp $VASILI_DIR/ssh_client_key pi@<your-pi>:/etc/vasili/"
}

# ============================================================================
# Iodine on UDP/53
# ============================================================================

setup_iodine() {
    hdr "Iodine DNS Tunnel Server (UDP/53)"

    # Check for WireGuard conflict
    if systemctl is-active --quiet vasili-wg.service 2>/dev/null; then
        warn "WireGuard is currently running on UDP/53"
        local ans
        ans=$(prompt_with_default "Stop WireGuard and switch to iodine?" "y")
        if [[ "$ans" != "y" ]]; then
            log "Keeping WireGuard, skipping iodine"
            return
        fi
        systemctl stop vasili-wg.service
        systemctl disable vasili-wg.service
        save_conf "UDP_SERVICE" ""
        log "WireGuard stopped"
    fi

    if ! check_port53_conflict "udp"; then
        warn "UDP/53 conflict detected — iodine setup may fail"
    fi

    if ! dpkg -l iodine &>/dev/null; then
        log "Installing iodine..."
        apt-get update -qq && apt-get install -y -qq iodine
    fi

    local domain password subnet
    domain=$(load_conf "IODINE_DOMAIN" "")
    domain=$(prompt_with_default "Tunnel subdomain (e.g. t.example.com)" "${domain:-t.example.com}")
    password=$(load_conf "IODINE_PASSWORD" "")
    password=$(prompt_with_default "Tunnel password" "${password:-vasili}")
    subnet=$(prompt_with_default "Tunnel subnet (server IP)" "10.53.53.1/24")

    save_conf "IODINE_DOMAIN" "$domain"
    save_conf "IODINE_PASSWORD" "$password"
    save_conf "IODINE_SUBNET" "$subnet"

    # Write systemd unit
    cat > /etc/systemd/system/vasili-iodined.service <<SVCEOF
[Unit]
Description=Vasili iodine DNS tunnel server (UDP/53)
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/iodined -f -c -P ${password} ${subnet} ${domain}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable vasili-iodined.service
    systemctl restart vasili-iodined.service
    log "vasili-iodined service started"

    ensure_ip_forwarding

    # NAT for iodine subnet
    local subnet_base
    subnet_base=$(echo "$subnet" | sed 's|\.[0-9]*/|.0/|')
    ensure_nat "$subnet_base"

    save_conf "UDP_SERVICE" "iodine"

    log "Iodine DNS tunnel server ready on UDP/53"
    echo ""
    warn "DNS delegation required! Add these records at your DNS provider:"
    echo "  ${domain}.   NS   ns-vasili.${domain#*.}."
    echo "  ns-vasili.${domain#*.}.   A   $(load_conf PUBLIC_IP)"
    echo ""
}

# ============================================================================
# WireGuard on UDP/53
# ============================================================================

setup_wireguard() {
    hdr "WireGuard VPN Server (UDP/53)"

    # Check for iodine conflict
    if systemctl is-active --quiet vasili-iodined.service 2>/dev/null; then
        warn "Iodine is currently running on UDP/53"
        local ans
        ans=$(prompt_with_default "Stop iodine and switch to WireGuard?" "y")
        if [[ "$ans" != "y" ]]; then
            log "Keeping iodine, skipping WireGuard"
            return
        fi
        systemctl stop vasili-iodined.service
        systemctl disable vasili-iodined.service
        save_conf "UDP_SERVICE" ""
        log "Iodine stopped"
    fi

    if ! check_port53_conflict "udp"; then
        warn "UDP/53 conflict detected — WireGuard setup may fail"
    fi

    if ! dpkg -l wireguard &>/dev/null || ! dpkg -l wireguard-tools &>/dev/null; then
        log "Installing wireguard..."
        apt-get update -qq && apt-get install -y -qq wireguard wireguard-tools
    fi

    local public_ip
    public_ip=$(load_conf "PUBLIC_IP" "$(detect_public_ip)")

    # Generate keys if needed
    mkdir -p "$VASILI_DIR"
    if [[ ! -f "$VASILI_DIR/wg_server_private" ]]; then
        log "Generating WireGuard keypairs"
        umask 077
        wg genkey | tee "$VASILI_DIR/wg_server_private" | wg pubkey > "$VASILI_DIR/wg_server_public"
        wg genkey | tee "$VASILI_DIR/wg_client_private" | wg pubkey > "$VASILI_DIR/wg_client_public"
    else
        log "WireGuard keypairs already exist"
    fi

    local srv_priv srv_pub cli_priv cli_pub
    srv_priv=$(cat "$VASILI_DIR/wg_server_private")
    srv_pub=$(cat "$VASILI_DIR/wg_server_public")
    cli_priv=$(cat "$VASILI_DIR/wg_client_private")
    cli_pub=$(cat "$VASILI_DIR/wg_client_public")

    # Server config
    log "Writing /etc/wireguard/wg-vasili.conf"
    cat > /etc/wireguard/wg-vasili.conf <<WGEOF
[Interface]
PrivateKey = ${srv_priv}
Address = 10.53.1.1/24
ListenPort = 53

[Peer]
PublicKey = ${cli_pub}
AllowedIPs = 10.53.1.2/32
WGEOF
    chmod 600 /etc/wireguard/wg-vasili.conf

    # Client config
    log "Writing $VASILI_DIR/wg-vasili-client.conf"
    cat > "$VASILI_DIR/wg-vasili-client.conf" <<WGEOF
[Interface]
PrivateKey = ${cli_priv}
Address = 10.53.1.2/24
DNS = 8.8.8.8

[Peer]
PublicKey = ${srv_pub}
Endpoint = ${public_ip}:53
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
WGEOF
    chmod 600 "$VASILI_DIR/wg-vasili-client.conf"

    # Use a wrapper service so we can control it independently
    cat > /etc/systemd/system/vasili-wg.service <<'SVCEOF'
[Unit]
Description=Vasili WireGuard VPN server (UDP/53)
After=network.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/wg-quick up wg-vasili
ExecStop=/usr/bin/wg-quick down wg-vasili

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable vasili-wg.service
    # Stop if already running (idempotent re-run)
    wg-quick down wg-vasili 2>/dev/null || true
    systemctl restart vasili-wg.service
    log "vasili-wg service started"

    ensure_ip_forwarding
    ensure_nat "10.53.1.0/24"

    save_conf "UDP_SERVICE" "wireguard"

    log "WireGuard VPN server ready on UDP/53"
    echo ""
    warn "Copy the client config to your Vasili Pi:"
    echo "  scp $VASILI_DIR/wg-vasili-client.conf pi@<your-pi>:/etc/wireguard/"
    echo ""
}

# ============================================================================
# Crack Server + DNS Proxy
# ============================================================================

setup_crack_server() {
    hdr "PMKID Crack Server (DNS offload)"

    local crack_domain crack_secret wordlist
    crack_domain=$(load_conf "CRACK_DOMAIN" "")
    crack_domain=$(prompt_with_default "Crack server domain (e.g. crack.example.com)" "${crack_domain:-crack.example.com}")
    crack_secret=$(load_conf "CRACK_SECRET" "")
    crack_secret=$(prompt_with_default "Shared secret (auth token)" "${crack_secret:-$(head -c 16 /dev/urandom | xxd -p)}")
    wordlist=$(prompt_with_default "Wordlist path" "/usr/share/wordlists/rockyou.txt")

    save_conf "CRACK_DOMAIN" "$crack_domain"
    save_conf "CRACK_SECRET" "$crack_secret"
    save_conf "CRACK_WORDLIST" "$wordlist"

    # Ensure hashcat is installed (optional but recommended)
    if ! command -v hashcat &>/dev/null; then
        local install_hc
        install_hc=$(prompt_with_default "Install hashcat for GPU-accelerated cracking? (y/n)" "y")
        if [[ "$install_hc" == "y" ]]; then
            apt-get update -qq && apt-get install -y -qq hashcat
        fi
    fi

    # Write crack server config
    mkdir -p "$VASILI_DIR"
    cat > "$VASILI_DIR/crack-server.json" <<JSONEOF
{
    "listen": "127.0.0.1",
    "port": 5353,
    "domain": "${crack_domain}",
    "secret": "${crack_secret}",
    "wordlist": "${wordlist}"
}
JSONEOF

    # Find the crack server script
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local crack_script="$script_dir/vasili-crack-server.py"
    if [[ ! -f "$crack_script" ]]; then
        crack_script="/opt/vasili/server/vasili-crack-server.py"
    fi
    if [[ ! -f "$crack_script" ]]; then
        err "Cannot find vasili-crack-server.py"
        err "Expected at: $script_dir/vasili-crack-server.py or /opt/vasili/server/"
        return 1
    fi

    cat > /etc/systemd/system/vasili-crack.service <<SVCEOF
[Unit]
Description=Vasili PMKID Crack Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${crack_script}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable vasili-crack.service
    systemctl restart vasili-crack.service
    log "vasili-crack service started on 127.0.0.1:5353"

    save_conf "CRACK_ENABLED" "true"

    # Check if we need the DNS proxy (tunnel service also on UDP/53)
    local udp_svc
    udp_svc=$(load_conf "UDP_SERVICE" "")
    if [[ -n "$udp_svc" ]]; then
        log "Tunnel service ($udp_svc) is also using UDP/53 — setting up DNS proxy"
        setup_dns_proxy "$crack_domain"
    else
        log "No tunnel service on UDP/53 — crack server can listen directly"
        warn "If you later add iodine or WireGuard, re-run setup to add the DNS proxy"
    fi

    log "Crack server ready"
    echo ""
    echo -e "${BOLD}Client config for Vasili:${NC}"
    echo "  offload_domain: $crack_domain"
    echo "  offload_secret: $crack_secret"
    echo ""
    warn "DNS delegation required! Add these records at your DNS provider:"
    echo "  ${crack_domain}.   NS   ns-crack.${crack_domain#*.}."
    echo "  ns-crack.${crack_domain#*.}.   A   $(load_conf PUBLIC_IP)"
    echo ""
}

setup_dns_proxy() {
    local crack_domain="${1:-$(load_conf CRACK_DOMAIN)}"
    hdr "DNS Proxy (domain-routing on UDP/53)"

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local proxy_script="$script_dir/vasili-dns-proxy.py"
    if [[ ! -f "$proxy_script" ]]; then
        proxy_script="/opt/vasili/server/vasili-dns-proxy.py"
    fi
    if [[ ! -f "$proxy_script" ]]; then
        err "Cannot find vasili-dns-proxy.py"
        return 1
    fi

    # Write proxy config
    cat > "$VASILI_DIR/dns-proxy.json" <<JSONEOF
{
    "listen": "0.0.0.0",
    "port": 53,
    "crack_backend": "127.0.0.1:5353",
    "tunnel_backend": "127.0.0.1:5354",
    "crack_domain": "${crack_domain}"
}
JSONEOF

    # Reconfigure tunnel service to listen on 5354 instead of 53
    local udp_svc
    udp_svc=$(load_conf "UDP_SERVICE" "")
    if [[ "$udp_svc" == "iodine" ]]; then
        log "Reconfiguring iodine to listen on 127.0.0.1:5354"
        # iodine doesn't support custom ports directly, but iodined
        # can be told to listen on a specific address
        # We need to update the systemd unit
        if [[ -f /etc/systemd/system/vasili-iodined.service ]]; then
            warn "Note: iodine listens on all interfaces — DNS proxy will forward to it on 5354"
            warn "You may need to manually adjust iodine's listen port"
        fi
    elif [[ "$udp_svc" == "wireguard" ]]; then
        log "Reconfiguring WireGuard to listen on port 5354"
        if [[ -f /etc/wireguard/wg-vasili.conf ]]; then
            sed -i 's/ListenPort = 53/ListenPort = 5354/' /etc/wireguard/wg-vasili.conf
            wg-quick down wg-vasili 2>/dev/null || true
            systemctl restart vasili-wg.service
            log "WireGuard restarted on port 5354"
        fi
    fi

    cat > /etc/systemd/system/vasili-dns-proxy.service <<SVCEOF
[Unit]
Description=Vasili DNS Proxy (domain router on UDP/53)
After=network.target
Before=vasili-crack.service vasili-iodined.service vasili-wg.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${proxy_script}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable vasili-dns-proxy.service
    systemctl restart vasili-dns-proxy.service
    log "vasili-dns-proxy service started on 0.0.0.0:53"
}

# ============================================================================
# Status
# ============================================================================

show_status() {
    hdr "Service Status"

    echo -e "${BOLD}TCP/53 (SSH):${NC}"
    if systemctl is-active --quiet vasili-sshd-53.service 2>/dev/null; then
        echo -e "  ${GREEN}RUNNING${NC}"
        ss -tlnp 2>/dev/null | grep ":53 " | sed 's/^/  /' || echo "  (no listeners found)"
    else
        echo -e "  ${RED}STOPPED${NC}"
    fi

    echo ""
    echo -e "${BOLD}UDP/53:${NC}"
    local udp_svc
    udp_svc=$(load_conf "UDP_SERVICE" "")
    if [[ "$udp_svc" == "iodine" ]] && systemctl is-active --quiet vasili-iodined.service 2>/dev/null; then
        echo -e "  ${GREEN}IODINE RUNNING${NC}"
        ss -ulnp 2>/dev/null | grep ":53 " | sed 's/^/  /' || true
        echo "  Domain: $(load_conf IODINE_DOMAIN)"
    elif [[ "$udp_svc" == "wireguard" ]] && systemctl is-active --quiet vasili-wg.service 2>/dev/null; then
        echo -e "  ${GREEN}WIREGUARD RUNNING${NC}"
        wg show wg-vasili 2>/dev/null | sed 's/^/  /' || true
    else
        echo -e "  ${RED}NO UDP SERVICE${NC}"
    fi

    echo ""
    echo -e "${BOLD}Crack Server:${NC}"
    if systemctl is-active --quiet vasili-crack.service 2>/dev/null; then
        echo -e "  ${GREEN}RUNNING${NC}"
        echo "  Domain: $(load_conf CRACK_DOMAIN)"
    else
        echo -e "  ${RED}STOPPED${NC}"
    fi

    echo ""
    echo -e "${BOLD}DNS Proxy:${NC}"
    if systemctl is-active --quiet vasili-dns-proxy.service 2>/dev/null; then
        echo -e "  ${GREEN}RUNNING${NC}"
    else
        echo -e "  ${YELLOW}NOT INSTALLED${NC} (only needed when crack server + tunnel coexist)"
    fi

    echo ""
    echo -e "${BOLD}IP Forwarding:${NC}"
    local fwd
    fwd=$(sysctl -n net.ipv4.ip_forward 2>/dev/null)
    if [[ "$fwd" == "1" ]]; then
        echo -e "  ${GREEN}ENABLED${NC}"
    else
        echo -e "  ${RED}DISABLED${NC}"
    fi
}

# ============================================================================
# Print Client Config
# ============================================================================

print_client_config() {
    hdr "Client Configuration for Vasili"

    local public_ip
    public_ip=$(load_conf "PUBLIC_IP" "$(detect_public_ip)")

    if [[ "$(load_conf SSH_ENABLED)" == "true" ]]; then
        echo -e "${BOLD}--- SSH Tunnel (dns_port_tunnel stage) ---${NC}"
        echo "  ssh_server:   $public_ip"
        echo "  ssh_user:     root"
        echo "  ssh_key_path: /etc/vasili/ssh_client_key"
        echo ""
        echo "  Private key to copy to Pi: $VASILI_DIR/ssh_client_key"
        echo ""
    fi

    local udp_svc
    udp_svc=$(load_conf "UDP_SERVICE" "")
    if [[ "$udp_svc" == "iodine" ]]; then
        echo -e "${BOLD}--- Iodine DNS Tunnel (dns_tunnel stage) ---${NC}"
        echo "  server_domain:   $(load_conf IODINE_DOMAIN)"
        echo "  tunnel_password: $(load_conf IODINE_PASSWORD)"
        echo "  tunnel_type:     iodine"
        echo ""
        echo -e "  ${YELLOW}Reminder: DNS delegation must be configured for $(load_conf IODINE_DOMAIN)${NC}"
        echo ""
    elif [[ "$udp_svc" == "wireguard" ]]; then
        echo -e "${BOLD}--- WireGuard VPN (dns_port_tunnel stage) ---${NC}"
        echo "  wg_config_path: /etc/wireguard/wg-vasili-client.conf"
        echo ""
        echo "  Client config to copy to Pi: $VASILI_DIR/wg-vasili-client.conf"
        echo "  Destination on Pi:           /etc/wireguard/wg-vasili-client.conf"
        echo ""
    fi

    if [[ "$(load_conf CRACK_ENABLED)" == "true" ]]; then
        echo -e "${BOLD}--- PMKID Crack Server (dns_offload_crack stage) ---${NC}"
        echo "  offload_domain: $(load_conf CRACK_DOMAIN)"
        echo "  offload_secret: $(load_conf CRACK_SECRET)"
        echo ""
        echo -e "  ${YELLOW}Reminder: DNS delegation must be configured for $(load_conf CRACK_DOMAIN)${NC}"
        echo ""
    fi

    if [[ "$(load_conf SSH_ENABLED)" == "true" ]] || [[ -n "$udp_svc" ]] || [[ "$(load_conf CRACK_ENABLED)" == "true" ]]; then
        echo -e "${BOLD}--- Quick copy commands ---${NC}"
        echo "  PI=<your-pi-ip>"
        if [[ "$(load_conf SSH_ENABLED)" == "true" ]]; then
            echo "  scp $VASILI_DIR/ssh_client_key \$PI:/etc/vasili/"
        fi
        if [[ "$udp_svc" == "wireguard" ]]; then
            echo "  scp $VASILI_DIR/wg-vasili-client.conf \$PI:/etc/wireguard/"
        fi
    else
        warn "No services configured yet. Run option 1 first."
    fi
    echo ""
}

# ============================================================================
# Uninstall
# ============================================================================

uninstall_services() {
    hdr "Uninstall"

    echo "What to remove?"
    echo "  1) SSH tunnel server (TCP/53)"
    echo "  2) Iodine DNS tunnel (UDP/53)"
    echo "  3) WireGuard VPN (UDP/53)"
    echo "  4) Everything"
    echo "  0) Cancel"
    local choice
    read -rp "Choice: " choice

    case "$choice" in
        1)
            systemctl stop vasili-sshd-53.service 2>/dev/null || true
            systemctl disable vasili-sshd-53.service 2>/dev/null || true
            rm -f /etc/systemd/system/vasili-sshd-53.service
            rm -f /etc/ssh/vasili-sshd-53.conf
            systemctl daemon-reload
            save_conf "SSH_ENABLED" ""
            log "SSH tunnel server removed"
            ;;
        2)
            systemctl stop vasili-iodined.service 2>/dev/null || true
            systemctl disable vasili-iodined.service 2>/dev/null || true
            rm -f /etc/systemd/system/vasili-iodined.service
            systemctl daemon-reload
            save_conf "UDP_SERVICE" ""
            log "Iodine removed"
            ;;
        3)
            wg-quick down wg-vasili 2>/dev/null || true
            systemctl stop vasili-wg.service 2>/dev/null || true
            systemctl disable vasili-wg.service 2>/dev/null || true
            rm -f /etc/systemd/system/vasili-wg.service
            rm -f /etc/wireguard/wg-vasili.conf
            systemctl daemon-reload
            save_conf "UDP_SERVICE" ""
            log "WireGuard removed"
            ;;
        4)
            systemctl stop vasili-sshd-53.service vasili-iodined.service vasili-wg.service 2>/dev/null || true
            systemctl disable vasili-sshd-53.service vasili-iodined.service vasili-wg.service 2>/dev/null || true
            wg-quick down wg-vasili 2>/dev/null || true
            rm -f /etc/systemd/system/vasili-sshd-53.service
            rm -f /etc/systemd/system/vasili-iodined.service
            rm -f /etc/systemd/system/vasili-wg.service
            rm -f /etc/ssh/vasili-sshd-53.conf
            rm -f /etc/wireguard/wg-vasili.conf
            systemctl daemon-reload

            local del_keys
            del_keys=$(prompt_with_default "Also delete keys and configs in $VASILI_DIR?" "n")
            if [[ "$del_keys" == "y" ]]; then
                rm -rf "$VASILI_DIR"
                log "Removed $VASILI_DIR"
            fi
            log "All Vasili services removed"
            ;;
        *)
            log "Cancelled"
            ;;
    esac
}

# ============================================================================
# Full Setup Flow
# ============================================================================

full_setup() {
    hdr "Full Vasili Server Setup"

    log "Step 1/3: SSH tunnel on TCP/53 (always installed)"
    setup_ssh

    log "Step 2/3: Choose UDP/53 service"
    echo ""
    echo "  1) iodine  — DNS tunnel (works through most firewalls, slower)"
    echo "  2) WireGuard — UDP VPN (faster, needs UDP/53 open)"
    echo "  0) Skip UDP service"

    local udp_choice
    read -rp "Choice [1]: " udp_choice
    udp_choice="${udp_choice:-1}"

    case "$udp_choice" in
        1) setup_iodine ;;
        2) setup_wireguard ;;
        0) log "Skipping UDP/53 service" ;;
        *) warn "Invalid choice, skipping" ;;
    esac

    log "Step 3/3: PMKID crack server (DNS offload)"
    local crack_choice
    crack_choice=$(prompt_with_default "Install PMKID crack server? (y/n)" "y")
    if [[ "$crack_choice" == "y" ]]; then
        setup_crack_server
    fi

    hdr "Setup Complete"
    print_client_config
}

# ============================================================================
# Main Menu
# ============================================================================

main_menu() {
    while true; do
        echo ""
        echo -e "${BOLD}${CYAN}Vasili Server Setup${NC}"
        echo "==================="
        echo "  1) Full setup (SSH + tunnel + crack server)"
        echo "  2) Show status of all services"
        echo "  3) Print client configuration"
        echo "  4) Setup crack server only"
        echo "  5) Uninstall services"
        echo "  0) Exit"
        echo ""
        local choice
        read -rp "Choice: " choice

        case "$choice" in
            1) full_setup ;;
            2) show_status ;;
            3) print_client_config ;;
            4) setup_crack_server ;;
            5) uninstall_services ;;
            0) log "Bye"; exit 0 ;;
            *) warn "Invalid choice" ;;
        esac
    done
}

# ============================================================================
# Entry point
# ============================================================================

ensure_root
mkdir -p "$VASILI_DIR"

# Allow running a specific action via command line arg
if [[ $# -ge 1 ]]; then
    case "$1" in
        setup)   full_setup ;;
        status)  show_status ;;
        config)  print_client_config ;;
        remove)  uninstall_services ;;
        *)       main_menu ;;
    esac
else
    main_menu
fi

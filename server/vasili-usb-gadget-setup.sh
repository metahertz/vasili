#!/usr/bin/env bash
# ============================================================================
# Vasili USB-C Gadget Mode Setup for Raspberry Pi 5
#
# Configures the Pi 5's USB-C power port as a USB Ethernet gadget so that
# when plugged into a laptop/PC the Pi appears as a USB network adapter.
#
# This gives you:
#   - A dedicated management link (SSH, Web UI on port 5000)
#   - Internet sharing: if Vasili has an active WiFi connection, the
#     connected computer can route through it via NAT
#   - Always-on: survives reboots, works headless, no WiFi needed
#
# Network:
#   Pi (usb0):   10.55.0.1/24   — runs DHCP server
#   Laptop:      10.55.0.50-150  — gets IP from Pi's DHCP
#
# Usage:
#   sudo bash vasili-usb-gadget-setup.sh          # interactive
#   sudo bash vasili-usb-gadget-setup.sh install   # non-interactive install
#   sudo bash vasili-usb-gadget-setup.sh status    # check status
#   sudo bash vasili-usb-gadget-setup.sh remove    # uninstall
# ============================================================================
set -euo pipefail

USB_IFACE="usb0"
ETH_IFACE="eth0"
MGMT_IP_USB="10.55.0.1"
MGMT_IP_ETH="10.55.0.2"
USB_NETMASK="255.255.255.0"
USB_SUBNET="10.55.0.0/24"
DHCP_RANGE_START="10.55.0.50"
DHCP_RANGE_END="10.55.0.150"
DHCP_LEASE="12h"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}=== $* ===${NC}\n"; }

ensure_root() {
    if [[ $EUID -ne 0 ]]; then
        err "Run as root (sudo)"
        exit 1
    fi
}

# ============================================================================
# Step 1: Enable dwc2 overlay and g_ether module
# ============================================================================

setup_boot_config() {
    hdr "Boot Configuration"

    local config_file=""
    # Pi 5 uses /boot/firmware/config.txt on Ubuntu
    for f in /boot/firmware/config.txt /boot/config.txt; do
        if [[ -f "$f" ]]; then
            config_file="$f"
            break
        fi
    done

    if [[ -z "$config_file" ]]; then
        err "Cannot find boot config.txt"
        return 1
    fi

    log "Using boot config: $config_file"

    # USB-C gadget mode needs the dwc2 controller in PERIPHERAL mode.
    # A bare "dtoverlay=dwc2" defaults to OTG; on the Pi 5 USB-C port OTG
    # role detection does not reliably switch to peripheral, so the
    # controller comes up as a USB host and the gadget never enumerates.
    if grep -q '^dtoverlay=dwc2,dr_mode=peripheral' "$config_file"; then
        log "dwc2 peripheral mode already configured"
    elif grep -q '^dtoverlay=dwc2$' "$config_file"; then
        sed -i 's/^dtoverlay=dwc2$/dtoverlay=dwc2,dr_mode=peripheral/' "$config_file"
        log "Upgraded 'dtoverlay=dwc2' (OTG) to peripheral mode in $config_file"
    else
        echo 'dtoverlay=dwc2,dr_mode=peripheral' >> "$config_file"
        log "Added dtoverlay=dwc2,dr_mode=peripheral to $config_file"
    fi

    # Ensure dwc2 and g_ether are loaded at boot
    local modules_file="/etc/modules"
    for mod in dwc2 g_ether; do
        if ! grep -q "^${mod}$" "$modules_file" 2>/dev/null; then
            echo "$mod" >> "$modules_file"
            log "Added $mod to $modules_file"
        else
            log "$mod already in $modules_file"
        fi
    done

    # Also load immediately if possible (may fail on first run before reboot)
    modprobe dwc2 2>/dev/null || true
    modprobe g_ether 2>/dev/null || true
}

# ============================================================================
# Step 2: Static IP on usb0 via networkd (NM-independent)
# ============================================================================

setup_usb_network() {
    hdr "Management Network Interfaces (usb0 + eth0)"

    # Tell NetworkManager to leave both management interfaces alone
    cat > /etc/NetworkManager/conf.d/99-unmanaged-mgmt.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:usb0;interface-name:eth0
EOF
    log "NetworkManager will ignore usb0 and eth0"

    # systemd-networkd configs
    mkdir -p /etc/systemd/network

    cat > /etc/systemd/network/50-vasili-usb0.network <<EOF
[Match]
Name=${USB_IFACE}

[Network]
Address=${MGMT_IP_USB}/24
DHCPServer=no
ConfigureWithoutCarrier=yes
EOF

    cat > /etc/systemd/network/50-vasili-eth0.network <<EOF
[Match]
Name=${ETH_IFACE}

[Network]
Address=${MGMT_IP_ETH}/24
DHCPServer=no
ConfigureWithoutCarrier=yes
EOF
    log "Wrote systemd-networkd configs for $USB_IFACE and $ETH_IFACE"

    # Systemd service that brings up both interfaces with static IPs.
    # eth0 gets an address on the same management subnet as usb0 so
    # a laptop plugged into either port reaches the same Pi.
    cat > /etc/systemd/system/vasili-usb-gadget.service <<EOF
[Unit]
Description=Vasili Management Network (usb0 + eth0)
After=network-pre.target
Before=vasili.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c ' \
    if ip link show ${USB_IFACE} &>/dev/null; then \
        ip addr flush dev ${USB_IFACE} 2>/dev/null || true; \
        ip addr add ${MGMT_IP_USB}/24 dev ${USB_IFACE} 2>/dev/null || true; \
        ip link set ${USB_IFACE} up; \
    fi; \
    if ip link show ${ETH_IFACE} &>/dev/null; then \
        ip addr add ${MGMT_IP_ETH}/24 dev ${ETH_IFACE} 2>/dev/null || true; \
        ip link set ${ETH_IFACE} up; \
    fi'
ExecStop=/bin/bash -c ' \
    ip link set ${USB_IFACE} down 2>/dev/null || true; \
    ip addr del ${MGMT_IP_ETH}/24 dev ${ETH_IFACE} 2>/dev/null || true'

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable vasili-usb-gadget.service
    log "vasili-usb-gadget.service enabled"

    # Bring up now if interfaces exist
    if ip link show "$USB_IFACE" &>/dev/null; then
        ip addr flush dev "$USB_IFACE" 2>/dev/null || true
        ip addr add "${MGMT_IP_USB}/24" dev "$USB_IFACE" 2>/dev/null || true
        ip link set "$USB_IFACE" up 2>/dev/null || true
        log "usb0 is up with IP $MGMT_IP_USB"
    else
        warn "usb0 not present yet — will activate after reboot"
    fi

    if ip link show "$ETH_IFACE" &>/dev/null; then
        ip addr add "${MGMT_IP_ETH}/24" dev "$ETH_IFACE" 2>/dev/null || true
        ip link set "$ETH_IFACE" up 2>/dev/null || true
        log "eth0 is up with IP $MGMT_IP_ETH"
    fi
}

# ============================================================================
# Step 3: DHCP server for connected laptop
# ============================================================================

setup_dhcp() {
    hdr "DHCP Server for USB clients"

    # Install dnsmasq if needed (likely already installed for HostAP)
    if ! command -v dnsmasq &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq dnsmasq
    fi

    # Disable the system-wide dnsmasq service — we run our own instance
    systemctl disable dnsmasq 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true

    # Write a dedicated dnsmasq config for both management interfaces
    cat > /etc/dnsmasq.d/vasili-usb-gadget.conf <<EOF
# Vasili management DHCP — usb0 and eth0 on same subnet
interface=${USB_IFACE}
interface=${ETH_IFACE}
bind-interfaces
dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},${USB_NETMASK},${DHCP_LEASE}
dhcp-option=option:router,${MGMT_IP_USB}
dhcp-option=option:dns-server,${MGMT_IP_USB},8.8.8.8
# Advertise ourselves as the gateway and DNS
dhcp-authoritative
# Don't read /etc/resolv.conf — forward to public DNS
no-resolv
server=8.8.8.8
server=1.1.1.1
EOF

    # Systemd service for the USB DHCP instance
    cat > /etc/systemd/system/vasili-usb-dhcp.service <<EOF
[Unit]
Description=Vasili USB Gadget DHCP Server
After=vasili-usb-gadget.service
Requires=vasili-usb-gadget.service

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --no-daemon --conf-file=/etc/dnsmasq.d/vasili-usb-gadget.conf
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable vasili-usb-dhcp.service

    if ip link show "$USB_IFACE" &>/dev/null; then
        systemctl restart vasili-usb-dhcp.service
        log "DHCP server running on $USB_IFACE"
    else
        log "DHCP server enabled — will start after reboot"
    fi
}

# ============================================================================
# Step 4: NAT / internet sharing from Vasili's WiFi to USB client
# ============================================================================

setup_nat() {
    hdr "NAT & Internet Sharing"

    # Enable IP forwarding (persistent)
    if [[ ! -f /etc/sysctl.d/99-vasili-usb.conf ]] || ! grep -q ip_forward /etc/sysctl.d/99-vasili-usb.conf 2>/dev/null; then
        echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-vasili-usb.conf
        sysctl -w net.ipv4.ip_forward=1 >/dev/null
        log "IP forwarding enabled"
    fi

    # Create a script that sets up NAT rules — called at boot and when
    # Vasili's active connection changes.
    cat > /etc/vasili/usb-gadget-nat.sh <<'NATEOF'
#!/bin/bash
# Set up NAT for the USB gadget subnet.
# Called with optional $1 = upstream interface (e.g. wlan0).
# If no arg, detects the default route interface.
USB_SUBNET="10.55.0.0/24"

UPSTREAM="${1:-}"
if [[ -z "$UPSTREAM" ]]; then
    UPSTREAM=$(ip -4 route show default | awk '{print $5; exit}')
fi
if [[ -z "$UPSTREAM" ]]; then
    echo "[usb-nat] No upstream interface found"
    exit 0
fi

# Create chains if needed
iptables -N VASILI-USB-FWD 2>/dev/null || true
iptables -t nat -N VASILI-USB-NAT 2>/dev/null || true

# Flush our chains
iptables -F VASILI-USB-FWD
iptables -t nat -F VASILI-USB-NAT

# Jump into our chains (idempotent)
iptables -C FORWARD -j VASILI-USB-FWD 2>/dev/null || \
    iptables -I FORWARD -j VASILI-USB-FWD
iptables -t nat -C POSTROUTING -j VASILI-USB-NAT 2>/dev/null || \
    iptables -t nat -I POSTROUTING -j VASILI-USB-NAT

# Masquerade
iptables -t nat -A VASILI-USB-NAT -s "$USB_SUBNET" -o "$UPSTREAM" -j MASQUERADE

# Forward
iptables -A VASILI-USB-FWD -s "$USB_SUBNET" -o "$UPSTREAM" -j ACCEPT
iptables -A VASILI-USB-FWD -d "$USB_SUBNET" -i "$UPSTREAM" -m state \
    --state RELATED,ESTABLISHED -j ACCEPT

echo "[usb-nat] NAT: $USB_SUBNET -> $UPSTREAM"
NATEOF
    chmod +x /etc/vasili/usb-gadget-nat.sh
    log "Wrote /etc/vasili/usb-gadget-nat.sh"

    # Systemd service to run NAT rules at boot
    cat > /etc/systemd/system/vasili-usb-nat.service <<'EOF'
[Unit]
Description=Vasili USB Gadget NAT Rules
After=vasili-usb-gadget.service
Requires=vasili-usb-gadget.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/vasili/usb-gadget-nat.sh

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable vasili-usb-nat.service

    # Run now if usb0 exists
    if ip link show "$USB_IFACE" &>/dev/null; then
        /etc/vasili/usb-gadget-nat.sh
        log "NAT rules applied"
    else
        log "NAT service enabled — will apply after reboot"
    fi
}

# ============================================================================
# Status
# ============================================================================

show_status() {
    hdr "USB Gadget Status"

    echo -e "${BOLD}Kernel Modules:${NC}"
    if lsmod | grep -q dwc2; then
        echo -e "  dwc2:    ${GREEN}LOADED${NC}"
    else
        echo -e "  dwc2:    ${RED}NOT LOADED${NC} (needs reboot)"
    fi
    if lsmod | grep -q g_ether; then
        echo -e "  g_ether: ${GREEN}LOADED${NC}"
    else
        echo -e "  g_ether: ${RED}NOT LOADED${NC} (needs reboot)"
    fi

    echo ""
    echo -e "${BOLD}Interfaces:${NC}"
    for iface in "$USB_IFACE" "$ETH_IFACE"; do
        if ip link show "$iface" &>/dev/null; then
            local state ip_addr carrier
            state=$(cat /sys/class/net/$iface/operstate 2>/dev/null || echo "unknown")
            ip_addr=$(ip -4 addr show "$iface" 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1)
            carrier=$(cat /sys/class/net/$iface/carrier 2>/dev/null || echo "0")
            local link_status
            if [[ "$carrier" == "1" ]]; then
                link_status="${GREEN}LINK UP${NC}"
            else
                link_status="${YELLOW}NO LINK${NC}"
            fi
            echo -e "  $iface: ${GREEN}EXISTS${NC}  ip=${ip_addr:-none}  $link_status"
        else
            echo -e "  $iface: ${RED}NOT PRESENT${NC}"
        fi
    done

    echo ""
    echo -e "${BOLD}Services:${NC}"
    for svc in vasili-usb-gadget vasili-usb-dhcp vasili-usb-nat; do
        if systemctl is-active --quiet "${svc}.service" 2>/dev/null; then
            echo -e "  $svc: ${GREEN}RUNNING${NC}"
        elif systemctl is-enabled --quiet "${svc}.service" 2>/dev/null; then
            echo -e "  $svc: ${YELLOW}ENABLED (not running)${NC}"
        else
            echo -e "  $svc: ${RED}NOT INSTALLED${NC}"
        fi
    done

    echo ""
    echo -e "${BOLD}DHCP Leases:${NC}"
    if [[ -f /var/lib/misc/dnsmasq.leases ]]; then
        grep "$USB_IFACE" /var/lib/misc/dnsmasq.leases 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    else
        echo "  (no lease file)"
    fi

    echo ""
    echo -e "${BOLD}Access:${NC}"
    echo "  Via USB-C: ssh ubuntu@$MGMT_IP_USB  /  http://$MGMT_IP_USB:5000"
    echo "  Via eth0:  ssh ubuntu@$MGMT_IP_ETH  /  http://$MGMT_IP_ETH:5000"
}

# ============================================================================
# Remove
# ============================================================================

remove() {
    hdr "Removing USB Gadget Configuration"

    for svc in vasili-usb-nat vasili-usb-dhcp vasili-usb-gadget; do
        systemctl stop "${svc}.service" 2>/dev/null || true
        systemctl disable "${svc}.service" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
    done

    rm -f /etc/dnsmasq.d/vasili-usb-gadget.conf
    rm -f /etc/NetworkManager/conf.d/99-unmanaged-mgmt.conf
    rm -f /etc/NetworkManager/conf.d/99-unmanaged-usb0.conf
    rm -f /etc/systemd/network/50-vasili-usb0.network
    rm -f /etc/systemd/network/50-vasili-eth0.network
    rm -f /etc/vasili/usb-gadget-nat.sh
    rm -f /etc/sysctl.d/99-vasili-usb.conf

    # Flush NAT chains
    iptables -F VASILI-USB-FWD 2>/dev/null || true
    iptables -X VASILI-USB-FWD 2>/dev/null || true
    iptables -t nat -F VASILI-USB-NAT 2>/dev/null || true
    iptables -t nat -X VASILI-USB-NAT 2>/dev/null || true

    systemctl daemon-reload

    warn "dwc2/g_ether entries in /etc/modules and boot config.txt were NOT removed."
    warn "Remove them manually if you want to fully disable gadget mode."
    log "USB gadget services removed"
}

# ============================================================================
# Install (full setup)
# ============================================================================

install_all() {
    hdr "Vasili USB-C Gadget Mode Setup"

    mkdir -p /etc/vasili

    setup_boot_config
    setup_usb_network
    setup_dhcp
    setup_nat

    hdr "Setup Complete"

    if ip link show "$USB_IFACE" &>/dev/null; then
        log "USB gadget is active NOW"
    else
        warn "A REBOOT is required to activate the USB-C gadget"
        echo "  sudo reboot"
    fi

    echo ""
    echo -e "${BOLD}After reboot / when connected:${NC}"
    echo "  Plug into USB-C or Ethernet — both are on the same management subnet."
    echo "  Your laptop gets IP ${DHCP_RANGE_START}-${DHCP_RANGE_END} via DHCP."
    echo ""
    echo "  Via USB-C:  ssh ubuntu@${MGMT_IP_USB}   http://${MGMT_IP_USB}:5000"
    echo "  Via eth0:   ssh ubuntu@${MGMT_IP_ETH}   http://${MGMT_IP_ETH}:5000"
    echo ""
    echo "  If Vasili has an active WiFi connection, your laptop"
    echo "  can route through it (NAT is automatic)."
    echo ""
    echo "  eth0 can be switched to 'pool' mode in the Vasili UI"
    echo "  for future Ethernet-based network testing."
    echo ""
}

# ============================================================================
# Entry point
# ============================================================================

ensure_root

case "${1:-}" in
    install) install_all ;;
    status)  show_status ;;
    remove)  remove ;;
    "")
        echo -e "${BOLD}${CYAN}Vasili USB-C Gadget Setup${NC}"
        echo "========================="
        echo "  1) Install (full setup)"
        echo "  2) Show status"
        echo "  3) Remove"
        echo "  0) Exit"
        read -rp "Choice: " choice
        case "$choice" in
            1) install_all ;;
            2) show_status ;;
            3) remove ;;
            0) exit 0 ;;
            *) err "Invalid choice" ;;
        esac
        ;;
    *) err "Usage: $0 [install|status|remove]"; exit 1 ;;
esac

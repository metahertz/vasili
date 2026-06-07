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

    # We deliberately do NOT use the legacy g_ether monolithic gadget — it binds
    # the UDC at module-load time, before the Mac's USB-C subsystem may be
    # ready. The Pi 5's dwc2 has no PHY VBUS sensing, so a missed enumeration
    # is silent and unrecoverable without a manual cycle. Instead we load
    # libcomposite and compose the gadget post-boot via configfs (see
    # setup_gadget_compose), with a watchdog that cycles the UDC binding when
    # the host fails to enumerate.
    local modules_file="/etc/modules"
    for mod in dwc2 libcomposite; do
        if ! grep -q "^${mod}$" "$modules_file" 2>/dev/null; then
            echo "$mod" >> "$modules_file"
            log "Added $mod to $modules_file"
        else
            log "$mod already in $modules_file"
        fi
    done

    # Purge legacy g_ether from module-load configs — it auto-binds the UDC.
    for f in "$modules_file" /etc/modules-load.d/modules.conf; do
        if [[ -f "$f" ]] && grep -q '^g_ether$' "$f"; then
            sed -i '/^g_ether$/d' "$f"
            log "Removed g_ether from $f"
        fi
    done

    # Strip g_ether from kernel cmdline (Ubuntu Pi A/B layout uses current/new).
    for cmdline in /boot/firmware/cmdline.txt \
                   /boot/firmware/current/cmdline.txt \
                   /boot/firmware/new/cmdline.txt; do
        if [[ -f "$cmdline" ]] && grep -qE 'g_ether(\.|,)' "$cmdline"; then
            # Drop ",g_ether" inside modules-load= and any "g_ether.host_addr=..." token.
            sed -i -E -e 's/(modules-load=[^ ]*),g_ether/\1/g' \
                      -e 's/ +g_ether\.host_addr=[^ ]+//g' "$cmdline"
            log "Stripped g_ether tokens from $cmdline"
        fi
    done

    # Also load immediately if possible (may fail on first run before reboot)
    modprobe dwc2 2>/dev/null || true
    modprobe libcomposite 2>/dev/null || true
}

# ============================================================================
# Step 1b: Compose the USB gadget via configfs (CDC NCM)
#
# We use CDC NCM (not the older ECM) because modern macOS prefers it and it's
# also natively supported by Linux and Windows 10+. The composed gadget is
# bound to the UDC post-boot (deferred ~5s) so the Mac's USB-C subsystem has
# time to be ready, avoiding the boot-time enumeration race that plagues the
# legacy g_ether path on Pi 5 (no PHY VBUS sense → missed enumerations are
# silent).
# ============================================================================

setup_gadget_compose() {
    hdr "USB Gadget Composition (configfs / CDC NCM)"

    mkdir -p /etc/vasili

    cat > /etc/vasili/usb-gadget-compose.sh <<'COMPOSE_EOF'
#!/usr/bin/env bash
# Compose the Vasili USB gadget. Idempotent — safe to re-run.
set -eu

UDC_NAME="$(ls /sys/class/udc 2>/dev/null | head -1 || true)"
GADGET_DIR=/sys/kernel/config/usb_gadget/vasili

# Stable MACs. Host side matches the value previously used by g_ether so any
# host-side rules/leases keyed off it continue to work.
HOST_MAC="00:11:aa:ff:aa:bb"
DEV_MAC="02:11:aa:ff:aa:cc"

# Wait for the UDC to appear — modprobe dwc2 may still be racing with us.
for _ in $(seq 1 40); do
    UDC_NAME="$(ls /sys/class/udc 2>/dev/null | head -1 || true)"
    [ -n "$UDC_NAME" ] && break
    sleep 0.25
done
if [ -z "$UDC_NAME" ]; then
    echo "[gadget] No UDC available — dwc2 not loaded?" >&2
    exit 1
fi

modprobe libcomposite 2>/dev/null || true

# Tear down any previous gadget (idempotent compose).
if [ -d "$GADGET_DIR" ]; then
    # Unbind UDC first
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    # Remove function symlinks from configs
    find "$GADGET_DIR/configs" -maxdepth 2 -type l -delete 2>/dev/null || true
    # Remove config strings then configs
    find "$GADGET_DIR/configs" -mindepth 2 -maxdepth 2 -type d -name '0x*' \
        -exec rmdir {} + 2>/dev/null || true
    find "$GADGET_DIR/configs" -mindepth 2 -maxdepth 2 -type d \
        -exec rmdir {} + 2>/dev/null || true
    find "$GADGET_DIR/configs" -mindepth 1 -maxdepth 1 -type d \
        -exec rmdir {} + 2>/dev/null || true
    # Remove functions
    find "$GADGET_DIR/functions" -mindepth 1 -maxdepth 1 -type d \
        -exec rmdir {} + 2>/dev/null || true
    # Remove gadget strings then gadget dir
    find "$GADGET_DIR/strings" -mindepth 1 -maxdepth 1 -type d \
        -exec rmdir {} + 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

# Device descriptors. 0x1d6b is the Linux Foundation VID; 0x0104 is the
# "Multifunction Composite Gadget" PID used by the upstream gadget examples.
echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "vasili-001"                 > strings/0x409/serialnumber
echo "Vasili"                     > strings/0x409/manufacturer
echo "Vasili Mgmt Network (NCM)"  > strings/0x409/product

# CDC NCM function (preferred by macOS Sequoia+, supported by Linux & Win10+).
mkdir -p functions/ncm.usb0
echo "$DEV_MAC"  > functions/ncm.usb0/dev_addr
echo "$HOST_MAC" > functions/ncm.usb0/host_addr

# Single configuration.
mkdir -p configs/c.1/strings/0x409
echo "CDC NCM"          > configs/c.1/strings/0x409/configuration
echo 250                > configs/c.1/MaxPower

ln -s functions/ncm.usb0 configs/c.1/

# Bind to UDC. This is what raises D+ pull-up; the host enumerates here.
echo "$UDC_NAME" > UDC

echo "[gadget] vasili gadget bound to $UDC_NAME"
COMPOSE_EOF
    chmod +x /etc/vasili/usb-gadget-compose.sh
    log "Wrote /etc/vasili/usb-gadget-compose.sh"
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

    # Systemd service that composes the gadget then brings up static IPs.
    # The 5s delay sidesteps a Pi 5 race: dwc2 has no PHY VBUS sensing, so if
    # we bind the UDC before the Mac's USB-C subsystem is enumerating, the
    # host silently misses us and there's no retry. Five seconds is enough
    # slack on every Mac we've tested.
    # eth0 gets an address on the same management subnet as usb0 so a laptop
    # plugged into either port reaches the same Pi.
    cat > /etc/systemd/system/vasili-usb-gadget.service <<EOF
[Unit]
Description=Vasili USB Gadget (compose + management IPs)
After=network-pre.target systemd-modules-load.service
Before=vasili.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/sleep 5
ExecStart=/etc/vasili/usb-gadget-compose.sh
ExecStartPost=/bin/bash -c ' \
    for _ in \$(seq 1 20); do \
        ip link show ${USB_IFACE} &>/dev/null && break; \
        sleep 0.25; \
    done; \
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
    echo "" > /sys/kernel/config/usb_gadget/vasili/UDC 2>/dev/null || true; \
    ip link set ${USB_IFACE} down 2>/dev/null || true; \
    ip addr del ${MGMT_IP_ETH}/24 dev ${ETH_IFACE} 2>/dev/null || true'

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable vasili-usb-gadget.service
    log "vasili-usb-gadget.service enabled"

    # Compose the gadget now (clear any legacy g_ether artifacts first).
    if lsmod | grep -q '^g_ether'; then
        log "Unloading legacy g_ether so libcomposite can own the UDC"
        rmmod g_ether 2>/dev/null || warn "rmmod g_ether failed — reboot may be required"
    fi
    modprobe libcomposite 2>/dev/null || true

    if [[ -x /etc/vasili/usb-gadget-compose.sh ]] && [[ -d /sys/class/udc ]]; then
        if /etc/vasili/usb-gadget-compose.sh; then
            log "Gadget composed and bound to UDC"
        else
            warn "Gadget compose failed — will retry at boot"
        fi
    fi

    # Wait briefly for usb0 to appear, then assign IPs.
    for _ in {1..20}; do
        ip link show "$USB_IFACE" &>/dev/null && break
        sleep 0.25
    done

    if ip link show "$USB_IFACE" &>/dev/null; then
        ip addr flush dev "$USB_IFACE" 2>/dev/null || true
        ip addr add "${MGMT_IP_USB}/24" dev "$USB_IFACE" 2>/dev/null || true
        ip link set "$USB_IFACE" up 2>/dev/null || true
        log "usb0 is up with IP $MGMT_IP_USB"
    else
        warn "usb0 not present — will activate after reboot"
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
# Point clients at ourselves for DNS so vasili.local resolves; we forward
# everything else upstream (see server= below).
dhcp-option=option:dns-server,${MGMT_IP_USB}
# Advertise ourselves as the gateway and DNS
dhcp-authoritative
# Don't read /etc/resolv.conf — forward to public DNS
no-resolv
server=8.8.8.8
server=1.1.1.1
# Authoritatively answer vasili.local with our own management IP. mDNS
# (avahi) covers most clients, but this is the unicast-DNS fallback for
# anything that respects the dnsmasq-pushed resolver.
address=/vasili.local/${MGMT_IP_USB}
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
LOCAL_IFACES=(usb0 eth0)
UI_PORT=5000

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
iptables -t nat -N VASILI-USB-PRE 2>/dev/null || true

# Flush our chains
iptables -F VASILI-USB-FWD
iptables -t nat -F VASILI-USB-NAT
iptables -t nat -F VASILI-USB-PRE

# Jump into our chains (idempotent)
iptables -C FORWARD -j VASILI-USB-FWD 2>/dev/null || \
    iptables -I FORWARD -j VASILI-USB-FWD
iptables -t nat -C POSTROUTING -j VASILI-USB-NAT 2>/dev/null || \
    iptables -t nat -I POSTROUTING -j VASILI-USB-NAT
iptables -t nat -C PREROUTING -j VASILI-USB-PRE 2>/dev/null || \
    iptables -t nat -I PREROUTING -j VASILI-USB-PRE

# Masquerade
iptables -t nat -A VASILI-USB-NAT -s "$USB_SUBNET" -o "$UPSTREAM" -j MASQUERADE

# Forward
iptables -A VASILI-USB-FWD -s "$USB_SUBNET" -o "$UPSTREAM" -j ACCEPT
iptables -A VASILI-USB-FWD -d "$USB_SUBNET" -i "$UPSTREAM" -m state \
    --state RELATED,ESTABLISHED -j ACCEPT

# Redirect tcp/80 -> Vasili UI on local-facing interfaces only, so clients
# connected via usb0/eth0 can reach http://vasili.local/ without a port.
# Upstream WiFi is untouched (port 80 stays closed there).
for IFACE in "${LOCAL_IFACES[@]}"; do
    iptables -t nat -A VASILI-USB-PRE -i "$IFACE" -p tcp --dport 80 \
        -j REDIRECT --to-ports "$UI_PORT"
done

echo "[usb-nat] NAT: $USB_SUBNET -> $UPSTREAM (UI :80 -> :$UI_PORT on ${LOCAL_IFACES[*]})"
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
# Step 4b: UDC watchdog — re-trigger enumeration when the host hasn't bitten
#
# The Pi 5's dwc2 has no PHY VBUS sensing, so the kernel can't observe Mac
# wake/replug. Once the host misses the initial D+ pull-up, the only signal
# we can send is to cycle the UDC binding. This watchdog does exactly that:
# checks usb0/carrier every 10s; if it's been 0 for 3 consecutive checks
# (~30s), unbind/rebind the UDC to force a fresh pull-up the Mac will see.
# ============================================================================

setup_gadget_watchdog() {
    hdr "USB Gadget Watchdog"

    cat > /etc/vasili/usb-gadget-watchdog.sh <<'WD_EOF'
#!/usr/bin/env bash
# Cycle the UDC binding when the host hasn't enumerated us.
# Pi 5's dwc2 can't observe VBUS, so this is our only retry mechanism.
set -u

GADGET_DIR=/sys/kernel/config/usb_gadget/vasili
USB_IFACE=usb0
CHECK_INTERVAL=10
NOT_UP_THRESHOLD=3   # consecutive checks (~30s) before we cycle

down_streak=0

while true; do
    sleep "$CHECK_INTERVAL"

    # No gadget composed yet? Try to compose it.
    if [[ ! -d "$GADGET_DIR" ]] || [[ ! -s "$GADGET_DIR/UDC" ]]; then
        if [[ -x /etc/vasili/usb-gadget-compose.sh ]]; then
            /etc/vasili/usb-gadget-compose.sh \
                && logger -t vasili-usb-watchdog "composed gadget"
        fi
        down_streak=0
        continue
    fi

    carrier=$(cat /sys/class/net/$USB_IFACE/carrier 2>/dev/null || echo 0)
    udc_state=$(cat /sys/class/udc/*/state 2>/dev/null | head -1)

    if [[ "$carrier" == "1" ]]; then
        down_streak=0
        continue
    fi

    down_streak=$((down_streak + 1))
    if (( down_streak < NOT_UP_THRESHOLD )); then
        continue
    fi

    # Cycle UDC binding. Empty write → unbind; UDC name → re-bind. This is
    # what the host (Mac) sees as a fresh device attach.
    udc_name=$(ls /sys/class/udc 2>/dev/null | head -1)
    if [[ -n "$udc_name" ]]; then
        echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
        sleep 0.5
        echo "$udc_name" > "$GADGET_DIR/UDC" 2>/dev/null || true
        logger -t vasili-usb-watchdog \
            "cycled UDC (carrier=$carrier udc_state=${udc_state:-?})"
    fi
    down_streak=0
done
WD_EOF
    chmod +x /etc/vasili/usb-gadget-watchdog.sh
    log "Wrote /etc/vasili/usb-gadget-watchdog.sh"

    cat > /etc/systemd/system/vasili-usb-watchdog.service <<'EOF'
[Unit]
Description=Vasili USB Gadget Watchdog (host-enumeration retry)
After=vasili-usb-gadget.service
Requires=vasili-usb-gadget.service

[Service]
Type=simple
ExecStart=/etc/vasili/usb-gadget-watchdog.sh
Restart=on-failure
RestartSec=5
# Quiet journald — watchdog is meant to be silent unless it cycles.
StandardOutput=null

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable vasili-usb-watchdog.service
    systemctl restart vasili-usb-watchdog.service 2>/dev/null || \
        log "Watchdog will start at next boot"
    log "vasili-usb-watchdog.service enabled"
}

# ============================================================================
# Step 5: mDNS — vasili.local resolves on every reasonable client
# ============================================================================

setup_mdns() {
    hdr "mDNS (avahi) — vasili.local"

    # Install avahi if needed
    if ! command -v avahi-daemon &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq avahi-daemon
    fi

    # Set hostname to 'vasili' so avahi advertises vasili.local
    local current_hostname
    current_hostname=$(hostnamectl --static 2>/dev/null || hostname)
    if [[ "$current_hostname" != "vasili" ]]; then
        hostnamectl set-hostname vasili
        log "Hostname set to 'vasili' (was '$current_hostname')"
    else
        log "Hostname already 'vasili'"
    fi

    # /etc/hosts entry so local lookups (and avahi) agree
    if ! grep -qE '^[0-9.]+\s+vasili(\s|$)' /etc/hosts; then
        echo "127.0.1.1 vasili" >> /etc/hosts
        log "Added vasili to /etc/hosts"
    fi

    systemctl enable avahi-daemon.service
    systemctl restart avahi-daemon.service
    log "avahi-daemon running — vasili.local advertised via mDNS"
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
    echo -e "${BOLD}Gadget binding:${NC}"
    local gadget_udc gadget_state
    gadget_udc=$(cat /sys/kernel/config/usb_gadget/vasili/UDC 2>/dev/null || echo "")
    gadget_state=$(cat /sys/class/udc/*/state 2>/dev/null | head -1 || echo "unknown")
    if [[ -n "$gadget_udc" ]]; then
        if [[ "$gadget_state" == "configured" ]]; then
            echo -e "  configfs gadget: ${GREEN}BOUND${NC} ($gadget_udc, state=$gadget_state)"
        else
            echo -e "  configfs gadget: ${YELLOW}BOUND but host not enumerated${NC} ($gadget_udc, state=$gadget_state)"
        fi
    else
        echo -e "  configfs gadget: ${RED}NOT COMPOSED${NC}"
    fi

    echo ""
    echo -e "${BOLD}Services:${NC}"
    for svc in vasili-usb-gadget vasili-usb-watchdog vasili-usb-dhcp vasili-usb-nat; do
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
    echo "  Friendly: http://vasili.local/   (mDNS + dnsmasq, port 80 redirected to 5000)"
    echo "  Via USB-C: ssh ubuntu@$MGMT_IP_USB  /  http://$MGMT_IP_USB/"
    echo "  Via eth0:  ssh ubuntu@$MGMT_IP_ETH  /  http://$MGMT_IP_ETH/"
}

# ============================================================================
# Remove
# ============================================================================

remove() {
    hdr "Removing USB Gadget Configuration"

    for svc in vasili-usb-nat vasili-usb-dhcp vasili-usb-watchdog vasili-usb-gadget; do
        systemctl stop "${svc}.service" 2>/dev/null || true
        systemctl disable "${svc}.service" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
    done

    # Unbind UDC and tear down the configfs gadget, if present.
    if [[ -d /sys/kernel/config/usb_gadget/vasili ]]; then
        echo "" > /sys/kernel/config/usb_gadget/vasili/UDC 2>/dev/null || true
    fi

    rm -f /etc/dnsmasq.d/vasili-usb-gadget.conf
    rm -f /etc/NetworkManager/conf.d/99-unmanaged-mgmt.conf
    rm -f /etc/NetworkManager/conf.d/99-unmanaged-usb0.conf
    rm -f /etc/systemd/network/50-vasili-usb0.network
    rm -f /etc/systemd/network/50-vasili-eth0.network
    rm -f /etc/vasili/usb-gadget-nat.sh
    rm -f /etc/vasili/usb-gadget-compose.sh
    rm -f /etc/vasili/usb-gadget-watchdog.sh
    rm -f /etc/sysctl.d/99-vasili-usb.conf

    # Flush NAT chains
    iptables -F VASILI-USB-FWD 2>/dev/null || true
    iptables -X VASILI-USB-FWD 2>/dev/null || true
    iptables -t nat -F VASILI-USB-NAT 2>/dev/null || true
    iptables -t nat -X VASILI-USB-NAT 2>/dev/null || true
    iptables -t nat -F VASILI-USB-PRE 2>/dev/null || true
    iptables -t nat -X VASILI-USB-PRE 2>/dev/null || true

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
    setup_gadget_compose
    setup_usb_network
    setup_dhcp
    setup_nat
    setup_gadget_watchdog
    setup_mdns

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
    echo "  Friendly:   http://vasili.local/   (mDNS + dnsmasq)"
    echo "  Via USB-C:  ssh ubuntu@${MGMT_IP_USB}   http://${MGMT_IP_USB}/"
    echo "  Via eth0:   ssh ubuntu@${MGMT_IP_ETH}   http://${MGMT_IP_ETH}/"
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

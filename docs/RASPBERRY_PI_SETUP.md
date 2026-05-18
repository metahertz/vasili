# Vasili on Raspberry Pi -- Setup Guide

This guide covers deploying Vasili on a Raspberry Pi from scratch, based on actual deployment experience on real hardware.

---

## 1. Recommended Hardware

### Board

- **Raspberry Pi 4** (4 GB or 8 GB RAM) or **Raspberry Pi 5** -- both are aarch64 (ARM64).
- A 4 GB (or larger) microSD card. 8 GB+ is recommended if you plan to store logs or a local MongoDB instance.

### WiFi Adapters

The Pi's built-in WiFi chip (`wlan0`) works well for **scanning**, but for connecting to networks you generally want one or more USB adapters so the scanner can keep running independently.

Tested and confirmed working:

| Adapter | USB ID | Chipset | Notes |
|---|---|---|---|
| Realtek RTL8187 | `0bda:8187` | RTL8187 | Long-range 802.11b/g, high-gain antenna options |
| Qualcomm Atheros AR9271 | `0cf3:9271` | AR9271 | Reliable, well-supported in mainline Linux |

**Important:** USB WiFi adapters show up as `wlx<mac>` interfaces (e.g., `wlxc83a35c2a49d`), **not** `wlan0` / `wlan1`. This is the systemd predictable-naming scheme. Plan your `config.yaml` interface names accordingly.

You can find interface names with:

```bash
ls /sys/class/net/*/wireless
# or
ip link show
```

---

## 2. OS Installation

**Tested OS:** Ubuntu 25.10 Server for ARM64.

1. Download the Ubuntu 25.10 Server image for Raspberry Pi from <https://ubuntu.com/download/raspberry-pi>.
2. Flash it to your SD card with `rpi-imager`, `dd`, or Balena Etcher.
3. Boot the Pi and complete first-login setup. Ensure you have a wired Ethernet connection (`eth0`) for initial access -- everything below assumes SSH over Ethernet.

---

## 3. Step-by-step Installation

### 3.1 Install system dependencies

```bash
sudo apt-get update
sudo apt-get install -y \
  python3-venv python3-pip python3-dev \
  network-manager iw rfkill \
  dnsmasq iptables \
  build-essential libnetfilter-queue-dev
```

> **Note:** The `wireless-tools` package (which provides `iwlist` and `iwconfig`) is **no longer available** on Ubuntu 25.10. Vasili uses `nmcli` and `iw` instead, so this is not a problem -- just be aware that legacy WiFi tutorials referencing `iwconfig` will not apply.

### 3.2 Protect eth0 from NetworkManager

NetworkManager will try to manage all interfaces. Since you are using Ethernet for SSH/management, you should tell NM to leave `eth0` alone so it does not get disrupted:

```bash
sudo tee /etc/NetworkManager/conf.d/99-unmanaged-eth0.conf > /dev/null <<'EOF'
[keyfile]
unmanaged-devices=interface-name:eth0
EOF

sudo systemctl restart NetworkManager
```

Verify with:

```bash
nmcli device status
```

`eth0` should show as `unmanaged`.

### 3.3 Get the Vasili source

Clone the repository (or copy files) to `/home/ubuntu/vasili`:

```bash
# If cloning:
git clone <repo-url> /home/ubuntu/vasili

# Create the symlink the systemd service expects:
sudo ln -s /home/ubuntu/vasili /opt/vasili
```

The service file (`vasili.service`) references `/opt/vasili` as its working directory, so the symlink is required.

### 3.4 Set up the Python environment

```bash
cd /home/ubuntu/vasili
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3.5 Configure Vasili

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set your interface preferences. A typical Raspberry Pi setup:

```yaml
interfaces:
  preferred:
    - wlxc83a35c2a49d   # USB adapter 1 (use your actual wlx* name)
    - wlx00e04c534708   # USB adapter 2 (if you have one)
  excluded: []
  scan_interface: wlan0  # built-in WiFi, dedicated to scanning
```

### 3.6 Unblock WiFi

On a fresh Ubuntu install the WiFi radios are often soft-blocked by default:

```bash
sudo rfkill unblock wifi
```

Verify with `rfkill list` -- all wireless entries should show `Soft blocked: no`.

### 3.7 Install and start the systemd service

```bash
sudo cp vasili.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vasili
```

---

## 4. Configuration Notes

### Interface selection

- Set `scan_interface` to the **built-in** `wlan0`. It is the most reliable for scanning because it is always present and does not depend on a USB connection.
- List USB adapters under `preferred` in priority order. Vasili leases them to modules for connections.

### MongoDB is optional

The `captive_portal` section in `config.yaml` references a MongoDB URI for storing portal patterns. If you do not need captive portal pattern storage, you can leave this as-is or set it to an empty string. Vasili runs fine without a MongoDB instance -- portal detection still works, it just won't persist learned patterns across restarts.

### Web interface

By default Vasili binds to `0.0.0.0:5000`. This is controlled by the `web` section in `config.yaml`.

---

## 5. Verification

### Check the service is running

```bash
systemctl status vasili
```

You should see `Active: active (running)`.

### Hit the API

```bash
curl http://localhost:5000/api/status
```

### Open the Web UI

From another machine on the same network, open:

```
http://<pi-ip>:5000
```

where `<pi-ip>` is the Pi's Ethernet IP address.

---

## 6. Troubleshooting

### Live logs

```bash
journalctl -u vasili -f
```

### eth0 managed by NetworkManager

If your SSH connection drops after starting Vasili, NetworkManager may have taken over `eth0`. Verify:

```bash
nmcli device status
```

`eth0` must show as `unmanaged`. If it does not, revisit step 3.2.

### USB WiFi adapter not detected

Check which wireless interfaces the kernel sees:

```bash
ls /sys/class/net/*/wireless
```

If your adapter does not appear, check `dmesg | tail -30` for firmware or driver errors. Both the RTL8187 and AR9271 use in-tree drivers on Ubuntu 25.10 and should work without additional firmware packages.

### WiFi is soft-blocked

```bash
rfkill list
```

If any wireless device shows `Soft blocked: yes`:

```bash
sudo rfkill unblock wifi
```

### Service fails to start

Common causes:

- **Missing venv or dependencies:** Re-run `./venv/bin/pip install -r requirements.txt`.
- **Symlink missing:** Ensure `/opt/vasili` points to `/home/ubuntu/vasili`.
- **Port 5000 in use:** Check with `ss -tlnp | grep 5000` and stop the conflicting process.
- **Permission denied:** The service runs as root (required for network management). Make sure the service file has `User=root`.

### Interface names changed after reboot

USB adapter interface names (`wlx*`) are derived from the adapter's MAC address, so they are stable across reboots. However, if you swap adapters or use a USB hub, the names will change. Update `config.yaml` to match the new names shown by `ip link show`.

---

## 7. USB-C Gadget Mode (Management Interface)

The Raspberry Pi 5's USB-C power port supports USB gadget mode. When enabled, plugging the Pi into a laptop makes it appear as a USB Ethernet adapter, giving you a dedicated management link without needing WiFi or a separate Ethernet cable.

### What you get

| Feature | Details |
|---|---|
| Pi IP | `10.55.0.1` on `usb0` |
| Laptop IP | `10.55.0.50-150` (DHCP from Pi) |
| SSH | `ssh ubuntu@10.55.0.1` |
| Web UI | `http://10.55.0.1:5000` |
| Internet sharing | If Vasili has a WiFi connection, the laptop routes through it |

### Quick setup

```bash
sudo bash server/vasili-usb-gadget-setup.sh install
sudo reboot
```

After reboot, plug the Pi's USB-C port into your laptop. You should get an IP in the `10.55.0.x` range automatically.

### How it works

The setup script:
1. Enables the `dwc2` overlay in **peripheral mode** (`dtoverlay=dwc2,dr_mode=peripheral`) and the `g_ether` kernel module (creates `usb0`). Peripheral mode is required — a bare `dtoverlay=dwc2` defaults to OTG, which on the Pi 5 USB-C port stays in host mode and the gadget never enumerates.
2. Assigns `10.55.0.1/24` to `usb0` via a systemd service
3. Runs a dedicated dnsmasq instance for DHCP on `usb0`
4. Configures iptables NAT so USB clients can reach the internet through Vasili's active WiFi connection

All four components run as systemd services (`vasili-usb-gadget`, `vasili-usb-dhcp`, `vasili-usb-nat`) and survive reboots.

### Status check

```bash
sudo bash server/vasili-usb-gadget-setup.sh status
```

### Uninstall

```bash
sudo bash server/vasili-usb-gadget-setup.sh remove
```

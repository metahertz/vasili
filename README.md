# Vasili

A WiFi connection orchestration daemon for Raspberry Pi (or any Linux box
with WiFi adapters). It scans, evaluates, and connects to nearby networks
through a pluggable pipeline of stages — handling captive portals,
credential probing, DNS-tunnel fallbacks, and a HostAP that NATs through
whichever upstream connection it finds. A Flask + Socket.IO web UI on
port 5000 (`/`, `/builder`, `/config`) drives the whole thing.

For more sophisticated bypass techniques (DNS tunneling, port-53
multiplexed VPN, PMKID-via-DNS offload cracking) vasili talks to an
**internet-side helper** that you run on a VM with a public IP. The
helper ships in this repo as a single Docker image; the Pi never needs
to know how the server side is wired up.

---

## Getting started

The end-to-end flow has two halves: the **server side** (helper, on a VM
with a public IP) and the **Pi side** (vasili, on the Raspberry Pi). You
do them in that order so vasili has somewhere to talk to.

### 1. Bring up the server side (`vasili-helper`)

On a small Ubuntu VM with a public IP and UDP/53 + TCP/53 free
(typically: disable systemd-resolved's stub listener):

```bash
git clone <this-repo> && cd vasili
docker compose -f helper/docker-compose.yml up -d --build
docker logs vasili-helper | grep 'HELPER_TOKEN'   # note the token printed on first boot
```

Then open `http://<server-public-ip>:8080` in a browser, paste the
token, and flip on whichever services you need:

| Toggle | Vasili stage that uses it | DNS delegation required? |
|---|---|---|
| **SSH tunnel (TCP/53)** | `dns_port_tunnel` (SSH path) | No |
| **iodine (UDP/53)** | `dns_tunnel` | Yes — NS-delegate a subdomain to the VM |
| **WireGuard (UDP/53)** | `dns_port_tunnel` (WG path) | No — demuxed by packet shape |
| **PMKID crack server** | `dns_offload_crack` | Yes — NS-delegate a subdomain |

All UDP/53 services can run **at the same time** on one public IP — the
helper's DNS proxy demuxes packets by shape (WireGuard) and DNS qname
suffix (iodine vs crack). See `helper/README.md` for the full details,
DNS-delegation recipes, and per-section file downloads (SSH key, WG
client config, etc.) the UI offers.

### 2. Bring up the Pi side (`vasili`)

Flash a Raspberry Pi with Ubuntu (server or core; vasili expects
NetworkManager). Connect one WiFi interface to a network that has
internet so you can SSH in for the install, then:

```bash
./deploy.sh <pi-ip>           # transfers code, installs deps, enables systemd unit
ssh root@<pi-ip> 'systemctl start vasili'
```

Now browse to `http://<pi-ip>:5000` and:

1. Open **Settings** → **Helper Connection Settings** and paste the
   helper UI's **Client Config** copy-paste block into the import box,
   then hit **Import Settings**. Vasili parses it and fills in the
   DNS-tunnel / SSH / WireGuard / PMKID-crack stage settings for you (no
   manual field-by-field entry). Then enable the matching stages under
   **Modules** and grant any consent they require.
2. **Host Access Point** (settings page) is optional but useful — it
   reserves one of the Pi's WiFi cards as a local AP that NATs through
   whichever upstream vasili picks.

`DEPLOYMENT.md` covers the full deploy script options, systemd
management, and post-install operations.

### Where to go next

- **`DEPLOYMENT.md`** — deploy script reference, systemd, troubleshooting.
- **`helper/README.md`** — helper container details, DNS delegation
  recipes, the multi-service UDP/53 demux design, port preflight
  (systemd-resolved), file downloads.
- **`ROADMAP.md`** — planned features.
- **`WEBSOCKET_SETUP.md`** — Socket.IO event reference for the dashboard.
- **`config.yaml`** — per-Pi vasili settings (preferred interfaces,
  modules enabled, scan card pinning).

## What is in the repo

- `vasili.py` — single-file core (~5k LOC). `WifiCardManager`,
  `ConnectionModule`, `PipelineModule`, `PipelineStage`, `HostAP`,
  `WifiManager`, all Flask routes.
- `modules/` — `ConnectionModule` subclasses auto-loaded by name from
  `config.yaml`'s `modules.enabled`.
- `modules/stages/` — `PipelineStage` building blocks shared across
  pipelines.
- `templates/` — dashboard, settings, pipeline-builder UI.
- `server/` — server-side scripts the helper image bundles
  (`vasili-dns-proxy.py`, `vasili-crack-server.py`,
  `vasili-server-setup.sh` for non-Docker installs).
- `helper/` — Docker image + Flask config UI for the server side.
- `tests/` — 386 tests; run with `./venv/bin/python -m pytest tests/ -q`.

## Local development

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python vasili.py     # runs on http://localhost:5000
./venv/bin/python -m pytest tests/ -q

# Optional: captive-portal headless-browser fallback (JS-only/tickbox portals).
# Skippable — the lightweight HTTP path degrades gracefully without it.
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright"
./venv/bin/python -m playwright install chromium
sudo env PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright" \
    ./venv/bin/python -m playwright install-deps chromium
```

MongoDB is optional — vasili degrades gracefully if it can't reach one.
The Playwright/Chromium captive-portal fallback is likewise optional; `deploy.sh`
installs it automatically on the target. See `docs/CAPTIVE_PORTAL.md`.

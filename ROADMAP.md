# Vasili Roadmap

> Get online "no matter what" - automated WiFi connectivity for travelers

## Current State

The project has an initial structure with:
- Core framework (`vasili.py`) - WiFi card management, network scanning, connection orchestration, NAT bridging
- Three connection modules: open networks, WPA2 networks, speedtest
- Basic Flask web interface (endpoints defined, no templates)

**Known Issues**: Module imports are broken, `WifiCard.connect()` not implemented, `scan_and_connect()` method missing, no tests, no CI.

---

## P0 - Core Functionality (Must Work First)

These items block all other work. The system cannot be tested or used without them.

- [ ] **Fix module import structure** - Modules use `from ..vasili import` but the package structure doesn't support this
- [ ] **Implement `WifiCard.connect(network)` method** - Core connection logic using `wpa_supplicant` or `nmcli`
- [ ] **Implement `WifiManager.scan_and_connect()` method** - Referenced in `main()` but not defined
- [ ] **Fix `modules/__init__.py`** - Currently named `init.py`, should be `__init__.py`
- [ ] **Add basic CI** - Run linting and import checks on PRs
- [ ] **Create Flask templates** - `index.html` for the web interface

---

## P0.5 - Deployment

Basic deployment tooling to get vasili onto target hardware.

- [ ] **SSH deployment script** - Simple script to deploy vasili to an Ubuntu-based micro router via SSH. Should handle file transfer, dependency installation, and basic service setup.

---

## P1 - Reliability & Testing

Once P0 is complete, focus on making it reliable.

- [ ] **Add unit tests for core classes** - WifiCard, WifiCardManager, NetworkScanner
- [ ] **Add integration tests** - Mock WiFi interfaces for testing scan/connect flow
- [ ] **Error handling improvements** - Graceful degradation when no cards available
- [ ] **Connection retry logic** - Automatic reconnection on drop
- [ ] **Logging improvements** - Structured logging, log levels, file output
- [ ] **Configuration file support** - YAML/JSON config for interface preferences, module enable/disable

---

## P2 - Feature Completeness

Features that make the tool genuinely useful.

- [ ] **Captive portal module** - Detect and attempt to authenticate through common captive portals
- [ ] **WPA3 support** - Modern encryption standard
- [ ] **Connection scoring algorithm** - Rank connections by speed, stability, signal strength
- [ ] **Auto-selection mode** - Automatically use the best available connection
- [ ] **Web UI improvements** - Real-time status updates, connection history, manual override
- [ ] **Systemd service** - Run as a daemon on boot
- [ ] **Multi-card orchestration** - Dedicated scanning card vs connection cards as described in README

---

## P3 - Nice to Have

Lower priority enhancements.

- [ ] **REST API documentation** - OpenAPI/Swagger spec
- [ ] **Connection persistence** - Remember working networks and their credentials
- [ ] **Notification system** - Alert when connection changes or degrades
- [ ] **Bandwidth monitoring** - Track usage over time
- [ ] **Raspberry Pi setup guide** - Hardware recommendations, SD card image

---

## Out of Scope

These will not be implemented (at least not now):

- **Offensive modules** (DNS tunneling, ICMP tunneling, WPA brute force) - The README mentions these as possibilities, but they require careful consideration, explicit user consent flows, and are legally sensitive. Defer indefinitely.
- **Mobile app** - Focus on the core Pi-based solution first
- **Cloud connectivity** - This is a local-first tool
- **Commercial captive portal bypass** - Only handle standard open portals, not paid services

---

## Contributing

PRs should target items in priority order (P0 before P1, etc.). Each PR should:
1. Include tests for new functionality
2. Pass CI checks
3. Update this roadmap if scope changes

See README.md for project context and motivation.

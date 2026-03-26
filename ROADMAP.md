# Vasili Roadmap

> Get online "no matter what" - automated WiFi connectivity for travelers

## Current State

**Status:** Core functionality complete and tested! ✅

The project now has:
- ✅ **Fully functional core framework** - WiFi card management, network scanning, connection orchestration, NAT bridging
- ✅ **Three connection modules** - Open networks, WPA2 networks, speedtest
- ✅ **Complete web interface** - Flask app with templates
- ✅ **Comprehensive test suite** - Unit and integration tests with CI
- ✅ **Production-ready features** - Error handling, logging, config files, auto-reconnect
- ✅ **Deployment tooling** - SSH deployment script for Ubuntu-based routers

**Completed:** All P0, P0.5, P1, P2, and P3 items (30 total features)
**Deployed:** Running on Raspberry Pi with 3 WiFi interfaces

---

## P0 - Core Functionality (Must Work First) ✅ COMPLETE

These items block all other work. The system cannot be tested or used without them.

- [x] **Fix module import structure** - Modules use `from vasili import` and work correctly
- [x] **Implement `WifiCard.connect(network)` method** - Core connection logic implemented (vasili.py:481, 1005)
- [x] **Implement `WifiManager.scan_and_connect()` method** - Implemented in vasili.py:1127
- [x] **Fix `modules/__init__.py`** - Fixed and present in modules directory
- [x] **Add basic CI** - Full CI pipeline with lint, import checks, and tests (.github/workflows/ci.yml)
- [x] **Create Flask templates** - templates/index.html created

---

## P0.5 - Deployment ✅ COMPLETE

Basic deployment tooling to get vasili onto target hardware.

- [x] **SSH deployment script** - deploy.sh handles file transfer, dependency installation, systemd service setup (PR #7)

---

## P1 - Reliability & Testing ✅ COMPLETE

Once P0 is complete, focus on making it reliable.

- [x] **Add unit tests for core classes** - WifiCard, WifiCardManager, NetworkScanner (PR #12)
- [x] **Add integration tests** - Mock WiFi interfaces for testing scan/connect flow (PR #15)
- [x] **Error handling improvements** - SystemHealth class with graceful degradation (PR #13)
- [x] **Connection retry logic** - ConnectionMonitor with reconnect() method and exponential backoff (PR #9)
- [x] **Logging improvements** - Structured logging with configurable levels and file output (PR #11)
- [x] **Configuration file support** - YAML config for interface preferences, module enable/disable (PR #10)

---

## P2 - Feature Completeness ✅ COMPLETE

Features that make the tool genuinely useful.

- [x] **Captive portal module** - Detect and attempt to authenticate through common captive portals (PR #19)
- [x] **WPA3 support** - Modern encryption standard (PR #34)
- [x] **Connection scoring algorithm** - Rank connections by speed, stability, signal strength (PR #21)
- [x] **Auto-selection mode** - Automatically use the best available connection
- [x] **Web UI improvements** - Real-time status updates, connection history, manual override (PR #18)
- [x] **Systemd service** - Run as a daemon on boot
- [x] **Multi-card orchestration** - Dedicated scanning card vs connection cards as described in README (WifiCardManager with scanning_card/connection_cards role separation)

---

## P3 - Nice to Have ✅ COMPLETE

Lower priority enhancements.

- [x] **REST API documentation** - OpenAPI/Swagger spec at docs/openapi.yaml
- [x] **Connection persistence** - MongoDB-backed storage of working networks (persistence.py)
- [x] **Notification system** - WebSocket/webhook/log alerts on connection changes (notifications.py)
- [x] **Bandwidth monitoring** - Per-interface bandwidth tracking with history (bandwidth.py)
- [x] **Raspberry Pi setup guide** - Tested hardware, step-by-step guide (docs/RASPBERRY_PI_SETUP.md)

---

## P4 - Pipeline Architecture & Modules (IN PROGRESS)

Infrastructure for chaining multiple sub-modules against a single connected network.

- [x] **Pipeline infrastructure** - PipelineStage, StageResult, PipelineModule base classes with context passing, auto_connect flag
- [x] **Reusable stage library** - modules/stages/ package with shared stages (ConnectivityCheck, DnsProbe, CaptivePortal, SavedCredentials, ConfiguredKeys)
- [x] **Module config store** - MongoDB-backed per-module settings with schema declaration (module_config.py)
- [x] **Consent system** - Per-module consent for offensive stages, MongoDB + YAML fallback (consent.py)
- [x] **Config/consent API** - GET/PUT /api/modules/<name>/config, POST /api/modules/<name>/consent
- [x] **Module priority sorting** - Modules sorted by priority in scan loop
- [x] **Parallel network testing** - ThreadPoolExecutor with one worker per connection card
- [x] **Open Network Pipeline** - connectivity_check → captive_portal → mac_clone → dns_probe
- [x] **WPA2 Network Pipeline** - saved_credentials → configured_keys → connectivity_check → dns_probe
- [x] **WPA3 Network Pipeline** - saved_credentials → configured_keys → connectivity_check → dns_probe
  - [x] ConnectivityCheckStage - Verify internet access
  - [x] CaptivePortalStage - Detect and auto-authenticate portals
  - [x] DnsProbeStage - Test external DNS reachability (TCP/UDP, configurable targets)
  - [x] MacCloneStage - Clone authenticated client MACs to bypass portals (requires consent)
- [x] **Hidden network discovery** - Resolve hidden SSIDs via saved connections, directed probes, and passive sniffing (modules/hiddenNetwork.py)

---

- [x] **PMKID capture & crack** - Capture PMKID from WPA2 APs via hcxdumptool, crack with hashcat or Python PBKDF2 fallback (modules/stages/pmkid.py, requires consent)

---

## P5 - Tunneling Modules (PLANNED)

Last-resort connectivity when HTTP is blocked but DNS/ICMP passes through.

- [ ] **DNS tunnel** - iodine-based DNS tunneling (requires consent + own server)
- [ ] **ICMP tunnel** - hans/ptunnel-based ICMP tunneling (requires consent + own server)

---

## P6 - Credential-based Modules (PLANNED)

Opt-in modules with explicit consent gates for authorized testing.

- [ ] **WPS PIN** - WPS PIN brute force via reaver/bully (requires consent)
- [ ] **WPA wordlist** - Dictionary attack via aircrack-ng (requires consent)

---

## P7 - Recon Modules (PLANNED)

Background intelligence gathering about the wireless environment.

- [ ] **Signal mapper** - Track signal strength over time per network
- [ ] **Network profiler** - Fingerprint APs (vendor, capabilities, security config)
- [ ] **Client monitor** - Observe client devices, feeds MAC clone candidates (requires consent)

---

## Out of Scope

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

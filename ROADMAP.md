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

**Completed:** All P0, P0.5, P1 items plus 5 P2 features (23 total features)
**Next:** Remaining P2 work (WPA3, systemd service)

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

## P2 - Feature Completeness

Features that make the tool genuinely useful.

- [x] **Captive portal module** - Detect and attempt to authenticate through common captive portals (PR #19)
- [ ] **WPA3 support** - Modern encryption standard
- [x] **Connection scoring algorithm** - Rank connections by speed, stability, signal strength (PR #21)
- [x] **Auto-selection mode** - Automatically use the best available connection
- [x] **Web UI improvements** - Real-time status updates, connection history, manual override (PR #18)
- [ ] **Systemd service** - Run as a daemon on boot
- [x] **Multi-card orchestration** - Dedicated scanning card vs connection cards as described in README (WifiCardManager with scanning_card/connection_cards role separation)

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

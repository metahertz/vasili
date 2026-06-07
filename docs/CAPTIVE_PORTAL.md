# Captive Portal Module

The Captive Portal module automatically detects and authenticates through captive portal networks commonly found in hotels, airports, cafes, and public spaces.

## Features

- **Automatic Detection**: Detects captive portals using HTTP redirect analysis
- **Multi-Portal Support**: Recognizes common portal types (Apple, Google, Microsoft, etc.)
- **Pattern Storage**: Learns and remembers portal patterns using MongoDB
- **Authentication Methods**: Handles click-through, terms acceptance, and more
- **Graceful Degradation**: Works without MongoDB (detection still functions)

## How It Works

1. **Connect**: Establishes connection to the WiFi network
2. **Detect**: Tests connectivity using known URLs (e.g., http://captive.apple.com)
3. **Analyze**: Identifies portal type and authentication method from redirects
4. **Authenticate**: Attempts automatic authentication based on portal type
5. **Verify**: Runs speedtest to confirm internet connectivity

## Detection Methods

The module tests multiple connectivity check URLs:
- `http://captive.apple.com/hotspot-detect.html`
- `http://connectivitycheck.gstatic.com/generate_204`
- `http://clients3.google.com/generate_204`
- `http://www.msftconnecttest.com/connecttest.txt`

When a captive portal is present, these requests return HTTP redirects (302/303) instead of the expected responses.

## Supported Authentication Types

### ✅ Automatic (Implemented)
- **Click-through**: Portals that just need you to visit a page
- **Terms acceptance**: Portals with simple accept buttons
- **Tickbox + single button**: Forms whose only controls are a checkbox and/or
  one (even unnamed) submit button
- **JS-only splash pages**: Portals with no real `<form>` — a `<button onclick>`
  or link that fires the "go online" request — via the headless-browser fallback

Authentication runs in three strategies, cheapest first, and **success is
confirmed by a real connectivity recheck** (HTTP 204 from a generate_204 URL),
not by page heuristics:

1. **Smart form parse + autofill** — stdlib HTML parsing, fills/ticks fields,
   submits (handles named *and* unnamed submit controls).
2. **Click-through** — follows the redirect for redirect-only portals.
3. **Headless browser (Playwright/Chromium)** — ticks checkboxes and clicks the
   accept/continue control for JS-driven portals. Requires the browser to be
   installed (see *Headless-Browser Fallback* below); **degrades gracefully** —
   if Playwright/Chromium are absent, strategies 1–2 still run.

### ⚠️ Manual (Not Automated)
- **Login required**: Portals requiring username/password
- **Payment required**: Paid WiFi services
- **Social login**: Facebook/Google authentication portals
- **SMS verification**: Phone number verification portals

## MongoDB Integration

The module uses MongoDB to store and reuse successful portal patterns:

```javascript
{
  "ssid": "Airport-WiFi",
  "redirect_domain": "portal.airport.com",
  "portal_type": "generic",
  "auth_method": "click_through",
  "success_count": 5,
  "failure_count": 0,
  "last_seen": 1706983200.0
}
```

### Setup MongoDB (Optional)

MongoDB is **optional** - the module works without it, but pattern storage improves performance for networks you use repeatedly.

#### Install MongoDB (Ubuntu/Debian):
```bash
# Install MongoDB
sudo apt-get update
sudo apt-get install -y mongodb

# Start MongoDB service
sudo systemctl start mongodb
sudo systemctl enable mongodb

# Verify it's running
sudo systemctl status mongodb
```

#### Configure MongoDB Connection:

Edit `config.yaml`:
```yaml
captive_portal:
  mongodb_uri: "mongodb://localhost:27017/"
```

For remote MongoDB:
```yaml
captive_portal:
  mongodb_uri: "mongodb://username:password@host:27017/vasili"
```

To disable MongoDB (detection still works):
```yaml
captive_portal:
  mongodb_uri: ""
```

## Configuration

Add to `config.yaml`:

```yaml
modules:
  enabled:
    - openNetwork
    - wpa2Network
    - captivePortal  # Add this line

captive_portal:
  # MongoDB connection (optional)
  mongodb_uri: "mongodb://localhost:27017/"

  # Detection timeout in seconds
  detection_timeout: 10

  # Authentication timeout in seconds
  auth_timeout: 15

  # Headless-browser fallback for JS-only portals (default true). If the
  # browser isn't installed, this silently no-ops and the HTTP path governs.
  use_browser: true

  # Browser interaction budget in seconds
  browser_timeout: 30
```

## Headless-Browser Fallback (Playwright/Chromium)

Strategy 3 drives a headless Chromium to solve portals that have no real HTML
form (JS `onclick`/XHR splash pages). It is **optional** and degrades
gracefully — if Playwright or Chromium is missing, the lightweight HTTP path
still runs and the pipeline never crashes.

`deploy.sh` installs this automatically. To set it up manually inside the app's
virtualenv:

```bash
# 1) Python package (already in requirements.txt)
venv/bin/pip install playwright

# 2) Browser binary — pin the cache under the app dir so a root systemd
#    service finds it regardless of $HOME
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright"
venv/bin/python -m playwright install chromium

# 3) OS libraries Chromium needs (apt; arm64-friendly on the Pi)
sudo env PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright" \
    venv/bin/python -m playwright install-deps chromium
```

The systemd unit (`vasili.service`) sets
`Environment="PLAYWRIGHT_BROWSERS_PATH=/opt/vasili/.playwright"` so the running
service uses the same browser the installer downloaded. If the env var is unset
(e.g. a manual `python vasili.py`), the code defaults to `<repo>/.playwright`.

## Usage

The module works automatically when enabled. It prioritizes open networks since they're most likely to have captive portals.

### Manual Testing:

```python
from modules.captivePortal import CaptivePortalDetector

detector = CaptivePortalDetector()
portal_info = detector.detect()

if portal_info:
    print(f"Portal detected: {portal_info['portal_type']}")
    print(f"Auth method: {portal_info['auth_method']}")
    print(f"Redirect: {portal_info['redirect_url']}")
else:
    print("No captive portal detected")
```

## Limitations

- Handles unauthenticated portals (click-through, terms, tickbox, single button,
  and JS-only splash pages via the browser fallback); cannot bypass paid WiFi or
  login/credential/SMS/social-login portals
- The headless-browser fallback can't bind a specific source IP, so it relies on
  the portal being reachable on the connected card's subnet (the usual case);
  per-interface egress for multi-card setups is a known TODO
- Detection requires HTTP access (some networks block even test URLs)

## Privacy & Ethics

This module:
- ✅ Automates legitimate free access (click-through, terms acceptance)
- ✅ Stores only portal patterns (no credentials or personal data)
- ❌ Does NOT bypass paid services
- ❌ Does NOT exploit security vulnerabilities
- ❌ Does NOT attempt to circumvent authentication requirements

## Troubleshooting

### Portal not detected
- Check if the network actually has a captive portal (try browsing)
- Verify HTTP traffic isn't blocked
- Check logs: `VASILI_LOG_LEVEL=DEBUG vasili.py`

### Authentication fails
- The portal may require manual interaction
- Check portal_info to see detected auth_method
- Some portals need specific user agents or cookies

### MongoDB connection fails
- Verify MongoDB is running: `sudo systemctl status mongodb`
- Check connection string in config.yaml
- Module will continue without MongoDB (no pattern storage)

## Development

### Adding New Portal Types

To add support for a new portal type, update `_analyze_portal()` in `modules/captivePortal.py`:

```python
elif 'newportal' in domain:
    portal_info['portal_type'] = 'newportal'
```

### Testing

```bash
# Run unit tests
pytest tests/unit/test_captive_portal.py -v

# Run integration tests
pytest tests/integration/test_captive_portal_flow.py -v
```

## Future Enhancements

- [x] Browser automation for complex portals (Playwright/Chromium) — *done; see Headless-Browser Fallback*
- [ ] Per-interface egress for the browser fallback (network namespace / policy routing)
- [ ] Social login support (OAuth flows)
- [ ] Custom portal scripts per SSID
- [ ] Manual credential storage for login-required portals
- [ ] HTTPS portal detection (DNS-based)
- [ ] Portal timeout and retry logic
- [ ] Machine learning for pattern recognition

## References

- [RFC 8910 - Captive Portal Architecture](https://datatracker.ietf.org/doc/html/rfc8910)
- [Apple Captive Portal Detection](https://developer.apple.com/library/archive/documentation/NetworkingInternet/Conceptual/NetworkingOverview/CaptivePortals/CaptivePortals.html)
- [Android Captive Portal Detection](https://android.googlesource.com/platform/frameworks/base/+/master/services/core/java/com/android/server/connectivity/NetworkMonitor.java)

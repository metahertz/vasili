"""Sample data for testing WiFi operations."""

# nmcli -t -f SSID,BSSID,SIGNAL,CHAN,SECURITY scan output
# Note: BSSID colons are escaped as \: in nmcli -t output
NMCLI_SCAN_OUTPUT = (
    "OpenCafe:00\\:11\\:22\\:33\\:44\\:55:95:6:\n"
    "SecureHome:AA\\:BB\\:CC\\:DD\\:EE\\:FF:71:11:WPA2\n"
    "WeakSignal:11\\:22\\:33\\:44\\:55\\:66:40:1:WPA1\n"
    "ModernWiFi:77\\:88\\:99\\:AA\\:BB\\:CC:85:36:WPA3\n"
)

# Empty scan output
NMCLI_SCAN_EMPTY = ""

# Legacy names kept for backward compatibility
IWLIST_SCAN_OUTPUT = NMCLI_SCAN_OUTPUT
IWLIST_SCAN_EMPTY = NMCLI_SCAN_EMPTY

# Sample network data structures
SAMPLE_NETWORKS = [
    {
        'ssid': 'OpenCafe',
        'bssid': '00:11:22:33:44:55',
        'is_open': True,
        'signal': 120,  # -40 dBm converted to percentage
        'channel': 6,
        'encryption_type': '',
    },
    {
        'ssid': 'SecureHome',
        'bssid': 'AA:BB:CC:DD:EE:FF',
        'is_open': False,
        'signal': 80,  # -60 dBm converted to percentage
        'channel': 11,
        'encryption_type': 'WPA2',
    },
    {
        'ssid': 'WeakSignal',
        'bssid': '11:22:33:44:55:66',
        'is_open': False,
        'signal': 40,  # -80 dBm converted to percentage
        'channel': 1,
        'encryption_type': 'WPA',
    },
    {
        'ssid': 'ModernWiFi',
        'bssid': '77:88:99:AA:BB:CC',
        'is_open': False,
        'signal': 100,  # -50 dBm converted to percentage
        'channel': 36,
        'encryption_type': 'WPA3',
    },
]

# Sample iwconfig output (legacy, no longer used in production)
IWCONFIG_OUTPUT_VALID = """wlan0     IEEE 802.11  ESSID:off/any
          Mode:Managed  Access Point: Not-Associated   Tx-Power=20 dBm
          Retry short limit:7   RTS thr:off   Fragment thr:off
          Power Management:off
"""

# Sample iwconfig error for invalid interface (legacy)
IWCONFIG_OUTPUT_INVALID = """eth0      no wireless extensions.
"""

# Sample nmcli connection success output
NMCLI_CONNECT_SUCCESS = """Device 'wlan0' successfully activated with 'abcd1234-5678-90ab-cdef-1234567890ab'.
"""

# Sample nmcli connection failure output
NMCLI_CONNECT_FAILURE = """Error: Connection activation failed: (7) Secrets were required, but not provided.
"""

# Sample interface operstate file contents
INTERFACE_UP = 'up\n'
INTERFACE_DOWN = 'down\n'

# Sample netifaces output
SAMPLE_INTERFACES = ['lo', 'eth0', 'wlan0', 'wlan1']
WIRELESS_INTERFACES = ['wlan0', 'wlan1']

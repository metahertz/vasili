"""Sample data for testing WiFi operations."""

# Realistic iwlist scan output with multiple networks
IWLIST_SCAN_OUTPUT = """wlan0     Scan completed :
          Cell 01 - Address: 00:11:22:33:44:55
                    Channel:6
                    Frequency:2.437 GHz (Channel 6)
                    Quality=70/70  Signal level=-40 dBm
                    Encryption key:off
                    ESSID:"OpenCafe"
                    Bit Rates:54 Mb/s
          Cell 02 - Address: AA:BB:CC:DD:EE:FF
                    Channel:11
                    Frequency:2.462 GHz (Channel 11)
                    Quality=50/70  Signal level=-60 dBm
                    Encryption key:on
                    ESSID:"SecureHome"
                    Bit Rates:54 Mb/s
                    IE: IEEE 802.11i/WPA2 Version 1
                        Group Cipher : CCMP
                        Pairwise Ciphers (1) : CCMP
                        Authentication Suites (1) : PSK
          Cell 03 - Address: 11:22:33:44:55:66
                    Channel:1
                    Frequency:2.412 GHz (Channel 1)
                    Quality=30/70  Signal level=-80 dBm
                    Encryption key:on
                    ESSID:"WeakSignal"
                    IE: WPA Version 1
"""

# Empty scan output
IWLIST_SCAN_EMPTY = """wlan0     Scan completed :
"""

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
]

# Sample iwconfig output for valid wireless interface
IWCONFIG_OUTPUT_VALID = """wlan0     IEEE 802.11  ESSID:off/any
          Mode:Managed  Access Point: Not-Associated   Tx-Power=20 dBm
          Retry short limit:7   RTS thr:off   Fragment thr:off
          Power Management:off
"""

# Sample iwconfig error for invalid interface
IWCONFIG_OUTPUT_INVALID = """eth0      no wireless extensions.
"""

# Sample nmcli connection success output
NMCLI_CONNECT_SUCCESS = """Device 'wlan0' successfully activated with 'abcd1234-5678-90ab-cdef-1234567890ab'.
"""

# Sample nmcli connection failure output
NMCLI_CONNECT_FAILURE = """Error: Connection activation failed: (7) Secrets were required, but not provided.
"""

# Sample interface operstate file contents
INTERFACE_UP = "up\n"
INTERFACE_DOWN = "down\n"

# Sample netifaces output
SAMPLE_INTERFACES = ['lo', 'eth0', 'wlan0', 'wlan1']
WIRELESS_INTERFACES = ['wlan0', 'wlan1']

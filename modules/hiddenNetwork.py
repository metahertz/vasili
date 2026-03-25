"""Hidden Network Discovery Module

Resolves hidden WiFi network SSIDs through passive and active techniques,
then connects and tests them like any other network.

Discovery methods (in order of invasiveness):
1. Saved connections — check nmcli profiles for matching BSSIDs
2. Directed probe scan — use iw scan with candidate SSIDs (managed mode)
3. Monitor mode capture — sniff probe request/response frames that reveal
   hidden SSIDs from clients and APs (uses tcpdump in monitor mode)

Once the SSID is resolved, the card is returned to managed mode and the
module connects via nmcli and runs a speedtest.
"""

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

from logging_config import get_logger
from vasili import ConnectionModule, WifiNetwork, ConnectionResult

logger = get_logger(__name__)


@dataclass
class ResolvedNetwork:
    """A hidden network whose SSID has been discovered."""
    original_bssid: str
    resolved_ssid: str
    method: str
    confidence: float


class HiddenNetworkModule(ConnectionModule):
    """Discover and connect to hidden WiFi networks.

    Runs at high priority (5) so it processes hidden networks before
    other modules skip them. Leases a card and uses it for scanning
    (managed mode directed probes and monitor mode frame capture)
    to discover the hidden SSID.
    """
    priority = 5

    def __init__(self, card_manager, probe_history=None, **kwargs):
        super().__init__(card_manager, **kwargs)
        self._probe_history = probe_history
        self._resolved: dict[str, ResolvedNetwork] = {}
        self._failed_bssids: dict[str, float] = {}

    RETRY_COOLDOWN = 300  # Retry failed BSSIDs after 5 minutes

    def can_connect(self, network: WifiNetwork) -> bool:
        if network.ssid:
            return False
        fail_time = self._failed_bssids.get(network.bssid)
        if fail_time and (time.time() - fail_time) < self.RETRY_COOLDOWN:
            return False
        return True

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        try:
            card = self.card_manager.get_card()
            if not card:
                logger.error('No wifi cards available for hidden network discovery')
                return self._fail_result(network)

            logger.info(
                f'Hidden network: BSSID={network.bssid} '
                f'signal={network.signal_strength}% ch={network.channel} '
                f'enc={network.encryption_type}'
            )

            # Ensure card starts in managed mode
            card.ensure_managed()

            # Try to resolve the SSID
            resolved = self._resolve_ssid(network, card)

            if not resolved:
                logger.info(f'Could not resolve SSID for {network.bssid}')
                self._failed_bssids[network.bssid] = time.time()
                self.card_manager.return_card(card)
                return self._fail_result(network)

            logger.info(
                f'Resolved {network.bssid} -> "{resolved.resolved_ssid}" '
                f'(via {resolved.method})'
            )

            resolved_network = WifiNetwork(
                ssid=resolved.resolved_ssid,
                bssid=network.bssid,
                signal_strength=network.signal_strength,
                channel=network.channel,
                encryption_type=network.encryption_type,
                is_open=network.is_open,
                uncloaked=True,
            )

            # Ensure managed mode before nmcli connect
            card.ensure_managed()

            if not card.connect(resolved_network):
                logger.warning(f'Failed to connect to "{resolved.resolved_ssid}"')
                self._failed_bssids[network.bssid] = time.time()
                self.card_manager.return_card(card)
                return self._fail_result(network, resolved.resolved_ssid)

            try:
                dl, ul, ping = self.run_speedtest(card)
            except ConnectionError as e:
                logger.warning(f'No internet on resolved network: {e}')
                card.disconnect()
                self.card_manager.return_card(card)
                return self._fail_result(network, resolved.resolved_ssid)

            return ConnectionResult(
                network=resolved_network,
                download_speed=dl, upload_speed=ul, ping=ping,
                connected=True,
                connection_method=f'hidden:{resolved.method}',
                interface=card.interface,
            )

        except Exception as e:
            logger.error(f'Hidden network module error: {e}')
            return self._fail_result(network)

    def _resolve_ssid(self, network: WifiNetwork,
                      card) -> Optional[ResolvedNetwork]:
        """Try multiple methods to resolve a hidden network's SSID.

        Methods are ordered from cheapest to most expensive:
        0. Module cache (in-memory)
        1. Probe history DB (SSIDs seen by scanning card in previous cycles)
        2. Saved nmcli connections
        3. Directed probe scan (managed mode, uses card)
        4. Monitor mode capture (expensive, uses card in monitor mode)
        """
        bssid = network.bssid

        if bssid in self._resolved:
            return self._resolved[bssid]

        # Method 0: Check probe history — SSIDs observed by the scanning card
        # This is the cheapest check: in-memory cache backed by MongoDB.
        # Works when the AP was previously broadcasting or when a client's
        # probe response was captured in a prior scan cycle.
        resolved = self._check_probe_history(bssid)
        if resolved:
            self._resolved[bssid] = resolved
            return resolved

        # Method 1: Check nmcli saved connections (no mode change)
        resolved = self._check_saved_connections(bssid)
        if resolved:
            self._resolved[bssid] = resolved
            return resolved

        # Method 2: Directed probe scan in managed mode
        resolved = self._directed_probe_scan(bssid, card)
        if resolved:
            self._resolved[bssid] = resolved
            return resolved

        # Method 3: Monitor mode passive capture — sniff probe
        # request/response frames that reveal hidden SSIDs
        resolved = self._monitor_capture(bssid, network.channel, card)
        if resolved:
            self._resolved[bssid] = resolved
            return resolved

        return None

    def _check_probe_history(self, bssid: str) -> Optional[ResolvedNetwork]:
        """Check if the scanning card previously observed this BSSID with an SSID."""
        if not self._probe_history:
            return None

        ssid = self._probe_history.lookup(bssid)
        if ssid:
            logger.info(f'Probe history hit: {bssid} -> "{ssid}"')
            return ResolvedNetwork(
                original_bssid=bssid,
                resolved_ssid=ssid,
                method='probe_history',
                confidence=0.85,
            )
        return None

    def _check_saved_connections(self, bssid: str) -> Optional[ResolvedNetwork]:
        """Check nmcli saved profiles for a matching BSSID."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME', 'connection', 'show'],
                capture_output=True, text=True, timeout=5,
            )
            saved_ssids = [
                line.strip() for line in result.stdout.strip().split('\n')
                if line.strip() and not line.startswith('lo')
                and not line.startswith('Wired')
            ]
            if not saved_ssids:
                return None

            scan_result = subprocess.run(
                ['nmcli', '-t', '-f', 'SSID,BSSID',
                 'device', 'wifi', 'list', '--rescan', 'no'],
                capture_output=True, text=True, timeout=5,
            )
            for line in scan_result.stdout.strip().split('\n'):
                if not line:
                    continue
                clean = line.replace('\\:', '\x00')
                parts = clean.split(':')
                if len(parts) >= 2:
                    found_bssid = parts[1].replace('\x00', ':')
                    if found_bssid.lower() == bssid.lower() and parts[0]:
                        return ResolvedNetwork(
                            original_bssid=bssid,
                            resolved_ssid=parts[0],
                            method='known_network',
                            confidence=0.9,
                        )
        except Exception as e:
            logger.debug(f'Saved connection check failed: {e}')
        return None

    def _directed_probe_scan(self, bssid: str,
                             card) -> Optional[ResolvedNetwork]:
        """Directed probe scan in managed mode using iw."""
        probe_ssids = self._get_candidate_ssids()
        if not probe_ssids:
            return None

        logger.info(
            f'Directed probe scan on {card.interface} '
            f'({len(probe_ssids)} candidates)'
        )

        card.ensure_managed()
        scan_output = card.run_scan(ssids=probe_ssids[:20])
        return self._find_bssid_in_iw_scan(bssid, scan_output, 'directed_scan')

    def _monitor_capture(self, bssid: str, channel: int,
                         card) -> Optional[ResolvedNetwork]:
        """Put card into monitor mode and capture probe frames.

        In monitor mode, we can see all 802.11 management frames including:
        - Probe Requests from clients looking for the hidden network (contain SSID)
        - Probe Responses from the AP to those clients (contain SSID)
        - Association Requests/Responses (contain SSID)

        These frames reveal the hidden SSID because the SSID is only hidden
        in beacon frames, not in directed probe exchanges.
        """
        logger.info(
            f'Monitor capture on {card.interface} for {bssid} (ch {channel})'
        )

        # Switch to monitor mode — must disconnect from NetworkManager first
        try:
            subprocess.run(
                ['nmcli', 'device', 'set', card.interface, 'managed', 'no'],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

        if not card.set_mode('monitor'):
            logger.warning(f'Cannot set {card.interface} to monitor mode')
            try:
                subprocess.run(
                    ['nmcli', 'device', 'set', card.interface, 'managed', 'yes'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
            card.ensure_managed()
            return None

        resolved = None
        try:
            # Set channel to match the target AP
            self._set_channel(card.interface, channel)

            # Capture with tcpdump — filter for probe req/resp and
            # association frames related to our target BSSID
            resolved = self._tcpdump_capture(card.interface, bssid)

        except Exception as e:
            logger.error(f'Monitor capture error: {e}')
        finally:
            # Always restore managed mode and NM control
            card.set_mode('managed')
            try:
                subprocess.run(
                    ['nmcli', 'device', 'set', card.interface, 'managed', 'yes'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

        return resolved

    def _tcpdump_capture(self, interface: str,
                         target_bssid: str) -> Optional[ResolvedNetwork]:
        """Run tcpdump in monitor mode to capture probe frames."""
        cfg = self.get_module_config()
        capture_seconds = cfg.get('monitor_capture_seconds', 15)
        target_mac = target_bssid.lower()

        # tcpdump filter: probe requests, probe responses, assoc requests
        # These are the frame types where hidden SSIDs appear in cleartext
        bpf_filter = (
            'type mgt subtype probe-req or '
            'type mgt subtype probe-resp or '
            'type mgt subtype assoc-req'
        )

        logger.info(
            f'tcpdump capture on {interface} for {capture_seconds}s '
            f'targeting {target_bssid}'
        )

        try:
            proc = subprocess.Popen(
                [
                    'tcpdump', '-i', interface,
                    '-e',  # Print link-layer header (includes MACs)
                    '-l',  # Line-buffered output
                    '-c', '200',  # Max 200 frames
                    bpf_filter,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            found_ssids = set()
            deadline = time.time() + capture_seconds

            while time.time() < deadline:
                # Read with timeout
                try:
                    import select
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not ready:
                        if proc.poll() is not None:
                            break
                        continue

                    line = proc.stdout.readline()
                    if not line:
                        break

                    # Check if this frame involves our target BSSID
                    if target_mac not in line.lower():
                        continue

                    # Extract SSID from probe/assoc frames
                    # tcpdump -e format includes "Probe Request (SSID)"
                    # or "Probe Response (SSID)" in the output
                    ssid = self._extract_ssid_from_tcpdump(line)
                    if ssid:
                        found_ssids.add(ssid)
                        logger.info(
                            f'Monitor captured SSID "{ssid}" for {target_bssid}'
                        )
                        # Found it — no need to keep capturing
                        break

                except Exception:
                    break

            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

            if found_ssids:
                # Use the first SSID found (most confident)
                ssid = next(iter(found_ssids))
                return ResolvedNetwork(
                    original_bssid=target_bssid,
                    resolved_ssid=ssid,
                    method='monitor_capture',
                    confidence=0.98,
                )

        except FileNotFoundError:
            logger.warning('tcpdump not installed')
        except Exception as e:
            logger.error(f'tcpdump capture error: {e}')

        return None

    @staticmethod
    def _extract_ssid_from_tcpdump(line: str) -> Optional[str]:
        """Extract SSID from a tcpdump -e output line.

        tcpdump prints probe/assoc frames like:
          ... Probe Request (MyNetwork) ...
          ... Probe Response (MyNetwork) ...
          ... Assoc Request (MyNetwork) ...
        """
        # Match "Probe Request (SSID)", "Probe Response (SSID)", etc.
        match = re.search(
            r'(?:Probe Request|Probe Response|Assoc(?:iation)? Request)\s+\(([^)]+)\)',
            line
        )
        if match:
            ssid = match.group(1).strip()
            # Filter out broadcast/empty probes
            if ssid and ssid != 'Broadcast' and not ssid.startswith('\\x00'):
                return ssid
        return None

    @staticmethod
    def _set_channel(interface: str, channel: int):
        """Set the monitor mode interface to a specific channel."""
        try:
            subprocess.run(
                ['iw', 'dev', interface, 'set', 'channel', str(channel)],
                capture_output=True, timeout=5, check=True,
            )
            logger.debug(f'Set {interface} to channel {channel}')
        except subprocess.CalledProcessError as e:
            logger.warning(f'Failed to set channel {channel} on {interface}: {e}')

    def _find_bssid_in_iw_scan(self, target_bssid: str, scan_output: str,
                                method: str) -> Optional[ResolvedNetwork]:
        """Parse iw scan output to find SSID for a given BSSID."""
        current_bssid = None
        current_ssid = None

        for line in scan_output.split('\n'):
            bssid_match = re.match(r'BSS ([0-9a-f:]{17})', line, re.I)
            if bssid_match:
                if (current_bssid and current_ssid
                        and current_bssid.lower() == target_bssid.lower()):
                    return ResolvedNetwork(
                        original_bssid=target_bssid,
                        resolved_ssid=current_ssid,
                        method=method,
                        confidence=0.95,
                    )
                current_bssid = bssid_match.group(1)
                current_ssid = None
            elif 'SSID:' in line:
                ssid = line.split('SSID:', 1)[1].strip()
                if ssid and ssid != '\\x00' * len(ssid):
                    current_ssid = ssid

        if (current_bssid and current_ssid
                and current_bssid.lower() == target_bssid.lower()):
            return ResolvedNetwork(
                original_bssid=target_bssid,
                resolved_ssid=current_ssid,
                method=method,
                confidence=0.95,
            )
        return None

    def _get_candidate_ssids(self) -> list[str]:
        """Get candidate SSIDs to probe for."""
        candidates = set()
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME', 'connection', 'show'],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split('\n'):
                name = line.strip()
                if name and name != 'lo' and not name.startswith('Wired'):
                    candidates.add(name)
        except Exception:
            pass

        candidates.update([
            'HOME', 'OFFICE', 'HIDDEN', 'PRIVATE',
            'MANAGEMENT', 'ADMIN', 'GUEST', 'IOT',
        ])
        return list(candidates)

    @staticmethod
    def _fail_result(network: WifiNetwork,
                     resolved_ssid: str = '') -> ConnectionResult:
        return ConnectionResult(
            network=WifiNetwork(
                ssid=resolved_ssid or '',
                bssid=network.bssid,
                signal_strength=network.signal_strength,
                channel=network.channel,
                encryption_type=network.encryption_type,
                is_open=network.is_open,
            ),
            download_speed=0, upload_speed=0, ping=0,
            connected=False, connection_method='hidden', interface='',
        )

    def get_config_schema(self) -> dict:
        return {
            'max_probe_ssids': {
                'type': 'int', 'default': 20,
                'description': 'Maximum SSIDs to try in directed probe scan',
            },
            'monitor_capture_seconds': {
                'type': 'int', 'default': 15,
                'description': 'Seconds to listen in monitor mode for probe frames',
            },
            'extra_probe_ssids': {
                'type': 'list', 'default': [],
                'description': 'Additional SSIDs to probe for hidden networks',
            },
        }

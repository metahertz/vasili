"""WireGuard tunnel helper — establish internet via WireGuard on UDP port 53.

Many captive-portal networks allow outbound UDP/53 (DNS).  If the user
runs a WireGuard peer on UDP port 53, this helper brings up the tunnel
using ``wg-quick``.

The user provides a WireGuard config file path.  The config should
already specify ``ListenPort``, ``Endpoint`` (on port 53), and the
peer's ``AllowedIPs``.

This module is lazy-imported by ``DnsPortTunnelStage``.
"""

import os
import shutil
import subprocess
import time

from logging_config import get_logger
import network_isolation

logger = get_logger(__name__)

# Default interface name matches the config file base name
DEFAULT_WG_INTERFACE = 'wg-vasili'


class WgTunnelHelper:
    """Manage a WireGuard tunnel via wg-quick."""

    def __init__(self, config_path: str, timeout: int = 15):
        self.config_path = config_path
        self.timeout = timeout

        self.tunnel_interface: str | None = None
        self.tunnel_ip: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if wg-quick is installed and config exists."""
        if not shutil.which('wg-quick'):
            return False
        if not self.config_path or not os.path.isfile(self.config_path):
            return False
        return True

    def establish(self, source_ip: str | None = None) -> dict | None:
        """Bring up the WireGuard interface.  Returns ``{interface, ip}``."""
        # Derive interface name from config filename
        # e.g. /etc/wireguard/wg-vasili.conf -> wg-vasili
        iface = os.path.splitext(os.path.basename(self.config_path))[0]

        # Tear down any stale instance first
        subprocess.run(
            ['wg-quick', 'down', self.config_path],
            capture_output=True, timeout=10,
        )

        logger.info('Starting WireGuard tunnel: wg-quick up %s', self.config_path)

        try:
            result = subprocess.run(
                ['wg-quick', 'up', self.config_path],
                capture_output=True, text=True, timeout=self.timeout,
            )
            if result.returncode != 0:
                logger.error('wg-quick up failed: %s', result.stderr[:300])
                return None
        except subprocess.TimeoutExpired:
            logger.error('wg-quick up timed out after %ds', self.timeout)
            return None
        except Exception as exc:
            logger.error('Failed to start WireGuard: %s', exc)
            return None

        # Wait for the interface to get an IP
        if not self._wait_for_ip(iface, self.timeout):
            logger.error('WireGuard interface %s has no IP', iface)
            self._teardown_wg(iface)
            return None

        self.tunnel_interface = iface
        self.tunnel_ip = network_isolation.get_interface_ip(iface)

        logger.info('WireGuard tunnel up: %s  ip=%s', iface, self.tunnel_ip)
        return {'interface': self.tunnel_interface, 'ip': self.tunnel_ip}

    def verify(self) -> bool:
        """Verify internet connectivity through the tunnel interface."""
        if not self.tunnel_interface:
            return False
        return network_isolation.verify_connectivity(self.tunnel_interface)

    def teardown(self):
        """Bring down the WireGuard interface."""
        if self.tunnel_interface:
            self._teardown_wg(self.tunnel_interface)
            self.tunnel_interface = None
            self.tunnel_ip = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _teardown_wg(self, iface: str):
        try:
            subprocess.run(
                ['wg-quick', 'down', self.config_path],
                capture_output=True, timeout=10,
            )
        except Exception as exc:
            logger.warning('Error stopping WireGuard %s: %s', iface, exc)

    @staticmethod
    def _wait_for_ip(interface: str, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = network_isolation.get_interface_ip(interface)
            if ip:
                return True
            time.sleep(0.5)
        return False

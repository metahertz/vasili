"""DNS tunnel helper — establish internet access via DNS tunneling.

Supports iodine (IP-over-DNS).  Requires a server the user controls
running the corresponding daemon (e.g. ``iodined``).

This module is lazy-imported by ``DnsTunnelStage`` and is never
loaded unless DNS reachability has been confirmed and the user has
configured a tunnel server domain.
"""

import shutil
import subprocess
import time

from logging_config import get_logger
import network_isolation

logger = get_logger(__name__)

# Interface name created by iodine
IODINE_INTERFACE = 'dns0'


class DnsTunnelHelper:
    """Manage an iodine DNS tunnel subprocess."""

    def __init__(self, server_domain: str, password: str = '',
                 tunnel_type: str = 'iodine', timeout: int = 30):
        self.server_domain = server_domain
        self.password = password
        self.tunnel_type = tunnel_type
        self.timeout = timeout

        self.process: subprocess.Popen | None = None
        self.tunnel_interface: str | None = None
        self.tunnel_ip: str | None = None
        # Reason for the most recent establish() failure (e.g. iodine's
        # "Bad password"/"Server rejected"/"couldn't connect"), surfaced by
        # DnsTunnelStage in the stage failure message.
        self.last_error: str = ''

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the tunnel binary is installed."""
        if self.tunnel_type == 'iodine':
            return shutil.which('iodine') is not None
        return False

    def establish(self, source_ip: str | None = None,
                  nameserver: str | None = None) -> dict | None:
        """Start the tunnel.  Returns ``{interface, ip}`` on success."""
        if self.tunnel_type == 'iodine':
            return self._establish_iodine(source_ip, nameserver)
        logger.error('Unsupported tunnel type: %s', self.tunnel_type)
        return None

    def verify(self) -> bool:
        """Verify internet connectivity through the tunnel interface."""
        if not self.tunnel_interface:
            return False
        return network_isolation.verify_connectivity(self.tunnel_interface)

    def teardown(self):
        """Kill tunnel process and clean up."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as exc:
                logger.warning('Error stopping tunnel process: %s', exc)
            self.process = None

        # Remove lingering interface (iodine usually cleans up on exit)
        if self.tunnel_interface:
            subprocess.run(
                ['ip', 'link', 'delete', self.tunnel_interface],
                capture_output=True, timeout=5,
            )
            self.tunnel_interface = None
            self.tunnel_ip = None

    # ------------------------------------------------------------------
    # Iodine implementation
    # ------------------------------------------------------------------

    def _establish_iodine(self, source_ip: str | None,
                          nameserver: str | None) -> dict | None:
        """Launch ``iodine`` and wait for the tunnel interface to appear."""
        cmd = ['iodine', '-f', '-r', '-I50']

        if self.password:
            cmd += ['-P', self.password]

        if nameserver:
            # Strip port if present (iodine only accepts host)
            ns_host = nameserver.split(':')[0]
            cmd += [ns_host]

        cmd.append(self.server_domain)

        logger.info('Starting iodine tunnel: %s', ' '.join(cmd))

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:
            self.last_error = f'failed to launch iodine: {exc}'
            logger.error('Failed to launch iodine: %s', exc)
            return None

        # Poll for the dns0 interface to appear
        if not self._wait_for_interface(IODINE_INTERFACE, self.timeout):
            # Capture iodine's own output — that's where the real reason is.
            captured = self._log_process_output()
            self.last_error = (
                f'iodine did not establish the tunnel within {self.timeout}s'
                + (f' — {captured}' if captured else
                   ' (no output; check the server_domain NS delegation, '
                   'password, and that iodined is running on :53)')
            )
            self.teardown()
            return None

        self.tunnel_interface = IODINE_INTERFACE
        self.tunnel_ip = network_isolation.get_interface_ip(IODINE_INTERFACE)

        if not self.tunnel_ip:
            self.last_error = (
                f'iodine interface {IODINE_INTERFACE} came up but got no IP'
            )
            logger.error('Tunnel interface %s has no IP', IODINE_INTERFACE)
            self.teardown()
            return None

        logger.info('Iodine tunnel up: %s  ip=%s', IODINE_INTERFACE,
                     self.tunnel_ip)
        return {'interface': self.tunnel_interface, 'ip': self.tunnel_ip}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wait_for_interface(interface: str, timeout: int) -> bool:
        """Block until *interface* appears and has an IP, or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = network_isolation.get_interface_ip(interface)
            if ip:
                return True
            time.sleep(0.5)
        return False

    def _log_process_output(self) -> str:
        """Drain iodine's output, log it, and return the most informative line.

        iodine prints the real cause here — "Bad password", "Server rejected
        secret", "Couldn't connect to server", "DNS query timed out", "Got NXDOMAIN",
        etc. We surface that to the stage so the failure message names it.
        """
        if not self.process:
            return ''
        try:
            out, _ = self.process.communicate(timeout=2)
        except Exception:
            return ''
        if not out:
            return ''
        text = out.decode(errors='replace').strip()
        logger.debug('iodine output: %s', text[:500])
        keywords = ('error', 'bad', 'reject', 'fail', 'denied', 'timed out',
                    'timeout', 'refused', 'nxdomain', 'cannot', "couldn't",
                    'could not', 'no response', 'unable')
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in reversed(lines):
            if any(k in ln.lower() for k in keywords):
                return ln[:200]
        return lines[-1][:200] if lines else ''

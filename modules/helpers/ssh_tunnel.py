"""SSH tunnel helper — establish internet via SSH on TCP port 53.

Many captive-portal networks allow outbound TCP/53 (DNS).  If the user
controls an SSH server listening on port 53, we can create a tun-based
VPN over that connection using ``ssh -w``.

This module is lazy-imported by ``DnsPortTunnelStage``.
"""

import shutil
import subprocess
import time

from logging_config import get_logger
import network_isolation

logger = get_logger(__name__)

# tun device index used by the SSH tunnel
SSH_TUN_INDEX = 53
SSH_TUN_INTERFACE = f'tun{SSH_TUN_INDEX}'
# Point-to-point addresses for the tun link
LOCAL_TUN_IP = '10.53.0.2'
REMOTE_TUN_IP = '10.53.0.1'


class SshTunnelHelper:
    """Manage an SSH tun-mode VPN over TCP port 53."""

    def __init__(self, server: str, user: str = 'root',
                 key_path: str = '', port: int = 53,
                 timeout: int = 15):
        self.server = server
        self.user = user
        self.key_path = key_path
        self.port = port
        self.timeout = timeout

        self.process: subprocess.Popen | None = None
        self.tunnel_interface: str | None = None
        self.tunnel_ip: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if ssh is installed."""
        return shutil.which('ssh') is not None

    def establish(self, source_ip: str | None = None) -> dict | None:
        """Open an SSH tun tunnel.  Returns ``{interface, ip}`` on success."""
        cmd = [
            'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', f'ConnectTimeout={self.timeout}',
            '-o', 'ServerAliveInterval=10',
            '-o', 'ExitOnForwardFailure=yes',
            '-N',  # no remote command
            '-w', f'{SSH_TUN_INDEX}:{SSH_TUN_INDEX}',
            '-p', str(self.port),
        ]

        if self.key_path:
            cmd += ['-i', self.key_path]

        if source_ip:
            cmd += ['-b', source_ip]

        cmd.append(f'{self.user}@{self.server}')

        logger.info('Starting SSH tunnel: %s',
                     ' '.join(c for c in cmd if not c.startswith('/')))

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:
            logger.error('Failed to launch ssh: %s', exc)
            return None

        # Wait for the tun interface to appear
        if not self._wait_for_interface(SSH_TUN_INTERFACE, self.timeout):
            self._log_process_output()
            self.teardown()
            return None

        # Configure the local tun endpoint
        if not self._configure_tun():
            self.teardown()
            return None

        self.tunnel_interface = SSH_TUN_INTERFACE
        self.tunnel_ip = LOCAL_TUN_IP

        logger.info('SSH tunnel up: %s  ip=%s', SSH_TUN_INTERFACE, LOCAL_TUN_IP)
        return {'interface': self.tunnel_interface, 'ip': self.tunnel_ip}

    def verify(self) -> bool:
        """Verify internet connectivity through the tunnel interface."""
        if not self.tunnel_interface:
            return False
        return network_isolation.verify_connectivity(self.tunnel_interface)

    def teardown(self):
        """Kill SSH process and clean up tun device."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as exc:
                logger.warning('Error stopping SSH tunnel: %s', exc)
            self.process = None

        if self.tunnel_interface:
            subprocess.run(
                ['ip', 'link', 'delete', self.tunnel_interface],
                capture_output=True, timeout=5,
            )
            self.tunnel_interface = None
            self.tunnel_ip = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _configure_tun(self) -> bool:
        """Assign IP and bring up the local tun device, add default route."""
        try:
            subprocess.run(
                ['ip', 'addr', 'add',
                 f'{LOCAL_TUN_IP}/32', 'peer', REMOTE_TUN_IP,
                 'dev', SSH_TUN_INTERFACE],
                capture_output=True, timeout=5, check=True,
            )
            subprocess.run(
                ['ip', 'link', 'set', SSH_TUN_INTERFACE, 'up'],
                capture_output=True, timeout=5, check=True,
            )
            # Route all traffic through the tunnel peer
            subprocess.run(
                ['ip', 'route', 'add', 'default',
                 'via', REMOTE_TUN_IP, 'dev', SSH_TUN_INTERFACE,
                 'table', str(200 + SSH_TUN_INDEX)],
                capture_output=True, timeout=5,
            )
            # Policy rule so traffic from the tun IP uses that table
            subprocess.run(
                ['ip', 'rule', 'add', 'from', LOCAL_TUN_IP,
                 'table', str(200 + SSH_TUN_INDEX), 'priority', '100'],
                capture_output=True, timeout=5,
            )
            return True
        except Exception as exc:
            logger.error('Failed to configure %s: %s', SSH_TUN_INTERFACE, exc)
            return False

    @staticmethod
    def _wait_for_interface(interface: str, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # tun device exists when it shows up in /sys
            result = subprocess.run(
                ['ip', 'link', 'show', interface],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return True
            time.sleep(0.5)
        return False

    def _log_process_output(self):
        if not self.process:
            return
        try:
            out, _ = self.process.communicate(timeout=2)
            if out:
                logger.debug('ssh output: %s', out.decode(errors='replace')[:500])
        except Exception:
            pass

"""DNS-port tunnel stage — SSH (TCP/53) and WireGuard (UDP/53) tunnels.

Runs after DnsProbeStage has confirmed external port-53 reachability.
Tries two approaches sequentially within a single stage:
  1. SSH tunnel over TCP port 53  (requires ``ssh_server`` configured)
  2. WireGuard tunnel over UDP port 53  (requires ``wg_config_path`` configured)

The first one that achieves internet wins.  Both require consent.
"""

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class DnsPortTunnelStage(PipelineStage):
    """Try SSH on TCP/53 and WireGuard on UDP/53 for internet access."""

    name = 'dns_port_tunnel'
    requires_consent = True

    _stage_config: dict | None = None

    def can_run(self, network, card, context):
        if context.get('has_internet', False):
            return False

        # Need port-53 reachability from DnsProbeStage
        has_tcp = context.get('dns_reachable_tcp', False)
        has_udp = context.get('dns_reachable_udp', False)
        if not (has_tcp or has_udp):
            return False

        # At least one method must be configured
        cfg = self._get_stage_config()
        has_ssh = bool(cfg.get('ssh_server')) and has_tcp
        has_wg = bool(cfg.get('wg_config_path')) and has_udp
        return has_ssh or has_wg

    def run(self, network, card, context):
        cfg = self._get_stage_config()
        source_ip = network_isolation.get_interface_ip(card.interface)
        has_tcp = context.get('dns_reachable_tcp', False)
        has_udp = context.get('dns_reachable_udp', False)

        # Record why each method was skipped or failed so the stage message
        # names the real cause instead of a generic "neither succeeded".
        attempts: list[str] = []

        # --- Try SSH tunnel over TCP/53 ---
        ssh_server = cfg.get('ssh_server', '')
        if not ssh_server:
            attempts.append('SSH/53 not configured (no ssh_server set)')
        elif not has_tcp:
            attempts.append('SSH/53 skipped — TCP/53 not reachable from this network')
        else:
            result, reason = self._try_ssh(cfg, source_ip)
            if result:
                return result
            attempts.append(f'SSH/53 failed: {reason}')

        # --- Try WireGuard over UDP/53 ---
        wg_config = cfg.get('wg_config_path', '')
        if not wg_config:
            attempts.append('WireGuard/53 not configured (no wg_config_path set)')
        elif not has_udp:
            attempts.append('WireGuard/53 skipped — UDP/53 not reachable from this network')
        else:
            result, reason = self._try_wireguard(cfg)
            if result:
                return result
            attempts.append(f'WireGuard/53 failed: {reason}')

        message = 'No DNS-port tunnel succeeded — ' + '; '.join(attempts)
        logger.warning('dns_port_tunnel: %s', message)
        return StageResult(
            success=False, has_internet=False,
            context_updates={},
            message=message,
        )

    # ------------------------------------------------------------------
    # SSH tunnel attempt
    # ------------------------------------------------------------------

    def _try_ssh(self, cfg: dict,
                 source_ip: str | None) -> tuple[StageResult | None, str]:
        """Returns ``(result, reason)``; ``result`` is None on failure and
        ``reason`` explains why (for the stage failure message)."""
        from modules.helpers.ssh_tunnel import SshTunnelHelper

        helper = SshTunnelHelper(
            server=cfg['ssh_server'],
            user=cfg.get('ssh_user', 'root'),
            key_path=cfg.get('ssh_key_path', ''),
            port=53,
            timeout=cfg.get('timeout', 15),
        )

        if not helper.is_available():
            logger.info('ssh not installed — skipping SSH/53 tunnel')
            return None, 'ssh client not installed on this device'

        logger.info('Attempting SSH tunnel to %s:53 (user %s, source %s)',
                    cfg['ssh_server'], cfg.get('ssh_user', 'root'),
                    source_ip or 'default')
        result = helper.establish(source_ip=source_ip)
        if not result:
            return None, (helper.last_error or 'tunnel failed to establish')

        if not helper.verify():
            logger.info('SSH tunnel up but no internet — tearing down')
            helper.teardown()
            return None, ('tunnel established but no internet through it '
                          '(connectivity check via the tun failed)')

        logger.info('SSH/53 tunnel internet confirmed on %s',
                     helper.tunnel_interface)
        return StageResult(
            success=True, has_internet=True,
            context_updates={
                'tunnel_active': True,
                'tunnel_interface': helper.tunnel_interface,
                'tunnel_type': 'ssh',
                '_tunnel_helper': helper,
            },
            message=f'SSH tunnel on port 53 via {helper.tunnel_interface}',
        ), ''

    # ------------------------------------------------------------------
    # WireGuard tunnel attempt
    # ------------------------------------------------------------------

    def _try_wireguard(self, cfg: dict) -> tuple[StageResult | None, str]:
        """Returns ``(result, reason)``; ``result`` is None on failure and
        ``reason`` explains why (for the stage failure message)."""
        from modules.helpers.wg_tunnel import WgTunnelHelper

        helper = WgTunnelHelper(
            config_path=cfg['wg_config_path'],
            timeout=cfg.get('timeout', 15),
        )

        if not helper.is_available():
            import os
            import shutil
            if not shutil.which('wg-quick'):
                reason = 'wg-quick not installed on this device'
            elif not os.path.isfile(cfg['wg_config_path']):
                reason = f'WireGuard config not found at {cfg["wg_config_path"]}'
            else:
                reason = 'WireGuard prerequisites unavailable'
            logger.info('WireGuard/53 unavailable: %s', reason)
            return None, reason

        logger.info('Attempting WireGuard tunnel via %s', cfg['wg_config_path'])
        result = helper.establish()
        if not result:
            return None, (helper.last_error or 'tunnel failed to establish')

        if not helper.verify():
            logger.info('WireGuard tunnel up but no internet — tearing down')
            helper.teardown()
            return None, ('tunnel established but no internet through it '
                          '(connectivity check via the tunnel failed)')

        logger.info('WireGuard/53 tunnel internet confirmed on %s',
                     helper.tunnel_interface)
        return StageResult(
            success=True, has_internet=True,
            context_updates={
                'tunnel_active': True,
                'tunnel_interface': helper.tunnel_interface,
                'tunnel_type': 'wireguard',
                '_tunnel_helper': helper,
            },
            message=f'WireGuard tunnel on port 53 via {helper.tunnel_interface}',
        ), ''

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_config_schema(self):
        return {
            'ssh_server': {
                'type': 'str',
                'default': '',
                'description': 'SSH server host for TCP/53 tunnel',
            },
            'ssh_user': {
                'type': 'str',
                'default': 'root',
                'description': 'SSH username',
            },
            'ssh_key_path': {
                'type': 'str',
                'default': '',
                'description': 'Path to SSH private key (empty = default key)',
            },
            'wg_config_path': {
                'type': 'str',
                'default': '',
                'description': 'Path to WireGuard config file for UDP/53 tunnel',
            },
            'timeout': {
                'type': 'int',
                'default': 15,
                'description': 'Connection timeout in seconds',
            },
        }

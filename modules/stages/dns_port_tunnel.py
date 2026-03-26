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

        # --- Try SSH tunnel over TCP/53 ---
        ssh_server = cfg.get('ssh_server', '')
        if ssh_server and has_tcp:
            result = self._try_ssh(cfg, source_ip)
            if result:
                return result

        # --- Try WireGuard over UDP/53 ---
        wg_config = cfg.get('wg_config_path', '')
        if wg_config and has_udp:
            result = self._try_wireguard(cfg)
            if result:
                return result

        return StageResult(
            success=False, has_internet=False,
            context_updates={},
            message='Neither SSH/53 nor WireGuard/53 succeeded',
        )

    # ------------------------------------------------------------------
    # SSH tunnel attempt
    # ------------------------------------------------------------------

    def _try_ssh(self, cfg: dict, source_ip: str | None) -> StageResult | None:
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
            return None

        logger.info('Attempting SSH tunnel to %s:53', cfg['ssh_server'])
        result = helper.establish(source_ip=source_ip)
        if not result:
            return None

        if not helper.verify():
            logger.info('SSH tunnel up but no internet — tearing down')
            helper.teardown()
            return None

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
        )

    # ------------------------------------------------------------------
    # WireGuard tunnel attempt
    # ------------------------------------------------------------------

    def _try_wireguard(self, cfg: dict) -> StageResult | None:
        from modules.helpers.wg_tunnel import WgTunnelHelper

        helper = WgTunnelHelper(
            config_path=cfg['wg_config_path'],
            timeout=cfg.get('timeout', 15),
        )

        if not helper.is_available():
            logger.info('wg-quick not installed or config missing — '
                        'skipping WireGuard/53')
            return None

        logger.info('Attempting WireGuard tunnel via %s', cfg['wg_config_path'])
        result = helper.establish()
        if not result:
            return None

        if not helper.verify():
            logger.info('WireGuard tunnel up but no internet — tearing down')
            helper.teardown()
            return None

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
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _get_stage_config(self) -> dict:
        if self._stage_config is not None:
            return self._stage_config
        schema = self.get_config_schema()
        self._stage_config = {k: v['default'] for k, v in schema.items()}
        return self._stage_config

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

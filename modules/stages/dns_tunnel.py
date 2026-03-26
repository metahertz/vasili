"""DNS tunnel stage — establish internet via DNS tunneling.

Runs after DnsProbeStage has confirmed external DNS reachability.
Lazy-imports the DnsTunnelHelper to start an iodine tunnel, then
verifies connectivity through the resulting virtual interface.

Requires consent (active tunneling technique) and a configured
``server_domain`` (the user must control a DNS tunnel server).
"""

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class DnsTunnelStage(PipelineStage):
    """Pipeline stage that attempts DNS tunneling for internet access."""

    name = 'dns_tunnel'
    requires_consent = True

    # Cached config (populated lazily by _get_stage_config)
    _stage_config: dict | None = None

    def can_run(self, network, card, context):
        # Skip if internet already available
        if context.get('has_internet', False):
            return False

        # Need DNS reachability from a prior DnsProbeStage
        if not (context.get('dns_reachable_tcp') or
                context.get('dns_reachable_udp')):
            return False

        # A server domain must be configured — tunneling is useless without one
        cfg = self._get_stage_config()
        if not cfg.get('server_domain'):
            return False

        return True

    def run(self, network, card, context):
        from modules.helpers.dns_tunnel import DnsTunnelHelper

        cfg = self._get_stage_config()
        server_domain = cfg['server_domain']
        password = cfg.get('tunnel_password', '')
        tunnel_type = cfg.get('tunnel_type', 'iodine')
        timeout = cfg.get('timeout', 30)

        helper = DnsTunnelHelper(
            server_domain=server_domain,
            password=password,
            tunnel_type=tunnel_type,
            timeout=timeout,
        )

        if not helper.is_available():
            logger.info('DNS tunnel tool "%s" not installed — skipping',
                        tunnel_type)
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message=f'{tunnel_type} not installed',
            )

        # Pick a nameserver from the DNS probe results
        dns_servers = context.get('reachable_dns_servers', [])
        nameserver = dns_servers[0].split(':')[0] if dns_servers else None

        source_ip = network_isolation.get_interface_ip(card.interface)

        logger.info('Attempting DNS tunnel via %s (ns=%s)',
                     server_domain, nameserver or 'default')

        result = helper.establish(source_ip=source_ip,
                                  nameserver=nameserver)
        if not result:
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message='Tunnel establishment failed',
            )

        if not helper.verify():
            logger.info('Tunnel up but no internet through it — tearing down')
            helper.teardown()
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message='Tunnel established but no internet through it',
            )

        logger.info('DNS tunnel internet confirmed on %s',
                     helper.tunnel_interface)

        return StageResult(
            success=True,
            has_internet=True,
            context_updates={
                'tunnel_active': True,
                'tunnel_interface': helper.tunnel_interface,
                'tunnel_type': helper.tunnel_type,
                '_tunnel_helper': helper,
            },
            message=f'DNS tunnel established via {helper.tunnel_interface}',
        )

    def _get_stage_config(self) -> dict:
        """Return merged config (schema defaults + user overrides)."""
        if self._stage_config is not None:
            return self._stage_config
        # Fall back to schema defaults — the PipelineModule config system
        # will supply overrides at runtime via the module_config store.
        schema = self.get_config_schema()
        self._stage_config = {k: v['default'] for k, v in schema.items()}
        return self._stage_config

    def get_config_schema(self):
        return {
            'server_domain': {
                'type': 'str',
                'default': '',
                'description': 'DNS tunnel server domain (e.g. t.example.com)',
            },
            'tunnel_password': {
                'type': 'str',
                'default': '',
                'description': 'Tunnel authentication password',
            },
            'tunnel_type': {
                'type': 'str',
                'default': 'iodine',
                'description': 'Tunnel tool to use (iodine)',
            },
            'timeout': {
                'type': 'int',
                'default': 30,
                'description': 'Tunnel establishment timeout in seconds',
            },
        }

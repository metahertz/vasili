"""DNS probe stage — test external DNS reachability via TCP and UDP."""

import socket

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class DnsProbeStage(PipelineStage):
    """Probe external DNS servers to check reachability via UDP and TCP.

    Even when HTTP is blocked (captive portal), DNS may be open — which
    enables DNS tunneling as a last-resort connectivity method.

    Sets 'dns_reachable_tcp', 'dns_reachable_udp', and 'reachable_dns_servers'
    in the context.
    """
    name = 'dns_probe'
    requires_consent = False

    DEFAULT_TARGETS = [
        {'host': 'tep1.metahertz.dev', 'port': 53, 'proto': 'tcp'},
        {'host': '8.8.8.8', 'port': 53, 'proto': 'udp'},
        {'host': '1.1.1.1', 'port': 53, 'proto': 'udp'},
    ]

    def can_run(self, network, card, context):
        return not context.get('has_internet', False)

    def run(self, network, card, context):
        targets = self.DEFAULT_TARGETS
        timeout = 5
        source_ip = network_isolation.get_interface_ip(card.interface)

        tcp_reachable = []
        udp_reachable = []

        for target in targets:
            host = target['host']
            port = target.get('port', 53)
            proto = target.get('proto', 'udp')

            try:
                if proto == 'tcp':
                    if self._probe_tcp(host, port, timeout, source_ip):
                        tcp_reachable.append(f'{host}:{port}')
                        logger.info(f'DNS reachable via TCP: {host}:{port}')
                elif proto == 'udp':
                    if self._probe_udp(host, port, timeout, source_ip):
                        udp_reachable.append(f'{host}:{port}')
                        logger.info(f'DNS reachable via UDP: {host}:{port}')
            except Exception as e:
                logger.debug(f'DNS probe failed for {host}:{port}/{proto}: {e}')

        has_dns = bool(tcp_reachable or udp_reachable)

        return StageResult(
            success=has_dns,
            has_internet=False,  # DNS != internet, don't stop pipeline
            context_updates={
                'dns_reachable_tcp': bool(tcp_reachable),
                'dns_reachable_udp': bool(udp_reachable),
                'reachable_dns_servers': tcp_reachable + udp_reachable,
            },
            message=f'DNS: TCP={tcp_reachable}, UDP={udp_reachable}' if has_dns else 'No external DNS',
        )

    @staticmethod
    def _probe_tcp(host, port, timeout, source_ip=None):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            if source_ip:
                sock.bind((source_ip, 0))
            sock.connect((host, port))
            sock.close()
            return True
        except (socket.timeout, socket.error, OSError):
            return False

    @staticmethod
    def _probe_udp(host, port, timeout, source_ip=None):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            if source_ip:
                sock.bind((source_ip, 0))
            query = (
                b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
                b'\x00\x00\x01\x00\x01'
            )
            sock.sendto(query, (host, port))
            data, _ = sock.recvfrom(512)
            sock.close()
            return len(data) > 0
        except (socket.timeout, socket.error, OSError):
            return False

    def get_config_schema(self):
        return {
            'targets': {
                'type': 'list',
                'default': self.DEFAULT_TARGETS,
                'description': 'DNS endpoints to probe: [{host, port, proto}]',
            },
            'timeout': {
                'type': 'int',
                'default': 5,
                'description': 'Probe timeout in seconds',
            },
        }

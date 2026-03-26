"""WPA3 Network Pipeline — multi-stage connection handler for WPA3 networks.

Phases:
  1. SavedCredentials — try nmcli stored credentials
  2. ConfiguredKeys — try passwords from config store
  3. ConnectionGate — STOP if not associated (no credentials worked)
  4. ConnectivityCheck — verify internet after association
  5. DnsProbe — check external DNS reachability
  6. [parallel] DnsTunnel + SSH/WG on port 53 — race for best path
"""

from logging_config import get_logger
from vasili import PipelineModule, WifiNetwork
from modules.stages import (
    SavedCredentialsStage,
    ConfiguredKeysStage,
    ConnectionGateStage,
    ConnectivityCheckStage,
    DnsProbeStage,
    DnsTunnelStage,
    DnsPortTunnelStage,
)

logger = get_logger(__name__)


class WPA3NetworkPipeline(PipelineModule):
    """Pipeline for WPA3-encrypted WiFi networks."""
    priority = 50
    auto_connect = False

    def __init__(self, card_manager, consent_manager=None, module_config=None, **kwargs):
        phases = [
            SavedCredentialsStage(),
            ConfiguredKeysStage(),
            ConnectionGateStage(),   # Stops pipeline if no WiFi association
            ConnectivityCheckStage(),
            DnsProbeStage(),
            [DnsTunnelStage(), DnsPortTunnelStage()],
        ]
        super().__init__(
            card_manager, phases=phases,
            consent_manager=consent_manager,
            module_config=module_config,
        )

    def can_connect(self, network: WifiNetwork) -> bool:
        return network.encryption_type == 'WPA3'

    def _get_connect_context(self) -> dict:
        return {'_passwords': self._get_passwords()}

    def _get_passwords(self) -> list[str]:
        cfg = self.get_module_config()
        passwords = cfg.get('passwords', [])
        return passwords if isinstance(passwords, list) else []

    def get_config_schema(self) -> dict:
        return {
            'passwords': {
                'type': 'list',
                'default': [],
                'description': 'WPA3 passwords/keys to try for unknown networks',
            },
        }

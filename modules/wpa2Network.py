"""WPA2 Network Pipeline — multi-stage connection handler for WPA2 networks.

Stages:
  1. SavedCredentials — try nmcli stored credentials
  2. ConfiguredKeys — try passwords from config store
  3. PmkidCapture — PMKID dictionary crack (requires consent)
  4. ConnectionGate — STOP if not associated (no credentials worked)
  5. ConnectivityCheck — verify internet after association
  6. DnsProbe — check external DNS reachability
"""

from logging_config import get_logger
from vasili import PipelineModule, WifiNetwork
from modules.stages import (
    SavedCredentialsStage,
    ConfiguredKeysStage,
    PmkidCaptureStage,
    ConnectionGateStage,
    ConnectivityCheckStage,
    DnsProbeStage,
)

logger = get_logger(__name__)


class WPA2NetworkPipeline(PipelineModule):
    """Pipeline for WPA2-encrypted WiFi networks."""
    priority = 50
    auto_connect = False

    def __init__(self, card_manager, consent_manager=None, module_config=None, **kwargs):
        stages = [
            SavedCredentialsStage(),
            ConfiguredKeysStage(),
            PmkidCaptureStage(),
            ConnectionGateStage(),   # Stops pipeline if no WiFi association
            ConnectivityCheckStage(),
            DnsProbeStage(),
        ]
        super().__init__(
            card_manager, stages=stages,
            consent_manager=consent_manager,
            module_config=module_config,
        )

    def can_connect(self, network: WifiNetwork) -> bool:
        return network.encryption_type == 'WPA2'

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
                'description': 'WPA2 passwords/keys to try for unknown networks',
            },
        }

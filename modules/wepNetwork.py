"""WEP Network Pipeline — multi-stage connection handler for WEP networks.

WEP (Wired Equivalent Privacy, 1997) is cryptographically broken beyond
repair.  The RC4 key-scheduling algorithm leaks key material through weak
IVs, and the PTW attack (2007) can recover a 104-bit key from roughly
40 000 captured packets — often under two minutes with traffic injection.

Pipeline phases:
  1. SavedCredentials — try nmcli stored WEP profiles
  2. ConfiguredKeys  — try user-provided WEP keys from config store
  3. WepCommonKeys   — brute-force a curated list of common factory defaults
  4. WepCrack        — full IV-capture + aircrack-ng recovery (consent required)
  5. ConnectionGate  — STOP if nothing worked (not associated)
  6. ConnectivityCheck — verify internet access
  7. DnsProbe        — test external DNS reachability
  8. [parallel] CaptivePortal + DnsTunnel + SSH/WG on port 53
"""

from logging_config import get_logger
from vasili import PipelineModule, WifiNetwork
from modules.stages import (
    SavedCredentialsStage,
    ConfiguredKeysStage,
    ConnectionGateStage,
    ConnectivityCheckStage,
    CaptivePortalStage,
    DnsProbeStage,
    DnsTunnelStage,
    DnsPortTunnelStage,
)
from modules.stages.wep_crack import WepCommonKeysStage, WepCrackStage

logger = get_logger(__name__)


class WEPNetworkPipeline(PipelineModule):
    """Pipeline for WEP-encrypted WiFi networks.

    WEP keys come in two sizes:
      - 64-bit  (40-bit key):  5 ASCII chars  /  10 hex digits
      - 128-bit (104-bit key): 13 ASCII chars  /  26 hex digits

    The pipeline tries the cheapest methods first (saved creds, known keys)
    before escalating to active IV capture and cracking.
    """
    priority = 45  # Slightly ahead of WPA2 — WEP cracks are faster
    auto_connect = False  # Credential stages handle connection

    def __init__(self, card_manager, consent_manager=None,
                 module_config=None, **kwargs):
        phases = [
            SavedCredentialsStage(),       # 1. nmcli stored profiles
            ConfiguredKeysStage(),         # 2. User-configured keys
            WepCommonKeysStage(),          # 3. Common factory defaults
            WepCrackStage(),               # 4. Full aircrack-ng (consent)
            ConnectionGateStage(),         # 5. Bail if not associated
            ConnectivityCheckStage(),      # 6. Internet check
            DnsProbeStage(),               # 7. DNS reachability
            # 8. Exploitation strategies (parallel — best speed wins)
            [CaptivePortalStage(), DnsTunnelStage(), DnsPortTunnelStage()],
        ]
        super().__init__(
            card_manager, phases=phases,
            consent_manager=consent_manager,
            module_config=module_config,
        )

    def can_connect(self, network: WifiNetwork) -> bool:
        return network.encryption_type == 'WEP'

    def _get_connect_context(self) -> dict:
        """Seed pipeline context with user-configured WEP keys."""
        cfg = self.get_module_config()
        wep_keys = cfg.get('wep_keys', [])
        if not isinstance(wep_keys, list):
            wep_keys = []
        # _passwords is used by ConfiguredKeysStage
        # _wep_keys is used by WepCommonKeysStage (prepended to defaults)
        return {
            '_passwords': wep_keys,
            '_wep_keys': wep_keys,
        }

    def get_config_schema(self) -> dict:
        return {
            'wep_keys': {
                'type': 'list',
                'default': [],
                'description': (
                    'WEP keys to try (ASCII or hex). '
                    '64-bit: 5 chars / 10 hex digits.  '
                    '128-bit: 13 chars / 26 hex digits.'
                ),
            },
        }

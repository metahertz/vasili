"""Open Network Pipeline — multi-stage connection handler for open WiFi networks.

Connects to an open network and runs stages to achieve internet connectivity:
  1. ConnectivityCheck — is internet already available?
  2. CaptivePortal — detect and auto-authenticate portal
  3. MacClone — clone authenticated client MAC (requires consent)
  4. DnsProbe — check external DNS reachability

Stages are imported from the shared modules.stages package.
"""

from logging_config import get_logger
from vasili import PipelineModule, WifiNetwork
from modules.stages import (
    ConnectivityCheckStage,
    CaptivePortalStage,
    DnsProbeStage,
)

logger = get_logger(__name__)


class OpenNetworkPipeline(PipelineModule):
    """Pipeline for open WiFi networks.

    Stages run sequentially. If any stage achieves internet (has_internet=True),
    the pipeline stops and runs a speedtest. If all stages exhaust, the card
    is disconnected.
    """
    priority = 10

    def __init__(self, card_manager, consent_manager=None, module_config=None, **kwargs):
        from modules.macClone import MacCloneStage

        stages = [
            ConnectivityCheckStage(),
            CaptivePortalStage(),
            MacCloneStage(),
            DnsProbeStage(),
        ]
        super().__init__(
            card_manager, stages=stages,
            consent_manager=consent_manager,
            module_config=module_config,
        )

    def can_connect(self, network: WifiNetwork) -> bool:
        return network.is_open

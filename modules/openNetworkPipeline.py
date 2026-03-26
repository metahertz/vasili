"""Open Network Pipeline — multi-stage connection handler for open WiFi networks.

Connects to an open network and runs phases to achieve internet connectivity:
  Phase 1 (sequential): ConnectivityCheck — is internet already available?
  Phase 2 (sequential): DnsProbe — check external DNS reachability
  Phase 3 (parallel):   CaptivePortal + DnsTunnel + SSH/WG on port 53
  Phase 4 (sequential): MacClone — clone authenticated client MAC (fallback)

Stages are imported from the shared modules.stages package.
"""

from logging_config import get_logger
from vasili import PipelineModule, WifiNetwork
from modules.stages import (
    ConnectivityCheckStage,
    CaptivePortalStage,
    DnsProbeStage,
    DnsTunnelStage,
    DnsPortTunnelStage,
)

logger = get_logger(__name__)


class OpenNetworkPipeline(PipelineModule):
    """Pipeline for open WiFi networks.

    Discovery stages run sequentially to build context, then exploitation
    strategies (captive portal bypass, DNS tunneling, SSH/53, WireGuard/53)
    run in parallel.  The pipeline picks the fastest path.  MacClone is a
    sequential fallback that only runs if the parallel phase fails.
    """
    priority = 10

    def __init__(self, card_manager, consent_manager=None, module_config=None, **kwargs):
        from modules.macClone import MacCloneStage

        phases = [
            # Phase 1-2: Discovery (sequential)
            ConnectivityCheckStage(),
            DnsProbeStage(),
            # Phase 3: Exploitation strategies (parallel — best speed wins)
            [CaptivePortalStage(), DnsTunnelStage(), DnsPortTunnelStage()],
            # Phase 4: Fallback (sequential, destructive — changes MAC)
            MacCloneStage(),
        ]
        super().__init__(
            card_manager, phases=phases,
            consent_manager=consent_manager,
            module_config=module_config,
        )

    def can_connect(self, network: WifiNetwork) -> bool:
        return network.is_open

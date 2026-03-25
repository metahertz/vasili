"""Connectivity check stage — verify if internet is reachable via the WiFi card."""

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class ConnectivityCheckStage(PipelineStage):
    """Check if the connected network provides internet access.

    Should typically be the first stage in any pipeline. Sets 'has_internet'
    and 'http_blocked' in the context for downstream stages.
    """
    name = 'connectivity_check'
    requires_consent = False

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        has_internet = network_isolation.verify_connectivity(card.interface)
        return StageResult(
            success=has_internet,
            has_internet=has_internet,
            context_updates={
                'has_internet': has_internet,
                'http_blocked': not has_internet,
            },
            message='Internet accessible' if has_internet else 'No direct internet',
        )

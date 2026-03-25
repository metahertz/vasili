"""Connection gate stage — prevents downstream stages from running if not associated.

This stage should be placed between credential/authentication stages and
connectivity/DNS stages. It checks that the card is actually connected
to the WiFi network before allowing expensive checks like HTTP connectivity
tests or DNS probes to proceed.

Without this gate, those stages waste time (5+ seconds each) trying to
reach the internet on a disconnected interface.
"""

from logging_config import get_logger
from vasili import PipelineStage, StageResult

logger = get_logger(__name__)


class ConnectionGateStage(PipelineStage):
    """Stop the pipeline if no WiFi association exists.

    Checks the 'wifi_associated' context flag set by credential stages
    or the auto_connect mechanism. If not set, returns a terminal failure
    that prevents all subsequent stages from running.

    Context reads:
        wifi_associated: bool — set by credential stages or auto_connect

    Context sets:
        connection_gated: True — signals that pipeline was stopped here
    """
    name = 'connection_gate'
    requires_consent = False

    def can_run(self, network, card, context):
        # Always run — this is a gate check
        return True

    def run(self, network, card, context):
        if context.get('wifi_associated', False):
            logger.debug(f'Connection gate passed for {network.ssid}')
            return StageResult(
                success=True, has_internet=False,
                context_updates={},
                message='WiFi associated — proceeding',
            )

        # Not connected — stop the pipeline entirely
        logger.info(
            f'Connection gate: not associated to {network.ssid or network.bssid}, '
            f'stopping pipeline'
        )
        return StageResult(
            success=False, has_internet=False,
            context_updates={'connection_gated': True},
            message='Not associated — no credentials worked',
            stop_pipeline=True,
        )

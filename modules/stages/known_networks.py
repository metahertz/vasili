"""KnownCredentialsStage — connect with a user-registered per-SSID credential.

Runs ahead of SavedCredentialsStage in encrypted-network pipelines. If the
scanned SSID matches an entry in the encrypted KnownNetworksStore, this
stage tries that credential first. On miss it returns success=False and
the pipeline falls through to the existing saved/configured/crack stages
exactly as before.

The store handle is passed via context key '_known_networks_store' by the
pipeline module's _get_connect_context(), so this stage never touches
MongoDB or the master key directly.
"""

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class KnownCredentialsStage(PipelineStage):
    """Try a user-pre-registered credential for the scanned SSID."""
    name = 'known_credentials'
    requires_consent = False

    def can_run(self, network, card, context):
        return not network.is_open

    def run(self, network, card, context):
        store = context.get('_known_networks_store')
        if store is None or not getattr(store, 'is_available', lambda: False)():
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message='Known-networks store unavailable',
            )

        entry = store.get(network.ssid)
        if not entry or not entry.get('password'):
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message=f'No known credential for {network.ssid}',
            )

        if context.get('wifi_associated'):
            card.disconnect()

        if card.connect(network, password=entry['password']):
            has_internet = network_isolation.verify_connectivity(card.interface)
            if has_internet:
                return StageResult(
                    success=True, has_internet=True,
                    context_updates={
                        'has_internet': True,
                        'connected_with': 'known_credential',
                    },
                    message=f'Connected with known credential for {network.ssid}',
                )
            return StageResult(
                success=True, has_internet=False,
                context_updates={
                    'wifi_associated': True,
                    'connected_with': 'known_credential',
                    'http_blocked': True,
                },
                message='Known credential connected but no internet',
            )

        card.disconnect()
        return StageResult(
            success=False, has_internet=False,
            context_updates={'known_credentials_failed': True},
            message=f'Known credential for {network.ssid} was rejected',
        )

"""Credential stages — try saved and configured passwords for encrypted networks.

These stages handle the authentication phase before connectivity is checked.
They attempt to connect to the network using various credential sources:
1. SavedCredentialsStage — relies on nmcli's stored connection profiles
2. ConfiguredKeysStage — tries passwords from the module config store
"""

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class SavedCredentialsStage(PipelineStage):
    """Try connecting with saved/stored nmcli credentials.

    This is typically the first stage for encrypted networks. nmcli
    remembers credentials from previous successful connections.

    Sets 'connected_with' in context on success.
    """
    name = 'saved_credentials'
    requires_consent = False

    def can_run(self, network, card, context):
        # Always try saved credentials first for encrypted networks
        return not network.is_open

    def run(self, network, card, context):
        # card.connect() without a password lets nmcli try stored credentials
        if card.connect(network):
            has_internet = network_isolation.verify_connectivity(card.interface)
            if has_internet:
                return StageResult(
                    success=True, has_internet=True,
                    context_updates={
                        'has_internet': True,
                        'connected_with': 'saved_credentials',
                    },
                    message=f'Connected with saved credentials — internet OK',
                )
            else:
                # Connected at WiFi layer but no internet
                # Keep connected — later stages may help (DNS probe etc.)
                return StageResult(
                    success=True, has_internet=False,
                    context_updates={
                        'wifi_associated': True,
                        'connected_with': 'saved_credentials',
                        'http_blocked': True,
                    },
                    message='Saved credentials connected but no internet',
                )

        return StageResult(
            success=False, has_internet=False,
            context_updates={'saved_credentials_failed': True},
            message='No saved credentials or connection failed',
        )


class ConfiguredKeysStage(PipelineStage):
    """Try connecting with passwords from the module config store.

    Users can configure a list of passwords to try on the /config page.
    Each password is attempted in order. The card is disconnected between
    attempts to ensure a clean state.

    Sets 'connected_with' in context on success.
    """
    name = 'configured_keys'
    requires_consent = False

    def can_run(self, network, card, context):
        # Only try if saved credentials didn't give us internet
        return (
            not network.is_open
            and not context.get('has_internet', False)
        )

    def run(self, network, card, context):
        passwords = self._get_passwords(context)
        if not passwords:
            return StageResult(
                success=False, has_internet=False,
                context_updates={},
                message='No configured passwords to try',
            )

        for i, pw in enumerate(passwords):
            logger.info(f'Trying configured key {i+1}/{len(passwords)} for {network.ssid}')

            # Disconnect first if previously associated
            if context.get('wifi_associated'):
                card.disconnect()

            if card.connect(network, password=pw):
                has_internet = network_isolation.verify_connectivity(card.interface)
                if has_internet:
                    return StageResult(
                        success=True, has_internet=True,
                        context_updates={
                            'has_internet': True,
                            'connected_with': 'configured_key',
                            'key_index': i,
                        },
                        message=f'Configured key {i+1} worked — internet OK',
                    )
                else:
                    # Key worked for WiFi but no internet
                    return StageResult(
                        success=True, has_internet=False,
                        context_updates={
                            'wifi_associated': True,
                            'connected_with': 'configured_key',
                            'http_blocked': True,
                        },
                        message=f'Configured key {i+1} connected but no internet',
                    )
            else:
                card.disconnect()

        return StageResult(
            success=False, has_internet=False,
            context_updates={'configured_keys_failed': True},
            message=f'All {len(passwords)} configured keys failed',
        )

    def _get_passwords(self, context) -> list[str]:
        """Get passwords from context (set by the pipeline module)."""
        return context.get('_passwords', [])

    def get_config_schema(self):
        return {
            'passwords': {
                'type': 'list',
                'default': [],
                'description': 'List of passwords/keys to try for unknown networks',
            },
        }

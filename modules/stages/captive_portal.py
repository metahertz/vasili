"""Captive portal stage — detect and authenticate through captive portals."""

import time

from logging_config import get_logger
from vasili import PipelineStage, StageResult
import network_isolation

logger = get_logger(__name__)


class CaptivePortalStage(PipelineStage):
    """Detect and attempt to authenticate through a captive portal.

    Uses the CaptivePortalDetector and CaptivePortalAuthenticator from
    the captivePortal module. Passes the WiFi interface for bound HTTP
    requests and captures detailed auth logs for the activity view.
    """
    name = 'captive_portal'
    requires_consent = False

    # Cached config (populated lazily by _get_stage_config)
    _stage_config: dict | None = None

    def can_run(self, network, card, context):
        return not context.get('has_internet', False)

    def run(self, network, card, context):
        from modules.captivePortal import (
            CaptivePortalDetector, CaptivePortalAuthenticator,
        )

        cfg = self._get_stage_config()
        detector = CaptivePortalDetector()
        authenticator = CaptivePortalAuthenticator(
            identity=self._get_identity(),
            use_browser=cfg.get('use_browser', True),
            browser_timeout=cfg.get('browser_timeout', 30),
        )

        time.sleep(1)

        portal_info = detector.detect(interface=card.interface)

        if not portal_info:
            logger.info('No captive portal detected')
            return StageResult(
                success=True, has_internet=False,
                context_updates={'captive_portal_detected': False},
                message='No captive portal, but no internet either',
            )

        portal_type = portal_info.get('portal_type', 'unknown')
        auth_method = portal_info.get('auth_method', 'unknown')
        logger.info(f'Portal detected: type={portal_type}, method={auth_method}')

        auth_success = authenticator.authenticate(
            portal_info, interface=card.interface
        )

        auth_details = {
            'portal_type': portal_type,
            'auth_method': auth_method,
            'redirect_url': portal_info.get('redirect_url', ''),
            'auth_steps': authenticator.auth_log,
        }

        if auth_success:
            has_internet = network_isolation.verify_connectivity(card.interface)
            return StageResult(
                success=True, has_internet=has_internet,
                context_updates={
                    'captive_portal_detected': True,
                    'portal_auth_attempted': True,
                    'portal_auth_success': True,
                    'has_internet': has_internet,
                    'portal_details': auth_details,
                },
                message=f'Portal ({portal_type}) authenticated' + (
                    ' with internet' if has_internet else ' but no internet'
                ),
            )

        return StageResult(
            success=False, has_internet=False,
            context_updates={
                'captive_portal_detected': True,
                'portal_auth_attempted': True,
                'portal_auth_success': False,
                'portal_details': auth_details,
            },
            message=f'Portal ({portal_type}/{auth_method}) auth failed',
        )

    def _get_identity(self) -> dict:
        return {}

    def _get_stage_config(self) -> dict:
        """Return merged config (schema defaults + user overrides)."""
        if self._stage_config is not None:
            return self._stage_config
        # Fall back to schema defaults — the PipelineModule config system
        # will supply overrides at runtime via the module_config store.
        schema = self.get_config_schema()
        self._stage_config = {k: v['default'] for k, v in schema.items()}
        return self._stage_config

    def get_config_schema(self):
        return {
            'detection_timeout': {
                'type': 'int', 'default': 10,
                'description': 'Timeout for portal detection requests (seconds)',
            },
            'auth_timeout': {
                'type': 'int', 'default': 15,
                'description': 'Timeout for portal auth requests (seconds)',
            },
            'use_browser': {
                'type': 'bool', 'default': True,
                'description': 'Enable headless-browser fallback for JS-only / '
                               'tickbox portals (requires Playwright + Chromium; '
                               'degrades gracefully if absent)',
            },
            'browser_timeout': {
                'type': 'int', 'default': 30,
                'description': 'Timeout for the headless-browser fallback (seconds)',
            },
            'autofill_email': {
                'type': 'str', 'default': 'traveler@vasili.local',
                'description': 'Email address for marketing portal forms',
            },
            'autofill_name': {
                'type': 'str', 'default': 'J. Traveler',
                'description': 'Name for portal forms',
            },
        }

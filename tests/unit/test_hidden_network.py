"""Unit tests for HiddenNetworkModule."""

import time

import pytest
from unittest.mock import patch, MagicMock

from vasili import WifiNetwork, ConnectionResult


def _hidden_network(**kwargs):
    defaults = dict(
        ssid='', bssid='AA:BB:CC:DD:EE:FF',
        signal_strength=75, channel=6,
        encryption_type='WPA2', is_open=False,
    )
    defaults.update(kwargs)
    return WifiNetwork(**defaults)


def _visible_network(**kwargs):
    defaults = dict(
        ssid='TestNet', bssid='11:22:33:44:55:66',
        signal_strength=80, channel=1,
        encryption_type='WPA2', is_open=False,
    )
    defaults.update(kwargs)
    return WifiNetwork(**defaults)


@pytest.fixture
def module():
    from modules.hiddenNetwork import HiddenNetworkModule
    mgr = MagicMock()
    card = MagicMock()
    card.interface = 'wlan0'
    card.connect.return_value = True
    card.get_ip_address.return_value = '192.168.1.100'
    card._routing_info = None
    card.ensure_managed.return_value = True
    card.run_scan.return_value = ''
    mgr.get_card.return_value = card
    mod = HiddenNetworkModule(mgr)
    return mod, mgr, card


@pytest.mark.unit
class TestHiddenNetworkCanConnect:
    def test_accepts_hidden_network(self, module):
        mod, _, _ = module
        assert mod.can_connect(_hidden_network()) is True

    def test_rejects_visible_network(self, module):
        mod, _, _ = module
        assert mod.can_connect(_visible_network()) is False

    def test_rejects_previously_failed_bssid(self, module):
        mod, _, _ = module
        mod._failed_bssids['AA:BB:CC:DD:EE:FF'] = time.time()
        assert mod.can_connect(_hidden_network()) is False


@pytest.mark.unit
class TestHiddenNetworkResolve:
    def test_uses_cached_resolution(self, module):
        from modules.hiddenNetwork import ResolvedNetwork
        mod, mgr, card = module
        mod._resolved['AA:BB:CC:DD:EE:FF'] = ResolvedNetwork(
            original_bssid='AA:BB:CC:DD:EE:FF',
            resolved_ssid='SecretNet',
            method='known_network',
            confidence=0.9,
        )

        with patch.object(mod, 'run_speedtest', return_value=(50.0, 25.0, 10.0)):
            result = mod.connect(_hidden_network())

        assert result.connected is True
        assert result.network.ssid == 'SecretNet'
        assert 'hidden:known_network' in result.connection_method

    def test_fail_when_no_resolution(self, module):
        mod, mgr, card = module

        with (
            patch.object(mod, '_check_saved_connections', return_value=None),
            patch.object(mod, '_directed_probe_scan', return_value=None),
            patch.object(mod, '_monitor_capture', return_value=None),
        ):
            result = mod.connect(_hidden_network())

        assert result.connected is False
        assert 'AA:BB:CC:DD:EE:FF' in mod._failed_bssids

    def test_no_card_available(self, module):
        mod, mgr, _ = module
        mgr.get_card.return_value = None
        result = mod.connect(_hidden_network())
        assert result.connected is False


@pytest.mark.unit
class TestHiddenNetworkHelpers:
    def test_get_candidate_ssids(self, module):
        mod, _, _ = module

        with patch('modules.hiddenNetwork.subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout='MyWiFi\nOfficeNet\nlo\n',
            )
            candidates = mod._get_candidate_ssids()

        assert 'MyWiFi' in candidates
        assert 'OfficeNet' in candidates
        assert 'lo' not in candidates
        # Common defaults should be present
        assert 'HIDDEN' in candidates

    def test_config_schema(self, module):
        mod, _, _ = module
        schema = mod.get_config_schema()
        assert 'max_probe_ssids' in schema
        assert 'extra_probe_ssids' in schema

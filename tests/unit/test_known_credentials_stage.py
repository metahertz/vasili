"""Unit tests for KnownCredentialsStage."""

import pytest
from unittest.mock import MagicMock, patch

from modules.stages.known_networks import KnownCredentialsStage
from vasili import WifiNetwork


def _network(ssid='HomeNet', is_open=False, enc='WPA2'):
    return WifiNetwork(
        ssid=ssid,
        bssid='AA:BB:CC:DD:EE:FF',
        signal_strength=70,
        channel=6,
        encryption_type=enc,
        is_open=is_open,
    )


def _store_with(entry=None, available=True):
    s = MagicMock()
    s.is_available.return_value = available
    s.get.return_value = entry
    return s


@pytest.mark.unit
class TestKnownCredentialsStage:

    def test_can_run_skips_open_networks(self):
        stage = KnownCredentialsStage()
        assert stage.can_run(_network(is_open=True), MagicMock(), {}) is False
        assert stage.can_run(_network(is_open=False), MagicMock(), {}) is True

    def test_store_missing_returns_failure_no_card_calls(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        result = stage.run(_network(), card, {})
        assert result.success is False
        assert 'unavailable' in result.message.lower()
        card.connect.assert_not_called()

    def test_store_unavailable_returns_failure(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        result = stage.run(_network(), card, {'_known_networks_store': _store_with(available=False)})
        assert result.success is False
        card.connect.assert_not_called()

    def test_unknown_ssid_returns_failure(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        result = stage.run(
            _network(ssid='Unknown'), card,
            {'_known_networks_store': _store_with(entry=None)},
        )
        assert result.success is False
        assert 'no known credential' in result.message.lower()
        card.connect.assert_not_called()

    def test_known_ssid_success_path(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        card.connect.return_value = True
        card.interface = 'wlan0'
        store = _store_with(entry={
            'ssid': 'HomeNet', 'password': 'hunter2',
            'security': 'WPA2', 'notes': '', 'added_at': '',
        })
        with patch('modules.stages.known_networks.network_isolation') as ni:
            ni.verify_connectivity.return_value = True
            result = stage.run(_network(), card, {'_known_networks_store': store})
        card.connect.assert_called_once()
        args, kwargs = card.connect.call_args
        assert kwargs.get('password') == 'hunter2' or 'hunter2' in args
        assert result.success is True
        assert result.has_internet is True
        assert result.context_updates['connected_with'] == 'known_credential'

    def test_associated_no_internet(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        card.connect.return_value = True
        card.interface = 'wlan0'
        store = _store_with(entry={
            'ssid': 'HomeNet', 'password': 'pw',
            'security': 'WPA2', 'notes': '', 'added_at': '',
        })
        with patch('modules.stages.known_networks.network_isolation') as ni:
            ni.verify_connectivity.return_value = False
            result = stage.run(_network(), card, {'_known_networks_store': store})
        assert result.success is True
        assert result.has_internet is False
        assert result.context_updates['connected_with'] == 'known_credential'
        assert result.context_updates['wifi_associated'] is True
        assert result.context_updates['http_blocked'] is True

    def test_credential_rejected(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        card.connect.return_value = False
        card.interface = 'wlan0'
        store = _store_with(entry={
            'ssid': 'HomeNet', 'password': 'wrong',
            'security': 'WPA2', 'notes': '', 'added_at': '',
        })
        result = stage.run(_network(), card, {'_known_networks_store': store})
        assert result.success is False
        assert 'rejected' in result.message.lower()
        assert result.context_updates.get('known_credentials_failed') is True
        card.disconnect.assert_called()

    def test_disconnect_when_previously_associated(self):
        stage = KnownCredentialsStage()
        card = MagicMock()
        card.connect.return_value = True
        card.interface = 'wlan0'
        store = _store_with(entry={
            'ssid': 'HomeNet', 'password': 'pw',
            'security': 'WPA2', 'notes': '', 'added_at': '',
        })
        with patch('modules.stages.known_networks.network_isolation') as ni:
            ni.verify_connectivity.return_value = True
            stage.run(_network(), card, {
                '_known_networks_store': store,
                'wifi_associated': True,
            })
        card.disconnect.assert_called()

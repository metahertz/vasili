"""Unit tests for ConsentManager (three-mode: off/on/by_ssid)."""

import pytest
from unittest.mock import patch, MagicMock

from consent import ConsentManager


@pytest.fixture
def manager():
    mock_client = MagicMock()
    mock_client.admin.command.return_value = {'ok': 1}
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_ssid_collection = MagicMock()
    mock_client.__getitem__ = MagicMock(return_value=mock_db)

    def db_getitem(name):
        if name == 'ssid_consent':
            return mock_ssid_collection
        return mock_collection
    mock_db.__getitem__ = MagicMock(side_effect=db_getitem)

    mock_collection.find_one.return_value = None
    mock_collection.find.return_value = []
    mock_ssid_collection.find_one.return_value = None
    mock_ssid_collection.find.return_value = []

    with patch('consent.MongoClient', return_value=mock_client):
        m = ConsentManager(yaml_consent={'yaml_on': True, 'yaml_byssid': 'by_ssid'})
    m.collection = mock_collection
    m.ssid_collection = mock_ssid_collection
    return m


@pytest.mark.unit
class TestConsentModes:
    def test_default_mode_is_off(self, manager):
        assert manager.get_mode('unknown') == 'off'
        assert manager.has_consent('unknown') is False

    def test_yaml_bool_true_maps_to_on(self, manager):
        assert manager.get_mode('yaml_on') == 'on'
        assert manager.has_consent('yaml_on') is True

    def test_yaml_string_by_ssid(self, manager):
        assert manager.get_mode('yaml_byssid') == 'by_ssid'

    def test_mongodb_mode_on(self, manager):
        manager.collection.find_one.return_value = {
            'module': 'test', 'mode': 'on'
        }
        assert manager.has_consent('test') is True

    def test_mongodb_mode_off(self, manager):
        manager.collection.find_one.return_value = {
            'module': 'test', 'mode': 'off'
        }
        assert manager.has_consent('test') is False

    def test_mongodb_mode_by_ssid_without_bssid(self, manager):
        manager.collection.find_one.return_value = {
            'module': 'test', 'mode': 'by_ssid'
        }
        # No bssid passed → no consent
        assert manager.has_consent('test') is False

    def test_mongodb_mode_by_ssid_approved(self, manager):
        manager.collection.find_one.return_value = {
            'module': 'test', 'mode': 'by_ssid'
        }
        manager.ssid_collection.find_one.return_value = {
            'module': 'test', 'bssid': 'aa:bb:cc:dd:ee:ff', 'approved': True
        }
        assert manager.has_consent('test', bssid='AA:BB:CC:DD:EE:FF') is True

    def test_mongodb_mode_by_ssid_not_approved(self, manager):
        manager.collection.find_one.return_value = {
            'module': 'test', 'mode': 'by_ssid'
        }
        manager.ssid_collection.find_one.return_value = None
        assert manager.has_consent('test', bssid='AA:BB:CC:DD:EE:FF') is False

    def test_legacy_consented_bool_migration(self, manager):
        manager.collection.find_one.return_value = {
            'module': 'old', 'consented': True
        }
        assert manager.get_mode('old') == 'on'

    def test_set_mode(self, manager):
        manager.collection.update_one.return_value = MagicMock()
        assert manager.set_mode('test', 'by_ssid') is True
        assert manager.set_mode('test', 'invalid') is False

    def test_grant_sets_on(self, manager):
        manager.collection.update_one.return_value = MagicMock()
        assert manager.grant('test') is True

    def test_revoke_sets_off(self, manager):
        manager.collection.update_one.return_value = MagicMock()
        assert manager.revoke('test') is True


@pytest.mark.unit
class TestSSIDConsent:
    def test_approve_ssid(self, manager):
        manager.ssid_collection.update_one.return_value = MagicMock()
        assert manager.approve_ssid('mac_clone', 'AA:BB:CC:DD:EE:FF', 'TestNet') is True

    def test_revoke_ssid(self, manager):
        manager.ssid_collection.delete_one.return_value = MagicMock()
        assert manager.revoke_ssid('mac_clone', 'AA:BB:CC:DD:EE:FF') is True

    def test_get_approved_ssids(self, manager):
        manager.ssid_collection.find.return_value = [
            {'module': 'mac_clone', 'bssid': 'aa:bb:cc:dd:ee:ff', 'ssid': 'TestNet', 'approved': True}
        ]
        result = manager.get_approved_ssids('mac_clone')
        assert len(result) == 1
        assert result[0]['ssid'] == 'TestNet'

    def test_graceful_degradation(self):
        from pymongo.errors import ConnectionFailure
        with patch('consent.MongoClient') as mock:
            mock.side_effect = ConnectionFailure('no mongo')
            m = ConsentManager(yaml_consent={'fallback': True})

        assert m.has_consent('fallback') is True
        assert m.has_consent('other') is False
        assert m.approve_ssid('x', 'y') is False

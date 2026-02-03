"""Integration tests for captive portal detection and authentication flow."""

import pytest
from unittest.mock import patch, Mock, MagicMock
from modules.captivePortal import CaptivePortalModule
from vasili import WifiNetwork


@pytest.mark.integration
class TestCaptivePortalFlow:
    """Integration tests for full captive portal flow."""

    def test_full_portal_flow_success(self, mock_speedtest):
        """Test complete flow: connect -> detect portal -> authenticate -> speedtest."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card.connect = Mock()
        mock_card_manager.get_card.return_value = mock_card

        portal_info = {
            'redirect_url': 'http://portal.airport.com/splash',
            'redirect_domain': 'portal.airport.com',
            'portal_type': 'generic',
            'auth_method': 'click_through',
        }

        with (
            patch('modules.captivePortal.PortalDatabase') as mock_db_class,
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('modules.captivePortal.CaptivePortalAuthenticator') as mock_auth_class,
            patch('time.sleep'),
        ):
            # Mock database
            mock_db = MagicMock()
            mock_db.get_portal_pattern.return_value = None  # No known pattern
            mock_db.store_portal_pattern = Mock()
            mock_db.record_auth_result = Mock()
            mock_db_class.return_value = mock_db

            # Mock detector
            mock_detector = Mock()
            mock_detector.detect.return_value = portal_info
            mock_detector_class.return_value = mock_detector

            # Mock authenticator
            mock_authenticator = Mock()
            mock_authenticator.authenticate.return_value = True
            mock_auth_class.return_value = mock_authenticator

            # Create module and test connection
            module = CaptivePortalModule(mock_card_manager)
            network = WifiNetwork(
                ssid='Airport-WiFi',
                bssid='AA:BB:CC:DD:EE:FF',
                signal_strength=85,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)

            # Verify the complete flow
            assert result.connected is True
            assert result.interface == 'wlan0'
            assert result.connection_method == 'captive_portal'

            # Verify card was used
            mock_card.connect.assert_called_once_with(network)

            # Verify portal was detected
            mock_detector.detect.assert_called_once()

            # Verify authentication was attempted
            mock_authenticator.authenticate.assert_called_once_with(portal_info)

            # Verify pattern was stored
            mock_db.store_portal_pattern.assert_called_once_with('Airport-WiFi', portal_info)

            # Verify result was recorded
            mock_db.record_auth_result.assert_called_once_with(
                'Airport-WiFi', 'portal.airport.com', True
            )

            # Verify speedtest was run
            assert result.download_speed > 0
            assert result.upload_speed > 0
            assert result.ping > 0

    def test_full_portal_flow_auth_failure(self):
        """Test flow when portal authentication fails."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card.connect = Mock()
        mock_card_manager.get_card.return_value = mock_card

        portal_info = {
            'redirect_url': 'http://portal.hotel.com/login',
            'redirect_domain': 'portal.hotel.com',
            'portal_type': 'generic',
            'auth_method': 'login_required',
        }

        with (
            patch('modules.captivePortal.PortalDatabase') as mock_db_class,
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('modules.captivePortal.CaptivePortalAuthenticator') as mock_auth_class,
            patch('time.sleep'),
        ):
            # Mock database
            mock_db = MagicMock()
            mock_db.get_portal_pattern.return_value = None
            mock_db.store_portal_pattern = Mock()
            mock_db.record_auth_result = Mock()
            mock_db_class.return_value = mock_db

            # Mock detector
            mock_detector = Mock()
            mock_detector.detect.return_value = portal_info
            mock_detector_class.return_value = mock_detector

            # Mock authenticator to fail
            mock_authenticator = Mock()
            mock_authenticator.authenticate.return_value = False
            mock_auth_class.return_value = mock_authenticator

            # Create module and test connection
            module = CaptivePortalModule(mock_card_manager)
            network = WifiNetwork(
                ssid='Hotel-WiFi',
                bssid='11:22:33:44:55:66',
                signal_strength=75,
                channel=11,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)

            # Verify connection failed due to auth failure
            assert result.connected is False
            assert result.connection_method == 'captive_portal'

            # Verify failure was recorded
            mock_db.record_auth_result.assert_called_once_with(
                'Hotel-WiFi', 'portal.hotel.com', False
            )

    def test_full_portal_flow_no_portal(self, mock_speedtest):
        """Test flow when no captive portal is detected."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card.connect = Mock()
        mock_card_manager.get_card.return_value = mock_card

        with (
            patch('modules.captivePortal.PortalDatabase') as mock_db_class,
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('time.sleep'),
        ):
            # Mock database
            mock_db = MagicMock()
            mock_db.get_portal_pattern.return_value = None
            mock_db_class.return_value = mock_db

            # Mock detector to return no portal
            mock_detector = Mock()
            mock_detector.detect.return_value = None
            mock_detector_class.return_value = mock_detector

            # Create module and test connection
            module = CaptivePortalModule(mock_card_manager)
            network = WifiNetwork(
                ssid='OpenCafe',
                bssid='99:88:77:66:55:44',
                signal_strength=90,
                channel=1,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)

            # Verify connection succeeded without portal auth
            assert result.connected is True
            assert result.interface == 'wlan0'
            assert result.connection_method == 'captive_portal'

            # Verify detector was called but no auth happened
            mock_detector.detect.assert_called_once()

    def test_known_portal_pattern_reuse(self, mock_speedtest):
        """Test that known portal patterns are retrieved and used."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card.connect = Mock()
        mock_card_manager.get_card.return_value = mock_card

        known_pattern = {
            'ssid': 'Starbucks-WiFi',
            'redirect_domain': 'portal.starbucks.com',
            'portal_type': 'starbucks',
            'auth_method': 'terms_acceptance',
            'success_count': 5,
        }

        portal_info = {
            'redirect_url': 'http://portal.starbucks.com/accept',
            'redirect_domain': 'portal.starbucks.com',
            'portal_type': 'starbucks',
            'auth_method': 'terms_acceptance',
        }

        with (
            patch('modules.captivePortal.PortalDatabase') as mock_db_class,
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('modules.captivePortal.CaptivePortalAuthenticator') as mock_auth_class,
            patch('time.sleep'),
        ):
            # Mock database with known pattern
            mock_db = MagicMock()
            mock_db.get_portal_pattern.return_value = known_pattern
            mock_db.store_portal_pattern = Mock()
            mock_db.record_auth_result = Mock()
            mock_db_class.return_value = mock_db

            # Mock detector
            mock_detector = Mock()
            mock_detector.detect.return_value = portal_info
            mock_detector_class.return_value = mock_detector

            # Mock authenticator
            mock_authenticator = Mock()
            mock_authenticator.authenticate.return_value = True
            mock_auth_class.return_value = mock_authenticator

            # Create module and test connection
            module = CaptivePortalModule(mock_card_manager)
            network = WifiNetwork(
                ssid='Starbucks-WiFi',
                bssid='AA:BB:CC:DD:EE:00',
                signal_strength=80,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)

            # Verify known pattern was retrieved
            mock_db.get_portal_pattern.assert_called_once_with('Starbucks-WiFi')

            # Verify connection succeeded
            assert result.connected is True

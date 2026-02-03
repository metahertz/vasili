"""Unit tests for Captive Portal module."""

import pytest
from unittest.mock import patch, Mock, MagicMock
from modules.captivePortal import (
    CaptivePortalDetector,
    CaptivePortalAuthenticator,
    CaptivePortalModule,
    PortalDatabase,
)
from vasili import WifiNetwork


@pytest.mark.unit
class TestCaptivePortalDetector:
    """Test suite for CaptivePortalDetector class."""

    def test_detect_no_portal(self):
        """Test detection when no captive portal is present."""
        detector = CaptivePortalDetector()

        # Mock successful 204 response (no portal)
        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 204
            mock_get.return_value = mock_response

            result = detector.detect()
            assert result is None

    def test_detect_redirect_portal(self):
        """Test detection of captive portal via HTTP redirect."""
        detector = CaptivePortalDetector()

        # Mock 302 redirect response
        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 302
            mock_response.headers = {'Location': 'http://portal.example.com/login'}
            mock_response.text = '<html>Please accept terms</html>'
            mock_get.return_value = mock_response

            result = detector.detect()
            assert result is not None
            assert result['redirect_url'] == 'http://portal.example.com/login'
            assert result['redirect_domain'] == 'portal.example.com'

    def test_detect_apple_portal(self):
        """Test detection of Apple-specific captive portal."""
        detector = CaptivePortalDetector()

        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 302
            mock_response.headers = {'Location': 'http://captive.apple.com/portal'}
            mock_response.text = 'Portal page'
            mock_get.return_value = mock_response

            result = detector.detect()
            assert result is not None
            assert result['portal_type'] == 'apple'

    def test_detect_terms_acceptance(self):
        """Test detection of terms acceptance portal."""
        detector = CaptivePortalDetector()

        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 302
            mock_response.headers = {'Location': 'http://portal.wifi.com/accept'}
            mock_response.text = '<html>Please accept the terms and conditions</html>'
            mock_get.return_value = mock_response

            result = detector.detect()
            assert result is not None
            assert result['auth_method'] == 'terms_acceptance'

    def test_detect_click_through(self):
        """Test detection of click-through portal."""
        detector = CaptivePortalDetector()

        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 302
            mock_response.headers = {'Location': 'http://portal.wifi.com/splash'}
            mock_response.text = '<html>Click continue to connect</html>'
            mock_get.return_value = mock_response

            result = detector.detect()
            assert result is not None
            assert result['auth_method'] == 'click_through'

    def test_detect_login_required(self):
        """Test detection of login-required portal."""
        detector = CaptivePortalDetector()

        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 302
            mock_response.headers = {'Location': 'http://portal.wifi.com/login'}
            mock_response.text = '<html>Please enter your username and password</html>'
            mock_get.return_value = mock_response

            result = detector.detect()
            assert result is not None
            assert result['auth_method'] == 'login_required'

    def test_detect_network_error(self):
        """Test detection handles network errors gracefully."""
        detector = CaptivePortalDetector()

        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception('Network error')

            result = detector.detect()
            # Should try other URLs and eventually return None
            assert result is None or isinstance(result, dict)


@pytest.mark.unit
class TestCaptivePortalAuthenticator:
    """Test suite for CaptivePortalAuthenticator class."""

    def test_authenticate_click_through(self):
        """Test authentication via click-through."""
        authenticator = CaptivePortalAuthenticator()
        portal_info = {
            'redirect_url': 'http://portal.example.com/splash',
            'auth_method': 'click_through',
            'portal_type': 'generic',
        }

        with patch('requests.get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_get.return_value = mock_response

            result = authenticator.authenticate(portal_info)
            assert result is True
            mock_get.assert_called_once()

    def test_authenticate_terms_acceptance(self):
        """Test authentication via terms acceptance."""
        authenticator = CaptivePortalAuthenticator()
        portal_info = {
            'redirect_url': 'http://portal.example.com/terms',
            'auth_method': 'terms_acceptance',
            'portal_type': 'generic',
        }

        with patch('requests.get') as mock_get, patch('requests.post') as mock_post:
            # Mock GET response with form
            mock_get_response = Mock()
            mock_get_response.text = '<form action="/accept" method="post"></form>'
            mock_get.return_value = mock_get_response

            # Mock POST response
            mock_post_response = Mock()
            mock_post_response.status_code = 200
            mock_post.return_value = mock_post_response

            result = authenticator.authenticate(portal_info)
            assert result is True

    def test_authenticate_login_required_fails(self):
        """Test that login-required portals return False."""
        authenticator = CaptivePortalAuthenticator()
        portal_info = {
            'redirect_url': 'http://portal.example.com/login',
            'auth_method': 'login_required',
            'portal_type': 'generic',
        }

        result = authenticator.authenticate(portal_info)
        assert result is False

    def test_authenticate_payment_required_fails(self):
        """Test that payment-required portals return False."""
        authenticator = CaptivePortalAuthenticator()
        portal_info = {
            'redirect_url': 'http://portal.example.com/payment',
            'auth_method': 'payment_required',
            'portal_type': 'generic',
        }

        result = authenticator.authenticate(portal_info)
        assert result is False

    def test_authenticate_network_error(self):
        """Test authentication handles network errors gracefully."""
        authenticator = CaptivePortalAuthenticator()
        portal_info = {
            'redirect_url': 'http://portal.example.com/splash',
            'auth_method': 'click_through',
            'portal_type': 'generic',
        }

        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception('Network error')

            result = authenticator.authenticate(portal_info)
            assert result is False


@pytest.mark.unit
class TestPortalDatabase:
    """Test suite for PortalDatabase class."""

    def test_init_success(self):
        """Test successful database initialization."""
        with patch('modules.captivePortal.MongoClient') as mock_client:
            mock_instance = MagicMock()
            mock_instance.admin.command.return_value = None
            mock_client.return_value = mock_instance

            db = PortalDatabase()
            assert db.client is not None
            mock_client.assert_called_once()

    def test_init_connection_failure(self):
        """Test database initialization with connection failure."""
        with patch('modules.captivePortal.MongoClient') as mock_client:
            from pymongo.errors import ConnectionFailure

            mock_client.return_value.admin.command.side_effect = ConnectionFailure(
                'Connection failed'
            )

            db = PortalDatabase()
            assert db.client is None

    def test_store_portal_pattern(self):
        """Test storing portal pattern."""
        with patch('modules.captivePortal.MongoClient') as mock_client:
            mock_instance = MagicMock()
            mock_collection = MagicMock()
            mock_instance.admin.command.return_value = None
            mock_instance.__getitem__.return_value.__getitem__.return_value = mock_collection
            mock_client.return_value = mock_instance

            db = PortalDatabase()
            pattern_data = {
                'redirect_domain': 'portal.example.com',
                'portal_type': 'generic',
                'auth_method': 'click_through',
            }

            db.store_portal_pattern('TestNetwork', pattern_data)
            mock_collection.update_one.assert_called_once()

    def test_get_portal_pattern(self):
        """Test retrieving portal pattern."""
        with patch('modules.captivePortal.MongoClient') as mock_client:
            mock_instance = MagicMock()
            mock_collection = MagicMock()
            mock_instance.admin.command.return_value = None
            mock_instance.__getitem__.return_value.__getitem__.return_value = mock_collection
            mock_client.return_value = mock_instance

            # Mock find_one return value
            expected_pattern = {
                'ssid': 'TestNetwork',
                'redirect_domain': 'portal.example.com',
                'portal_type': 'generic',
            }
            mock_collection.find_one.return_value = expected_pattern

            db = PortalDatabase()
            result = db.get_portal_pattern('TestNetwork')

            assert result == expected_pattern
            mock_collection.find_one.assert_called_once()

    def test_get_portal_pattern_not_found(self):
        """Test retrieving non-existent portal pattern."""
        with patch('modules.captivePortal.MongoClient') as mock_client:
            mock_instance = MagicMock()
            mock_collection = MagicMock()
            mock_instance.admin.command.return_value = None
            mock_instance.__getitem__.return_value.__getitem__.return_value = mock_collection
            mock_client.return_value = mock_instance

            mock_collection.find_one.return_value = None

            db = PortalDatabase()
            result = db.get_portal_pattern('UnknownNetwork')

            assert result is None

    def test_record_auth_result_success(self):
        """Test recording successful authentication."""
        with patch('modules.captivePortal.MongoClient') as mock_client:
            mock_instance = MagicMock()
            mock_collection = MagicMock()
            mock_instance.admin.command.return_value = None
            mock_instance.__getitem__.return_value.__getitem__.return_value = mock_collection
            mock_client.return_value = mock_instance

            db = PortalDatabase()
            db.record_auth_result('TestNetwork', 'portal.example.com', True)

            mock_collection.update_one.assert_called_once()
            call_args = mock_collection.update_one.call_args
            assert 'success_count' in str(call_args)


@pytest.mark.unit
class TestCaptivePortalModule:
    """Test suite for CaptivePortalModule class."""

    def test_can_connect_open_network(self):
        """Test can_connect returns True for open networks."""
        mock_card_manager = Mock()

        with patch('modules.captivePortal.PortalDatabase'):
            module = CaptivePortalModule(mock_card_manager)

            network = WifiNetwork(
                ssid='OpenNetwork',
                bssid='00:11:22:33:44:55',
                signal_strength=70,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            assert module.can_connect(network) is True

    def test_can_connect_encrypted_network(self):
        """Test can_connect returns False for encrypted networks (prioritizes open)."""
        mock_card_manager = Mock()

        with patch('modules.captivePortal.PortalDatabase'):
            module = CaptivePortalModule(mock_card_manager)

            network = WifiNetwork(
                ssid='SecureNetwork',
                bssid='00:11:22:33:44:55',
                signal_strength=70,
                channel=6,
                encryption_type='WPA2',
                is_open=False,
            )

            assert module.can_connect(network) is False

    def test_connect_no_cards_available(self):
        """Test connect when no WiFi cards are available."""
        mock_card_manager = Mock()
        mock_card_manager.get_card.return_value = None

        with patch('modules.captivePortal.PortalDatabase'):
            module = CaptivePortalModule(mock_card_manager)

            network = WifiNetwork(
                ssid='TestNetwork',
                bssid='00:11:22:33:44:55',
                signal_strength=70,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)
            assert result.connected is False

    def test_connect_no_portal_detected(self, mock_speedtest):
        """Test successful connection with no captive portal."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card_manager.get_card.return_value = mock_card

        with (
            patch('modules.captivePortal.PortalDatabase'),
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('time.sleep'),
        ):
            # Mock detector to return None (no portal)
            mock_detector = Mock()
            mock_detector.detect.return_value = None
            mock_detector_class.return_value = mock_detector

            module = CaptivePortalModule(mock_card_manager)

            network = WifiNetwork(
                ssid='TestNetwork',
                bssid='00:11:22:33:44:55',
                signal_strength=70,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)
            assert result.connected is True
            assert result.interface == 'wlan0'

    def test_connect_portal_auth_success(self, mock_speedtest):
        """Test successful connection through captive portal."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card_manager.get_card.return_value = mock_card

        portal_info = {
            'redirect_url': 'http://portal.example.com',
            'redirect_domain': 'portal.example.com',
            'portal_type': 'generic',
            'auth_method': 'click_through',
        }

        with (
            patch('modules.captivePortal.PortalDatabase'),
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('modules.captivePortal.CaptivePortalAuthenticator') as mock_auth_class,
            patch('time.sleep'),
        ):
            # Mock detector to return portal info
            mock_detector = Mock()
            mock_detector.detect.return_value = portal_info
            mock_detector_class.return_value = mock_detector

            # Mock authenticator to succeed
            mock_authenticator = Mock()
            mock_authenticator.authenticate.return_value = True
            mock_auth_class.return_value = mock_authenticator

            module = CaptivePortalModule(mock_card_manager)

            network = WifiNetwork(
                ssid='PortalNetwork',
                bssid='00:11:22:33:44:55',
                signal_strength=70,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)
            assert result.connected is True
            assert result.interface == 'wlan0'
            mock_authenticator.authenticate.assert_called_once_with(portal_info)

    def test_connect_portal_auth_failure(self):
        """Test connection failure when portal auth fails."""
        mock_card_manager = Mock()
        mock_card = Mock()
        mock_card.interface = 'wlan0'
        mock_card_manager.get_card.return_value = mock_card

        portal_info = {
            'redirect_url': 'http://portal.example.com',
            'redirect_domain': 'portal.example.com',
            'portal_type': 'generic',
            'auth_method': 'login_required',
        }

        with (
            patch('modules.captivePortal.PortalDatabase'),
            patch('modules.captivePortal.CaptivePortalDetector') as mock_detector_class,
            patch('modules.captivePortal.CaptivePortalAuthenticator') as mock_auth_class,
            patch('time.sleep'),
        ):
            # Mock detector to return portal info
            mock_detector = Mock()
            mock_detector.detect.return_value = portal_info
            mock_detector_class.return_value = mock_detector

            # Mock authenticator to fail
            mock_authenticator = Mock()
            mock_authenticator.authenticate.return_value = False
            mock_auth_class.return_value = mock_authenticator

            module = CaptivePortalModule(mock_card_manager)

            network = WifiNetwork(
                ssid='SecurePortalNetwork',
                bssid='00:11:22:33:44:55',
                signal_strength=70,
                channel=6,
                encryption_type='Open',
                is_open=True,
            )

            result = module.connect(network)
            assert result.connected is False

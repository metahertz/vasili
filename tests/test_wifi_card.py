"""Unit tests for WifiCard class"""

import subprocess
from unittest.mock import MagicMock, Mock, mock_open, patch

import pytest

from vasili import WifiCard, WifiNetwork


class TestWifiCardInit:
    """Test WifiCard initialization"""

    @patch('subprocess.run')
    def test_init_valid_interface(self, mock_run):
        """Test initializing with a valid wireless interface"""
        mock_run.return_value = Mock(returncode=0)

        card = WifiCard('wlan0')

        assert card.interface == 'wlan0'
        assert card.in_use is False
        mock_run.assert_called_once_with(['iwconfig', 'wlan0'], check=True, capture_output=True)

    @patch('subprocess.run')
    def test_init_invalid_interface(self, mock_run):
        """Test initializing with an invalid interface raises ValueError"""
        mock_run.side_effect = subprocess.CalledProcessError(1, 'iwconfig')

        with pytest.raises(ValueError, match='not a valid wireless device'):
            WifiCard('eth0')


class TestWifiCardScan:
    """Test WifiCard.scan() method"""

    @patch('subprocess.run')
    def test_scan_success(self, mock_run):
        """Test successful network scan"""
        # Mock iwconfig for init
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        # Mock scan output
        scan_output = """wlan0     Scan completed :
          Cell 01 - Address: AA:BB:CC:DD:EE:F1
                    ESSID:"TestNetwork1"
                    Channel:6
                    Signal level=-45 dBm
                    Encryption key:off
          Cell 02 - Address: AA:BB:CC:DD:EE:F2
                    ESSID:"TestNetwork2"
                    Channel:11
                    Signal level=-65 dBm
                    Encryption key:on
                    IE: IEEE 802.11i/WPA2 Version 1
"""

        mock_run.return_value = Mock(returncode=0, stdout=scan_output)

        networks = card.scan()

        assert len(networks) == 2

        # Check first network
        assert networks[0].ssid == 'TestNetwork1'
        assert networks[0].bssid == 'AA:BB:CC:DD:EE:F1'
        assert networks[0].channel == 6
        assert networks[0].is_open is True
        assert networks[0].signal_strength > 0

        # Check second network
        assert networks[1].ssid == 'TestNetwork2'
        assert networks[1].bssid == 'AA:BB:CC:DD:EE:F2'
        assert networks[1].channel == 11
        assert networks[1].is_open is False
        assert networks[1].encryption_type == 'WPA2'

    @patch('subprocess.run')
    def test_scan_empty_results(self, mock_run):
        """Test scan with no networks found"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        mock_run.return_value = Mock(returncode=0, stdout='wlan0     Scan completed :\n')

        networks = card.scan()

        assert networks == []

    @patch('subprocess.run')
    def test_scan_failure(self, mock_run):
        """Test scan failure returns empty list"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        mock_run.side_effect = subprocess.CalledProcessError(1, 'iwlist')

        networks = card.scan()

        assert networks == []


class TestWifiCardConnect:
    """Test WifiCard.connect() method"""

    @patch('subprocess.run')
    def test_connect_open_network_success(self, mock_run):
        """Test successful connection to open network"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        network = WifiNetwork(
            ssid='OpenNetwork',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        result = card.connect(network)

        assert result is True
        assert card.in_use is True

    @patch('subprocess.run')
    def test_connect_encrypted_network_with_password(self, mock_run):
        """Test connection to encrypted network with password"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        network = WifiNetwork(
            ssid='SecureNetwork',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

        result = card.connect(network, password='secret123')

        assert result is True
        assert card.in_use is True

    @patch('subprocess.run')
    def test_connect_failure(self, mock_run):
        """Test connection failure"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        network = WifiNetwork(
            ssid='TestNetwork',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # Make nmcli connect fail
        mock_run.return_value = Mock(returncode=1, stderr='Connection failed')

        result = card.connect(network)

        assert result is False
        assert card.in_use is False

    @patch('subprocess.run')
    def test_connect_timeout(self, mock_run):
        """Test connection timeout"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        network = WifiNetwork(
            ssid='TestNetwork',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # Simulate timeout
        mock_run.side_effect = subprocess.TimeoutExpired('nmcli', 30)

        result = card.connect(network)

        assert result is False


class TestWifiCardDisconnect:
    """Test WifiCard.disconnect() method"""

    @patch('subprocess.run')
    def test_disconnect_success(self, mock_run):
        """Test successful disconnect"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')
        card.in_use = True

        result = card.disconnect()

        assert result is True
        assert card.in_use is False

    @patch('subprocess.run')
    def test_disconnect_failure(self, mock_run):
        """Test disconnect failure"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        mock_run.return_value = Mock(returncode=1, stderr='Disconnect failed')

        result = card.disconnect()

        assert result is False


class TestWifiCardStatus:
    """Test WifiCard.get_status() and _is_interface_up() methods"""

    @patch('subprocess.run')
    @patch('builtins.open', new_callable=mock_open, read_data='up\n')
    def test_get_status_interface_up(self, mock_file, mock_run):
        """Test get_status when interface is up"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')
        card.in_use = True

        status = card.get_status()

        assert status['interface'] == 'wlan0'
        assert status['in_use'] is True
        assert status['is_up'] is True

    @patch('subprocess.run')
    @patch('builtins.open', new_callable=mock_open, read_data='down\n')
    def test_get_status_interface_down(self, mock_file, mock_run):
        """Test get_status when interface is down"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        status = card.get_status()

        assert status['is_up'] is False

    @patch('subprocess.run')
    @patch('builtins.open', side_effect=FileNotFoundError)
    def test_is_interface_up_file_not_found(self, mock_file, mock_run):
        """Test _is_interface_up when sysfs file doesn't exist"""
        mock_run.return_value = Mock(returncode=0)
        card = WifiCard('wlan0')

        assert card._is_interface_up() is False

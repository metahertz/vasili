"""Unit tests for WifiCard class"""

import subprocess
from unittest.mock import Mock, mock_open, patch

import pytest

from vasili import WifiCard, WifiNetwork


def _patch_wireless_isdir():
    """Patch os.path.isdir to treat wlan* as wireless interfaces."""
    import os.path as real_ospath

    def mock_isdir(path):
        if '/sys/class/net/' in path and '/wireless' in path:
            iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
            return iface.startswith('wlan')
        return real_ospath.isdir(path)

    return patch('os.path.isdir', side_effect=mock_isdir)


class TestWifiCardInit:
    """Test WifiCard initialization"""

    def test_init_valid_interface(self):
        """Test initializing with a valid wireless interface"""
        with _patch_wireless_isdir():
            card = WifiCard('wlan0')

        assert card.interface == 'wlan0'
        assert card.in_use is False

    def test_init_invalid_interface(self):
        """Test initializing with an invalid interface raises ValueError"""
        with patch('os.path.isdir', return_value=False):
            with pytest.raises(ValueError, match='not a valid wireless device'):
                WifiCard('eth0')


class TestWifiCardScan:
    """Test WifiCard.scan() method"""

    def test_scan_success(self):
        """Test successful network scan"""
        scan_output = (
            "TestNetwork1:AA\\:BB\\:CC\\:DD\\:EE\\:F1:90:6:\n"
            "TestNetwork2:AA\\:BB\\:CC\\:DD\\:EE\\:F2:60:11:WPA2\n"
        )

        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout=scan_output, stderr='')
            card = WifiCard('wlan0')
            networks = card.scan()

        assert len(networks) == 2

        # Check first network
        assert networks[0].ssid == 'TestNetwork1'
        assert networks[0].bssid == 'AA:BB:CC:DD:EE:F1'
        assert networks[0].channel == 6
        assert networks[0].is_open is True
        assert networks[0].signal_strength == 90

        # Check second network
        assert networks[1].ssid == 'TestNetwork2'
        assert networks[1].bssid == 'AA:BB:CC:DD:EE:F2'
        assert networks[1].channel == 11
        assert networks[1].is_open is False
        assert networks[1].encryption_type == 'WPA2'

    def test_scan_empty_results(self):
        """Test scan with no networks found"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
            card = WifiCard('wlan0')
            networks = card.scan()

        assert networks == []

    def test_scan_failure(self):
        """Test scan failure returns empty list"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            # Init succeeds (no subprocess needed for init now)
            card = WifiCard('wlan0')

            mock_run.side_effect = subprocess.CalledProcessError(1, 'nmcli')
            networks = card.scan()

        assert networks == []


class TestWifiCardConnect:
    """Test WifiCard.connect() method"""

    def test_connect_open_network_success(self):
        """Test successful connection to open network"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
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

    def test_connect_encrypted_network_with_password(self):
        """Test connection to encrypted network with password"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
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

    def test_connect_failure(self):
        """Test connection failure"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
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

    def test_connect_timeout(self):
        """Test connection timeout"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
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

    def test_disconnect_success(self):
        """Test successful disconnect"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
            card = WifiCard('wlan0')
            card.in_use = True

            result = card.disconnect()

        assert result is True
        assert card.in_use is False

    def test_disconnect_failure(self):
        """Test disconnect failure"""
        with _patch_wireless_isdir(), patch('subprocess.run') as mock_run, patch('time.sleep'):
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
            card = WifiCard('wlan0')

            mock_run.return_value = Mock(returncode=1, stderr='Disconnect failed')

            result = card.disconnect()

        assert result is False


class TestWifiCardStatus:
    """Test WifiCard.get_status() and _is_interface_up() methods"""

    def test_get_status_interface_up(self):
        """Test get_status when interface is up"""
        with (
            _patch_wireless_isdir(),
            patch('builtins.open', new_callable=mock_open, read_data='up\n'),
        ):
            card = WifiCard('wlan0')
            card.in_use = True

            status = card.get_status()

        assert status['interface'] == 'wlan0'
        assert status['in_use'] is True
        assert status['is_up'] is True

    def test_get_status_interface_down(self):
        """Test get_status when interface is down"""
        with (
            _patch_wireless_isdir(),
            patch('builtins.open', new_callable=mock_open, read_data='down\n'),
        ):
            card = WifiCard('wlan0')

            status = card.get_status()

        assert status['is_up'] is False

    def test_is_interface_up_file_not_found(self):
        """Test _is_interface_up when sysfs file doesn't exist"""
        with _patch_wireless_isdir():
            card = WifiCard('wlan0')

        with patch('builtins.open', side_effect=FileNotFoundError):
            assert card._is_interface_up() is False

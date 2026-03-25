"""Unit tests for WifiCard class."""

import pytest
from unittest.mock import patch
from subprocess import CompletedProcess
from vasili import WifiCard, WifiNetwork


@pytest.mark.unit
class TestWifiCard:
    """Test suite for WifiCard class."""

    def test_init_valid_interface(self, mock_subprocess):
        """Test WifiCard initialization with valid wireless interface."""
        card = WifiCard('wlan0')
        assert card.interface == 'wlan0'
        assert card.in_use is False

    def test_init_invalid_interface(self):
        """Test WifiCard initialization with invalid interface."""
        with patch('os.path.isdir', return_value=False):
            with pytest.raises(ValueError, match='not a valid wireless device'):
                WifiCard('eth0')

    def test_scan_success(self, mock_subprocess):
        """Test successful network scan."""
        card = WifiCard('wlan0')
        networks = card.scan()

        assert len(networks) == 4
        assert networks[0].ssid == 'OpenCafe'
        assert networks[0].is_open is True
        assert networks[1].ssid == 'SecureHome'
        assert networks[1].is_open is False
        assert networks[1].encryption_type == 'WPA2'
        assert networks[3].ssid == 'ModernWiFi'
        assert networks[3].encryption_type == 'WPA3'

    def test_scan_empty_results(self, mock_subprocess_scan_empty):
        """Test scan with no networks found."""
        card = WifiCard('wlan0')
        networks = card.scan()
        assert len(networks) == 0

    def test_scan_failure(self):
        """Test scan when nmcli command fails."""
        import os.path as real_ospath
        from tests.fixtures.mock_data import WIRELESS_INTERFACES

        def mock_isdir(path):
            if '/sys/class/net/' in path and '/wireless' in path:
                iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
                return iface in WIRELESS_INTERFACES
            return real_ospath.isdir(path)

        from subprocess import CalledProcessError

        with (
            patch('os.path.isdir', side_effect=mock_isdir),
            patch('subprocess.run') as mock_run,
            patch('time.sleep'),
        ):
            mock_run.side_effect = [
                # ip link set up succeeds
                CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
                # nmcli rescan succeeds
                CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
                # nmcli wifi list fails
                CalledProcessError(1, 'nmcli'),
            ]

            card = WifiCard('wlan0')
            networks = card.scan()
            assert networks == []

    def test_scan_parses_signal_strength(self, mock_subprocess):
        """Test that signal strength is correctly parsed from nmcli output."""
        card = WifiCard('wlan0')
        networks = card.scan()

        # nmcli reports signal as percentage directly
        assert networks[0].signal_strength == 95  # OpenCafe
        assert networks[1].signal_strength == 71  # SecureHome
        assert networks[2].signal_strength == 40  # WeakSignal

    def test_connect_open_network(self, mock_subprocess):
        """Test connecting to an open network."""
        card = WifiCard('wlan0')
        network = WifiNetwork(
            ssid='OpenCafe',
            bssid='00:11:22:33:44:55',
            signal_strength=85,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        result = card.connect(network)
        assert result is True
        assert card.in_use is True

    def test_connect_encrypted_network_with_password(self, mock_subprocess):
        """Test connecting to encrypted network with password."""
        card = WifiCard('wlan0')
        network = WifiNetwork(
            ssid='SecureHome',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=70,
            channel=11,
            encryption_type='WPA2',
            is_open=False,
        )

        result = card.connect(network, password='mysecretpassword')
        assert result is True
        assert card.in_use is True

    def test_connect_failure(self, mock_subprocess_connect_fail):
        """Test connection failure handling."""
        card = WifiCard('wlan0')
        network = WifiNetwork(
            ssid='SecureHome',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=70,
            channel=11,
            encryption_type='WPA2',
            is_open=False,
        )

        result = card.connect(network, password='wrongpassword')
        assert result is False
        assert card.in_use is False

    def test_connect_timeout(self):
        """Test connection timeout handling."""
        import os.path as real_ospath
        from tests.fixtures.mock_data import WIRELESS_INTERFACES

        def mock_isdir(path):
            if '/sys/class/net/' in path and '/wireless' in path:
                iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
                return iface in WIRELESS_INTERFACES
            return real_ospath.isdir(path)

        with (
            patch('os.path.isdir', side_effect=mock_isdir),
            patch('subprocess.run') as mock_run,
            patch('time.sleep'),
        ):
            mock_run.side_effect = [
                # ip link set up succeeds
                CompletedProcess(args=[], returncode=0, stdout=''),
                # nmcli disconnect succeeds
                CompletedProcess(args=[], returncode=0, stdout=''),
                # nmcli connect times out
                Exception('Timeout'),
            ]

            card = WifiCard('wlan0')
            network = WifiNetwork(
                ssid='SlowNetwork',
                bssid='11:22:33:44:55:66',
                signal_strength=50,
                channel=1,
                encryption_type='',
                is_open=True,
            )

            result = card.connect(network)
            assert result is False

    def test_disconnect_success(self, mock_subprocess):
        """Test successful disconnect."""
        card = WifiCard('wlan0')
        card.in_use = True

        result = card.disconnect()
        assert result is True
        assert card.in_use is False

    def test_disconnect_failure(self):
        """Test disconnect failure handling."""
        import os.path as real_ospath
        from tests.fixtures.mock_data import WIRELESS_INTERFACES

        def mock_isdir(path):
            if '/sys/class/net/' in path and '/wireless' in path:
                iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
                return iface in WIRELESS_INTERFACES
            return real_ospath.isdir(path)

        with (
            patch('os.path.isdir', side_effect=mock_isdir),
            patch('subprocess.run') as mock_run,
            patch('time.sleep'),
        ):
            mock_run.side_effect = [
                # nmcli disconnect fails
                CompletedProcess(args=[], returncode=1, stdout='', stderr='Error'),
            ]

            card = WifiCard('wlan0')
            card.in_use = True

            result = card.disconnect()
            assert result is False

    def test_get_status_interface_up(self, mock_subprocess, mock_file_io):
        """Test get_status when interface is up."""
        card = WifiCard('wlan0')
        status = card.get_status()

        assert status['interface'] == 'wlan0'
        assert status['in_use'] is False
        assert status['is_up'] is True

    def test_get_status_interface_down(self, mock_subprocess, mock_file_io_interface_down):
        """Test get_status when interface is down."""
        card = WifiCard('wlan0')
        status = card.get_status()

        assert status['interface'] == 'wlan0'
        assert status['is_up'] is False

    def test_multiple_scans(self, mock_subprocess):
        """Test that multiple scans can be performed."""
        card = WifiCard('wlan0')

        networks1 = card.scan()
        networks2 = card.scan()

        assert len(networks1) == 4
        assert len(networks2) == 4
        assert networks1[0].ssid == networks2[0].ssid

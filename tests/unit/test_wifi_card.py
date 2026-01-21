"""Unit tests for WifiCard class."""

import pytest
from unittest.mock import patch
from subprocess import CompletedProcess
from vasili import WifiCard, WifiNetwork
from tests.fixtures.mock_data import (
    IWLIST_SCAN_OUTPUT,
    IWCONFIG_OUTPUT_VALID,
    SAMPLE_NETWORKS,
)


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
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("no wireless extensions")
            with pytest.raises(ValueError, match="not a valid wireless device"):
                WifiCard('eth0')

    def test_scan_success(self, mock_subprocess):
        """Test successful network scan."""
        card = WifiCard('wlan0')
        networks = card.scan()

        assert len(networks) == 3
        assert networks[0].ssid == 'OpenCafe'
        assert networks[0].is_open is True
        assert networks[1].ssid == 'SecureHome'
        assert networks[1].is_open is False
        assert networks[1].encryption_type == 'WPA2'

    def test_scan_empty_results(self, mock_subprocess_scan_empty):
        """Test scan with no networks found."""
        card = WifiCard('wlan0')
        networks = card.scan()
        assert len(networks) == 0

    def test_scan_failure(self):
        """Test scan when iwlist command fails."""
        with patch('subprocess.run') as mock_run:
            # First call for __init__ succeeds
            mock_run.side_effect = [
                CompletedProcess(args=[], returncode=0, stdout=IWCONFIG_OUTPUT_VALID),
                # ip link set up succeeds
                CompletedProcess(args=[], returncode=0, stdout=''),
                # iwlist scan fails
                CompletedProcess(
                    args=[], returncode=1, stdout='', stderr='Operation not permitted'
                ),
            ]

            card = WifiCard('wlan0')
            networks = card.scan()
            assert networks == []

    def test_scan_parses_signal_strength(self, mock_subprocess):
        """Test that signal strength is correctly converted from dBm."""
        card = WifiCard('wlan0')
        networks = card.scan()

        # -40 dBm should be high signal (120%)
        assert networks[0].signal_strength >= 100
        # -60 dBm should be medium signal (~80%)
        assert 70 <= networks[1].signal_strength <= 90
        # -80 dBm should be low signal (~40%)
        assert 30 <= networks[2].signal_strength <= 50

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
        with patch('subprocess.run') as mock_run:
            # First call for __init__ succeeds
            mock_run.side_effect = [
                CompletedProcess(args=[], returncode=0, stdout=IWCONFIG_OUTPUT_VALID),
                # ip link set up succeeds
                CompletedProcess(args=[], returncode=0, stdout=''),
                # nmcli disconnect succeeds
                CompletedProcess(args=[], returncode=0, stdout=''),
                # nmcli connect times out
                Exception("Timeout"),
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
        with patch('subprocess.run') as mock_run:
            # First call for __init__ succeeds
            mock_run.side_effect = [
                CompletedProcess(args=[], returncode=0, stdout=IWCONFIG_OUTPUT_VALID),
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

        assert len(networks1) == 3
        assert len(networks2) == 3
        assert networks1[0].ssid == networks2[0].ssid

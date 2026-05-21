"""Unit tests for WifiCard class."""

import pytest
from unittest.mock import patch
from subprocess import CompletedProcess
from vasili import WifiCard, WifiNetwork, _classify_nmcli_connect_error


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
        # Lease ownership belongs to WifiCardManager — connect must not touch in_use.
        assert card.in_use is False
        assert card._connected_network is network

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
        # connect() does not own the lease bit.
        assert card.in_use is False
        assert card._connection_password == 'mysecretpassword'

    def test_connect_failure(self, mock_subprocess_connect_fail):
        """Test connection failure handling."""
        card = WifiCard('wlan0')
        # Pretend the card was leased before the connect attempt.
        card.in_use = True
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
        # Failed connect must not surrender the caller's lease.
        assert card.in_use is True

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
        # disconnect() tears down the network state but does not release
        # the lease — that's WifiCardManager.return_card's job.
        assert card.in_use is True
        assert card._connected_network is None

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


@pytest.mark.unit
class TestClassifyNmcliConnectError:
    """Classifier should fail-fast on permanent errors and retry transient ones."""

    @pytest.mark.parametrize('stderr', [
        'Error: Connection activation failed: (7) Secrets were required, but not provided.',
        'Error: Connection activation failed: (7) Secrets were not provided.',
        'Error: 802.1X supplicant failed.',
    ])
    def test_auth_failures(self, stderr):
        assert _classify_nmcli_connect_error(stderr) == 'auth'

    @pytest.mark.parametrize('stderr', [
        "Error: No network with SSID 'Foo' found.",
        'Error: ssid was not found.',
    ])
    def test_ssid_not_found(self, stderr):
        assert _classify_nmcli_connect_error(stderr) == 'ssid_not_found'

    @pytest.mark.parametrize('stderr', [
        '',
        'Error: Connection activation failed: (4) The connection is not available.',
        'Error: Timeout expired.',
        'something unexpected',
    ])
    def test_transient_or_empty(self, stderr):
        assert _classify_nmcli_connect_error(stderr) == 'transient'


@pytest.mark.unit
class TestWifiCardConnectFailFast:
    """connect() must skip remaining retries on permanent failures."""

    @staticmethod
    def _isdir_mock():
        import os.path as real_ospath
        from tests.fixtures.mock_data import WIRELESS_INTERFACES

        def mock_isdir(path):
            if '/sys/class/net/' in path and '/wireless' in path:
                iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
                return iface in WIRELESS_INTERFACES
            return real_ospath.isdir(path)
        return mock_isdir

    def _run_connect_with_stderr(self, stderr_text):
        """Drive WifiCard.connect against an nmcli that always returns this stderr.

        Returns the number of times the `nmcli ... wifi connect` command was
        invoked, so we can tell fail-fast (1) from full-retry (3).
        """
        connect_call_count = {'n': 0}

        def fake_run(cmd, *args, **kwargs):
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else str(cmd)
            if 'wifi' in cmd_str and 'connect' in cmd_str:
                connect_call_count['n'] += 1
                return CompletedProcess(args=cmd, returncode=1, stdout='', stderr=stderr_text)
            # ip link set up / nmcli disconnect / anything else: succeed silently.
            return CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

        with (
            patch('os.path.isdir', side_effect=self._isdir_mock()),
            patch('subprocess.run', side_effect=fake_run),
            patch('time.sleep'),
        ):
            card = WifiCard('wlan0')
            card.in_use = True
            network = WifiNetwork(
                ssid='SecureHome',
                bssid='AA:BB:CC:DD:EE:FF',
                signal_strength=70,
                channel=11,
                encryption_type='WPA2',
                is_open=False,
            )
            result = card.connect(network, password='wrong', max_retries=3)
        return result, connect_call_count['n'], card

    def test_auth_failure_breaks_after_first_attempt(self):
        result, attempts, card = self._run_connect_with_stderr(
            'Error: Connection activation failed: (7) Secrets were required, but not provided.'
        )
        assert result is False
        assert attempts == 1  # fail-fast: no retries on bad password
        assert card.in_use is True  # lease still held

    def test_ssid_not_found_breaks_after_first_attempt(self):
        result, attempts, _ = self._run_connect_with_stderr(
            "Error: No network with SSID 'SecureHome' found."
        )
        assert result is False
        assert attempts == 1

    def test_transient_failure_retries_all_attempts(self):
        result, attempts, _ = self._run_connect_with_stderr(
            'Error: Connection activation failed: (4) The connection is not available.'
        )
        assert result is False
        assert attempts == 3  # full retry budget consumed

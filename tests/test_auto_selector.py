"""Unit tests for AutoSelector class"""

from unittest.mock import Mock, patch

from vasili import AutoSelector, ConnectionResult, WifiNetwork


class TestAutoSelectorInit:
    """Test AutoSelector initialization"""

    def test_init_with_defaults(self):
        """Test AutoSelector initialization with default parameters"""
        mock_wifi_manager = Mock()

        selector = AutoSelector(
            wifi_manager=mock_wifi_manager,
            evaluation_interval=30,
            min_score_improvement=10.0,
            initial_delay=10,
        )

        assert selector.wifi_manager == mock_wifi_manager
        assert selector.evaluation_interval == 30
        assert selector.min_score_improvement == 10.0
        assert selector.initial_delay == 10
        assert selector._enabled is False
        assert selector._running is False
        assert selector._selector_thread is None
        assert selector._evaluation_count == 0

    def test_init_with_custom_values(self):
        """Test AutoSelector initialization with custom parameters"""
        mock_wifi_manager = Mock()

        selector = AutoSelector(
            wifi_manager=mock_wifi_manager,
            evaluation_interval=60,
            min_score_improvement=15.0,
            initial_delay=20,
        )

        assert selector.evaluation_interval == 60
        assert selector.min_score_improvement == 15.0
        assert selector.initial_delay == 20


class TestAutoSelectorEnableDisable:
    """Test AutoSelector enable() and disable() methods"""

    @patch('vasili.emit_status_update')
    def test_enable(self, mock_emit):
        """Test enabling auto-selection"""
        mock_wifi_manager = Mock()
        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)

        selector.enable()

        assert selector._enabled is True
        mock_emit.assert_called_once()

    @patch('vasili.emit_status_update')
    def test_enable_already_enabled(self, mock_emit):
        """Test enabling when already enabled does not emit multiple times"""
        mock_wifi_manager = Mock()
        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)

        selector.enable()
        mock_emit.reset_mock()
        selector.enable()

        # Should not emit again
        mock_emit.assert_not_called()

    @patch('vasili.emit_status_update')
    def test_disable(self, mock_emit):
        """Test disabling auto-selection"""
        mock_wifi_manager = Mock()
        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)

        selector.enable()
        mock_emit.reset_mock()
        selector.disable()

        assert selector._enabled is False
        mock_emit.assert_called_once()

    @patch('vasili.emit_status_update')
    def test_disable_already_disabled(self, mock_emit):
        """Test disabling when already disabled does nothing"""
        mock_wifi_manager = Mock()
        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)

        selector.disable()

        # Should not emit when already disabled
        mock_emit.assert_not_called()

    def test_is_enabled(self):
        """Test is_enabled() returns correct state"""
        mock_wifi_manager = Mock()
        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)

        assert selector.is_enabled() is False

        selector.enable()
        assert selector.is_enabled() is True

        selector.disable()
        assert selector.is_enabled() is False


class TestAutoSelectorStartStop:
    """Test AutoSelector start() and stop() methods"""

    @patch('vasili.threading.Thread')
    def test_start(self, mock_thread_class):
        """Test starting the auto-selector"""
        mock_wifi_manager = Mock()
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)
        selector.start()

        assert selector._running is True
        assert selector._selector_thread == mock_thread
        mock_thread_class.assert_called_once()
        assert mock_thread.daemon is True
        mock_thread.start.assert_called_once()

    @patch('vasili.threading.Thread')
    def test_start_already_running(self, mock_thread_class):
        """Test that starting when already running does nothing"""
        mock_wifi_manager = Mock()
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)
        selector.start()
        mock_thread_class.reset_mock()

        # Try to start again
        selector.start()

        # Should not create a new thread
        mock_thread_class.assert_not_called()

    @patch('vasili.threading.Thread')
    def test_stop(self, mock_thread_class):
        """Test stopping the auto-selector"""
        mock_wifi_manager = Mock()
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 10)
        selector.start()
        selector.stop()

        assert selector._running is False
        mock_thread.join.assert_called_once()
        assert selector._selector_thread is None


class TestAutoSelectorEvaluation:
    """Test AutoSelector evaluation and switching logic"""

    @patch('vasili.emit_status_update')
    @patch('vasili.emit_connections_update')
    def test_select_best_connection_when_none_active(self, mock_emit_conn, mock_emit_status):
        """Test selecting best connection when no connection is active"""
        mock_wifi_manager = Mock()
        mock_wifi_manager.status = {}
        mock_wifi_manager.suitable_connections = []

        # Create mock connections
        network1 = WifiNetwork('TestNet1', 'AA:BB:CC:DD:EE:01', 70, 6, 'WPA2', False)
        network2 = WifiNetwork('TestNet2', 'AA:BB:CC:DD:EE:02', 90, 11, 'WPA2', False)

        conn1 = ConnectionResult(
            network=network1,
            download_speed=10.0,
            upload_speed=5.0,
            ping=50,
            connected=True,
            connection_method='wpa2',
            interface='wlan0',
        )

        conn2 = ConnectionResult(
            network=network2,
            download_speed=50.0,
            upload_speed=25.0,
            ping=20,
            connected=True,
            connection_method='wpa2',
            interface='wlan1',
        )

        mock_wifi_manager.suitable_connections = [conn1, conn2]
        mock_wifi_manager.get_sorted_connections.return_value = [conn2, conn1]
        mock_wifi_manager.use_connection.return_value = True

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 0)
        selector._select_best_connection()

        # Should select the best connection (conn2 at index 1)
        mock_wifi_manager.use_connection.assert_called_once_with(1)

    @patch('vasili.emit_status_update')
    @patch('vasili.emit_connections_update')
    def test_evaluate_and_switch_with_improvement(self, mock_emit_conn, mock_emit_status):
        """Test switching to better connection when improvement threshold is met"""
        mock_wifi_manager = Mock()

        # Create mock connections
        network1 = WifiNetwork('CurrentNet', 'AA:BB:CC:DD:EE:01', 70, 6, 'WPA2', False)
        network2 = WifiNetwork('BetterNet', 'AA:BB:CC:DD:EE:02', 90, 11, 'WPA2', False)

        current_conn = ConnectionResult(
            network=network1,
            download_speed=10.0,
            upload_speed=5.0,
            ping=50,
            connected=True,
            connection_method='wpa2',
            interface='wlan0',
        )

        better_conn = ConnectionResult(
            network=network2,
            download_speed=50.0,
            upload_speed=25.0,
            ping=20,
            connected=True,
            connection_method='wpa2',
            interface='wlan1',
        )

        mock_wifi_manager.status = {
            'current_bridge': {
                'ssid': 'CurrentNet',
                'wifi_interface': 'wlan0',
                'ethernet_interface': 'eth0',
            }
        }
        mock_wifi_manager.suitable_connections = [current_conn, better_conn]
        mock_wifi_manager.get_sorted_connections.return_value = [better_conn, current_conn]
        mock_wifi_manager.use_connection.return_value = True

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 0)
        selector.enable()
        selector._evaluate_and_switch()

        # Should switch to better connection
        mock_wifi_manager.use_connection.assert_called_once()

    def test_evaluate_no_switch_when_below_threshold(self):
        """Test no switch when improvement is below threshold"""
        mock_wifi_manager = Mock()

        # Create mock connections with minimal score difference
        network1 = WifiNetwork('CurrentNet', 'AA:BB:CC:DD:EE:01', 80, 6, 'WPA2', False)
        network2 = WifiNetwork('SlightlyBetterNet', 'AA:BB:CC:DD:EE:02', 82, 11, 'WPA2', False)

        current_conn = ConnectionResult(
            network=network1,
            download_speed=30.0,
            upload_speed=15.0,
            ping=30,
            connected=True,
            connection_method='wpa2',
            interface='wlan0',
        )

        better_conn = ConnectionResult(
            network=network2,
            download_speed=32.0,
            upload_speed=16.0,
            ping=28,
            connected=True,
            connection_method='wpa2',
            interface='wlan1',
        )

        mock_wifi_manager.status = {
            'current_bridge': {
                'ssid': 'CurrentNet',
                'wifi_interface': 'wlan0',
                'ethernet_interface': 'eth0',
            }
        }
        mock_wifi_manager.suitable_connections = [current_conn, better_conn]
        mock_wifi_manager.get_sorted_connections.return_value = [better_conn, current_conn]

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 0)
        selector.enable()
        selector._evaluate_and_switch()

        # Should NOT switch (improvement too small)
        mock_wifi_manager.use_connection.assert_not_called()

    def test_evaluate_disabled_selector_does_nothing(self):
        """Test that disabled selector doesn't evaluate or switch"""
        mock_wifi_manager = Mock()

        selector = AutoSelector(mock_wifi_manager, 30, 10.0, 0)
        # Don't enable
        selector._evaluate_and_switch()

        # Should not attempt any evaluation
        mock_wifi_manager.get_sorted_connections.assert_not_called()
        mock_wifi_manager.use_connection.assert_not_called()


class TestAutoSelectorStats:
    """Test AutoSelector statistics"""

    def test_get_stats(self):
        """Test get_stats returns correct information"""
        mock_wifi_manager = Mock()

        selector = AutoSelector(mock_wifi_manager, 60, 15.0, 20)
        selector.enable()
        selector._evaluation_count = 5
        selector._last_switch_time = 1234567890.0

        stats = selector.get_stats()

        assert stats['enabled'] is True
        assert stats['running'] is False
        assert stats['evaluation_count'] == 5
        assert stats['last_switch_time'] == 1234567890.0
        assert stats['evaluation_interval'] == 60
        assert stats['min_score_improvement'] == 15.0

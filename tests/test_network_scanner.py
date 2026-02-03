"""Unit tests for NetworkScanner class"""

from unittest.mock import Mock, patch


from vasili import NetworkScanner, WifiCard, WifiCardManager, WifiNetwork


class TestNetworkScannerInit:
    """Test NetworkScanner initialization"""

    def test_init(self):
        """Test NetworkScanner initialization"""
        mock_card_manager = Mock(spec=WifiCardManager)

        scanner = NetworkScanner(mock_card_manager)

        assert scanner.card_manager == mock_card_manager
        assert scanner.scan_results == []
        assert scanner.scanning is False
        assert scanner.scan_thread is None
        assert scanner.scan_queue is not None


class TestNetworkScannerStartStop:
    """Test NetworkScanner start_scan() and stop_scan() methods"""

    @patch('vasili.threading.Thread')
    def test_start_scan(self, mock_thread_class):
        """Test starting the scanner"""
        mock_card_manager = Mock(spec=WifiCardManager)
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread

        scanner = NetworkScanner(mock_card_manager)
        scanner.start_scan()

        assert scanner.scanning is True
        assert scanner.scan_thread == mock_thread
        mock_thread_class.assert_called_once()
        assert mock_thread.daemon is True
        mock_thread.start.assert_called_once()

    @patch('vasili.threading.Thread')
    def test_start_scan_already_scanning(self, mock_thread_class):
        """Test that starting scan while already scanning does nothing"""
        mock_card_manager = Mock(spec=WifiCardManager)
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread

        scanner = NetworkScanner(mock_card_manager)
        scanner.start_scan()

        # Start again
        scanner.start_scan()

        # Thread should only be created once
        mock_thread_class.assert_called_once()

    @patch('vasili.threading.Thread')
    def test_stop_scan(self, mock_thread_class):
        """Test stopping the scanner"""
        mock_card_manager = Mock(spec=WifiCardManager)
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread

        scanner = NetworkScanner(mock_card_manager)
        scanner.start_scan()
        scanner.stop_scan()

        assert scanner.scanning is False
        mock_thread.join.assert_called_once()
        assert scanner.scan_thread is None

    def test_stop_scan_not_running(self):
        """Test stopping when not running"""
        mock_card_manager = Mock(spec=WifiCardManager)

        scanner = NetworkScanner(mock_card_manager)
        scanner.stop_scan()  # Should not crash

        assert scanner.scanning is False


class TestNetworkScannerScanWorker:
    """Test NetworkScanner._scan_worker() method"""

    @patch('vasili.time.sleep')
    def test_scan_worker_successful_scan(self, mock_sleep):
        """Test scan worker performs scan and updates results"""
        mock_card_manager = Mock(spec=WifiCardManager)
        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wlan0'

        # Create test networks
        network1 = WifiNetwork(
            ssid='TestNet1',
            bssid='AA:BB:CC:DD:EE:F1',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )
        network2 = WifiNetwork(
            ssid='TestNet2',
            bssid='AA:BB:CC:DD:EE:F2',
            signal_strength=60,
            channel=11,
            encryption_type='WPA2',
            is_open=False,
        )

        mock_card.scan.return_value = [network1, network2]
        mock_card_manager.lease_card.return_value = mock_card

        scanner = NetworkScanner(mock_card_manager)
        scanner.scanning = True

        # Mock sleep to stop after one iteration
        def stop_scanning(*args):
            scanner.scanning = False

        mock_sleep.side_effect = stop_scanning

        scanner._scan_worker()

        # Verify card was leased and returned
        mock_card_manager.lease_card.assert_called()
        mock_card.scan.assert_called_once()
        mock_card_manager.return_card.assert_called_once_with(mock_card)

        # Verify results were updated
        assert scanner.scan_results == [network1, network2]

    @patch('vasili.time.sleep')
    def test_scan_worker_no_cards_available(self, mock_sleep):
        """Test scan worker handles no cards available"""
        mock_card_manager = Mock(spec=WifiCardManager)
        mock_card_manager.lease_card.return_value = None

        scanner = NetworkScanner(mock_card_manager)
        scanner.scanning = True

        # Mock sleep to stop after one iteration
        iteration_count = [0]

        def stop_after_one(*args):
            iteration_count[0] += 1
            if iteration_count[0] >= 2:
                scanner.scanning = False

        mock_sleep.side_effect = stop_after_one

        scanner._scan_worker()

        # Should have tried to lease a card
        mock_card_manager.lease_card.assert_called()

    @patch('vasili.time.sleep')
    def test_scan_worker_scan_exception(self, mock_sleep):
        """Test scan worker handles exceptions during scan"""
        mock_card_manager = Mock(spec=WifiCardManager)
        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wlan0'
        mock_card.scan.side_effect = Exception('Scan error')
        mock_card_manager.lease_card.return_value = mock_card

        scanner = NetworkScanner(mock_card_manager)
        scanner.scanning = True

        # Mock sleep to stop after one iteration
        iteration_count = [0]

        def stop_after_one(*args):
            iteration_count[0] += 1
            if iteration_count[0] >= 2:
                scanner.scanning = False

        mock_sleep.side_effect = stop_after_one

        # Should not crash
        scanner._scan_worker()

        # Card should still be returned
        mock_card_manager.return_card.assert_called_with(mock_card)


class TestNetworkScannerGetResults:
    """Test NetworkScanner.get_scan_results() and get_next_scan() methods"""

    def test_get_scan_results(self):
        """Test getting current scan results"""
        mock_card_manager = Mock(spec=WifiCardManager)

        scanner = NetworkScanner(mock_card_manager)

        network = WifiNetwork(
            ssid='TestNet',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        scanner.scan_results = [network]

        results = scanner.get_scan_results()

        assert results == [network]

    def test_get_next_scan(self):
        """Test blocking wait for next scan"""
        mock_card_manager = Mock(spec=WifiCardManager)

        scanner = NetworkScanner(mock_card_manager)

        network = WifiNetwork(
            ssid='TestNet',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # Put networks in the queue
        scanner.scan_queue.put([network])

        results = scanner.get_next_scan()

        assert results == [network]

    def test_get_scan_results_empty(self):
        """Test getting scan results when no scans have been performed"""
        mock_card_manager = Mock(spec=WifiCardManager)

        scanner = NetworkScanner(mock_card_manager)

        results = scanner.get_scan_results()

        assert results == []

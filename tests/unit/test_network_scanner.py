"""Unit tests for NetworkScanner class."""

import pytest
import time
from unittest.mock import patch
from vasili import NetworkScanner, WifiCardManager, WifiNetwork


@pytest.mark.unit
class TestNetworkScanner:
    """Test suite for NetworkScanner class."""

    def test_init(self, mock_subprocess, mock_netifaces):
        """Test NetworkScanner initialization."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        assert scanner.card_manager == manager
        assert scanner.scanning is False
        assert scanner.scan_thread is None
        assert len(scanner.scan_results) == 0

    def test_start_scan_starts_thread(self, mock_subprocess, mock_netifaces, mock_time_sleep):
        """Test that start_scan starts a background thread."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        assert scanner.scanning is True
        assert scanner.scan_thread is not None
        assert scanner.scan_thread.is_alive()

        # Clean up
        scanner.stop_scan()

    def test_start_scan_already_scanning(self, mock_subprocess, mock_netifaces, mock_time_sleep):
        """Test that start_scan is idempotent."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        first_thread = scanner.scan_thread

        # Try to start again
        scanner.start_scan()
        second_thread = scanner.scan_thread

        # Should be the same thread
        assert first_thread == second_thread

        # Clean up
        scanner.stop_scan()

    def test_stop_scan(self, mock_subprocess, mock_netifaces, mock_time_sleep):
        """Test that stop_scan stops the background thread."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        assert scanner.scanning is True

        scanner.stop_scan()
        assert scanner.scanning is False
        assert scanner.scan_thread is None

    def test_stop_scan_when_not_scanning(self, mock_subprocess, mock_netifaces):
        """Test that stop_scan is safe when not scanning."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Should not raise an error
        scanner.stop_scan()
        assert scanner.scanning is False

    def test_scan_worker_updates_results(self, mock_subprocess, mock_netifaces):
        """Test that scan worker updates scan_results."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Start scanning
        scanner.start_scan()

        # Wait for at least one scan to complete
        time.sleep(0.2)

        # Check that results were updated
        assert len(scanner.scan_results) > 0

        # Clean up
        scanner.stop_scan()

    def test_get_scan_results(self, mock_subprocess, mock_netifaces):
        """Test get_scan_results returns current results."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        time.sleep(0.2)

        results = scanner.get_scan_results()
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(net, WifiNetwork) for net in results)

        scanner.stop_scan()

    def test_get_next_scan_waits_for_scan(self, mock_subprocess, mock_netifaces):
        """Test get_next_scan waits for and returns scan results."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()

        # This should block until a scan completes
        networks = scanner.get_next_scan()
        assert isinstance(networks, list)
        assert len(networks) > 0

        scanner.stop_scan()

    def test_scan_worker_no_cards_available(
        self, mock_subprocess, mock_netifaces_no_wireless, mock_time_sleep
    ):
        """Test scan worker handles no cards available gracefully."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        time.sleep(0.1)

        # Should not crash, just have empty results
        assert len(scanner.scan_results) == 0

        scanner.stop_scan()

    def test_scan_worker_scanning_card_busy(self, mock_subprocess, mock_netifaces, mock_time_sleep):
        """Test scan worker when scanning card is in use."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Lease the dedicated scanning card
        scanning_card = manager.lease_card(for_scanning=True)
        assert scanning_card is not None

        scanner.start_scan()
        time.sleep(0.1)

        # Scanner should wait for scanning card
        # Results should be empty or stale
        scanner.stop_scan()

        # Return the scanning card
        manager.return_card(scanning_card)

    def test_scan_worker_handles_exceptions(self, mock_subprocess, mock_netifaces):
        """Test that scan worker handles exceptions and continues."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Patch the dedicated scanning card's scan method to raise an exception
        scanning_card = manager.get_scanning_card()
        with patch.object(scanning_card, 'scan', side_effect=Exception('Scan error')):
            scanner.start_scan()
            time.sleep(0.2)

            # Scanner should continue running despite error
            assert scanner.scanning is True

            scanner.stop_scan()

    def test_scan_worker_returns_scanning_card(self, mock_subprocess, mock_netifaces):
        """Test that scan worker returns scanning card after scanning."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        time.sleep(0.2)

        # Stop scanning to ensure card is returned
        scanner.stop_scan()

        # Scanning card should be available
        scanning_card = manager.get_scanning_card()
        assert scanning_card.in_use is False

    def test_multiple_scan_cycles(self, mock_subprocess, mock_netifaces):
        """Test that scanner can be started and stopped multiple times."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # First cycle
        scanner.start_scan()
        time.sleep(0.1)
        scanner.stop_scan()

        # Second cycle
        scanner.start_scan()
        time.sleep(0.1)
        scanner.stop_scan()

        # Should complete without errors
        assert scanner.scanning is False

    def test_scan_results_updated_continuously(self, mock_subprocess, mock_netifaces):
        """Test that scan results are updated on each scan cycle."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()

        # Get first scan
        results1 = scanner.get_next_scan()

        # Get second scan
        results2 = scanner.get_next_scan()

        # Both should have results
        assert len(results1) > 0
        assert len(results2) > 0

        scanner.stop_scan()

    def test_scan_queue_receives_results(self, mock_subprocess, mock_netifaces):
        """Test that scan_queue receives scan results."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()

        # Queue should have results after a scan
        networks = scanner.scan_queue.get(timeout=2)
        assert isinstance(networks, list)
        assert len(networks) > 0

        scanner.stop_scan()

    def test_uses_dedicated_scanning_card(self, mock_subprocess, mock_netifaces):
        """Test that scanner uses the dedicated scanning card."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Get the designated scanning card
        scanning_card = manager.get_scanning_card()
        assert scanning_card is not None

        scanner.start_scan()
        time.sleep(0.2)

        # Scanning card should be available after scan completes
        # (it's returned to pool between scans)
        scanner.stop_scan()

        # Verify scanning card can be leased again
        card = manager.lease_card(for_scanning=True)
        assert card is not None
        assert card == scanning_card

    def test_scanner_does_not_use_connection_cards(self, mock_subprocess, mock_netifaces):
        """Test that scanner uses dedicated card, not connection cards."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Lease all connection cards
        conn_cards = []
        while True:
            card = manager.lease_card(for_scanning=False)
            if card is None:
                break
            conn_cards.append(card)

        # Should have leased the connection card(s)
        assert len(conn_cards) > 0

        # Scanner should still work - uses dedicated scanning card
        scanner.start_scan()
        time.sleep(0.2)

        # Scanner should complete scans even with connection cards busy
        assert len(scanner.scan_results) > 0

        scanner.stop_scan()

        # Return cards
        for card in conn_cards:
            manager.return_card(card)

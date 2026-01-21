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

    def test_scan_worker_all_cards_busy(self, mock_subprocess, mock_netifaces, mock_time_sleep):
        """Test scan worker when all cards are in use."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Lease all cards
        card1 = manager.lease_card()
        card2 = manager.lease_card()

        scanner.start_scan()
        time.sleep(0.1)

        # Scanner should wait for cards
        # Results should be empty or stale
        scanner.stop_scan()

        # Return cards
        manager.return_card(card1)
        manager.return_card(card2)

    def test_scan_worker_handles_exceptions(self, mock_subprocess, mock_netifaces):
        """Test that scan worker handles exceptions and continues."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Patch scan to raise an exception
        with patch.object(manager.cards[0], 'scan', side_effect=Exception('Scan error')):
            scanner.start_scan()
            time.sleep(0.2)

            # Scanner should continue running despite error
            assert scanner.scanning is True

            scanner.stop_scan()

    def test_scan_worker_returns_card(self, mock_subprocess, mock_netifaces):
        """Test that scan worker returns card after scanning."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        scanner.start_scan()
        time.sleep(0.2)

        # Check that cards are not permanently in use
        available_cards = [card for card in manager.cards if not card.in_use]
        # At least one card should be available (scanner returns them)
        assert len(available_cards) >= 1

        scanner.stop_scan()

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

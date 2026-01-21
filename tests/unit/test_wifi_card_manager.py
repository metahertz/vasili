"""Unit tests for WifiCardManager class."""

import pytest
import threading
from unittest.mock import patch
from vasili import WifiCardManager, WifiCard


@pytest.mark.unit
class TestWifiCardManager:
    """Test suite for WifiCardManager class."""

    def test_init_scans_for_cards(self, mock_subprocess, mock_netifaces):
        """Test that WifiCardManager initializes and scans for cards."""
        manager = WifiCardManager()
        assert len(manager.cards) == 2  # wlan0 and wlan1 from SAMPLE_INTERFACES

    def test_scan_for_cards_finds_wireless_only(self, mock_subprocess, mock_netifaces):
        """Test that scan_for_cards only finds wireless interfaces."""
        manager = WifiCardManager()
        manager.scan_for_cards()

        # Should find wlan0 and wlan1, but not lo or eth0
        assert len(manager.cards) == 2
        assert all(card.interface.startswith('wlan') for card in manager.cards)

    def test_scan_for_cards_no_wireless(self, mock_subprocess, mock_netifaces_no_wireless):
        """Test scan_for_cards when no wireless interfaces exist."""
        manager = WifiCardManager()
        assert len(manager.cards) == 0

    def test_lease_card_returns_available(self, mock_subprocess, mock_netifaces):
        """Test that lease_card returns an available card."""
        manager = WifiCardManager()

        card = manager.lease_card()
        assert card is not None
        assert card.in_use is True
        assert card in manager.cards

    def test_lease_card_marks_in_use(self, mock_subprocess, mock_netifaces):
        """Test that leased cards are marked as in use."""
        manager = WifiCardManager()

        card1 = manager.lease_card()
        card2 = manager.lease_card()

        assert card1 is not None
        assert card2 is not None
        assert card1 != card2
        assert card1.in_use is True
        assert card2.in_use is True

    def test_lease_card_returns_none_when_all_busy(self, mock_subprocess, mock_netifaces):
        """Test that lease_card returns None when all cards are in use."""
        manager = WifiCardManager()

        # Lease all cards
        card1 = manager.lease_card()
        card2 = manager.lease_card()
        card3 = manager.lease_card()  # Should be None

        assert card1 is not None
        assert card2 is not None
        assert card3 is None

    def test_return_card_marks_available(self, mock_subprocess, mock_netifaces):
        """Test that return_card marks card as available."""
        manager = WifiCardManager()

        card = manager.lease_card()
        assert card.in_use is True

        manager.return_card(card)
        assert card.in_use is False

    def test_return_card_allows_re_lease(self, mock_subprocess, mock_netifaces):
        """Test that returned cards can be leased again."""
        manager = WifiCardManager()

        card1 = manager.lease_card()
        card2 = manager.lease_card()
        card3 = manager.lease_card()  # None, all busy

        assert card3 is None

        # Return a card
        manager.return_card(card1)

        # Should be able to lease again
        card4 = manager.lease_card()
        assert card4 is not None
        assert card4 == card1

    def test_return_card_not_in_pool(self, mock_subprocess, mock_netifaces):
        """Test return_card with card not in the pool."""
        manager = WifiCardManager()

        # Create a card that's not in the manager
        with patch('subprocess.run'):
            external_card = WifiCard('wlan99')
            external_card.in_use = True

        # Should not raise an error
        manager.return_card(external_card)
        # Card should still be in_use since it's not in the pool
        assert external_card.in_use is True

    def test_get_all_cards(self, mock_subprocess, mock_netifaces):
        """Test get_all_cards returns all cards."""
        manager = WifiCardManager()

        cards = manager.get_all_cards()
        assert len(cards) == 2
        assert all(isinstance(card, WifiCard) for card in cards)

    def test_thread_safety_lease_return(self, mock_subprocess, mock_netifaces):
        """Test thread safety of lease/return operations."""
        manager = WifiCardManager()
        results = []
        errors = []

        def lease_and_return():
            try:
                card = manager.lease_card()
                if card:
                    results.append(card)
                    # Simulate some work
                    import time

                    time.sleep(0.01)
                    manager.return_card(card)
            except Exception as e:
                errors.append(e)

        # Create multiple threads
        threads = [threading.Thread(target=lease_and_return) for _ in range(10)]

        # Start all threads
        for t in threads:
            t.start()

        # Wait for all to complete
        for t in threads:
            t.join()

        # Should have no errors
        assert len(errors) == 0
        # All cards should be available again
        assert all(not card.in_use for card in manager.cards)

    def test_scan_for_cards_handles_init_failure(self, mock_netifaces):
        """Test that scan_for_cards handles WifiCard initialization failures."""
        with patch('subprocess.run') as mock_run:
            # Make all iwconfig calls fail
            mock_run.side_effect = Exception("Interface error")

            manager = WifiCardManager()
            # Should have no cards due to init failures
            assert len(manager.cards) == 0

    def test_rescan_clears_previous_cards(self, mock_subprocess, mock_netifaces):
        """Test that scan_for_cards clears previously found cards."""
        manager = WifiCardManager()
        initial_count = len(manager.cards)

        # Lease a card
        card = manager.lease_card()
        assert card is not None

        # Rescan
        manager.scan_for_cards()

        # Should have same number of cards (fresh scan)
        assert len(manager.cards) == initial_count
        # Previously leased card reference is no longer valid
        # (new card objects are created)

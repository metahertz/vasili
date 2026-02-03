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
        # Should be a connection card, not the scanning card
        assert card != manager._scanning_card

    def test_lease_card_marks_in_use(self, mock_subprocess, mock_netifaces):
        """Test that leased cards are marked as in use."""
        manager = WifiCardManager()

        # Lease both scanning and connection cards
        scan_card = manager.lease_card(for_scanning=True)
        conn_card = manager.lease_card(for_scanning=False)

        assert scan_card is not None
        assert conn_card is not None
        assert scan_card != conn_card
        assert scan_card.in_use is True
        assert conn_card.in_use is True

    def test_lease_card_returns_none_when_all_busy(self, mock_subprocess, mock_netifaces):
        """Test that lease_card returns None when all cards are in use."""
        manager = WifiCardManager()

        # Lease scanning card
        scan_card = manager.lease_card(for_scanning=True)
        # Lease all connection cards (there's only one with 2 total cards)
        conn_card = manager.lease_card(for_scanning=False)
        # Next should be None
        card3 = manager.lease_card(for_scanning=False)

        assert scan_card is not None
        assert conn_card is not None
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

        # Lease the connection card (only 1 available with 2 total cards)
        conn_card = manager.lease_card(for_scanning=False)
        card2 = manager.lease_card(for_scanning=False)  # Should be None, all busy

        assert conn_card is not None
        assert card2 is None

        # Return the card
        manager.return_card(conn_card)

        # Should be able to lease again
        card3 = manager.lease_card(for_scanning=False)
        assert card3 is not None
        assert card3 == conn_card

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
            mock_run.side_effect = Exception('Interface error')

            manager = WifiCardManager()
            # Should have no cards due to init failures
            assert len(manager.cards) == 0

    def test_rescan_clears_previous_cards(self, mock_subprocess, mock_netifaces):
        """Test that scan_for_cards clears previously found cards."""
        manager = WifiCardManager()
        initial_count = len(manager.cards)

        # Lease a card (connection card, not scanning)
        card = manager.lease_card(for_scanning=False)
        assert card is not None

        # Rescan
        manager.scan_for_cards()

        # Should have same number of cards (fresh scan)
        assert len(manager.cards) == initial_count
        # Previously leased card reference is no longer valid
        # (new card objects are created)

    # ========== Multi-card orchestration tests ==========

    def test_scanning_card_designated_on_init(self, mock_subprocess, mock_netifaces):
        """Test that a scanning card is designated when manager initializes."""
        manager = WifiCardManager()
        assert manager._scanning_card is not None
        assert manager._scanning_card in manager.cards

    def test_scanning_card_uses_first_card_by_default(self, mock_subprocess, mock_netifaces):
        """Test that the first card is used as scanning card when no config."""
        manager = WifiCardManager()
        assert manager._scanning_card == manager.cards[0]

    def test_get_scanning_card(self, mock_subprocess, mock_netifaces):
        """Test get_scanning_card returns the designated scanning card."""
        manager = WifiCardManager()
        scanning_card = manager.get_scanning_card()
        assert scanning_card is not None
        assert scanning_card == manager._scanning_card

    def test_get_connection_cards_excludes_scanning_card(self, mock_subprocess, mock_netifaces):
        """Test get_connection_cards returns all cards except scanning card."""
        manager = WifiCardManager()
        connection_cards = manager.get_connection_cards()

        # Should have one less than total (scanning card excluded)
        assert len(connection_cards) == len(manager.cards) - 1
        assert manager._scanning_card not in connection_cards

    def test_lease_card_for_scanning_returns_scanning_card(self, mock_subprocess, mock_netifaces):
        """Test lease_card(for_scanning=True) returns the scanning card."""
        manager = WifiCardManager()
        card = manager.lease_card(for_scanning=True)

        assert card is not None
        assert card == manager._scanning_card
        assert card.in_use is True

    def test_lease_card_for_scanning_returns_none_when_busy(self, mock_subprocess, mock_netifaces):
        """Test lease_card(for_scanning=True) returns None when scanning card busy."""
        manager = WifiCardManager()

        # Lease scanning card first
        card1 = manager.lease_card(for_scanning=True)
        assert card1 is not None

        # Try to lease again
        card2 = manager.lease_card(for_scanning=True)
        assert card2 is None

    def test_lease_card_for_connection_skips_scanning_card(self, mock_subprocess, mock_netifaces):
        """Test lease_card(for_scanning=False) never returns the scanning card."""
        manager = WifiCardManager()

        # Lease all connection cards
        leased = []
        while True:
            card = manager.lease_card(for_scanning=False)
            if card is None:
                break
            leased.append(card)

        # Should have leased all cards except scanning card
        assert len(leased) == len(manager.cards) - 1
        assert manager._scanning_card not in leased

    def test_lease_card_default_is_for_connection(self, mock_subprocess, mock_netifaces):
        """Test lease_card() defaults to connection card behavior."""
        manager = WifiCardManager()

        # Default call should not return scanning card
        card = manager.lease_card()
        assert card is not None
        assert card != manager._scanning_card

    def test_get_status_includes_scanning_info(self, mock_subprocess, mock_netifaces):
        """Test get_status includes scanning card information."""
        manager = WifiCardManager()
        status = manager.get_status()

        assert 'scanning_card' in status
        assert 'connection_cards' in status
        assert status['scanning_card'] == manager._scanning_card.interface
        assert len(status['connection_cards']) == len(manager.cards) - 1

    def test_return_scanning_card_makes_available(self, mock_subprocess, mock_netifaces):
        """Test returning scanning card makes it available again."""
        manager = WifiCardManager()

        # Lease and return scanning card
        card = manager.lease_card(for_scanning=True)
        assert card.in_use is True

        manager.return_card(card)
        assert card.in_use is False

        # Can lease again
        card2 = manager.lease_card(for_scanning=True)
        assert card2 is not None
        assert card2 == card

    def test_single_card_works_for_both(self, mock_subprocess):
        """Test behavior with only one card - it serves as both scanning and connection."""
        with patch('netifaces.interfaces') as mock_ifaces:
            mock_ifaces.return_value = ['lo', 'wlan0']  # Only one wireless
            manager = WifiCardManager()

        assert len(manager.cards) == 1
        assert manager._scanning_card == manager.cards[0]
        assert len(manager.get_connection_cards()) == 0

        # Can lease for scanning
        card = manager.lease_card(for_scanning=True)
        assert card is not None

        # Cannot lease for connection (it's the scanning card)
        manager.return_card(card)
        conn_card = manager.lease_card(for_scanning=False)
        assert conn_card is None

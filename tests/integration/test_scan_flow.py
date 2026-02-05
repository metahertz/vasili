"""Integration tests for WiFi scanning flow."""

import pytest
from vasili import WifiCardManager


@pytest.mark.integration
class TestScanFlow:
    """Test suite for complete scanning workflow."""

    def test_complete_scan_flow(self, all_mocks):
        """Test complete scan: WifiCardManager → WifiCard.scan() → WifiNetwork[]."""
        # Initialize card manager
        manager = WifiCardManager()
        assert len(manager.cards) == 2

        # Lease a card
        card = manager.lease_card()
        assert card is not None

        # Perform scan
        networks = card.scan()
        assert len(networks) == 4

        # Verify network data
        assert networks[0].ssid == 'OpenCafe'
        assert networks[0].is_open is True
        assert networks[1].ssid == 'SecureHome'
        assert networks[1].encryption_type == 'WPA2'
        assert networks[3].ssid == 'ModernWiFi'
        assert networks[3].encryption_type == 'WPA3'

        # Return card
        manager.return_card(card)
        assert card.in_use is False

    def test_multiple_cards_scanning_simultaneously(self, all_mocks):
        """Test multiple cards scanning at the same time."""
        manager = WifiCardManager()

        # Lease both scanning and connection cards
        # With multi-card orchestration, one card is dedicated to scanning
        scan_card = manager.lease_card(for_scanning=True)
        conn_card = manager.lease_card(for_scanning=False)

        assert scan_card is not None
        assert conn_card is not None
        assert scan_card != conn_card

        # Both can scan simultaneously
        networks1 = scan_card.scan()
        networks2 = conn_card.scan()

        # Both should get results
        assert len(networks1) == 4
        assert len(networks2) == 4

        # Return cards
        manager.return_card(scan_card)
        manager.return_card(conn_card)

    def test_scan_with_card_lifecycle(self, all_mocks):
        """Test complete card lifecycle: discover → lease → scan → return."""
        # Step 1: Discover cards
        manager = WifiCardManager()
        initial_cards = manager.get_all_cards()
        assert len(initial_cards) == 2

        # Step 2: Lease card
        card = manager.lease_card()
        assert card.in_use is True

        # Step 3: Scan for networks
        networks = card.scan()
        assert len(networks) > 0

        # Step 4: Return card
        manager.return_card(card)
        assert card.in_use is False

        # Card can be leased again
        card2 = manager.lease_card()
        assert card2 is not None

    def test_parse_real_world_iwlist_output(self, all_mocks):
        """Test parsing of realistic iwlist scan output."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()

        # Verify all expected fields are parsed
        for network in networks:
            assert network.ssid != ''
            assert network.bssid != ''
            assert network.channel > 0
            assert 0 <= network.signal_strength <= 200
            assert isinstance(network.is_open, bool)

        manager.return_card(card)

    def test_scan_empty_results(self, mock_subprocess_scan_empty, mock_netifaces):
        """Test scan when no networks are found."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()
        assert len(networks) == 0

        manager.return_card(card)

    def test_repeated_scans_update_results(self, all_mocks):
        """Test that repeated scans return updated results."""
        manager = WifiCardManager()
        card = manager.lease_card()

        # First scan
        networks1 = card.scan()
        assert len(networks1) == 4

        # Second scan
        networks2 = card.scan()
        assert len(networks2) == 4

        # Should get same networks (mock returns same data)
        assert networks1[0].ssid == networks2[0].ssid

        manager.return_card(card)

    def test_scan_sorts_by_signal_strength(self, all_mocks):
        """Test that networks can be sorted by signal strength."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()

        # Sort by signal strength (descending)
        sorted_networks = sorted(networks, key=lambda n: n.signal_strength, reverse=True)

        # Strongest signal should be first
        assert sorted_networks[0].signal_strength >= sorted_networks[1].signal_strength

        manager.return_card(card)

    def test_scan_filters_open_networks(self, all_mocks):
        """Test filtering for open networks only."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()
        open_networks = [n for n in networks if n.is_open]

        assert len(open_networks) == 1
        assert open_networks[0].ssid == 'OpenCafe'

        manager.return_card(card)

    def test_scan_filters_encrypted_networks(self, all_mocks):
        """Test filtering for encrypted networks."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()
        encrypted_networks = [n for n in networks if not n.is_open]

        assert len(encrypted_networks) == 3
        assert 'SecureHome' in [n.ssid for n in encrypted_networks]
        assert 'WeakSignal' in [n.ssid for n in encrypted_networks]
        assert 'ModernWiFi' in [n.ssid for n in encrypted_networks]

        manager.return_card(card)

    def test_scan_identifies_wpa2_networks(self, all_mocks):
        """Test identification of WPA2 encrypted networks."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()
        wpa2_networks = [n for n in networks if n.encryption_type == 'WPA2']

        assert len(wpa2_networks) == 1
        assert wpa2_networks[0].ssid == 'SecureHome'

        manager.return_card(card)

    def test_scan_identifies_wpa3_networks(self, all_mocks):
        """Test identification of WPA3 encrypted networks."""
        manager = WifiCardManager()
        card = manager.lease_card()

        networks = card.scan()
        wpa3_networks = [n for n in networks if n.encryption_type == 'WPA3']

        assert len(wpa3_networks) == 1
        assert wpa3_networks[0].ssid == 'ModernWiFi'

        manager.return_card(card)

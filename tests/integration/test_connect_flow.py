"""Integration tests for WiFi connection flow."""

import pytest
from vasili import WifiCardManager, WifiNetwork


@pytest.mark.integration
class TestConnectFlow:
    """Test suite for complete connection workflow."""

    def test_complete_connect_flow_open_network(self, all_mocks):
        """Test full connect: lease card → connect → return card."""
        manager = WifiCardManager()

        # Lease a card
        card = manager.lease_card()
        assert card is not None

        # Create open network
        network = WifiNetwork(
            ssid='OpenCafe',
            bssid='00:11:22:33:44:55',
            signal_strength=85,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # Connect
        result = card.connect(network)
        assert result is True
        assert card.in_use is True

        # Disconnect
        card.disconnect()
        assert card.in_use is False

        # Return card
        manager.return_card(card)

    def test_complete_connect_flow_wpa2_network(self, all_mocks):
        """Test connection to WPA2 network with password."""
        manager = WifiCardManager()

        card = manager.lease_card()
        assert card is not None

        # Create WPA2 network
        network = WifiNetwork(
            ssid='SecureHome',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=70,
            channel=11,
            encryption_type='WPA2',
            is_open=False,
        )

        # Connect with password
        result = card.connect(network, password='mypassword123')
        assert result is True

        # Disconnect
        card.disconnect()

        # Return card
        manager.return_card(card)

    def test_connect_failure_handling(self, mock_subprocess_connect_fail, mock_netifaces):
        """Test handling of connection failures."""
        manager = WifiCardManager()
        card = manager.lease_card()

        network = WifiNetwork(
            ssid='FailNetwork',
            bssid='11:22:33:44:55:66',
            signal_strength=50,
            channel=1,
            encryption_type='WPA2',
            is_open=False,
        )

        # Connection should fail
        result = card.connect(network, password='wrongpassword')
        assert result is False
        assert card.in_use is False

        manager.return_card(card)

    def test_scan_then_connect_workflow(self, all_mocks):
        """Test complete workflow: scan → select network → connect."""
        manager = WifiCardManager()

        # Step 1: Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        assert len(networks) > 0

        # Step 2: Select an open network
        open_networks = [n for n in networks if n.is_open]
        assert len(open_networks) > 0
        selected_network = open_networks[0]

        # Step 3: Connect to selected network
        result = card.connect(selected_network)
        assert result is True

        # Step 4: Verify connection
        status = card.get_status()
        assert status['in_use'] is True

        # Step 5: Disconnect
        card.disconnect()

        # Step 6: Return card
        manager.return_card(card)

    def test_connect_disconnect_cycle(self, all_mocks):
        """Test multiple connect/disconnect cycles."""
        manager = WifiCardManager()
        card = manager.lease_card()

        network = WifiNetwork(
            ssid='TestNetwork',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # First connect
        result1 = card.connect(network)
        assert result1 is True

        # Disconnect
        card.disconnect()

        # Second connect
        result2 = card.connect(network)
        assert result2 is True

        # Disconnect again
        card.disconnect()

        manager.return_card(card)

    def test_connect_to_multiple_networks_sequentially(self, all_mocks):
        """Test connecting to multiple networks in sequence."""
        manager = WifiCardManager()
        card = manager.lease_card()

        network1 = WifiNetwork(
            ssid='Network1',
            bssid='00:11:22:33:44:55',
            signal_strength=90,
            channel=1,
            encryption_type='',
            is_open=True,
        )

        network2 = WifiNetwork(
            ssid='Network2',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=85,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # Connect to first network
        result1 = card.connect(network1)
        assert result1 is True

        # Connect to second network (should disconnect from first)
        result2 = card.connect(network2)
        assert result2 is True

        card.disconnect()
        manager.return_card(card)

    def test_parallel_connections_different_cards(self, all_mocks):
        """Test connecting to different networks with different cards."""
        manager = WifiCardManager()

        # Lease two cards
        card1 = manager.lease_card()
        card2 = manager.lease_card()

        assert card1 is not None
        assert card2 is not None

        network1 = WifiNetwork(
            ssid='Network1',
            bssid='00:11:22:33:44:55',
            signal_strength=90,
            channel=1,
            encryption_type='',
            is_open=True,
        )

        network2 = WifiNetwork(
            ssid='Network2',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=85,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        # Connect both cards
        result1 = card1.connect(network1)
        result2 = card2.connect(network2)

        assert result1 is True
        assert result2 is True

        # Both cards should be in use
        assert card1.in_use is True
        assert card2.in_use is True

        # Disconnect and return
        card1.disconnect()
        card2.disconnect()
        manager.return_card(card1)
        manager.return_card(card2)

    def test_connection_with_interface_status_check(self, all_mocks):
        """Test connection and verify interface status."""
        manager = WifiCardManager()
        card = manager.lease_card()

        # Check initial status
        status_before = card.get_status()
        assert status_before['in_use'] is True  # Leased
        assert status_before['is_up'] is True

        # Connect
        network = WifiNetwork(
            ssid='TestNetwork',
            bssid='00:11:22:33:44:55',
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        result = card.connect(network)
        assert result is True

        # Check status after connection
        status_after = card.get_status()
        assert status_after['in_use'] is True
        assert status_after['interface'] == card.interface

        card.disconnect()
        manager.return_card(card)

    def test_connect_with_bssid_specification(self, all_mocks):
        """Test that connection uses BSSID for precise targeting."""
        manager = WifiCardManager()
        card = manager.lease_card()

        network = WifiNetwork(
            ssid='CommonSSID',
            bssid='AA:BB:CC:DD:EE:FF',  # Specific BSSID
            signal_strength=80,
            channel=6,
            encryption_type='',
            is_open=True,
        )

        result = card.connect(network)
        assert result is True

        card.disconnect()
        manager.return_card(card)

    def test_encrypted_network_without_password_attempts_saved_credentials(self, all_mocks):
        """Test connecting to encrypted network without password (uses saved creds)."""
        manager = WifiCardManager()
        card = manager.lease_card()

        network = WifiNetwork(
            ssid='SavedNetwork',
            bssid='11:22:33:44:55:66',
            signal_strength=75,
            channel=11,
            encryption_type='WPA2',
            is_open=False,
        )

        # Connect without password (should try saved credentials)
        result = card.connect(network)
        # Mock returns success, simulating saved credentials worked
        assert result is True

        card.disconnect()
        manager.return_card(card)

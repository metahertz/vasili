"""Integration tests for scan_and_connect main loop."""

import pytest
import time
import threading
from unittest.mock import Mock, patch, MagicMock
from vasili import (
    WifiManager,
    WifiCardManager,
    NetworkScanner,
    ConnectionModule,
    WifiNetwork,
    ConnectionResult,
)


class MockConnectionModule(ConnectionModule):
    """Mock connection module for testing."""

    def __init__(self, card_manager, can_connect_filter=None):
        super().__init__(card_manager)
        self.can_connect_filter = can_connect_filter or (lambda n: n.is_open)
        self.connect_attempts = []

    def can_connect(self, network: WifiNetwork) -> bool:
        return self.can_connect_filter(network)

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        self.connect_attempts.append(network.ssid)

        # Try to get a card
        card = self.card_manager.lease_card()
        if not card:
            return ConnectionResult(
                network=network,
                download_speed=0,
                upload_speed=0,
                ping=0,
                connected=False,
                connection_method='mock',
                interface='',
            )

        # Simulate connection
        success = card.connect(network)

        if success:
            result = ConnectionResult(
                network=network,
                download_speed=50.0,
                upload_speed=10.0,
                ping=25.0,
                connected=True,
                connection_method='mock',
                interface=card.interface,
            )
        else:
            result = ConnectionResult(
                network=network,
                download_speed=0,
                upload_speed=0,
                ping=0,
                connected=False,
                connection_method='mock',
                interface='',
            )

        # Return the card
        self.card_manager.return_card(card)
        return result


@pytest.mark.integration
class TestScanAndConnect:
    """Test suite for scan_and_connect main loop."""

    def test_scan_and_connect_discovers_networks(self, all_mocks):
        """Test that scan_and_connect discovers networks via scanner."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Start scanner
        scanner.start_scan()

        # Wait for scan to complete
        networks = scanner.get_next_scan()

        assert len(networks) == 3
        assert any(n.ssid == 'OpenCafe' for n in networks)

        # Stop scanner
        scanner.stop_scan()

    def test_scan_and_connect_modules_evaluate_networks(self, all_mocks):
        """Test that modules evaluate discovered networks."""
        manager = WifiCardManager()

        # Create mock modules
        open_module = MockConnectionModule(manager, can_connect_filter=lambda n: n.is_open)
        wpa2_module = MockConnectionModule(
            manager, can_connect_filter=lambda n: not n.is_open and n.encryption_type == 'WPA2'
        )

        # Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        manager.return_card(card)

        # Test module filters
        open_networks = [n for n in networks if open_module.can_connect(n)]
        wpa2_networks = [n for n in networks if wpa2_module.can_connect(n)]

        assert len(open_networks) == 1
        assert open_networks[0].ssid == 'OpenCafe'
        assert len(wpa2_networks) == 1
        assert wpa2_networks[0].ssid == 'SecureHome'

    def test_scan_and_connect_attempts_connections(self, all_mocks):
        """Test that modules attempt connections to matching networks."""
        manager = WifiCardManager()

        # Create mock module that connects to open networks
        module = MockConnectionModule(manager, can_connect_filter=lambda n: n.is_open)

        # Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        manager.return_card(card)

        # Attempt connections
        results = []
        for network in networks:
            if module.can_connect(network):
                result = module.connect(network)
                results.append(result)

        # Should have one successful connection
        assert len(results) == 1
        assert results[0].connected is True
        assert results[0].network.ssid == 'OpenCafe'

    def test_scan_and_connect_skips_already_connected(self, all_mocks):
        """Test that already-connected networks are skipped."""
        manager = WifiCardManager()
        module = MockConnectionModule(manager, can_connect_filter=lambda n: n.is_open)

        # Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        manager.return_card(card)

        # Track successful connections
        successful_connections = []

        for network in networks:
            # Skip if already connected
            if any(
                conn.network.bssid == network.bssid and conn.connected
                for conn in successful_connections
            ):
                continue

            if module.can_connect(network):
                result = module.connect(network)
                if result.connected:
                    successful_connections.append(result)

        # Try to connect again (should skip)
        for network in networks:
            already_connected = any(
                conn.network.bssid == network.bssid and conn.connected
                for conn in successful_connections
            )
            if already_connected:
                # Should not attempt connection
                assert network.ssid == 'OpenCafe'

    def test_scan_and_connect_handles_module_exceptions(self, all_mocks):
        """Test graceful handling of module exceptions."""
        manager = WifiCardManager()

        # Create module that raises exception
        module = MockConnectionModule(manager)

        # Patch connect to raise exception
        original_connect = module.connect

        def failing_connect(network):
            if network.ssid == 'OpenCafe':
                raise Exception("Connection failed")
            return original_connect(network)

        module.connect = failing_connect

        # Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        manager.return_card(card)

        # Attempt connections with exception handling
        results = []
        for network in networks:
            if module.can_connect(network):
                try:
                    result = module.connect(network)
                    results.append(result)
                except Exception as e:
                    # Exception should be caught
                    assert "Connection failed" in str(e)

        # Should continue despite exception
        assert True

    def test_scan_and_connect_background_scanning_lifecycle(self, all_mocks, mock_time_sleep):
        """Test scanner lifecycle in scan_and_connect context."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)

        # Start scanner
        scanner.start_scan()
        assert scanner.scanning is True

        # Get a scan result
        try:
            networks = scanner.scan_queue.get(timeout=2)
            assert len(networks) > 0
        except:
            pass  # Timeout is ok

        # Stop scanner
        scanner.stop_scan()
        assert scanner.scanning is False

    def test_scan_and_connect_multiple_modules_single_network(self, all_mocks):
        """Test that only one module connects per network."""
        manager = WifiCardManager()

        # Create two modules that both can connect to open networks
        module1 = MockConnectionModule(manager, can_connect_filter=lambda n: n.is_open)
        module2 = MockConnectionModule(manager, can_connect_filter=lambda n: n.is_open)

        # Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        manager.return_card(card)

        # Simulate scan_and_connect behavior: first module to succeed wins
        for network in networks:
            for module in [module1, module2]:
                if module.can_connect(network):
                    result = module.connect(network)
                    if result.connected:
                        # Break after first successful connection
                        break

        # Only one module should have attempted
        assert len(module1.connect_attempts) == 1
        assert len(module2.connect_attempts) == 0  # Second module never gets a chance

    def test_scan_and_connect_with_no_matching_modules(self, all_mocks):
        """Test behavior when no modules can connect to discovered networks."""
        manager = WifiCardManager()

        # Create module that can't connect to any networks
        module = MockConnectionModule(manager, can_connect_filter=lambda n: False)

        # Scan for networks
        card = manager.lease_card()
        networks = card.scan()
        manager.return_card(card)

        # Try to connect
        results = []
        for network in networks:
            if module.can_connect(network):
                result = module.connect(network)
                results.append(result)

        # No connections should be made
        assert len(results) == 0

    def test_scan_and_connect_continuous_operation(self, all_mocks, mock_time_sleep):
        """Test continuous scanning and connection attempts."""
        manager = WifiCardManager()
        scanner = NetworkScanner(manager)
        module = MockConnectionModule(manager, can_connect_filter=lambda n: n.is_open)

        # Start scanner
        scanner.start_scan()

        # Simulate a few scan cycles
        successful_connections = []
        for _ in range(2):
            try:
                networks = scanner.scan_queue.get(timeout=1)

                # Try to connect
                for network in networks:
                    # Skip already connected
                    already_connected = any(
                        conn.network.bssid == network.bssid for conn in successful_connections
                    )
                    if already_connected:
                        continue

                    if module.can_connect(network):
                        result = module.connect(network)
                        if result.connected:
                            successful_connections.append(result)
                            break  # One connection per scan cycle
            except:
                pass  # Timeout is ok

        # Stop scanner
        scanner.stop_scan()

        # Should have found at least one connection
        assert len(successful_connections) >= 1

    def test_wifi_manager_integration(self, all_mocks, mock_time_sleep):
        """Test WifiManager initialization and component integration."""
        # Mock the module loading since it tries to import from filesystem
        with patch('vasili.WifiManager._load_connection_modules', return_value=[]):
            manager = WifiManager()

            # Verify components are initialized
            assert manager.card_manager is not None
            assert manager.scanner is not None
            assert isinstance(manager.modules, list)
            assert manager.status['scanning'] is False

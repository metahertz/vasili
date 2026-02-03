"""
Integration tests for multi-card orchestration.

Tests the coordination between scanning and connection cards to ensure
proper role assignment and no interference.
"""

import pytest
from unittest.mock import Mock, patch
import sys
from unittest.mock import MagicMock

# Mock iptc at module level before importing vasili
sys.modules['iptc'] = MagicMock()

from config import VasiliConfig, InterfaceConfig, MongoDBConfig
from vasili import WifiCardManager


@pytest.fixture
def test_config():
    """Create a test configuration."""
    config = VasiliConfig()
    config.interfaces = InterfaceConfig(preferred=[])
    config.mongodb = MongoDBConfig(enabled=False)
    return config


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_role_assignment_with_multiple_cards(
    mock_get_config, mock_interfaces, mock_subprocess, test_config
):
    """Test that roles are correctly assigned when multiple cards are present."""
    # Setup mocks
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0', 'wlan1', 'wlan2']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # Verify we have 3 cards
    assert len(card_manager.cards) == 3

    # Verify scanning card is assigned
    scanning_card = card_manager.get_scanning_card()
    assert scanning_card is not None
    assert scanning_card.interface == 'wlan0'

    # Verify connection cards
    connection_cards = card_manager.get_connection_cards()
    assert len(connection_cards) == 2
    assert 'wlan1' in [c.interface for c in connection_cards]
    assert 'wlan2' in [c.interface for c in connection_cards]


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_scanning_card_not_leased_for_connections(
    mock_get_config, mock_interfaces, mock_subprocess, test_config
):
    """Test that the scanning card cannot be leased for connections."""
    # Setup mocks
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0', 'wlan1']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # Try to lease a connection card
    connection_card = card_manager.lease_card(for_scanning=False)
    assert connection_card is not None
    assert connection_card.interface == 'wlan1'
    assert connection_card.interface != 'wlan0'

    # Scanning card (wlan0) should not be leased for connections
    # Even if wlan1 is in use, wlan0 should not be returned
    another_card = card_manager.lease_card(for_scanning=False)
    assert another_card is None


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_scanning_card_can_only_be_leased_for_scanning(
    mock_get_config, mock_interfaces, mock_subprocess, test_config
):
    """Test that the scanning card can only be leased for scanning."""
    # Setup mocks
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0', 'wlan1']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # Lease scanning card
    scanning_card = card_manager.lease_card(for_scanning=True)
    assert scanning_card is not None
    assert scanning_card.interface == 'wlan0'
    assert scanning_card.in_use is True

    # Try to lease scanning card again while in use
    another_scan_card = card_manager.lease_card(for_scanning=True)
    assert another_scan_card is None

    # Return the card
    card_manager.return_card(scanning_card)
    assert scanning_card.in_use is False

    # Should be able to lease it again
    scanning_card_again = card_manager.lease_card(for_scanning=True)
    assert scanning_card_again is not None
    assert scanning_card_again.interface == 'wlan0'


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_connection_cards_available_while_scanning(
    mock_get_config, mock_interfaces, mock_subprocess, test_config
):
    """Test that connection cards can be leased while scanning is active."""
    # Setup mocks
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0', 'wlan1', 'wlan2']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # Lease scanning card
    scanning_card = card_manager.lease_card(for_scanning=True)
    assert scanning_card is not None
    assert scanning_card.interface == 'wlan0'

    # Should still be able to lease connection cards
    connection_card1 = card_manager.lease_card(for_scanning=False)
    assert connection_card1 is not None
    assert connection_card1.interface in ['wlan1', 'wlan2']

    connection_card2 = card_manager.lease_card(for_scanning=False)
    assert connection_card2 is not None
    assert connection_card2.interface in ['wlan1', 'wlan2']
    assert connection_card1.interface != connection_card2.interface

    # No more connection cards available
    connection_card3 = card_manager.lease_card(for_scanning=False)
    assert connection_card3 is None


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_single_card_can_only_scan(mock_get_config, mock_interfaces, mock_subprocess, test_config):
    """Test that with a single card, it's dedicated to scanning."""
    # Setup mocks
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # Should have 1 card
    assert len(card_manager.cards) == 1

    # It should be the scanning card
    scanning_card = card_manager.get_scanning_card()
    assert scanning_card is not None
    assert scanning_card.interface == 'wlan0'

    # Should have no connection cards
    connection_cards = card_manager.get_connection_cards()
    assert len(connection_cards) == 0

    # Cannot lease for connection
    connection_card = card_manager.lease_card(for_scanning=False)
    assert connection_card is None

    # Can lease for scanning
    scan_card = card_manager.lease_card(for_scanning=True)
    assert scan_card is not None
    assert scan_card.interface == 'wlan0'


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_preferred_scan_interface(mock_get_config, mock_interfaces, mock_subprocess, test_config):
    """Test that preferred scan interface is respected."""
    # Setup mocks with preferred scan interface
    test_config.interfaces.scan_interface = 'wlan2'
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0', 'wlan1', 'wlan2']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # wlan2 should be the scanning card
    scanning_card = card_manager.get_scanning_card()
    assert scanning_card is not None
    assert scanning_card.interface == 'wlan2'

    # wlan0 and wlan1 should be connection cards
    connection_cards = card_manager.get_connection_cards()
    assert len(connection_cards) == 2
    interfaces = [c.interface for c in connection_cards]
    assert 'wlan0' in interfaces
    assert 'wlan1' in interfaces


@patch('vasili.subprocess.run')
@patch('vasili.netifaces.interfaces')
@patch('vasili.get_config')
def test_card_state_manager_integration(
    mock_get_config, mock_interfaces, mock_subprocess, test_config
):
    """Test that card state manager properly tracks card states."""
    # Setup mocks
    mock_get_config.return_value = test_config
    mock_interfaces.return_value = ['wlan0', 'wlan1']
    mock_subprocess.return_value = Mock(returncode=0)

    # Create card manager
    card_manager = WifiCardManager()

    # Get status - should include card state manager info
    status = card_manager.get_status()
    assert 'card_state_manager' in status
    assert status['card_state_manager']['total_cards'] == 2
    assert status['card_state_manager']['scanning_interface'] == 'wlan0'
    assert status['card_state_manager']['connection_cards'] == 1

    # Lease a connection card
    connection_card = card_manager.lease_card(for_scanning=False)

    # Status should reflect card in use
    status = card_manager.get_status()
    assert status['card_state_manager']['cards_in_use'] == 1

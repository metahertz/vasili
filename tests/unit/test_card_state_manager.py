"""
Unit tests for CardStateManager.
"""

import time
import unittest

from card_state_manager import (
    CardInfo,
    CardRole,
    CardState,
    CardStateManager,
)
from config import VasiliConfig, MongoDBConfig


class TestCardStateManager(unittest.TestCase):
    """Test CardStateManager functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a test config with MongoDB disabled for unit tests
        self.config = VasiliConfig()
        self.config.mongodb = MongoDBConfig(enabled=False)

    def test_initialization_without_mongodb(self):
        """Test that CardStateManager works without MongoDB."""
        manager = CardStateManager(self.config)
        self.assertFalse(manager.mongodb_enabled)
        self.assertIsNotNone(manager._memory_store)

    def test_role_assignment_single_card(self):
        """Test role assignment with a single WiFi card."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0']

        roles = manager.assign_roles(interfaces)

        self.assertEqual(len(roles), 1)
        self.assertEqual(roles['wlan0'], CardRole.SCANNING)
        self.assertEqual(manager.get_scanning_interface(), 'wlan0')

    def test_role_assignment_multiple_cards(self):
        """Test role assignment with multiple WiFi cards."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1', 'wlan2']

        roles = manager.assign_roles(interfaces)

        self.assertEqual(len(roles), 3)
        # First card should be scanning
        self.assertEqual(roles['wlan0'], CardRole.SCANNING)
        # Others should be connection cards
        self.assertEqual(roles['wlan1'], CardRole.CONNECTION)
        self.assertEqual(roles['wlan2'], CardRole.CONNECTION)

        # Verify scanning interface
        self.assertEqual(manager.get_scanning_interface(), 'wlan0')

        # Verify connection interfaces
        connection_ifaces = manager.get_connection_interfaces()
        self.assertEqual(len(connection_ifaces), 2)
        self.assertIn('wlan1', connection_ifaces)
        self.assertIn('wlan2', connection_ifaces)

    def test_role_assignment_with_preferred_scan_interface(self):
        """Test role assignment when scan_interface is specified in config."""
        self.config.interfaces.scan_interface = 'wlan1'
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1', 'wlan2']

        roles = manager.assign_roles(interfaces)

        # wlan1 should be the scanning card (as configured)
        self.assertEqual(roles['wlan1'], CardRole.SCANNING)
        self.assertEqual(manager.get_scanning_interface(), 'wlan1')

        # Others should be connection cards
        self.assertEqual(roles['wlan0'], CardRole.CONNECTION)
        self.assertEqual(roles['wlan2'], CardRole.CONNECTION)

    def test_update_card_state(self):
        """Test updating card state."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1']
        manager.assign_roles(interfaces)

        # Update state
        manager.update_card_state(
            'wlan1', CardState.CONNECTED, in_use=True, connected_ssid='TestNetwork'
        )

        # Verify update
        card_info = manager.get_card_info('wlan1')
        self.assertIsNotNone(card_info)
        self.assertEqual(card_info.state, CardState.CONNECTED)
        self.assertTrue(card_info.in_use)
        self.assertEqual(card_info.connected_ssid, 'TestNetwork')

    def test_get_available_connection_card(self):
        """Test getting an available connection card."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1', 'wlan2']
        manager.assign_roles(interfaces)

        # Initially, both wlan1 and wlan2 should be available
        available = manager.get_available_connection_card()
        self.assertIn(available, ['wlan1', 'wlan2'])

        # Mark wlan1 as in use
        manager.update_card_state('wlan1', CardState.CONNECTING, in_use=True)

        # Now only wlan2 should be available
        available = manager.get_available_connection_card()
        self.assertEqual(available, 'wlan2')

        # Mark wlan2 as in use too
        manager.update_card_state('wlan2', CardState.CONNECTING, in_use=True)

        # No connection cards available
        available = manager.get_available_connection_card()
        self.assertIsNone(available)

    def test_is_scanning_card(self):
        """Test checking if an interface is the scanning card."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1']
        manager.assign_roles(interfaces)

        self.assertTrue(manager.is_scanning_card('wlan0'))
        self.assertFalse(manager.is_scanning_card('wlan1'))

    def test_get_all_cards(self):
        """Test retrieving all card information."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1', 'wlan2']
        manager.assign_roles(interfaces)

        all_cards = manager.get_all_cards()
        self.assertEqual(len(all_cards), 3)

        # Verify each card has correct info
        interface_names = [card.interface for card in all_cards]
        self.assertIn('wlan0', interface_names)
        self.assertIn('wlan1', interface_names)
        self.assertIn('wlan2', interface_names)

    def test_get_status(self):
        """Test getting status information."""
        manager = CardStateManager(self.config)
        interfaces = ['wlan0', 'wlan1', 'wlan2']
        manager.assign_roles(interfaces)

        status = manager.get_status()

        self.assertEqual(status['total_cards'], 3)
        self.assertEqual(status['scanning_interface'], 'wlan0')
        self.assertEqual(status['connection_cards'], 2)
        self.assertEqual(status['cards_in_use'], 0)
        self.assertFalse(status['mongodb_enabled'])

        # Mark one card as in use
        manager.update_card_state('wlan1', CardState.CONNECTING, in_use=True)

        status = manager.get_status()
        self.assertEqual(status['cards_in_use'], 1)

    def test_card_info_to_dict(self):
        """Test CardInfo serialization to dictionary."""
        card_info = CardInfo(
            interface='wlan0',
            role=CardRole.SCANNING,
            state=CardState.SCANNING,
            in_use=True,
            connected_ssid=None,
            last_updated=time.time(),
            error_message=None,
        )

        data = card_info.to_dict()

        self.assertEqual(data['interface'], 'wlan0')
        self.assertEqual(data['role'], 'scanning')
        self.assertEqual(data['state'], 'scanning')
        self.assertTrue(data['in_use'])

    def test_card_info_from_dict(self):
        """Test CardInfo deserialization from dictionary."""
        data = {
            'interface': 'wlan1',
            'role': 'connection',
            'state': 'connected',
            'in_use': False,
            'connected_ssid': 'TestNet',
            'last_updated': time.time(),
            'error_message': None,
        }

        card_info = CardInfo.from_dict(data)

        self.assertEqual(card_info.interface, 'wlan1')
        self.assertEqual(card_info.role, CardRole.CONNECTION)
        self.assertEqual(card_info.state, CardState.CONNECTED)
        self.assertFalse(card_info.in_use)
        self.assertEqual(card_info.connected_ssid, 'TestNet')

    def test_empty_interfaces_list(self):
        """Test behavior with empty interfaces list."""
        manager = CardStateManager(self.config)
        roles = manager.assign_roles([])

        self.assertEqual(len(roles), 0)
        self.assertIsNone(manager.get_scanning_interface())


if __name__ == '__main__':
    unittest.main()

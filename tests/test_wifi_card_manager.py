"""Unit tests for WifiCardManager class"""

import os.path as real_ospath
from unittest.mock import Mock, patch

from vasili import WifiCard, WifiCardManager


def _mock_isdir_wireless(*wireless_ifaces):
    """Create an os.path.isdir mock that treats given interfaces as wireless."""
    wireless = set(wireless_ifaces)

    def side_effect(path):
        if '/sys/class/net/' in path and '/wireless' in path:
            iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
            return iface in wireless
        return real_ospath.isdir(path)

    return side_effect


class TestWifiCardManagerInit:
    """Test WifiCardManager initialization"""

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0', 'wlan1'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_init_with_wifi_cards(self, mock_wifi_card_class, mock_interfaces, _):
        """Test initialization with available wifi cards"""
        mock_interfaces.return_value = ['lo', 'eth0', 'wlan0', 'wlan1']

        # Mock WifiCard instances
        mock_card1 = Mock(spec=WifiCard)
        mock_card1.interface = 'wlan0'
        mock_card2 = Mock(spec=WifiCard)
        mock_card2.interface = 'wlan1'
        mock_wifi_card_class.side_effect = [mock_card1, mock_card2]

        manager = WifiCardManager()

        assert len(manager.cards) == 2
        assert mock_wifi_card_class.call_count == 2
        mock_wifi_card_class.assert_any_call('wlan0')
        mock_wifi_card_class.assert_any_call('wlan1')

    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_init_no_wifi_cards(self, mock_wifi_card_class, mock_interfaces):
        """Test initialization with no wifi cards"""
        mock_interfaces.return_value = ['lo', 'eth0']

        with patch('os.path.isdir', return_value=False):
            manager = WifiCardManager()

        assert len(manager.cards) == 0
        mock_wifi_card_class.assert_not_called()

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wifi0'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_init_with_wifi_interface(self, mock_wifi_card_class, mock_interfaces, _):
        """Test initialization with 'wifi' prefixed interface"""
        mock_interfaces.return_value = ['lo', 'wifi0']

        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wifi0'
        mock_wifi_card_class.return_value = mock_card

        manager = WifiCardManager()

        assert len(manager.cards) == 1
        mock_wifi_card_class.assert_called_once_with('wifi0')

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0', 'wlan1', 'wlan2'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_init_card_initialization_failure(self, mock_wifi_card_class, mock_interfaces, _):
        """Test that failed card initialization is skipped"""
        mock_interfaces.return_value = ['wlan0', 'wlan1', 'wlan2']

        # First card succeeds, second fails, third succeeds
        mock_card1 = Mock(spec=WifiCard)
        mock_card1.interface = 'wlan0'
        mock_card2 = Mock(spec=WifiCard)
        mock_card2.interface = 'wlan2'
        mock_wifi_card_class.side_effect = [
            mock_card1,
            ValueError('Not a valid wireless device'),
            mock_card2,
        ]

        manager = WifiCardManager()

        # Only 2 cards should be added (the ones that succeeded)
        assert len(manager.cards) == 2


class TestWifiCardManagerScanForCards:
    """Test WifiCardManager.scan_for_cards() method"""

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0', 'wlan1', 'wlan2'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_scan_for_cards_clears_existing(self, mock_wifi_card_class, mock_interfaces, _):
        """Test that scan_for_cards clears existing cards"""
        mock_interfaces.return_value = ['wlan0']
        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wlan0'
        mock_wifi_card_class.return_value = mock_card

        manager = WifiCardManager()
        assert len(manager.cards) == 1

        # Change the interfaces returned
        mock_interfaces.return_value = ['wlan1', 'wlan2']
        mock_card.interface = 'wlan1'  # Update for new scan
        manager.scan_for_cards()

        # Should have 2 new cards, not 3
        assert len(manager.cards) == 2


class TestWifiCardManagerLeaseCard:
    """Test WifiCardManager.lease_card() method"""

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0', 'wlan1'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_lease_card_success(self, mock_wifi_card_class, mock_interfaces, _):
        """Test successfully leasing an available card"""
        mock_interfaces.return_value = ['wlan0', 'wlan1']

        mock_card1 = Mock(spec=WifiCard)
        mock_card1.interface = 'wlan0'
        mock_card1.in_use = False
        mock_card2 = Mock(spec=WifiCard)
        mock_card2.interface = 'wlan1'
        mock_card2.in_use = False
        mock_wifi_card_class.side_effect = [mock_card1, mock_card2]

        manager = WifiCardManager()

        leased_card = manager.lease_card()

        # With multi-card orchestration, lease_card() defaults to connection cards
        # mock_card1 is the scanning card (first card), so mock_card2 is returned
        assert leased_card == mock_card2
        assert leased_card.in_use is True

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_lease_card_all_in_use(self, mock_wifi_card_class, mock_interfaces, _):
        """Test leasing when all cards are in use"""
        mock_interfaces.return_value = ['wlan0']

        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wlan0'
        mock_card.in_use = True
        mock_wifi_card_class.return_value = mock_card

        manager = WifiCardManager()

        leased_card = manager.lease_card()

        assert leased_card is None

    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_lease_card_no_cards_available(self, mock_wifi_card_class, mock_interfaces):
        """Test leasing when no cards exist"""
        mock_interfaces.return_value = []

        with patch('os.path.isdir', return_value=False):
            manager = WifiCardManager()

        leased_card = manager.lease_card()

        assert leased_card is None

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0', 'wlan1', 'wlan2'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_lease_card_skips_in_use(self, mock_wifi_card_class, mock_interfaces, _):
        """Test that lease_card skips cards already in use"""
        mock_interfaces.return_value = ['wlan0', 'wlan1', 'wlan2']

        mock_card1 = Mock(spec=WifiCard)
        mock_card1.interface = 'wlan0'
        mock_card1.in_use = True
        mock_card2 = Mock(spec=WifiCard)
        mock_card2.interface = 'wlan1'
        mock_card2.in_use = True
        mock_card3 = Mock(spec=WifiCard)
        mock_card3.interface = 'wlan2'
        mock_card3.in_use = False
        mock_wifi_card_class.side_effect = [mock_card1, mock_card2, mock_card3]

        manager = WifiCardManager()

        leased_card = manager.lease_card()

        assert leased_card == mock_card3
        assert leased_card.in_use is True


class TestWifiCardManagerReturnCard:
    """Test WifiCardManager.return_card() method"""

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_return_card_success(self, mock_wifi_card_class, mock_interfaces, _):
        """Test successfully returning a card"""
        mock_interfaces.return_value = ['wlan0']

        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wlan0'
        mock_card.in_use = True
        mock_wifi_card_class.return_value = mock_card

        manager = WifiCardManager()
        manager.return_card(mock_card)

        assert mock_card.in_use is False

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_return_card_not_in_manager(self, mock_wifi_card_class, mock_interfaces, _):
        """Test returning a card not in the manager's pool"""
        mock_interfaces.return_value = ['wlan0']

        mock_card = Mock(spec=WifiCard)
        mock_card.interface = 'wlan0'
        mock_wifi_card_class.return_value = mock_card

        manager = WifiCardManager()

        # Create a different card not in the manager
        other_card = Mock(spec=WifiCard)
        other_card.interface = 'wlan1'
        other_card.in_use = True

        # Should not crash, just do nothing
        manager.return_card(other_card)

        assert other_card.in_use is True


class TestWifiCardManagerGetAllCards:
    """Test WifiCardManager.get_all_cards() method"""

    @patch('os.path.isdir', side_effect=_mock_isdir_wireless('wlan0', 'wlan1'))
    @patch('vasili.netifaces.interfaces')
    @patch('vasili.WifiCard')
    def test_get_all_cards(self, mock_wifi_card_class, mock_interfaces, _):
        """Test getting all cards"""
        mock_interfaces.return_value = ['wlan0', 'wlan1']

        mock_card1 = Mock(spec=WifiCard)
        mock_card1.interface = 'wlan0'
        mock_card2 = Mock(spec=WifiCard)
        mock_card2.interface = 'wlan1'
        mock_wifi_card_class.side_effect = [mock_card1, mock_card2]

        manager = WifiCardManager()
        all_cards = manager.get_all_cards()

        assert len(all_cards) == 2
        assert mock_card1 in all_cards
        assert mock_card2 in all_cards

"""Pytest configuration and shared fixtures."""

import pytest
from unittest.mock import patch, MagicMock
from tests.fixtures.mock_helpers import (
    SubprocessMockFactory,
    FileIOMockFactory,
    SpeedtestMockFactory,
)
from tests.fixtures.mock_data import SAMPLE_INTERFACES, WIRELESS_INTERFACES


def _mock_isdir_for_wireless(original_isdir):
    """Create an os.path.isdir mock that recognizes wireless interfaces."""

    def side_effect(path):
        if '/sys/class/net/' in path and '/wireless' in path:
            iface = path.split('/sys/class/net/')[1].split('/wireless')[0]
            return iface in WIRELESS_INTERFACES
        return original_isdir(path)

    return side_effect


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run with smart command handling."""
    import os.path

    side_effect = SubprocessMockFactory.create_mock()
    with (
        patch('subprocess.run', side_effect=side_effect) as mock,
        patch('os.path.isdir', side_effect=_mock_isdir_for_wireless(os.path.isdir)),
        patch('time.sleep'),
    ):
        yield mock


@pytest.fixture
def mock_subprocess_scan_empty():
    """Mock subprocess.run with empty scan results."""
    import os.path

    side_effect = SubprocessMockFactory.create_mock(scan_output='')
    with (
        patch('subprocess.run', side_effect=side_effect) as mock,
        patch('os.path.isdir', side_effect=_mock_isdir_for_wireless(os.path.isdir)),
        patch('time.sleep'),
    ):
        yield mock


@pytest.fixture
def mock_subprocess_connect_fail():
    """Mock subprocess.run with connection failures."""
    import os.path

    side_effect = SubprocessMockFactory.create_mock(connect_success=False)
    with (
        patch('subprocess.run', side_effect=side_effect) as mock,
        patch('os.path.isdir', side_effect=_mock_isdir_for_wireless(os.path.isdir)),
        patch('time.sleep'),
    ):
        yield mock


@pytest.fixture
def mock_netifaces():
    """Mock netifaces.interfaces() to return sample interfaces."""
    with patch('netifaces.interfaces') as mock:
        mock.return_value = SAMPLE_INTERFACES
        yield mock


@pytest.fixture
def mock_netifaces_no_wireless():
    """Mock netifaces.interfaces() with no wireless interfaces."""
    with patch('netifaces.interfaces') as mock:
        mock.return_value = ['lo', 'eth0']
        yield mock


@pytest.fixture
def mock_speedtest():
    """Mock speedtest.Speedtest class."""
    with patch('speedtest.Speedtest') as mock_class:
        mock_class.return_value = SpeedtestMockFactory.create_mock()
        yield mock_class


@pytest.fixture
def mock_file_io():
    """Mock file I/O for /sys and /proc."""
    side_effect = FileIOMockFactory.create_mock_open()
    with patch('builtins.open', side_effect=side_effect):
        yield


@pytest.fixture
def mock_file_io_interface_down():
    """Mock file I/O with interfaces down."""
    side_effect = FileIOMockFactory.create_mock_open(interface_state='down')
    with patch('builtins.open', side_effect=side_effect):
        yield


@pytest.fixture
def all_mocks(mock_subprocess, mock_netifaces, mock_speedtest, mock_file_io):
    """Combine all common mocks into a single fixture."""
    return {
        'subprocess': mock_subprocess,
        'netifaces': mock_netifaces,
        'speedtest': mock_speedtest,
    }


@pytest.fixture
def mock_iptc():
    """Mock iptc library used by NetworkBridge."""
    with (
        patch('iptc.Chain') as mock_chain,
        patch('iptc.Rule') as mock_rule,
        patch('iptc.Target') as mock_target,
        patch('iptc.Table') as mock_table,
    ):
        yield {
            'chain': mock_chain,
            'rule': mock_rule,
            'target': mock_target,
            'table': mock_table,
        }


@pytest.fixture
def mock_time_sleep():
    """Mock time.sleep to speed up tests."""
    with patch('time.sleep') as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_mongodb():
    """Mock MongoDB connections so tests don't require a live database.

    CardLeaseStore: mocked to always succeed (permissive fallback).
    ConnectionStore: mocked with in-memory dict storage.
    PerformanceMetricsStore: mocked to report unavailable.
    """
    mock_lease_store = MagicMock()
    mock_lease_store.is_available.return_value = True
    mock_lease_store.acquire.return_value = True
    mock_lease_store.release.return_value = True
    mock_lease_store.release_all.return_value = 0
    mock_lease_store.get_all_leases.return_value = []
    mock_lease_store.clear_all.return_value = None

    with patch('vasili.CardLeaseStore', return_value=mock_lease_store):
        yield mock_lease_store


@pytest.fixture(autouse=True)
def mock_network_isolation():
    """Mock network_isolation so tests don't manipulate real routing tables."""
    with (
        patch('vasili.network_isolation') as mock_ni,
        patch('network_isolation.subprocess'),
    ):
        mock_ni.setup_interface_routing.return_value = {
            'ip': '192.168.1.100',
            'gateway': '192.168.1.1',
            'table': 100,
            'interface': 'wlan0',
        }
        mock_ni.teardown_interface_routing.return_value = None
        mock_ni.verify_connectivity.return_value = True
        mock_ni.get_interface_ip.return_value = '192.168.1.100'
        mock_ni.get_interface_gateway.return_value = '192.168.1.1'
        yield mock_ni


@pytest.fixture(autouse=True)
def mock_mac_manager():
    """Mock MacManager so tests don't change real MAC addresses."""
    mock_mm = MagicMock()
    mock_mm.get_mac_for_network.return_value = '02:aa:bb:cc:dd:ee'
    mock_mm.get_current_mac.return_value = '00:11:22:33:44:55'
    mock_mm.set_mac.return_value = True

    with patch('vasili.MacManager', return_value=mock_mm):
        yield mock_mm

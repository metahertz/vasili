"""Pytest configuration and shared fixtures."""

import pytest
from unittest.mock import patch
from tests.fixtures.mock_helpers import (
    SubprocessMockFactory,
    FileIOMockFactory,
    SpeedtestMockFactory,
)
from tests.fixtures.mock_data import SAMPLE_INTERFACES


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run with smart command handling."""
    side_effect = SubprocessMockFactory.create_mock()
    with patch('subprocess.run', side_effect=side_effect) as mock:
        yield mock


@pytest.fixture
def mock_subprocess_scan_empty():
    """Mock subprocess.run with empty scan results."""
    side_effect = SubprocessMockFactory.create_mock(scan_output='wlan0     Scan completed :\n')
    with patch('subprocess.run', side_effect=side_effect) as mock:
        yield mock


@pytest.fixture
def mock_subprocess_connect_fail():
    """Mock subprocess.run with connection failures."""
    side_effect = SubprocessMockFactory.create_mock(connect_success=False)
    with patch('subprocess.run', side_effect=side_effect) as mock:
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

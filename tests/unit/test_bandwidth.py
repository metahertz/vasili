"""Unit tests for BandwidthMonitor."""

import pytest
from unittest.mock import patch, MagicMock

from bandwidth import BandwidthMonitor


@pytest.fixture
def monitor():
    """Create a BandwidthMonitor with mocked MongoDB."""
    mock_client = MagicMock()
    mock_client.admin.command.return_value = {'ok': 1}
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_client.__getitem__ = MagicMock(return_value=mock_db)
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)

    # Make find/aggregate return empty results by default
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.__iter__ = MagicMock(return_value=iter([]))
    mock_collection.find.return_value = mock_cursor
    mock_collection.aggregate.return_value = []

    with patch('bandwidth.MongoClient', return_value=mock_client):
        m = BandwidthMonitor(sample_interval=1)
    yield m
    m.stop()


@pytest.mark.unit
class TestBandwidthMonitor:
    """Test suite for BandwidthMonitor."""

    def test_read_bytes(self):
        """Test reading bytes from a real interface (lo always exists)."""
        result = BandwidthMonitor._read_bytes('lo')
        assert result is not None
        rx, tx = result
        assert rx >= 0
        assert tx >= 0

    def test_read_bytes_invalid_interface(self):
        """Test reading bytes from non-existent interface."""
        result = BandwidthMonitor._read_bytes('nonexistent99')
        assert result is None

    def test_sample_records_data(self, monitor):
        """Test that sampling records data."""
        monitor._sample()
        # Should have entries for wireless interfaces (may be 0 if none exist in test)
        history = monitor.get_history(hours=1)
        assert isinstance(history, list)

    def test_get_current_rates(self, monitor):
        """Test getting current rates."""
        rates = monitor.get_current_rates()
        assert isinstance(rates, dict)

    def test_get_total_usage(self, monitor):
        """Test getting total usage."""
        total = monitor.get_total_usage(hours=1)
        assert 'rx_bytes' in total
        assert 'tx_bytes' in total
        assert 'hours' in total

    def test_start_stop(self, monitor):
        """Test starting and stopping the monitor."""
        monitor.start()
        assert monitor._running is True
        assert monitor._thread is not None

        monitor.stop()
        assert monitor._running is False

    def test_double_start(self, monitor):
        """Test that starting twice doesn't create duplicate threads."""
        monitor.start()
        thread1 = monitor._thread
        monitor.start()
        assert monitor._thread == thread1
        monitor.stop()

    def test_graceful_degradation(self):
        """Test that BandwidthMonitor works without MongoDB."""
        with patch('bandwidth.MongoClient') as mock_client:
            from pymongo.errors import ConnectionFailure
            mock_client.side_effect = ConnectionFailure('no mongo')
            m = BandwidthMonitor(sample_interval=1)

        assert m.is_available() is False
        assert m.get_history() == []
        assert m.get_total_usage() == {'rx_bytes': 0, 'tx_bytes': 0, 'hours': 24}

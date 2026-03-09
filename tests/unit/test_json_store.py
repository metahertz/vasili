"""Tests for JSON file-based storage (json_store.py)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock


# Minimal dataclass stubs matching vasili's WifiNetwork and ConnectionResult
@dataclass
class WifiNetwork:
    ssid: str
    bssid: str
    signal_strength: int
    channel: int
    encryption_type: str
    is_open: bool


@dataclass
class ConnectionResult:
    network: WifiNetwork
    download_speed: float
    upload_speed: float
    ping: float
    connected: bool
    connection_method: str
    interface: str

    def calculate_score(self) -> float:
        download_score = min(100, (self.download_speed / 100.0) * 100)
        signal_score = self.network.signal_strength
        upload_score = min(100, (self.upload_speed / 50.0) * 100)
        ping_score = max(0, 100 - (self.ping / 2.0))
        total_score = (
            download_score * 0.4 + signal_score * 0.3 + upload_score * 0.2 + ping_score * 0.1
        )
        return round(total_score, 2)


from json_store import JsonHistoryStore, JsonMetricsStore, JsonPortalStore


class TestJsonMetricsStore(unittest.TestCase):
    """Tests for JsonMetricsStore."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = JsonMetricsStore(data_dir=self.test_dir, max_records=100)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _make_connection(self, ssid='TestNet', connected=True, download=50.0):
        network = WifiNetwork(
            ssid=ssid,
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=75,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )
        return ConnectionResult(
            network=network,
            download_speed=download,
            upload_speed=25.0,
            ping=20.0,
            connected=connected,
            connection_method='wpa2',
            interface='wlan0',
        )

    def test_initialization_success(self):
        """Test successful initialization."""
        self.assertTrue(self.store.is_available())

    def test_initialization_failure(self):
        """Test initialization with invalid directory."""
        store = JsonMetricsStore(data_dir='/nonexistent/readonly/path')
        self.assertFalse(store.is_available())

    def test_store_metrics(self):
        """Test storing connection metrics."""
        conn = self._make_connection()
        result = self.store.store_metrics(conn)
        self.assertTrue(result)

        # Verify data was written
        filepath = os.path.join(self.test_dir, 'connection_metrics.json')
        self.assertTrue(os.path.exists(filepath))
        with open(filepath) as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['ssid'], 'TestNet')
        self.assertEqual(data[0]['download_speed'], 50.0)

    def test_store_metrics_when_unavailable(self):
        """Test storing metrics when store is unavailable."""
        self.store._available = False
        conn = self._make_connection()
        result = self.store.store_metrics(conn)
        self.assertFalse(result)

    def test_get_network_history(self):
        """Test retrieving network history."""
        # Store multiple metrics
        for i in range(5):
            conn = self._make_connection(download=10.0 + i * 10)
            self.store.store_metrics(conn)

        history = self.store.get_network_history('TestNet', limit=3)
        self.assertEqual(len(history), 3)
        # Should be sorted newest first (highest timestamp)
        self.assertGreaterEqual(history[0]['timestamp'], history[1]['timestamp'])

    def test_get_network_history_empty(self):
        """Test retrieving history for non-existent network."""
        history = self.store.get_network_history('NonExistent')
        self.assertEqual(history, [])

    def test_get_average_score(self):
        """Test calculating average score."""
        for i in range(3):
            conn = self._make_connection(download=50.0 + i * 10)
            self.store.store_metrics(conn)

        avg = self.store.get_average_score('TestNet')
        self.assertIsNotNone(avg)
        self.assertIsInstance(avg, float)
        self.assertGreater(avg, 0)

    def test_get_average_score_no_data(self):
        """Test average score with no data."""
        avg = self.store.get_average_score('NonExistent')
        self.assertIsNone(avg)

    def test_get_average_score_unavailable(self):
        """Test average score when store unavailable."""
        self.store._available = False
        avg = self.store.get_average_score('TestNet')
        self.assertIsNone(avg)

    def test_get_best_networks(self):
        """Test getting best networks."""
        for name, speed in [('Fast', 80.0), ('Medium', 40.0), ('Slow', 10.0)]:
            for _ in range(3):
                conn = self._make_connection(ssid=name, download=speed)
                self.store.store_metrics(conn)

        best = self.store.get_best_networks(limit=2)
        self.assertEqual(len(best), 2)
        self.assertEqual(best[0]['_id'], 'Fast')
        self.assertGreater(best[0]['avg_score'], best[1]['avg_score'])

    def test_get_best_networks_empty(self):
        """Test best networks with no data."""
        best = self.store.get_best_networks()
        self.assertEqual(best, [])

    def test_pruning(self):
        """Test that records are pruned when exceeding max_records."""
        store = JsonMetricsStore(data_dir=self.test_dir, max_records=5)
        for i in range(10):
            conn = self._make_connection(download=float(i))
            store.store_metrics(conn)

        filepath = os.path.join(self.test_dir, 'connection_metrics.json')
        with open(filepath) as f:
            data = json.load(f)
        self.assertLessEqual(len(data), 5)

    def test_only_connected_in_average(self):
        """Test that only connected results count in average score."""
        # Store disconnected result
        conn_disconnected = self._make_connection(connected=False, download=100.0)
        self.store.store_metrics(conn_disconnected)

        avg = self.store.get_average_score('TestNet')
        self.assertIsNone(avg)  # No connected results

        # Store connected result
        conn_connected = self._make_connection(connected=True, download=50.0)
        self.store.store_metrics(conn_connected)

        avg = self.store.get_average_score('TestNet')
        self.assertIsNotNone(avg)


class TestJsonHistoryStore(unittest.TestCase):
    """Tests for JsonHistoryStore."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = JsonHistoryStore(data_dir=self.test_dir, max_records=100)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _make_network(self, ssid='TestNet'):
        return WifiNetwork(
            ssid=ssid,
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=70,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

    def test_store_and_retrieve(self):
        """Test storing and retrieving history entries."""
        network = self._make_network()
        self.store.store(network, success=True, interface='wlan0')
        self.store.store(network, success=False, interface='wlan0')

        recent = self.store.get_recent(limit=10)
        self.assertEqual(len(recent), 2)

    def test_store_with_speed_test(self):
        """Test storing history with speed test data."""
        network = self._make_network()
        speed = {'download': 50.0, 'upload': 25.0, 'ping': 10.0}
        self.store.store(network, success=True, speed_test=speed)

        recent = self.store.get_recent()
        self.assertEqual(recent[0]['download_speed'], 50.0)

    def test_store_when_unavailable(self):
        """Test storing when unavailable."""
        self.store._available = False
        network = self._make_network()
        result = self.store.store(network, success=True)
        self.assertFalse(result)

    def test_get_recent_limit(self):
        """Test that limit is respected."""
        network = self._make_network()
        for _ in range(10):
            self.store.store(network, success=True)

        recent = self.store.get_recent(limit=3)
        self.assertEqual(len(recent), 3)

    def test_pruning(self):
        """Test record pruning."""
        store = JsonHistoryStore(data_dir=self.test_dir, max_records=5)
        network = self._make_network()
        for _ in range(10):
            store.store(network, success=True)

        filepath = os.path.join(self.test_dir, 'connection_history.json')
        with open(filepath) as f:
            data = json.load(f)
        self.assertLessEqual(len(data), 5)


class TestJsonPortalStore(unittest.TestCase):
    """Tests for JsonPortalStore."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = JsonPortalStore(data_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_store_and_retrieve_pattern(self):
        """Test storing and retrieving a portal pattern."""
        pattern = {
            'redirect_domain': 'portal.example.com',
            'portal_type': 'click-through',
            'auth_method': 'none',
        }
        self.store.store_portal_pattern('HotelWifi', pattern)

        result = self.store.get_portal_pattern('HotelWifi')
        self.assertIsNotNone(result)
        self.assertEqual(result['redirect_domain'], 'portal.example.com')
        self.assertEqual(result['success_count'], 1)

    def test_upsert_pattern(self):
        """Test that storing same pattern updates instead of duplicating."""
        pattern = {
            'redirect_domain': 'portal.example.com',
            'portal_type': 'click-through',
        }
        self.store.store_portal_pattern('HotelWifi', pattern)
        self.store.store_portal_pattern('HotelWifi', pattern)

        filepath = os.path.join(self.test_dir, 'portal_patterns.json')
        with open(filepath) as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['success_count'], 2)

    def test_get_pattern_not_found(self):
        """Test retrieving non-existent pattern."""
        result = self.store.get_portal_pattern('NonExistent')
        self.assertIsNone(result)

    def test_get_pattern_unavailable(self):
        """Test retrieving when unavailable."""
        self.store._available = False
        result = self.store.get_portal_pattern('HotelWifi')
        self.assertIsNone(result)

    def test_record_auth_result_success(self):
        """Test recording successful auth."""
        pattern = {'redirect_domain': 'portal.example.com'}
        self.store.store_portal_pattern('HotelWifi', pattern)
        self.store.record_auth_result('HotelWifi', 'portal.example.com', success=True)

        result = self.store.get_portal_pattern('HotelWifi')
        self.assertEqual(result['success_count'], 2)  # 1 from store + 1 from record

    def test_record_auth_result_failure(self):
        """Test recording failed auth."""
        pattern = {'redirect_domain': 'portal.example.com'}
        self.store.store_portal_pattern('HotelWifi', pattern)
        self.store.record_auth_result('HotelWifi', 'portal.example.com', success=False)

        result = self.store.get_portal_pattern('HotelWifi')
        self.assertEqual(result['failure_count'], 1)

    def test_best_pattern_returned(self):
        """Test that pattern with highest success_count is returned."""
        p1 = {'redirect_domain': 'portal1.example.com'}
        p2 = {'redirect_domain': 'portal2.example.com'}
        self.store.store_portal_pattern('HotelWifi', p1)
        self.store.store_portal_pattern('HotelWifi', p2)
        # Boost p2's success count
        for _ in range(5):
            self.store.record_auth_result('HotelWifi', 'portal2.example.com', success=True)

        result = self.store.get_portal_pattern('HotelWifi')
        self.assertEqual(result['redirect_domain'], 'portal2.example.com')


if __name__ == '__main__':
    unittest.main()

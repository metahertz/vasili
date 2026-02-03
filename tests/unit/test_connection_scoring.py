"""
Unit tests for connection scoring functionality.
"""

import unittest
from unittest.mock import patch, MagicMock
from vasili import WifiNetwork, ConnectionResult, PerformanceMetricsStore


class TestConnectionScoring(unittest.TestCase):
    """Test connection scoring algorithm."""

    def setUp(self):
        """Set up test fixtures."""
        self.network = WifiNetwork(
            ssid='TestNetwork',
            bssid='00:11:22:33:44:55',
            signal_strength=80,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

    def test_perfect_connection_score(self):
        """Test scoring for a perfect connection."""
        connection = ConnectionResult(
            network=self.network,
            download_speed=100.0,  # Perfect download
            upload_speed=50.0,  # Perfect upload
            ping=0.0,  # Perfect ping
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        # Perfect connection should score very high
        score = connection.calculate_score()
        self.assertGreaterEqual(score, 90.0)
        self.assertLessEqual(score, 100.0)

    def test_poor_connection_score(self):
        """Test scoring for a poor connection."""
        poor_network = WifiNetwork(
            ssid='PoorNetwork',
            bssid='AA:BB:CC:DD:EE:FF',
            signal_strength=20,  # Poor signal
            channel=11,
            encryption_type='WPA2',
            is_open=False,
        )

        connection = ConnectionResult(
            network=poor_network,
            download_speed=1.0,  # Slow download
            upload_speed=0.5,  # Slow upload
            ping=200.0,  # High latency
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        # Poor connection should score low
        score = connection.calculate_score()
        self.assertLess(score, 50.0)

    def test_medium_connection_score(self):
        """Test scoring for a medium quality connection."""
        medium_network = WifiNetwork(
            ssid='MediumNetwork',
            bssid='11:22:33:44:55:66',
            signal_strength=60,
            channel=1,
            encryption_type='WPA2',
            is_open=False,
        )

        connection = ConnectionResult(
            network=medium_network,
            download_speed=25.0,  # Moderate download
            upload_speed=10.0,  # Moderate upload
            ping=50.0,  # Moderate ping
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        score = connection.calculate_score()
        # Score calculation: download(25/100*100)*0.4 + signal(60)*0.3 + upload(10/50*100)*0.2 + ping(100-50/2)*0.1
        # = 10 + 18 + 4 + 7.5 = 39.5
        self.assertGreater(score, 35.0)
        self.assertLess(score, 70.0)

    def test_score_weights(self):
        """Test that download speed has the highest impact on score."""
        base_network = WifiNetwork(
            ssid='BaseNetwork',
            bssid='12:34:56:78:90:AB',
            signal_strength=50,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

        # Connection with high download speed
        high_download = ConnectionResult(
            network=base_network,
            download_speed=100.0,
            upload_speed=10.0,
            ping=50.0,
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        # Connection with low download speed
        low_download = ConnectionResult(
            network=base_network,
            download_speed=10.0,
            upload_speed=10.0,
            ping=50.0,
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        # High download should score significantly better
        self.assertGreater(
            high_download.calculate_score() - low_download.calculate_score(),
            30.0,  # Download has 40% weight, so impact should be significant
        )

    def test_signal_strength_impact(self):
        """Test that signal strength affects the score."""
        # Strong signal
        strong_network = WifiNetwork(
            ssid='StrongSignal',
            bssid='AA:BB:CC:DD:EE:11',
            signal_strength=90,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

        strong_connection = ConnectionResult(
            network=strong_network,
            download_speed=50.0,
            upload_speed=20.0,
            ping=30.0,
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        # Weak signal
        weak_network = WifiNetwork(
            ssid='WeakSignal',
            bssid='AA:BB:CC:DD:EE:22',
            signal_strength=30,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

        weak_connection = ConnectionResult(
            network=weak_network,
            download_speed=50.0,
            upload_speed=20.0,
            ping=30.0,
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

        # Strong signal should score better (30% weight)
        score_diff = strong_connection.calculate_score() - weak_connection.calculate_score()
        self.assertGreater(score_diff, 10.0)
        self.assertLess(score_diff, 25.0)


class TestPerformanceMetricsStore(unittest.TestCase):
    """Test MongoDB metrics storage."""

    def setUp(self):
        """Set up test fixtures."""
        self.network = WifiNetwork(
            ssid='TestNetwork',
            bssid='00:11:22:33:44:55',
            signal_strength=75,
            channel=6,
            encryption_type='WPA2',
            is_open=False,
        )

        self.connection = ConnectionResult(
            network=self.network,
            download_speed=50.0,
            upload_speed=25.0,
            ping=20.0,
            connected=True,
            connection_method='test',
            interface='wlan0',
        )

    @patch('vasili.MongoClient')
    def test_metrics_store_initialization_success(self, mock_mongo_client):
        """Test successful MongoDB initialization."""
        # Mock successful connection
        mock_client = MagicMock()
        mock_client.admin.command.return_value = {'ok': 1}
        mock_mongo_client.return_value = mock_client

        store = PerformanceMetricsStore()

        self.assertTrue(store.is_available())
        mock_mongo_client.assert_called_once()

    @patch('vasili.MongoClient')
    def test_metrics_store_initialization_failure(self, mock_mongo_client):
        """Test MongoDB initialization failure handling."""
        # Mock connection failure
        from pymongo.errors import ConnectionFailure

        mock_mongo_client.side_effect = ConnectionFailure('Connection failed')

        store = PerformanceMetricsStore()

        self.assertFalse(store.is_available())

    @patch('vasili.MongoClient')
    def test_store_metrics_when_available(self, mock_mongo_client):
        """Test storing metrics when MongoDB is available."""
        # Mock successful connection and insert
        mock_client = MagicMock()
        mock_client.admin.command.return_value = {'ok': 1}
        mock_db = MagicMock()
        mock_collection = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_collection
        mock_mongo_client.return_value = mock_client

        store = PerformanceMetricsStore()
        result = store.store_metrics(self.connection)

        self.assertTrue(result)
        mock_collection.insert_one.assert_called_once()

    @patch('vasili.MongoClient')
    def test_store_metrics_when_unavailable(self, mock_mongo_client):
        """Test storing metrics when MongoDB is unavailable."""
        from pymongo.errors import ConnectionFailure

        mock_mongo_client.side_effect = ConnectionFailure('Connection failed')

        store = PerformanceMetricsStore()
        result = store.store_metrics(self.connection)

        self.assertFalse(result)

    @patch('vasili.MongoClient')
    def test_get_network_history(self, mock_mongo_client):
        """Test retrieving network history."""
        # Mock successful connection
        mock_client = MagicMock()
        mock_client.admin.command.return_value = {'ok': 1}
        mock_db = MagicMock()
        mock_collection = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_collection

        # Mock find query
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value.limit.return_value = [
            {'ssid': 'TestNetwork', 'score': 85.5},
            {'ssid': 'TestNetwork', 'score': 87.2},
        ]
        mock_collection.find.return_value = mock_cursor

        mock_mongo_client.return_value = mock_client

        store = PerformanceMetricsStore()
        history = store.get_network_history('TestNetwork')

        self.assertEqual(len(history), 2)
        mock_collection.find.assert_called_once()


if __name__ == '__main__':
    unittest.main()
# Force CI trigger

"""
JSON file-based storage alternative to MongoDB for vasili metrics.

Provides the same interface as PerformanceMetricsStore but uses JSON files
instead of MongoDB/BSON. Designed for embedded devices where MongoDB's
~100MB RAM overhead is prohibitive.

Storage layout:
    <data_dir>/
        connection_metrics.json   - Array of metric documents
        connection_history.json   - Array of history documents
        portal_patterns.json      - Array of portal pattern documents
"""

import json
import logging
import os
import statistics
import threading
import time
from typing import Any, Optional

logger = logging.getLogger('vasili.json_store')


class JsonMetricsStore:
    """
    Store and retrieve WiFi connection performance metrics using JSON files.
    Drop-in replacement for PerformanceMetricsStore (MongoDB-backed).
    """

    def __init__(self, data_dir: str = '/var/lib/vasili/data', max_records: int = 1000):
        """
        Initialize the JSON metrics store.

        Args:
            data_dir: Directory for storing JSON files
            max_records: Maximum records to keep per collection (oldest pruned)
        """
        self.data_dir = data_dir
        self.max_records = max_records
        self._metrics_file = os.path.join(data_dir, 'connection_metrics.json')
        self._history_file = os.path.join(data_dir, 'connection_history.json')
        self._portal_file = os.path.join(data_dir, 'portal_patterns.json')
        self._available = False
        self._lock = threading.Lock()

        try:
            os.makedirs(data_dir, exist_ok=True)
            # Verify we can write to the directory
            test_file = os.path.join(data_dir, '.write_test')
            with open(test_file, 'w') as f:
                f.write('ok')
            os.unlink(test_file)
            self._available = True
            logger.info(f'JSON metrics store initialized at {data_dir}')
        except OSError as e:
            logger.warning(f'Cannot initialize JSON store at {data_dir}: {e}')
            self._available = False

    def is_available(self) -> bool:
        """Check if the store is available."""
        return self._available

    def _read_collection(self, filepath: str) -> list[dict]:
        """Read a JSON collection file."""
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f'Failed to read {filepath}: {e}')
            return []

    def _write_collection(self, filepath: str, docs: list[dict]):
        """Write a JSON collection file atomically."""
        tmp_path = filepath + '.tmp'
        try:
            with open(tmp_path, 'w') as f:
                json.dump(docs, f)
            os.replace(tmp_path, filepath)
        except OSError as e:
            logger.error(f'Failed to write {filepath}: {e}')
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _prune(self, docs: list[dict], key: str = 'timestamp') -> list[dict]:
        """Prune oldest records if over max_records."""
        if len(docs) <= self.max_records:
            return docs
        docs.sort(key=lambda d: d.get(key, 0), reverse=True)
        return docs[: self.max_records]

    def store_metrics(self, connection) -> bool:
        """
        Store connection metrics to JSON file.

        Args:
            connection: ConnectionResult containing performance data

        Returns:
            True if stored successfully, False otherwise
        """
        if not self._available:
            return False

        try:
            metric = {
                'ssid': connection.network.ssid,
                'bssid': connection.network.bssid,
                'signal_strength': connection.network.signal_strength,
                'channel': connection.network.channel,
                'encryption_type': connection.network.encryption_type,
                'download_speed': connection.download_speed,
                'upload_speed': connection.upload_speed,
                'ping': connection.ping,
                'connection_method': connection.connection_method,
                'interface': connection.interface,
                'score': connection.calculate_score(),
                'timestamp': time.time(),
                'connected': connection.connected,
            }

            with self._lock:
                docs = self._read_collection(self._metrics_file)
                docs.append(metric)
                docs = self._prune(docs)
                self._write_collection(self._metrics_file, docs)

            logger.debug(f'Stored metrics for {connection.network.ssid} (score: {metric["score"]})')
            return True

        except Exception as e:
            logger.error(f'Failed to store metrics: {e}')
            return False

    def get_network_history(self, ssid: str, limit: int = 10) -> list[dict]:
        """
        Get historical metrics for a specific network.

        Args:
            ssid: Network SSID to query
            limit: Maximum number of records to return

        Returns:
            List of metric dictionaries, newest first
        """
        if not self._available:
            return []

        try:
            docs = self._read_collection(self._metrics_file)
            filtered = [d for d in docs if d.get('ssid') == ssid]
            filtered.sort(key=lambda d: d.get('timestamp', 0), reverse=True)
            return filtered[:limit]
        except Exception as e:
            logger.error(f'Failed to retrieve network history: {e}')
            return []

    def get_average_score(self, ssid: str) -> Optional[float]:
        """
        Calculate average score for a network based on historical data.

        Args:
            ssid: Network SSID

        Returns:
            Average score or None if no data available
        """
        if not self._available:
            return None

        try:
            docs = self._read_collection(self._metrics_file)
            scores = [d['score'] for d in docs if d.get('ssid') == ssid and d.get('connected')]
            if scores:
                return round(statistics.mean(scores), 2)
            return None
        except Exception as e:
            logger.error(f'Failed to calculate average score: {e}')
            return None

    def get_best_networks(self, limit: int = 5) -> list[dict]:
        """
        Get the best performing networks based on average scores.

        Args:
            limit: Number of networks to return

        Returns:
            List of networks with their average scores
        """
        if not self._available:
            return []

        try:
            docs = self._read_collection(self._metrics_file)
            connected = [d for d in docs if d.get('connected')]

            groups: dict[str, dict[str, list]] = {}
            for d in connected:
                ssid = d['ssid']
                if ssid not in groups:
                    groups[ssid] = {
                        'scores': [],
                        'downloads': [],
                        'uploads': [],
                        'pings': [],
                        'signals': [],
                    }
                groups[ssid]['scores'].append(d['score'])
                groups[ssid]['downloads'].append(d['download_speed'])
                groups[ssid]['uploads'].append(d['upload_speed'])
                groups[ssid]['pings'].append(d['ping'])
                groups[ssid]['signals'].append(d['signal_strength'])

            results = []
            for ssid, data in groups.items():
                results.append(
                    {
                        '_id': ssid,
                        'avg_score': round(statistics.mean(data['scores']), 2),
                        'avg_download': round(statistics.mean(data['downloads']), 2),
                        'avg_upload': round(statistics.mean(data['uploads']), 2),
                        'avg_ping': round(statistics.mean(data['pings']), 2),
                        'avg_signal': round(statistics.mean(data['signals']), 2),
                        'connection_count': len(data['scores']),
                    }
                )
            results.sort(key=lambda x: x['avg_score'], reverse=True)
            return results[:limit]
        except Exception as e:
            logger.error(f'Failed to get best networks: {e}')
            return []

    def close(self):
        """No-op for compatibility with PerformanceMetricsStore."""
        logger.info('JSON metrics store closed')


class JsonHistoryStore:
    """
    Store and retrieve connection history using JSON files.
    Replaces the global history_collection MongoDB usage.
    """

    def __init__(self, data_dir: str = '/var/lib/vasili/data', max_records: int = 1000):
        self.data_dir = data_dir
        self.max_records = max_records
        self._history_file = os.path.join(data_dir, 'connection_history.json')
        self._available = False
        self._lock = threading.Lock()

        try:
            os.makedirs(data_dir, exist_ok=True)
            self._available = True
        except OSError as e:
            logger.warning(f'Cannot initialize JSON history store: {e}')

    def is_available(self) -> bool:
        return self._available

    def store(self, network, success: bool, speed_test: dict = None, interface: str = None) -> bool:
        """Store a connection attempt."""
        if not self._available:
            return False

        try:
            from datetime import datetime, timezone

            entry = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'ssid': network.ssid,
                'bssid': network.bssid,
                'signal_strength': network.signal_strength,
                'channel': network.channel,
                'encryption_type': network.encryption_type,
                'success': success,
                'interface': interface,
            }

            if speed_test:
                entry['download_speed'] = speed_test.get('download', 0)
                entry['upload_speed'] = speed_test.get('upload', 0)
                entry['ping'] = speed_test.get('ping', 0)

            with self._lock:
                docs = self._read()
                docs.append(entry)
                if len(docs) > self.max_records:
                    docs = docs[-self.max_records :]
                self._write(docs)

            logger.debug(f'Stored connection history for {network.ssid}')
            return True
        except Exception as e:
            logger.error(f'Failed to store connection history: {e}')
            return False

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Get recent connection history entries."""
        if not self._available:
            return []
        try:
            docs = self._read()
            docs.sort(key=lambda d: d.get('timestamp', ''), reverse=True)
            return docs[:limit]
        except Exception as e:
            logger.error(f'Failed to read connection history: {e}')
            return []

    def _read(self) -> list[dict]:
        if not os.path.exists(self._history_file):
            return []
        try:
            with open(self._history_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, docs: list[dict]):
        tmp = self._history_file + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(docs, f)
            os.replace(tmp, self._history_file)
        except OSError as e:
            logger.error(f'Failed to write history: {e}')
            if os.path.exists(tmp):
                os.unlink(tmp)

    def close(self):
        pass


class JsonPortalStore:
    """
    Store and retrieve captive portal patterns using JSON files.
    Replaces PortalDatabase (MongoDB-backed).
    """

    def __init__(self, data_dir: str = '/var/lib/vasili/data'):
        self.data_dir = data_dir
        self._portal_file = os.path.join(data_dir, 'portal_patterns.json')
        self._available = False
        self._lock = threading.Lock()

        try:
            os.makedirs(data_dir, exist_ok=True)
            self._available = True
        except OSError as e:
            logger.warning(f'Cannot initialize JSON portal store: {e}')

    def is_available(self) -> bool:
        return self._available

    def store_portal_pattern(self, ssid: str, pattern_data: dict[str, Any]):
        """Store a detected portal pattern for future reference."""
        if not self._available:
            return

        try:
            with self._lock:
                docs = self._read()

                # Find existing pattern for upsert
                existing = None
                for d in docs:
                    if d.get('ssid') == ssid and d.get('redirect_domain') == pattern_data.get(
                        'redirect_domain'
                    ):
                        existing = d
                        break

                if existing:
                    existing.update(pattern_data)
                    existing['ssid'] = ssid
                    existing['last_seen'] = time.time()
                    existing['success_count'] = existing.get('success_count', 0) + 1
                else:
                    new_doc = dict(pattern_data)
                    new_doc['ssid'] = ssid
                    new_doc['last_seen'] = time.time()
                    new_doc['success_count'] = 1
                    new_doc['failure_count'] = 0
                    docs.append(new_doc)

                self._write(docs)
            logger.debug(f'Stored portal pattern for {ssid}')
        except Exception as e:
            logger.error(f'Failed to store portal pattern: {e}')

    def get_portal_pattern(self, ssid: str) -> Optional[dict[str, Any]]:
        """Retrieve a known portal pattern for an SSID."""
        if not self._available:
            return None

        try:
            docs = self._read()
            matches = [d for d in docs if d.get('ssid') == ssid]
            if matches:
                # Return the one with highest success_count
                matches.sort(key=lambda d: d.get('success_count', 0), reverse=True)
                return matches[0]
            return None
        except Exception as e:
            logger.error(f'Failed to retrieve portal pattern: {e}')
            return None

    def record_auth_result(self, ssid: str, redirect_domain: str, success: bool):
        """Record the result of an authentication attempt."""
        if not self._available:
            return

        try:
            with self._lock:
                docs = self._read()
                for d in docs:
                    if d.get('ssid') == ssid and d.get('redirect_domain') == redirect_domain:
                        field = 'success_count' if success else 'failure_count'
                        d[field] = d.get(field, 0) + 1
                        d['last_seen'] = time.time()
                        break
                self._write(docs)
        except Exception as e:
            logger.error(f'Failed to record auth result: {e}')

    def _read(self) -> list[dict]:
        if not os.path.exists(self._portal_file):
            return []
        try:
            with open(self._portal_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, docs: list[dict]):
        tmp = self._portal_file + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(docs, f)
            os.replace(tmp, self._portal_file)
        except OSError as e:
            logger.error(f'Failed to write portal patterns: {e}')
            if os.path.exists(tmp):
                os.unlink(tmp)

    def close(self):
        pass

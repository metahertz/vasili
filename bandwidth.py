"""Bandwidth monitoring - track network usage over time via MongoDB."""

import os
import threading
import time
from datetime import datetime, timedelta

from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('bandwidth')


class BandwidthMonitor:
    """Monitors and records bandwidth usage per interface.

    Stores samples in MongoDB. Gracefully degrades if MongoDB is unavailable.
    """

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili', sample_interval: int = 60):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.sample_interval = sample_interval
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._last_readings: dict[str, tuple[int, int, float]] = {}
        self._current_rates: dict[str, dict] = {}
        self._available = False
        self._init_db()

    def _init_db(self):
        """Connect to MongoDB and set up indexes."""
        try:
            self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[self.db_name]
            self.collection = self.db['bandwidth']
            self._available = True

            self.collection.create_index([('timestamp', DESCENDING)])
            self.collection.create_index([('interface', 1), ('timestamp', DESCENDING)])
            logger.info('BandwidthMonitor connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for bandwidth: {e}. Storage disabled.')
        except Exception as e:
            logger.error(f'Failed to initialize BandwidthMonitor DB: {e}')

    def is_available(self) -> bool:
        return self._available

    @staticmethod
    def _read_bytes(interface: str) -> tuple[int, int] | None:
        """Read current rx/tx bytes from sysfs."""
        try:
            rx_path = f'/sys/class/net/{interface}/statistics/rx_bytes'
            tx_path = f'/sys/class/net/{interface}/statistics/tx_bytes'
            with open(rx_path) as f:
                rx = int(f.read().strip())
            with open(tx_path) as f:
                tx = int(f.read().strip())
            return rx, tx
        except (FileNotFoundError, ValueError, PermissionError):
            return None

    def _sample(self):
        """Take a single bandwidth sample for all wireless interfaces."""
        now = time.time()
        timestamp = datetime.now().isoformat()

        # Find wireless interfaces
        interfaces = []
        try:
            for iface in os.listdir('/sys/class/net'):
                if os.path.isdir(f'/sys/class/net/{iface}/wireless'):
                    interfaces.append(iface)
        except OSError:
            return

        for iface in interfaces:
            reading = self._read_bytes(iface)
            if reading is None:
                continue

            rx, tx = reading
            rx_rate = 0.0
            tx_rate = 0.0

            if iface in self._last_readings:
                last_rx, last_tx, last_time = self._last_readings[iface]
                elapsed = now - last_time
                if elapsed > 0:
                    rx_rate = (rx - last_rx) / elapsed
                    tx_rate = (tx - last_tx) / elapsed

            self._last_readings[iface] = (rx, tx, now)
            self._current_rates[iface] = {
                'rx_rate': rx_rate,
                'tx_rate': tx_rate,
                'rx_bytes': rx,
                'tx_bytes': tx,
            }

            if self._available:
                try:
                    self.collection.insert_one({
                        'timestamp': timestamp,
                        'interface': iface,
                        'rx_bytes': rx,
                        'tx_bytes': tx,
                        'rx_rate': rx_rate,
                        'tx_rate': tx_rate,
                    })
                except Exception as e:
                    logger.error(f'Failed to record bandwidth sample: {e}')

    def _monitor_loop(self):
        """Background monitoring loop."""
        logger.info(f'Bandwidth monitor started (interval: {self.sample_interval}s)')
        while self._running:
            self._sample()
            for _ in range(self.sample_interval):
                if not self._running:
                    break
                time.sleep(1)
        logger.info('Bandwidth monitor stopped')

    def start(self):
        """Start background bandwidth monitoring."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop background monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def get_current_rates(self) -> dict:
        """Get current bandwidth rates for all monitored interfaces."""
        if not self._current_rates:
            self._sample()
        return dict(self._current_rates)

    def get_history(self, hours: int = 24, interface: str = None) -> list[dict]:
        """Get historical bandwidth data."""
        if not self._available:
            return []

        try:
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            query: dict = {'timestamp': {'$gt': cutoff}}
            if interface:
                query['interface'] = interface

            cursor = self.collection.find(
                query,
                {'_id': 0},
            ).sort('timestamp', DESCENDING)
            return list(cursor)
        except Exception as e:
            logger.error(f'Failed to get bandwidth history: {e}')
            return []

    def get_total_usage(self, hours: int = 24, interface: str = None) -> dict:
        """Get total data usage over a time period."""
        if not self._available:
            return {'rx_bytes': 0, 'tx_bytes': 0, 'hours': hours}

        try:
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            match: dict = {'timestamp': {'$gt': cutoff}}
            if interface:
                match['interface'] = interface

            pipeline = [
                {'$match': match},
                {'$group': {
                    '_id': '$interface',
                    'min_rx': {'$min': '$rx_bytes'},
                    'max_rx': {'$max': '$rx_bytes'},
                    'min_tx': {'$min': '$tx_bytes'},
                    'max_tx': {'$max': '$tx_bytes'},
                }},
            ]
            results = list(self.collection.aggregate(pipeline))

            if interface:
                if results:
                    r = results[0]
                    return {
                        'interface': interface,
                        'rx_bytes': (r.get('max_rx', 0) or 0) - (r.get('min_rx', 0) or 0),
                        'tx_bytes': (r.get('max_tx', 0) or 0) - (r.get('min_tx', 0) or 0),
                        'hours': hours,
                    }
            else:
                total_rx = sum(
                    (r.get('max_rx', 0) or 0) - (r.get('min_rx', 0) or 0) for r in results
                )
                total_tx = sum(
                    (r.get('max_tx', 0) or 0) - (r.get('min_tx', 0) or 0) for r in results
                )
                return {'rx_bytes': total_rx, 'tx_bytes': total_tx, 'hours': hours}

            return {'rx_bytes': 0, 'tx_bytes': 0, 'hours': hours}
        except Exception as e:
            logger.error(f'Failed to get total usage: {e}')
            return {'rx_bytes': 0, 'tx_bytes': 0, 'hours': hours}

"""Connection persistence - remember working networks and their credentials via MongoDB."""

import hashlib
from datetime import datetime

from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('persistence')


class ConnectionStore:
    """MongoDB-backed storage for known-good network connections.

    Gracefully degrades if MongoDB is unavailable.
    """

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/', db_name: str = 'vasili'):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self._available = False

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db['known_networks']
            self._available = True
            logger.info(f'ConnectionStore connected to MongoDB at {mongo_uri}')

            # Ensure unique index on (ssid, bssid)
            self.collection.create_index(
                [('ssid', 1), ('bssid', 1)],
                unique=True,
            )
            self.collection.create_index([('avg_score', DESCENDING)])
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available: {e}. Connection persistence disabled.')
        except Exception as e:
            logger.error(f'Failed to initialize ConnectionStore: {e}. Persistence disabled.')

    def is_available(self) -> bool:
        """Check if MongoDB is available."""
        return self._available

    @staticmethod
    def _hash_password(password: str) -> str:
        """Hash a password for storage. Not for high-security use - just obfuscation."""
        if not password:
            return ''
        return hashlib.sha256(password.encode()).hexdigest()

    def store_network(
        self,
        ssid: str,
        bssid: str,
        encryption_type: str = '',
        password: str = '',
        score: float = 0.0,
        download_speed: float = 0.0,
        upload_speed: float = 0.0,
        ping: float = 0.0,
        success: bool = True,
    ):
        """Store or update a network after a connection attempt."""
        if not self._available:
            return

        try:
            now = datetime.now().isoformat()
            existing = self.collection.find_one({'ssid': ssid, 'bssid': bssid})

            if existing:
                if success:
                    old_success = existing['success_count']
                    new_success = old_success + 1
                    new_score = ((existing['avg_score'] * old_success) + score) / new_success
                    new_dl = ((existing['avg_download'] * old_success) + download_speed) / new_success
                    new_ul = ((existing['avg_upload'] * old_success) + upload_speed) / new_success
                    new_ping = ((existing['avg_ping'] * old_success) + ping) / new_success

                    self.collection.update_one(
                        {'ssid': ssid, 'bssid': bssid},
                        {'$set': {
                            'last_connected': now,
                            'success_count': new_success,
                            'avg_score': new_score,
                            'avg_download': new_dl,
                            'avg_upload': new_ul,
                            'avg_ping': new_ping,
                        }},
                    )
                else:
                    self.collection.update_one(
                        {'ssid': ssid, 'bssid': bssid},
                        {'$set': {'last_connected': now},
                         '$inc': {'fail_count': 1}},
                    )
            else:
                pw_hash = self._hash_password(password) if password else ''
                self.collection.insert_one({
                    'ssid': ssid,
                    'bssid': bssid,
                    'encryption_type': encryption_type,
                    'password_hash': pw_hash,
                    'last_connected': now,
                    'success_count': 1 if success else 0,
                    'fail_count': 0 if success else 1,
                    'avg_score': score if success else 0.0,
                    'avg_download': download_speed if success else 0.0,
                    'avg_upload': upload_speed if success else 0.0,
                    'avg_ping': ping if success else 0.0,
                })
            logger.debug(f'Stored network {ssid} ({bssid}): success={success}')
        except Exception as e:
            logger.error(f'Failed to store network {ssid}: {e}')

    def get_known_networks(self, limit: int = 50) -> list[dict]:
        """Get all known networks, sorted by score descending."""
        if not self._available:
            return []

        try:
            cursor = self.collection.find(
                {},
                {'_id': 0, 'password_hash': 0},
            ).sort('avg_score', DESCENDING).limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f'Failed to retrieve known networks: {e}')
            return []

    def is_known_network(self, ssid: str, bssid: str) -> bool:
        """Check if a network is in the store."""
        if not self._available:
            return False

        try:
            return self.collection.find_one({'ssid': ssid, 'bssid': bssid}) is not None
        except Exception as e:
            logger.error(f'Failed to check network: {e}')
            return False

    def get_network(self, ssid: str, bssid: str) -> dict | None:
        """Get a specific network's data."""
        if not self._available:
            return None

        try:
            doc = self.collection.find_one(
                {'ssid': ssid, 'bssid': bssid},
                {'_id': 0},
            )
            return doc
        except Exception as e:
            logger.error(f'Failed to get network: {e}')
            return None

    def delete_network(self, ssid: str) -> bool:
        """Delete all entries for a given SSID."""
        if not self._available:
            return False

        try:
            result = self.collection.delete_many({'ssid': ssid})
            deleted = result.deleted_count > 0
            if deleted:
                logger.info(f'Deleted saved network: {ssid}')
            return deleted
        except Exception as e:
            logger.error(f'Failed to delete network: {e}')
            return False

    def clear_all(self) -> int:
        """Delete all saved networks. Returns count of deleted entries."""
        if not self._available:
            return 0

        try:
            result = self.collection.delete_many({})
            logger.info(f'Cleared {result.deleted_count} saved networks')
            return result.deleted_count
        except Exception as e:
            logger.error(f'Failed to clear networks: {e}')
            return 0

    def get_best_networks(self, limit: int = 10) -> list[dict]:
        """Get top performing networks by average score."""
        if not self._available:
            return []

        try:
            cursor = self.collection.find(
                {'success_count': {'$gt': 0}},
                {'_id': 0, 'ssid': 1, 'bssid': 1, 'avg_score': 1,
                 'success_count': 1, 'last_connected': 1},
            ).sort('avg_score', DESCENDING).limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f'Failed to get best networks: {e}')
            return []

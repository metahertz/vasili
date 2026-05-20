"""Known-networks credential store — Fernet-encrypted per-SSID passwords.

Stores a user-curated list of WiFi networks (SSID + password + security)
that Vasili is authorized to auto-connect to. Passwords are encrypted at
rest with a device-bound Fernet key loaded from disk; the encryption key
never leaves the host.

Threat model: protects against database-only exfiltration (a leaked Mongo
dump can't be used without the key file). Does NOT protect against an
attacker with local read access on the device — same trust boundary as
the rest of vasili.

Gracefully degrades: if MongoDB is unavailable or the key file can't be
read/created, all reads return empty / None and writes return False.
The credential stage path must never raise.
"""

import os
import stat
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('known_networks_store')


DEFAULT_KEY_PATHS = [
    '/home/ubuntu/vasili/.vasili-master.key',
    os.path.expanduser('~/.vasili/master.key'),
]


class KnownNetworksStore:
    """MongoDB-backed encrypted vault for per-SSID credentials."""

    def __init__(
        self,
        mongo_uri: str = 'mongodb://localhost:27017/',
        db_name: str = 'vasili',
        key_path: Optional[str] = None,
    ):
        self._available = False
        self._fernet: Optional[Fernet] = None
        self._key_path: Optional[str] = None

        self._load_or_create_key(key_path)

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db['known_network_credentials']
            self.collection.create_index('ssid', unique=True)
            self._available = True
            logger.info('KnownNetworksStore connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for known networks: {e}')
        except Exception as e:
            logger.error(f'Failed to initialize KnownNetworksStore: {e}')

    def is_available(self) -> bool:
        return self._available and self._fernet is not None

    def _load_or_create_key(self, key_path: Optional[str]):
        candidates = [key_path] if key_path else list(DEFAULT_KEY_PATHS)
        for path in candidates:
            if not path:
                continue
            try:
                if os.path.exists(path):
                    with open(path, 'rb') as f:
                        raw = f.read().strip()
                    self._fernet = Fernet(raw)
                    self._key_path = path
                    logger.info(f'Loaded known-networks master key from {path}')
                    return
                parent = os.path.dirname(path) or '.'
                os.makedirs(parent, exist_ok=True)
                key = Fernet.generate_key()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, key)
                finally:
                    os.close(fd)
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                self._fernet = Fernet(key)
                self._key_path = path
                logger.info(f'Generated new known-networks master key at {path}')
                return
            except FileExistsError:
                try:
                    with open(path, 'rb') as f:
                        raw = f.read().strip()
                    self._fernet = Fernet(raw)
                    self._key_path = path
                    return
                except Exception as e:
                    logger.warning(f'Key file appeared at {path} but unreadable: {e}')
            except (PermissionError, OSError) as e:
                logger.warning(f'Cannot use key path {path}: {e}')
                continue
        logger.error('No usable master key path; known-networks encryption disabled')

    def add(self, ssid: str, password: str, security: str = 'WPA2',
            notes: str = '') -> bool:
        if not self.is_available() or not ssid:
            return False
        try:
            token = self._fernet.encrypt(password.encode('utf-8')).decode('ascii')
            self.collection.update_one(
                {'ssid': ssid},
                {'$set': {
                    'ssid': ssid,
                    'password_enc': token,
                    'security': security,
                    'notes': notes,
                    'updated_at': datetime.now().isoformat(),
                },
                 '$setOnInsert': {'added_at': datetime.now().isoformat()}},
                upsert=True,
            )
            logger.info(f'Stored known credential for {ssid}')
            return True
        except Exception as e:
            logger.error(f'Failed to store credential for {ssid}: {e}')
            return False

    def remove(self, ssid: str) -> bool:
        if not self._available or not ssid:
            return False
        try:
            result = self.collection.delete_one({'ssid': ssid})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f'Failed to remove credential for {ssid}: {e}')
            return False

    def get(self, ssid: str) -> Optional[dict]:
        if not self.is_available() or not ssid:
            return None
        try:
            doc = self.collection.find_one({'ssid': ssid}, {'_id': 0})
            if not doc:
                return None
            password = self._decrypt(doc.get('password_enc', ''))
            if password is None:
                return None
            return {
                'ssid': doc.get('ssid', ssid),
                'password': password,
                'security': doc.get('security', 'WPA2'),
                'notes': doc.get('notes', ''),
                'added_at': doc.get('added_at', ''),
            }
        except Exception as e:
            logger.error(f'Failed to fetch credential for {ssid}: {e}')
            return None

    def list_all(self) -> list[dict]:
        if not self._available:
            return []
        try:
            cursor = self.collection.find({}, {'_id': 0, 'password_enc': 0})
            return [
                {
                    'ssid': d.get('ssid', ''),
                    'security': d.get('security', 'WPA2'),
                    'notes': d.get('notes', ''),
                    'added_at': d.get('added_at', ''),
                    'updated_at': d.get('updated_at', ''),
                    'password': '***',
                }
                for d in cursor
            ]
        except Exception as e:
            logger.error(f'Failed to list known networks: {e}')
            return []

    def reveal(self, ssid: str) -> Optional[str]:
        entry = self.get(ssid)
        return entry['password'] if entry else None

    def _decrypt(self, token: str) -> Optional[str]:
        if not token or self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(token.encode('ascii')).decode('utf-8')
        except InvalidToken:
            logger.error('Stored credential could not be decrypted (key mismatch?)')
            return None
        except Exception as e:
            logger.error(f'Decryption error: {e}')
            return None

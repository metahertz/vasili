"""Consent management for offensive/sensitive modules.

Three consent modes per module:
- 'off': Module never runs (default)
- 'on': Module runs on all networks
- 'by_ssid': Module only runs on user-approved networks (selected via UI)

Consent is stored in MongoDB with a config.yaml fallback for headless operation.
"""

import time

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('consent')

# Valid consent modes
MODES = ('off', 'on', 'by_ssid')


class ConsentManager:
    """Manages user consent for sensitive modules."""

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili', yaml_consent: dict = None):
        self._available = False
        self._yaml_consent = yaml_consent or {}

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db['module_consent']
            self.ssid_collection = self.db['ssid_consent']
            self._available = True
            self.collection.create_index('module', unique=True)
            self.ssid_collection.create_index(
                [('module', 1), ('bssid', 1)], unique=True
            )
            logger.info('ConsentManager connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for consent: {e}')
        except Exception as e:
            logger.error(f'Failed to initialize ConsentManager: {e}')

    def get_mode(self, module_name: str) -> str:
        """Get the consent mode for a module: 'off', 'on', or 'by_ssid'."""
        if self._available:
            try:
                doc = self.collection.find_one({'module': module_name})
                if doc:
                    mode = doc.get('mode', None)
                    if mode in MODES:
                        return mode
                    # Legacy boolean format migration
                    if doc.get('consented', False):
                        return 'on'
                    return 'off'
            except Exception as e:
                logger.error(f'Failed to get consent mode for {module_name}: {e}')

        # YAML fallback
        val = self._yaml_consent.get(module_name, False)
        if isinstance(val, str) and val in MODES:
            return val
        return 'on' if val else 'off'

    def has_consent(self, module_name: str, bssid: str = None,
                    ssid: str = None) -> bool:
        """Check if consent is granted for a module, optionally for a specific network.

        Args:
            module_name: The stage/module name
            bssid: Network BSSID (used for by_ssid mode)
            ssid: Network SSID (used for by_ssid mode)
        """
        mode = self.get_mode(module_name)

        if mode == 'off':
            return False
        if mode == 'on':
            return True
        if mode == 'by_ssid':
            if not bssid:
                return False
            return self._has_ssid_consent(module_name, bssid)

        return False

    def set_mode(self, module_name: str, mode: str) -> bool:
        """Set the consent mode for a module."""
        if mode not in MODES:
            return False
        if not self._available:
            return False

        try:
            self.collection.update_one(
                {'module': module_name},
                {'$set': {
                    'module': module_name,
                    'mode': mode,
                    'updated_at': time.time(),
                }},
                upsert=True,
            )
            logger.info(f'Consent mode for {module_name} set to {mode}')
            return True
        except Exception as e:
            logger.error(f'Failed to set consent mode for {module_name}: {e}')
            return False

    # Legacy compat
    def grant(self, module_name: str) -> bool:
        return self.set_mode(module_name, 'on')

    def revoke(self, module_name: str) -> bool:
        return self.set_mode(module_name, 'off')

    # --- Per-SSID consent ---

    def _has_ssid_consent(self, module_name: str, bssid: str) -> bool:
        if not self._available:
            return False
        try:
            doc = self.ssid_collection.find_one({
                'module': module_name,
                'bssid': bssid.lower(),
            })
            return doc is not None and doc.get('approved', False)
        except Exception as e:
            logger.error(f'Failed to check SSID consent: {e}')
            return False

    def approve_ssid(self, module_name: str, bssid: str,
                     ssid: str = '') -> bool:
        """Approve a specific network for an offensive module."""
        if not self._available:
            return False
        try:
            self.ssid_collection.update_one(
                {'module': module_name, 'bssid': bssid.lower()},
                {'$set': {
                    'module': module_name,
                    'bssid': bssid.lower(),
                    'ssid': ssid,
                    'approved': True,
                    'approved_at': time.time(),
                }},
                upsert=True,
            )
            logger.info(f'SSID consent approved: {module_name} on {ssid} ({bssid})')
            return True
        except Exception as e:
            logger.error(f'Failed to approve SSID: {e}')
            return False

    def revoke_ssid(self, module_name: str, bssid: str) -> bool:
        """Revoke approval for a specific network."""
        if not self._available:
            return False
        try:
            self.ssid_collection.delete_one({
                'module': module_name,
                'bssid': bssid.lower(),
            })
            return True
        except Exception as e:
            logger.error(f'Failed to revoke SSID consent: {e}')
            return False

    def get_approved_ssids(self, module_name: str) -> list[dict]:
        """Get all approved networks for a module."""
        if not self._available:
            return []
        try:
            return list(self.ssid_collection.find(
                {'module': module_name, 'approved': True},
                {'_id': 0},
            ))
        except Exception as e:
            logger.error(f'Failed to get approved SSIDs: {e}')
            return []

    def get_all(self) -> dict[str, dict]:
        """Get consent status for all modules.

        Returns dict of {module_name: {mode: str, approved_ssids: [...]}}
        """
        result = {}

        # YAML defaults
        for name, val in self._yaml_consent.items():
            if isinstance(val, str) and val in MODES:
                result[name] = {'mode': val}
            else:
                result[name] = {'mode': 'on' if val else 'off'}

        # MongoDB overrides
        if self._available:
            try:
                for doc in self.collection.find({}, {'_id': 0}):
                    module = doc['module']
                    mode = doc.get('mode', 'on' if doc.get('consented') else 'off')
                    result[module] = {'mode': mode}
            except Exception:
                pass

        # Add approved SSIDs for by_ssid modules
        for name, info in result.items():
            if info['mode'] == 'by_ssid':
                info['approved_ssids'] = self.get_approved_ssids(name)

        return result

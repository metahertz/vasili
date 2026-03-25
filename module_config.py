"""Module configuration store — MongoDB-backed per-module settings.

Each module/stage declares a config schema via get_config_schema().
User-modified values are persisted in MongoDB. Falls back to defaults
when MongoDB is unavailable.
"""

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('module_config')


class ModuleConfigStore:
    """MongoDB-backed configuration store for module settings."""

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili'):
        self._available = False
        self._schemas: dict[str, dict] = {}

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db['module_config']
            self._available = True
            self.collection.create_index('module', unique=True)
            logger.info('ModuleConfigStore connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for module config: {e}')
        except Exception as e:
            logger.error(f'Failed to initialize ModuleConfigStore: {e}')

    def is_available(self) -> bool:
        return self._available

    def register_schema(self, module_name: str, schema: dict):
        """Register a module's config schema (called during module loading)."""
        self._schemas[module_name] = schema

    def get_schema(self, module_name: str) -> dict:
        """Get the config schema for a module."""
        return self._schemas.get(module_name, {})

    def get_all_schemas(self) -> dict[str, dict]:
        """Get all registered config schemas."""
        return dict(self._schemas)

    def get_defaults(self, module_name: str) -> dict:
        """Get default values from the schema."""
        schema = self._schemas.get(module_name, {})
        return {key: spec.get('default') for key, spec in schema.items()}

    def get_config(self, module_name: str) -> dict:
        """Get effective config for a module (stored values merged over defaults)."""
        defaults = self.get_defaults(module_name)

        if not self._available:
            return defaults

        try:
            doc = self.collection.find_one(
                {'module': module_name}, {'_id': 0, 'module': 0}
            )
            if doc:
                merged = dict(defaults)
                merged.update(doc.get('values', {}))
                return merged
        except Exception as e:
            logger.error(f'Failed to get config for {module_name}: {e}')

        return defaults

    def set_config(self, module_name: str, key: str, value):
        """Set a single config value for a module."""
        if not self._available:
            logger.warning('Cannot save config: MongoDB unavailable')
            return False

        try:
            self.collection.update_one(
                {'module': module_name},
                {'$set': {f'values.{key}': value}},
                upsert=True,
            )
            logger.debug(f'Config set: {module_name}.{key} = {value}')
            return True
        except Exception as e:
            logger.error(f'Failed to set config {module_name}.{key}: {e}')
            return False

    def set_config_bulk(self, module_name: str, values: dict):
        """Set multiple config values for a module at once."""
        if not self._available:
            return False

        try:
            update = {f'values.{k}': v for k, v in values.items()}
            self.collection.update_one(
                {'module': module_name},
                {'$set': update},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f'Failed to set config for {module_name}: {e}')
            return False

    def reset_config(self, module_name: str):
        """Reset a module's config to defaults (delete stored values)."""
        if not self._available:
            return False

        try:
            self.collection.delete_one({'module': module_name})
            return True
        except Exception as e:
            logger.error(f'Failed to reset config for {module_name}: {e}')
            return False

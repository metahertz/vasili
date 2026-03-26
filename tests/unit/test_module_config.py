"""Unit tests for ModuleConfigStore."""

import pytest
from unittest.mock import patch, MagicMock

from module_config import ModuleConfigStore


@pytest.fixture
def store():
    """Create a ModuleConfigStore with mocked MongoDB."""
    mock_client = MagicMock()
    mock_client.admin.command.return_value = {'ok': 1}
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_client.__getitem__ = MagicMock(return_value=mock_db)
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)

    mock_collection.find_one.return_value = None

    with patch('module_config.MongoClient', return_value=mock_client):
        s = ModuleConfigStore()
    s._collection = mock_collection
    return s


@pytest.mark.unit
class TestModuleConfigStore:
    def test_register_and_get_schema(self, store):
        store.register_schema('dns_probe', {'timeout': {'type': 'int', 'default': 5}})
        assert store.get_schema('dns_probe') == {'timeout': {'type': 'int', 'default': 5}}

    def test_get_defaults(self, store):
        store.register_schema('test', {
            'key1': {'type': 'int', 'default': 10},
            'key2': {'type': 'str', 'default': 'hello'},
        })
        assert store.get_defaults('test') == {'key1': 10, 'key2': 'hello'}

    def test_get_config_returns_defaults_when_no_stored(self, store):
        store.register_schema('test', {'key': {'type': 'int', 'default': 42}})
        config = store.get_config('test')
        assert config == {'key': 42}

    def test_get_all_schemas(self, store):
        store.register_schema('a', {'x': {'default': 1}})
        store.register_schema('b', {'y': {'default': 2}})
        schemas = store.get_all_schemas()
        assert 'a' in schemas
        assert 'b' in schemas

    def test_graceful_degradation(self):
        """Test that store works without MongoDB."""
        from pymongo.errors import ConnectionFailure
        with patch('module_config.MongoClient') as mock:
            mock.side_effect = ConnectionFailure('no mongo')
            store = ModuleConfigStore()

        assert store.is_available() is False
        store.register_schema('test', {'k': {'type': 'int', 'default': 5}})
        assert store.get_config('test') == {'k': 5}
        assert store.set_config('test', 'k', 10) is False

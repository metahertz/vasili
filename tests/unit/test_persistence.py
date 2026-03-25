"""Unit tests for ConnectionStore (persistence module) with MongoDB."""

import pytest
from unittest.mock import patch, MagicMock

from persistence import ConnectionStore


def _make_mock_store():
    """Create a ConnectionStore backed by a mock MongoDB collection."""
    mock_client = MagicMock()
    mock_client.admin.command.return_value = {'ok': 1}
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_client.__getitem__ = MagicMock(return_value=mock_db)
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)

    # In-memory store to simulate MongoDB
    _docs = []

    def _find_one(query, projection=None):
        for doc in _docs:
            if all(doc.get(k) == v for k, v in query.items()
                   if not k.startswith('$')):
                if projection and '_id' in projection and projection['_id'] == 0:
                    return {k: v for k, v in doc.items() if k != '_id'}
                return dict(doc)
        return None

    def _insert_one(doc):
        _docs.append(dict(doc))
        result = MagicMock()
        result.inserted_id = 'mock_id'
        return result

    def _update_one(query, update):
        for doc in _docs:
            if all(doc.get(k) == v for k, v in query.items()
                   if not k.startswith('$')):
                if '$set' in update:
                    doc.update(update['$set'])
                if '$inc' in update:
                    for k, v in update['$inc'].items():
                        doc[k] = doc.get(k, 0) + v
                result = MagicMock()
                result.modified_count = 1
                return result
        result = MagicMock()
        result.modified_count = 0
        return result

    def _find(query=None, projection=None):
        query = query or {}
        results = []
        for doc in _docs:
            match = True
            for k, v in query.items():
                if isinstance(v, dict) and '$gt' in v:
                    if not doc.get(k, 0) > v['$gt']:
                        match = False
                elif doc.get(k) != v:
                    match = False
            if match:
                d = dict(doc)
                if projection:
                    if '_id' in projection and projection['_id'] == 0:
                        d.pop('_id', None)
                    if 'password_hash' in projection and projection['password_hash'] == 0:
                        d.pop('password_hash', None)
                    # If there are inclusion projections (value=1), filter to those
                    includes = {k for k, v in projection.items() if v == 1}
                    if includes:
                        d = {k: v for k, v in d.items() if k in includes or k == '_id'}
                        d.pop('_id', None)
                results.append(d)
        # Return a mock cursor with sort/limit chaining
        cursor = MagicMock()
        cursor.__iter__ = MagicMock(return_value=iter(results))
        cursor.__next__ = MagicMock(side_effect=StopIteration)

        def _sort(field, direction):
            results.sort(key=lambda x: x.get(field, 0), reverse=(direction == -1))
            c2 = MagicMock()
            c2.__iter__ = MagicMock(return_value=iter(results))

            def _limit(n):
                limited = results[:n]
                c3 = MagicMock()
                c3.__iter__ = MagicMock(return_value=iter(limited))
                return c3
            c2.limit = _limit
            return c2
        cursor.sort = _sort
        return cursor

    def _delete_many(query):
        to_remove = []
        for i, doc in enumerate(_docs):
            if all(doc.get(k) == v for k, v in query.items()):
                to_remove.append(i)
        for i in reversed(to_remove):
            _docs.pop(i)
        result = MagicMock()
        result.deleted_count = len(to_remove)
        return result

    mock_collection.find_one = _find_one
    mock_collection.insert_one = _insert_one
    mock_collection.update_one = _update_one
    mock_collection.find = _find
    mock_collection.delete_many = _delete_many
    mock_collection.create_index = MagicMock()

    with patch('persistence.MongoClient', return_value=mock_client):
        store = ConnectionStore()
    store._docs = _docs  # expose for tests
    return store


@pytest.fixture
def store():
    """Create a ConnectionStore with mocked MongoDB."""
    return _make_mock_store()


@pytest.mark.unit
class TestConnectionStore:
    """Test suite for ConnectionStore."""

    def test_store_and_retrieve_network(self, store):
        """Test storing and retrieving a network."""
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF', 'WPA2', score=75.0)
        networks = store.get_known_networks()
        assert len(networks) == 1
        assert networks[0]['ssid'] == 'TestNet'
        assert networks[0]['avg_score'] == 75.0

    def test_update_existing_network(self, store):
        """Test that storing same network updates running averages."""
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF', score=70.0)
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF', score=80.0)
        networks = store.get_known_networks()
        assert len(networks) == 1
        assert networks[0]['success_count'] == 2
        assert networks[0]['avg_score'] == 75.0  # (70 + 80) / 2

    def test_store_failure(self, store):
        """Test storing a failed connection attempt."""
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF', success=False)
        networks = store.get_known_networks()
        assert len(networks) == 1
        assert networks[0]['fail_count'] == 1
        assert networks[0]['success_count'] == 0

    def test_is_known_network(self, store):
        """Test checking if a network is known."""
        assert not store.is_known_network('TestNet', 'AA:BB:CC:DD:EE:FF')
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF')
        assert store.is_known_network('TestNet', 'AA:BB:CC:DD:EE:FF')

    def test_delete_network(self, store):
        """Test deleting a saved network."""
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF')
        assert store.delete_network('TestNet') is True
        assert store.get_known_networks() == []
        assert store.delete_network('NonExistent') is False

    def test_get_best_networks(self, store):
        """Test getting best performing networks."""
        store.store_network('Low', 'AA:AA:AA:AA:AA:AA', score=30.0)
        store.store_network('High', 'BB:BB:BB:BB:BB:BB', score=90.0)
        store.store_network('Mid', 'CC:CC:CC:CC:CC:CC', score=60.0)

        best = store.get_best_networks(limit=2)
        assert len(best) == 2
        assert best[0]['ssid'] == 'High'
        assert best[1]['ssid'] == 'Mid'

    def test_get_network(self, store):
        """Test getting a specific network."""
        store.store_network('TestNet', 'AA:BB:CC:DD:EE:FF', 'WPA2')
        network = store.get_network('TestNet', 'AA:BB:CC:DD:EE:FF')
        assert network is not None
        assert network['encryption_type'] == 'WPA2'

        assert store.get_network('NonExistent', 'XX:XX:XX:XX:XX:XX') is None

    def test_multiple_bssids_same_ssid(self, store):
        """Test that same SSID with different BSSIDs are stored separately."""
        store.store_network('TestNet', 'AA:AA:AA:AA:AA:AA', score=70.0)
        store.store_network('TestNet', 'BB:BB:BB:BB:BB:BB', score=80.0)
        networks = store.get_known_networks()
        assert len(networks) == 2

    def test_graceful_degradation_when_unavailable(self):
        """Test that ConnectionStore degrades gracefully without MongoDB."""
        with patch('persistence.MongoClient') as mock_client:
            from pymongo.errors import ConnectionFailure
            mock_client.side_effect = ConnectionFailure('no mongo')
            store = ConnectionStore()

        assert store.is_available() is False
        assert store.get_known_networks() == []
        assert store.is_known_network('x', 'y') is False
        assert store.get_network('x', 'y') is None
        assert store.delete_network('x') is False
        assert store.get_best_networks() == []

"""Unit tests for KnownNetworksStore."""

import os
import stat
import pytest
from unittest.mock import patch, MagicMock

from known_networks_store import KnownNetworksStore


def _mock_collection():
    """In-memory fake of the subset of pymongo Collection that the store uses."""
    docs: list[dict] = []

    def _find_one(query, projection=None):
        for d in docs:
            if all(d.get(k) == v for k, v in query.items()):
                out = dict(d)
                if projection:
                    for k, v in projection.items():
                        if v == 0:
                            out.pop(k, None)
                return out
        return None

    def _update_one(query, update, upsert=False):
        for d in docs:
            if all(d.get(k) == v for k, v in query.items()):
                if '$set' in update:
                    d.update(update['$set'])
                return MagicMock(modified_count=1, upserted_id=None)
        if upsert:
            new_doc = {}
            new_doc.update(update.get('$setOnInsert', {}))
            new_doc.update(update.get('$set', {}))
            new_doc.update(query)
            docs.append(new_doc)
            return MagicMock(modified_count=0, upserted_id='mock')
        return MagicMock(modified_count=0, upserted_id=None)

    def _delete_one(query):
        for i, d in enumerate(docs):
            if all(d.get(k) == v for k, v in query.items()):
                docs.pop(i)
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def _find(query=None, projection=None):
        query = query or {}
        results = []
        for d in docs:
            if all(d.get(k) == v for k, v in query.items()):
                out = dict(d)
                if projection:
                    for k, v in projection.items():
                        if v == 0:
                            out.pop(k, None)
                results.append(out)
        cursor = MagicMock()
        cursor.__iter__ = lambda self=cursor: iter(results)
        return cursor

    coll = MagicMock()
    coll.find_one = _find_one
    coll.update_one = _update_one
    coll.delete_one = _delete_one
    coll.find = _find
    coll.create_index = MagicMock()
    coll._docs = docs
    return coll


@pytest.fixture
def store_factory(tmp_path):
    """Build a KnownNetworksStore with mocked Mongo and a temp key file."""
    def _make(key_path=None):
        if key_path is None:
            key_path = str(tmp_path / 'master.key')
        coll = _mock_collection()
        mock_client = MagicMock()
        mock_client.admin.command.return_value = {'ok': 1}
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=coll)
        mock_client.__getitem__ = MagicMock(return_value=mock_db)
        with patch('known_networks_store.MongoClient', return_value=mock_client):
            store = KnownNetworksStore(key_path=key_path)
        return store, coll
    return _make


@pytest.mark.unit
class TestKnownNetworksStore:

    def test_round_trip_add_and_get(self, store_factory):
        store, _ = store_factory()
        assert store.is_available()
        assert store.add('HomeNet', 'hunter2', security='WPA2', notes='lounge')
        entry = store.get('HomeNet')
        assert entry is not None
        assert entry['ssid'] == 'HomeNet'
        assert entry['password'] == 'hunter2'
        assert entry['security'] == 'WPA2'
        assert entry['notes'] == 'lounge'

    def test_list_all_redacts_password(self, store_factory):
        store, _ = store_factory()
        store.add('A', 'pw-a')
        store.add('B', 'pw-b')
        rows = store.list_all()
        assert len(rows) == 2
        for r in rows:
            assert r['password'] == '***'
            assert 'pw-a' not in str(r.values())
            assert 'pw-b' not in str(r.values())

    def test_password_encrypted_on_disk(self, store_factory):
        store, coll = store_factory()
        store.add('Secret', 'supersecret')
        raw = coll._docs[0]
        assert 'password_enc' in raw
        assert raw['password_enc'] != 'supersecret'
        assert 'supersecret' not in raw['password_enc']

    def test_remove(self, store_factory):
        store, _ = store_factory()
        store.add('Tmp', 'pw')
        assert store.remove('Tmp') is True
        assert store.get('Tmp') is None
        assert store.remove('Tmp') is False

    def test_reveal(self, store_factory):
        store, _ = store_factory()
        store.add('R', 'plain')
        assert store.reveal('R') == 'plain'
        assert store.reveal('missing') is None

    def test_key_file_mode_is_0600(self, tmp_path, store_factory):
        key_path = str(tmp_path / 'mk.key')
        store, _ = store_factory(key_path=key_path)
        assert os.path.exists(key_path)
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600, f'expected 0600, got {oct(mode)}'
        assert store.is_available()

    def test_key_file_persists_across_instances(self, tmp_path, store_factory):
        key_path = str(tmp_path / 'persist.key')
        store_a, _ = store_factory(key_path=key_path)
        store_a.add('Same', 'p@ss')

        # Second instance loads existing key and can decrypt the value
        # written by the first (using the same mocked collection contents
        # would require sharing state — easier to test the key reload via
        # raw read-back of the file's contents).
        with open(key_path, 'rb') as f:
            first_key = f.read()

        store_b, _ = store_factory(key_path=key_path)
        with open(key_path, 'rb') as f:
            second_key = f.read()
        assert first_key == second_key
        assert store_b.is_available()

    def test_mongo_unavailable_no_exception(self, tmp_path):
        key_path = str(tmp_path / 'mk.key')
        with patch('known_networks_store.MongoClient') as mc:
            mc.return_value.admin.command.side_effect = Exception('boom')
            store = KnownNetworksStore(key_path=key_path)
        assert store.is_available() is False
        # Calls don't raise
        assert store.get('Anything') is None
        assert store.list_all() == []
        assert store.add('X', 'y') is False
        assert store.remove('X') is False
        assert store.reveal('X') is None

    def test_empty_ssid_rejected(self, store_factory):
        store, _ = store_factory()
        assert store.add('', 'pw') is False
        assert store.get('') is None

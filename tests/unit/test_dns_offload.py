"""Unit tests for DNS offload client and crack stage."""

import importlib
import struct
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

from vasili import WifiNetwork, StageResult


def make_network(**kw):
    defaults = dict(
        ssid='TestWPA2', bssid='aa:bb:cc:dd:ee:ff', signal_strength=70,
        channel=6, encryption_type='WPA2', is_open=False,
    )
    defaults.update(kw)
    return WifiNetwork(**defaults)


def make_card(interface='wlan0', ip='10.0.0.5'):
    card = MagicMock()
    card.interface = interface
    card.get_ip_address.return_value = ip
    return card


def _load_server_module(name):
    """Import a server script with hyphens in filename."""
    server_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'server')
    path = os.path.join(server_dir, name)
    spec = importlib.util.spec_from_file_location(name.replace('-', '_').replace('.py', ''), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# DnsOffloadClient tests
# ---------------------------------------------------------------------------

class TestDnsOffloadClient:

    def _make_client(self):
        from modules.helpers.dns_offload import DnsOffloadClient
        return DnsOffloadClient(
            domain='crack.example.com',
            secret='testsecret',
            nameserver='8.8.8.8',
            source_ip='10.0.0.5',
            timeout=2,
        )

    def test_build_dns_query_a_record(self):
        client = self._make_client()
        query = client._build_dns_query('test.crack.example.com', qtype=1)
        assert len(query) > 12
        qdcount = struct.unpack('!H', query[4:6])[0]
        assert qdcount == 1

    def test_build_dns_query_txt_record(self):
        client = self._make_client()
        query = client._build_dns_query('job.status.test.crack.example.com', qtype=16)
        assert len(query) > 12

    def test_parse_a_response_roundtrip(self):
        cs = _load_server_module('vasili-crack-server.py')
        client = self._make_client()
        query = client._build_dns_query('test.crack.example.com')
        resp = cs.build_a_response(query, '1.0.0.1')
        ip = client._parse_a_response(resp)
        assert ip == '1.0.0.1'

    def test_parse_a_response_rejected(self):
        cs = _load_server_module('vasili-crack-server.py')
        client = self._make_client()
        query = client._build_dns_query('test.crack.example.com')
        resp = cs.build_a_response(query, '1.0.0.0')
        ip = client._parse_a_response(resp)
        assert ip == '1.0.0.0'

    def test_parse_txt_response_roundtrip(self):
        cs = _load_server_module('vasili-crack-server.py')
        client = self._make_client()
        query = client._build_dns_query('job.status.test.crack.example.com', qtype=16)
        resp = cs.build_txt_response(query, 'found MyPassword123')
        txt = client._parse_txt_response(resp)
        assert txt == 'found MyPassword123'

    def test_parse_status_text_found(self):
        from modules.helpers.dns_offload import DnsOffloadClient
        result = DnsOffloadClient._parse_status_text('found SecretPass')
        assert result['status'] == 'found'
        assert result['password'] == 'SecretPass'
        assert result['progress'] == 100

    def test_parse_status_text_working(self):
        from modules.helpers.dns_offload import DnsOffloadClient
        result = DnsOffloadClient._parse_status_text('working 45')
        assert result['status'] == 'working'
        assert result['progress'] == 45

    def test_parse_status_text_queued(self):
        from modules.helpers.dns_offload import DnsOffloadClient
        result = DnsOffloadClient._parse_status_text('queued')
        assert result['status'] == 'queued'

    def test_parse_status_text_exhausted(self):
        from modules.helpers.dns_offload import DnsOffloadClient
        result = DnsOffloadClient._parse_status_text('exhausted')
        assert result['status'] == 'exhausted'

    def test_submit_builds_correct_domain(self):
        client = self._make_client()
        captured = {}
        def mock_send(qname, qtype=1):
            captured['qname'] = qname
            return None
        client._send_query = mock_send
        client.submit_pmkid('aabbccdd' * 4, 'aabbccddeeff', '112233445566', '54657374')
        qname = captured['qname']
        assert 'submit' in qname
        assert 'crack.example.com' in qname

    def test_secret_truncated_to_8(self):
        from modules.helpers.dns_offload import DnsOffloadClient
        client = DnsOffloadClient('crack.example.com', 'verylongsecretkey', '8.8.8.8')
        assert client.secret == 'verylong'


# ---------------------------------------------------------------------------
# DnsOffloadCrackStage tests
# ---------------------------------------------------------------------------

class TestDnsOffloadCrackStage:

    def _make_stage(self, **cfg_overrides):
        from modules.stages.dns_offload_crack import DnsOffloadCrackStage
        stage = DnsOffloadCrackStage()
        cfg = {
            'offload_domain': 'crack.example.com',
            'offload_secret': 'testsecret',
            'poll_interval': 1,
            'poll_timeout': 5,
            'timeout': 2,
        }
        cfg.update(cfg_overrides)
        stage._stage_config = cfg
        return stage

    def test_can_run_needs_pmkid_captured(self):
        stage = self._make_stage()
        ctx = {'pmkid_captured': False, 'dns_reachable_udp': True,
               '_pmkid_hash_line': 'WPA*02*...'}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_can_run_skips_if_already_cracked(self):
        stage = self._make_stage()
        ctx = {'pmkid_captured': True, 'pmkid_cracked': True,
               'dns_reachable_udp': True, '_pmkid_hash_line': 'WPA*02*...'}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_can_run_needs_dns_reachable(self):
        stage = self._make_stage()
        ctx = {'pmkid_captured': True, 'pmkid_cracked': False,
               '_pmkid_hash_line': 'WPA*02*...'}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_can_run_needs_hash_line(self):
        stage = self._make_stage()
        ctx = {'pmkid_captured': True, 'dns_reachable_udp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_can_run_needs_domain(self):
        stage = self._make_stage(offload_domain='')
        ctx = {'pmkid_captured': True, 'dns_reachable_udp': True,
               '_pmkid_hash_line': 'WPA*02*...'}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_can_run_all_conditions_met(self):
        stage = self._make_stage()
        ctx = {'pmkid_captured': True, 'pmkid_cracked': False,
               'dns_reachable_udp': True,
               '_pmkid_hash_line': 'WPA*02*aabb*ccdd*eeff*5465*'}
        assert stage.can_run(make_network(), make_card(), ctx) is True

    def test_run_submit_rejected(self):
        stage = self._make_stage()
        ctx = {
            'pmkid_captured': True,
            '_pmkid_hash_line': 'WPA*02*aabbccdd*112233*445566*546573*',
            'reachable_dns_servers': ['8.8.8.8:53'],
        }
        mock_client = MagicMock()
        mock_client.submit_pmkid.return_value = None

        with patch('modules.helpers.dns_offload.DnsOffloadClient',
                   return_value=mock_client):
            result = stage.run(make_network(), make_card(), ctx)
        assert result.success is False

    def test_run_found_password(self):
        stage = self._make_stage()
        ctx = {
            'pmkid_captured': True,
            '_pmkid_hash_line': 'WPA*02*aabbccdd*112233*445566*546573*',
            'reachable_dns_servers': ['8.8.8.8:53'],
        }
        mock_client = MagicMock()
        mock_client.submit_pmkid.return_value = 'aabbccdd'
        mock_client.poll_status.return_value = {
            'status': 'found', 'password': 'MyPassword', 'progress': 100,
        }
        mock_card = make_card()
        mock_card.connect.return_value = True

        with patch('modules.helpers.dns_offload.DnsOffloadClient',
                   return_value=mock_client):
            result = stage.run(make_network(), mock_card, ctx)
        assert result.success is True
        assert result.context_updates.get('pmkid_cracked') is True

    def test_run_exhausted(self):
        stage = self._make_stage()
        ctx = {
            'pmkid_captured': True,
            '_pmkid_hash_line': 'WPA*02*aabbccdd*112233*445566*546573*',
            'reachable_dns_servers': ['8.8.8.8:53'],
        }
        mock_client = MagicMock()
        mock_client.submit_pmkid.return_value = 'aabbccdd'
        mock_client.poll_status.return_value = {
            'status': 'exhausted', 'password': None, 'progress': 0,
        }
        with patch('modules.helpers.dns_offload.DnsOffloadClient',
                   return_value=mock_client):
            result = stage.run(make_network(), make_card(), ctx)
        assert result.success is False
        assert result.context_updates.get('offload_exhausted') is True


# ---------------------------------------------------------------------------
# Crack Server + DNS Proxy wire format tests
# ---------------------------------------------------------------------------

class TestServerDnsParsing:

    def test_crack_server_parse_query(self):
        cs = _load_server_module('vasili-crack-server.py')
        from modules.helpers.dns_offload import DnsOffloadClient
        client = DnsOffloadClient('crack.example.com', 'sec', '8.8.8.8')
        raw = client._build_dns_query('test.submit.sec.crack.example.com')
        parsed = cs.parse_dns_query(raw)
        assert parsed['qname'] == 'test.submit.sec.crack.example.com'
        assert parsed['qtype'] == 1

    def test_a_response_roundtrip(self):
        cs = _load_server_module('vasili-crack-server.py')
        from modules.helpers.dns_offload import DnsOffloadClient
        client = DnsOffloadClient('crack.example.com', 'sec', '8.8.8.8')
        query = client._build_dns_query('test.crack.example.com')
        resp = cs.build_a_response(query, '1.0.0.1')
        assert client._parse_a_response(resp) == '1.0.0.1'

    def test_txt_response_roundtrip(self):
        cs = _load_server_module('vasili-crack-server.py')
        from modules.helpers.dns_offload import DnsOffloadClient
        client = DnsOffloadClient('crack.example.com', 'sec', '8.8.8.8')
        query = client._build_dns_query('j.status.sec.crack.example.com', qtype=16)
        resp = cs.build_txt_response(query, 'found SuperSecret')
        assert client._parse_txt_response(resp) == 'found SuperSecret'

    def test_dns_proxy_parse_qname(self):
        proxy = _load_server_module('vasili-dns-proxy.py')
        from modules.helpers.dns_offload import DnsOffloadClient
        client = DnsOffloadClient('crack.example.com', 'sec', '8.8.8.8')
        raw = client._build_dns_query('job.status.sec.crack.example.com')
        assert proxy.parse_qname(raw) == 'job.status.sec.crack.example.com'

    def test_dns_proxy_routes_crack_domain(self):
        proxy = _load_server_module('vasili-dns-proxy.py')
        from modules.helpers.dns_offload import DnsOffloadClient
        client = DnsOffloadClient('crack.example.com', 'sec', '8.8.8.8')
        raw = client._build_dns_query('abc.submit.sec.crack.example.com')
        assert proxy.parse_qname(raw).endswith('crack.example.com')

        raw2 = client._build_dns_query('t.tunnel.example.com')
        assert not proxy.parse_qname(raw2).endswith('crack.example.com')

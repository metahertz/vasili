"""Tests for the UDP/53 multiplexer classifier and flow table.

The proxy script lives at server/vasili-dns-proxy.py — filename has a
hyphen so it isn't importable as a normal module. We load it via
importlib.util.
"""

import importlib.util
import socket
import struct
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROXY_PATH = Path(__file__).resolve().parents[2] / 'server' / 'vasili-dns-proxy.py'


@pytest.fixture(scope='module')
def proxy():
    spec = importlib.util.spec_from_file_location('vasili_dns_proxy', PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# Synthetic packet builders
# ----------------------------------------------------------------------

def wg_packet(msg_type: int, length: int) -> bytes:
    """A packet that matches the WireGuard byte/length signature."""
    return bytes([msg_type, 0, 0, 0]) + b'\x00' * (length - 4)


def dns_query(qname: str, txn_id: int = 0x1234) -> bytes:
    """Minimal DNS query packet."""
    header = struct.pack(
        '!HHHHHH',
        txn_id,
        0x0100,  # flags: standard recursive query
        1, 0, 0, 0,
    )
    body = b''
    for label in qname.rstrip('.').split('.'):
        body += bytes([len(label)]) + label.encode('ascii')
    body += b'\x00'             # name terminator
    body += struct.pack('!HH', 1, 1)  # QTYPE=A, QCLASS=IN
    return header + body


# ----------------------------------------------------------------------
# WG signature detection
# ----------------------------------------------------------------------

def test_handshake_init_is_wg(proxy):
    assert proxy.looks_like_wireguard(wg_packet(1, 148))


def test_handshake_response_is_wg(proxy):
    assert proxy.looks_like_wireguard(wg_packet(2, 92))


def test_cookie_reply_is_wg(proxy):
    assert proxy.looks_like_wireguard(wg_packet(3, 64))


def test_transport_data_is_wg(proxy):
    # Type-4 transport data has a min length but no max.
    assert proxy.looks_like_wireguard(wg_packet(4, 32))
    assert proxy.looks_like_wireguard(wg_packet(4, 1500))


def test_wrong_wg_length_is_not_wg(proxy):
    # Handshake init MUST be exactly 148 bytes; 147 isn't WG.
    assert not proxy.looks_like_wireguard(wg_packet(1, 147))
    assert not proxy.looks_like_wireguard(wg_packet(2, 91))


def test_wrong_wg_reserved_bytes_is_not_wg(proxy):
    pkt = bytearray(wg_packet(1, 148))
    pkt[1] = 0xFF  # reserved byte must be zero
    assert not proxy.looks_like_wireguard(bytes(pkt))


def test_dns_query_is_not_wg(proxy):
    assert not proxy.looks_like_wireguard(dns_query('whatever.crack.test'))


def test_garbage_under_four_bytes_is_not_wg(proxy):
    assert not proxy.looks_like_wireguard(b'\x01\x00\x00')


# ----------------------------------------------------------------------
# Classifier
# ----------------------------------------------------------------------

def test_classify_wg(proxy):
    assert proxy.classify(wg_packet(1, 148), 'crack.test', 't.test') == 'wireguard'


def test_classify_crack_suffix(proxy):
    assert proxy.classify(dns_query('a.b.crack.test'), 'crack.test', 't.test') == 'crack'


def test_classify_iodine_suffix(proxy):
    assert proxy.classify(dns_query('abc.t.test'), 'crack.test', 't.test') == 'iodine'


def test_classify_unknown_domain_drops(proxy):
    assert proxy.classify(dns_query('example.com'), 'crack.test', 't.test') == 'drop'


def test_classify_unset_iodine_routes_only_crack(proxy):
    """When iodine_domain is empty, non-crack DNS is dropped."""
    assert proxy.classify(dns_query('example.com'), 'crack.test', '') == 'drop'
    assert proxy.classify(dns_query('a.crack.test'), 'crack.test', '') == 'crack'


def test_classify_short_packet_drops(proxy):
    # Less than a DNS header AND not a recognisable WG packet.
    assert proxy.classify(b'\x00\x00', 'crack.test', 't.test') == 'drop'


def test_classify_garbage_drops(proxy):
    assert proxy.classify(b'\xff' * 100, 'crack.test', 't.test') == 'drop'


# ----------------------------------------------------------------------
# parse_qname guards
# ----------------------------------------------------------------------

def test_parse_qname_strips_to_lowercase(proxy):
    assert proxy.parse_qname(dns_query('Foo.Crack.Test')) == 'foo.crack.test'


def test_parse_qname_truncated_returns_empty(proxy):
    pkt = dns_query('a.crack.test')[:14]  # cut mid-label
    assert proxy.parse_qname(pkt) == ''


def test_parse_qname_pointer_compression_bails(proxy):
    pkt = b'\x12\x34' + b'\x01\x00' + b'\x00\x01' * 4
    pkt += b'\xc0\x0c'  # pointer at offset 12
    assert proxy.parse_qname(pkt) == ''


# ----------------------------------------------------------------------
# FlowTable
# ----------------------------------------------------------------------

class _FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_flow_table_creates_socket_per_client(proxy):
    clock = _FakeClock(100.0)
    ft = proxy.FlowTable(('127.0.0.1', 55555), idle_timeout=60, clock=clock)
    f1 = ft.get_or_create(('1.2.3.4', 1000))
    f2 = ft.get_or_create(('5.6.7.8', 2000))
    assert f1['sock'] is not f2['sock']
    # Same client returns the same socket.
    assert ft.get_or_create(('1.2.3.4', 1000))['sock'] is f1['sock']


def test_flow_table_evicts_idle(proxy):
    clock = _FakeClock(100.0)
    ft = proxy.FlowTable(('127.0.0.1', 55555), idle_timeout=30, clock=clock)
    ft.get_or_create(('1.2.3.4', 1000))
    ft.get_or_create(('5.6.7.8', 2000))

    clock.t = 110.0  # only 10s elapsed — nothing evicted
    assert ft.evict_idle() == []
    assert len(ft.by_client) == 2

    clock.t = 200.0  # 100s elapsed — both gone
    evicted = ft.evict_idle()
    assert set(evicted) == {('1.2.3.4', 1000), ('5.6.7.8', 2000)}
    assert ft.by_client == {}


def test_flow_table_client_for_sock_fd_roundtrip(proxy):
    ft = proxy.FlowTable(('127.0.0.1', 55555))
    addr = ('9.9.9.9', 4444)
    f = ft.get_or_create(addr)
    assert ft.client_for_sock_fd(f['sock'].fileno()) == addr


def test_flow_table_touch_resets_idle(proxy):
    clock = _FakeClock(100.0)
    ft = proxy.FlowTable(('127.0.0.1', 55555), idle_timeout=30, clock=clock)
    addr = ('1.1.1.1', 1)
    ft.get_or_create(addr)
    clock.t = 125.0
    ft.touch(addr)
    clock.t = 140.0  # 15s since last touch, still under 30s window
    assert ft.evict_idle() == []
    clock.t = 200.0
    assert ft.evict_idle() == [addr]


# ----------------------------------------------------------------------
# End-to-end: spin up the proxy and three socket sinks
# ----------------------------------------------------------------------

@pytest.fixture
def proxy_with_sinks(proxy):
    """Start the proxy on a random localhost port with three sink sockets."""
    crack = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    crack.bind(('127.0.0.1', 0))
    iodine = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    iodine.bind(('127.0.0.1', 0))
    wg = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wg.bind(('127.0.0.1', 0))

    proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    proxy_sock.bind(('127.0.0.1', 0))
    proxy_port = proxy_sock.getsockname()[1]
    proxy_sock.close()  # release so serve() can bind it

    conf = {
        'listen': '127.0.0.1',
        'port': proxy_port,
        'crack_backend':     f'127.0.0.1:{crack.getsockname()[1]}',
        'iodine_backend':    f'127.0.0.1:{iodine.getsockname()[1]}',
        'wireguard_backend': f'127.0.0.1:{wg.getsockname()[1]}',
        'crack_domain':  'crack.test',
        'iodine_domain': 't.test',
    }

    thread = threading.Thread(target=proxy.serve, args=(conf,), daemon=True)
    thread.start()
    time.sleep(0.2)  # let the proxy bind

    yield {
        'port': proxy_port,
        'crack': crack,
        'iodine': iodine,
        'wg': wg,
    }

    # The serve loop is a daemon thread — it dies with the process.
    for s in (crack, iodine, wg):
        s.close()


def test_e2e_wg_packet_routed_to_wg_sink(proxy_with_sinks):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(('127.0.0.1', 0))
    sender.sendto(wg_packet(1, 148), ('127.0.0.1', proxy_with_sinks['port']))
    proxy_with_sinks['wg'].settimeout(2.0)
    data, _ = proxy_with_sinks['wg'].recvfrom(4096)
    assert data[:4] == b'\x01\x00\x00\x00'
    assert len(data) == 148
    sender.close()


def test_e2e_crack_query_routed_to_crack_sink(proxy_with_sinks):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(('127.0.0.1', 0))
    sender.sendto(dns_query('abc.crack.test'), ('127.0.0.1', proxy_with_sinks['port']))
    proxy_with_sinks['crack'].settimeout(2.0)
    data, _ = proxy_with_sinks['crack'].recvfrom(4096)
    assert proxy.__module__ or True  # smoke
    assert b'crack' in data.lower()
    sender.close()


def test_e2e_iodine_query_routed_to_iodine_sink(proxy_with_sinks):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(('127.0.0.1', 0))
    sender.sendto(dns_query('abc.t.test'), ('127.0.0.1', proxy_with_sinks['port']))
    proxy_with_sinks['iodine'].settimeout(2.0)
    data, _ = proxy_with_sinks['iodine'].recvfrom(4096)
    assert b't' in data.lower()
    sender.close()


def test_e2e_garbage_packet_dropped(proxy_with_sinks):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(('127.0.0.1', 0))
    sender.sendto(b'\xff' * 50, ('127.0.0.1', proxy_with_sinks['port']))
    for sink in (proxy_with_sinks['crack'], proxy_with_sinks['iodine'],
                 proxy_with_sinks['wg']):
        sink.settimeout(0.5)
        with pytest.raises(socket.timeout):
            sink.recvfrom(4096)
    sender.close()

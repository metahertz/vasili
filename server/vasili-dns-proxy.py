#!/usr/bin/env python3
"""Vasili UDP/53 multiplexer.

Sits in front of three services sharing a single public UDP/53:

  - WireGuard (binary protocol, type byte 1..4 then three zero bytes)
  - iodine    (DNS-shaped traffic whose qname suffix matches iodine_domain)
  - crack     (DNS-shaped traffic whose qname suffix matches crack_domain)

Classification order: WG first (byte+length signature is cheap and
unambiguous), then DNS parse + suffix match. Everything else is dropped.

WireGuard is a stateful protocol — peer sessions are keyed by source
ip+port — so the proxy maintains per-flow sockets connected to the WG
backend and shuttles replies back to the original sender. iodine and
crack are request/response DNS, so a fire-and-forget ephemeral socket
per packet is sufficient (and matches the previous proxy's behaviour).

Backwards compatibility: the legacy ``tunnel_backend`` config key (used
when only one of iodine/WG was enabled) is honoured as a fallback for
DNS-shaped traffic when ``iodine_backend`` is unset. New deployments
should set the per-service backends explicitly.
"""

import argparse
import json
import os
import select
import socket
import struct
import sys
import time

CONF_PATH = '/etc/vasili/dns-proxy.json'
DEFAULT_LISTEN = '0.0.0.0'
DEFAULT_PORT = 53

# WireGuard message-type bytes (first byte of every packet).
WG_HANDSHAKE_INIT = 1
WG_HANDSHAKE_RESP = 2
WG_COOKIE_REPLY = 3
WG_TRANSPORT_DATA = 4

# Exact / minimum length per WG type. Numbers from the WireGuard
# protocol spec (handshake_initiation = 148, handshake_response = 92,
# cookie_reply = 64, transport_data ≥ 32). Length matching keeps the
# classifier from being fooled by a DNS query whose first four bytes
# happen to coincide with a WG signature.
WG_LENGTHS = {
    WG_HANDSHAKE_INIT: (148, 148),
    WG_HANDSHAKE_RESP: (92, 92),
    WG_COOKIE_REPLY:   (64, 64),
    WG_TRANSPORT_DATA: (32, 65535),
}

# How long a WG flow can sit idle before its outbound socket is reaped.
FLOW_IDLE_SECONDS = 180


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------

def looks_like_wireguard(data: bytes) -> bool:
    """True if ``data`` matches a WireGuard message signature.

    Cheap byte/length check designed to be unambiguous against DNS
    traffic — a legitimate DNS query would have to carry a transaction
    ID of 0x01..0x04 followed by flag bytes of 0x00 0x00 *and* match
    the WG length window exactly. Real-world collisions are practically
    zero.
    """
    if len(data) < 4:
        return False
    msg_type = data[0]
    if msg_type not in WG_LENGTHS:
        return False
    if data[1:4] != b'\x00\x00\x00':
        return False
    lo, hi = WG_LENGTHS[msg_type]
    return lo <= len(data) <= hi


def parse_qname(data: bytes) -> str:
    """Extract the query name from a raw DNS packet (wire format).

    Returns the FQDN in lowercase dotted notation, or '' if the packet
    isn't a parseable DNS query.
    """
    if len(data) < 12:
        return ''
    labels = []
    offset = 12  # skip 12-byte DNS header
    terminated = False
    while offset < len(data):
        length = data[offset]
        if length == 0:
            terminated = True
            break
        if length & 0xC0:
            # Pointer compression — legal in DNS responses but not in
            # the question section, so for a query parser bail out.
            return ''
        offset += 1
        if offset + length > len(data):
            return ''
        try:
            labels.append(data[offset:offset + length].decode('ascii'))
        except UnicodeDecodeError:
            return ''
        offset += length
    if not terminated:
        return ''
    return '.'.join(labels).lower()


def classify(data: bytes, crack_domain: str, iodine_domain: str) -> str:
    """Return one of 'wireguard', 'crack', 'iodine', 'drop'."""
    if looks_like_wireguard(data):
        return 'wireguard'
    qname = parse_qname(data)
    if not qname:
        return 'drop'
    if crack_domain and qname.endswith(crack_domain):
        return 'crack'
    if iodine_domain and qname.endswith(iodine_domain):
        return 'iodine'
    return 'drop'


# ----------------------------------------------------------------------
# Backends — addr parsing + forwarding helpers
# ----------------------------------------------------------------------

def parse_backend(spec: str):
    """'host:port' -> (host, int(port)). Empty string -> None."""
    if not spec:
        return None
    host, port = spec.rsplit(':', 1)
    return host, int(port)


def forward_oneshot(listen_sock, data, backend, client_addr, timeout=5.0):
    """Forward a DNS-shaped packet and relay the single response back.

    Used for crack + iodine where the protocol is request/response.
    Opens a fresh ephemeral socket per packet, blocks up to ``timeout``
    waiting for the reply, then closes. Errors are logged and the
    client just times out client-side — same behaviour as the previous
    single-tunnel proxy.
    """
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fwd.settimeout(timeout)
    try:
        fwd.sendto(data, backend)
        resp, _ = fwd.recvfrom(4096)
        listen_sock.sendto(resp, client_addr)
    except socket.timeout:
        pass
    except Exception as exc:
        print(f'[proxy] forward error to {backend}: {exc}', file=sys.stderr)
    finally:
        fwd.close()


# ----------------------------------------------------------------------
# WireGuard flow table
# ----------------------------------------------------------------------

class FlowTable:
    """Per-client outbound sockets for the WireGuard backend.

    Each ``(client_ip, client_port)`` gets a dedicated UDP socket
    connected to the WG backend. The select loop also watches these
    sockets so reply packets can be sent back to the original client.
    Flows are evicted after ``FLOW_IDLE_SECONDS`` of inactivity.
    """

    def __init__(self, backend, idle_timeout=FLOW_IDLE_SECONDS, clock=time.monotonic):
        self.backend = backend
        self.idle_timeout = idle_timeout
        self.clock = clock
        self.by_client = {}      # (ip, port) -> {sock, last_seen}
        self.by_sock_fd = {}     # int fd -> (ip, port)

    def get_or_create(self, client_addr):
        flow = self.by_client.get(client_addr)
        if flow is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.connect(self.backend)
            flow = {'sock': sock, 'last_seen': self.clock()}
            self.by_client[client_addr] = flow
            self.by_sock_fd[sock.fileno()] = client_addr
        else:
            flow['last_seen'] = self.clock()
        return flow

    def client_for_sock_fd(self, fd):
        return self.by_sock_fd.get(fd)

    def touch(self, client_addr):
        flow = self.by_client.get(client_addr)
        if flow:
            flow['last_seen'] = self.clock()

    def sockets(self):
        return [f['sock'] for f in self.by_client.values()]

    def evict_idle(self):
        now = self.clock()
        stale = [addr for addr, flow in self.by_client.items()
                 if now - flow['last_seen'] > self.idle_timeout]
        for addr in stale:
            self._remove(addr)
        return stale

    def _remove(self, client_addr):
        flow = self.by_client.pop(client_addr, None)
        if not flow:
            return
        fd = flow['sock'].fileno()
        self.by_sock_fd.pop(fd, None)
        try:
            flow['sock'].close()
        except OSError:
            pass


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

def serve(conf: dict):
    listen = conf.get('listen', DEFAULT_LISTEN)
    port = int(conf.get('port', DEFAULT_PORT))
    crack_domain = (conf.get('crack_domain') or '').lower().rstrip('.')
    iodine_domain = (conf.get('iodine_domain') or '').lower().rstrip('.')

    crack_backend = parse_backend(conf.get('crack_backend', ''))
    iodine_backend = parse_backend(
        conf.get('iodine_backend') or conf.get('tunnel_backend', '')
    )
    wg_backend = parse_backend(conf.get('wireguard_backend', ''))

    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_sock.bind((listen, port))
    listen_sock.setblocking(False)
    print(f'[proxy] listening on {listen}:{port}')
    print(f'[proxy] crack:     *.{crack_domain or "(unset)"} -> {crack_backend}')
    print(f'[proxy] iodine:    *.{iodine_domain or "(unset)"} -> {iodine_backend}')
    print(f'[proxy] wireguard: <packet sig>     -> {wg_backend}')

    flows = FlowTable(wg_backend) if wg_backend else None

    while True:
        readers = [listen_sock]
        if flows:
            readers.extend(flows.sockets())
        try:
            ready, _, _ = select.select(readers, [], [], 5.0)
        except (KeyboardInterrupt, InterruptedError):
            break

        for sock in ready:
            if sock is listen_sock:
                try:
                    data, client_addr = sock.recvfrom(4096)
                except OSError:
                    continue
                _handle_inbound(
                    listen_sock, data, client_addr,
                    crack_backend, iodine_backend, wg_backend,
                    crack_domain, iodine_domain, flows,
                )
            else:
                # Reply from a WG flow socket — relay back to client.
                client_addr = flows.client_for_sock_fd(sock.fileno()) if flows else None
                if client_addr is None:
                    continue
                try:
                    data = sock.recv(4096)
                except OSError:
                    continue
                listen_sock.sendto(data, client_addr)
                flows.touch(client_addr)

        if flows:
            flows.evict_idle()

    listen_sock.close()


def _handle_inbound(listen_sock, data, client_addr,
                    crack_backend, iodine_backend, wg_backend,
                    crack_domain, iodine_domain, flows):
    verdict = classify(data, crack_domain, iodine_domain)
    if verdict == 'wireguard':
        if not wg_backend or flows is None:
            return
        flow = flows.get_or_create(client_addr)
        try:
            flow['sock'].send(data)
        except OSError as exc:
            print(f'[proxy] wg send error: {exc}', file=sys.stderr)
    elif verdict == 'crack':
        if crack_backend:
            forward_oneshot(listen_sock, data, crack_backend, client_addr)
    elif verdict == 'iodine':
        if iodine_backend:
            forward_oneshot(listen_sock, data, iodine_backend, client_addr)
    # else: drop silently


# ----------------------------------------------------------------------
# Config + CLI
# ----------------------------------------------------------------------

def load_config() -> dict:
    conf = {
        'listen': DEFAULT_LISTEN,
        'port': DEFAULT_PORT,
        'crack_backend': '',
        'iodine_backend': '',
        'wireguard_backend': '',
        'crack_domain': '',
        'iodine_domain': '',
    }
    if os.path.isfile(CONF_PATH):
        try:
            with open(CONF_PATH) as f:
                conf.update(json.load(f))
        except Exception as exc:
            print(f'[proxy] config load error: {exc}', file=sys.stderr)
    return conf


def main():
    parser = argparse.ArgumentParser(description='Vasili UDP/53 multiplexer')
    parser.add_argument('--listen')
    parser.add_argument('--port', type=int)
    parser.add_argument('--crack-backend')
    parser.add_argument('--iodine-backend')
    parser.add_argument('--wireguard-backend')
    parser.add_argument('--crack-domain')
    parser.add_argument('--iodine-domain')
    # Legacy alias for older configs that called everything-non-crack
    # the "tunnel" backend.
    parser.add_argument('--tunnel-backend',
                        help='(deprecated) alias for --iodine-backend')
    args = parser.parse_args()

    conf = load_config()
    for k in ('listen', 'port', 'crack_backend', 'iodine_backend',
              'wireguard_backend', 'crack_domain', 'iodine_domain',
              'tunnel_backend'):
        v = getattr(args, k, None)
        if v is not None:
            conf[k] = v

    serve(conf)


if __name__ == '__main__':
    main()

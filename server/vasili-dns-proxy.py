#!/usr/bin/env python3
"""Vasili DNS Proxy — domain-routing UDP proxy on port 53.

Sits in front of multiple DNS-based services on the same box:
  - Queries for *.crack.example.com → crack server (127.0.0.1:5353)
  - Everything else → tunnel backend: iodine or WireGuard (127.0.0.1:5354)

Only parses enough of the DNS wire format to extract the query name.
"""

import argparse
import json
import os
import socket
import struct
import sys

CONF_PATH = '/etc/vasili/dns-proxy.json'
DEFAULT_LISTEN = '0.0.0.0'
DEFAULT_PORT = 53
DEFAULT_CRACK_BACKEND = '127.0.0.1:5353'
DEFAULT_TUNNEL_BACKEND = '127.0.0.1:5354'


def parse_qname(data: bytes) -> str:
    """Extract the query name from a raw DNS packet (wire format).

    Returns the fully qualified domain name in lowercase dotted notation.
    """
    labels = []
    offset = 12  # skip DNS header (12 bytes)
    while offset < len(data):
        length = data[offset]
        if length == 0:
            break
        if length & 0xC0:
            # Pointer — shouldn't appear in queries, but bail
            break
        offset += 1
        labels.append(data[offset:offset + length].decode('ascii', errors='replace'))
        offset += length
    return '.'.join(labels).lower()


def forward(sock: socket.socket, data: bytes, backend: str,
            client_addr: tuple, timeout: float = 5.0):
    """Forward a DNS packet to a backend and relay the response."""
    host, port = backend.rsplit(':', 1)
    port = int(port)

    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fwd.settimeout(timeout)
    try:
        fwd.sendto(data, (host, port))
        resp, _ = fwd.recvfrom(4096)
        sock.sendto(resp, client_addr)
    except socket.timeout:
        pass  # silently drop — client will retry
    except Exception as exc:
        print(f'[proxy] forward error: {exc}', file=sys.stderr)
    finally:
        fwd.close()


def load_config() -> dict:
    """Load proxy config from JSON file, with defaults."""
    conf = {
        'listen': DEFAULT_LISTEN,
        'port': DEFAULT_PORT,
        'crack_backend': DEFAULT_CRACK_BACKEND,
        'tunnel_backend': DEFAULT_TUNNEL_BACKEND,
        'crack_domain': '',
    }
    if os.path.isfile(CONF_PATH):
        try:
            with open(CONF_PATH) as f:
                conf.update(json.load(f))
        except Exception as exc:
            print(f'[proxy] config load error: {exc}', file=sys.stderr)
    return conf


def serve(conf: dict):
    listen = conf['listen']
    port = conf['port']
    crack_backend = conf['crack_backend']
    tunnel_backend = conf['tunnel_backend']
    crack_domain = conf.get('crack_domain', '').lower().rstrip('.')

    if not crack_domain:
        print('[proxy] WARNING: no crack_domain configured — all traffic goes to tunnel backend',
              file=sys.stderr)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((listen, port))
    print(f'[proxy] listening on {listen}:{port}')
    print(f'[proxy] crack: *.{crack_domain} -> {crack_backend}')
    print(f'[proxy] tunnel: * -> {tunnel_backend}')

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            if len(data) < 12:
                continue

            qname = parse_qname(data)

            if crack_domain and qname.endswith(crack_domain):
                forward(sock, data, crack_backend, addr)
            else:
                forward(sock, data, tunnel_backend, addr)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f'[proxy] error: {exc}', file=sys.stderr)

    sock.close()


def main():
    parser = argparse.ArgumentParser(description='Vasili DNS Proxy')
    parser.add_argument('--listen', default=None, help='Listen address')
    parser.add_argument('--port', type=int, default=None, help='Listen port')
    parser.add_argument('--crack-backend', default=None)
    parser.add_argument('--tunnel-backend', default=None)
    parser.add_argument('--crack-domain', default=None)
    args = parser.parse_args()

    conf = load_config()
    # CLI args override config file
    if args.listen:
        conf['listen'] = args.listen
    if args.port:
        conf['port'] = args.port
    if args.crack_backend:
        conf['crack_backend'] = args.crack_backend
    if args.tunnel_backend:
        conf['tunnel_backend'] = args.tunnel_backend
    if args.crack_domain:
        conf['crack_domain'] = args.crack_domain

    serve(conf)


if __name__ == '__main__':
    main()

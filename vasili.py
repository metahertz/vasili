#!/usr/bin/env python3
# Main application entry point
# Modules are loaded dynamically from the modules directory

import collections
import concurrent.futures
import importlib
import inspect
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

try:
    import iptc
except ImportError:
    iptc = None
import netifaces
import speedtest
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure
from datetime import datetime

from config import VasiliConfig, apply_logging_config, load_config
from logging_config import setup_logging, get_logger
from persistence import ConnectionStore
from notifications import NotificationManager, NotificationEvent
from bandwidth import BandwidthMonitor
from module_config import ModuleConfigStore
from known_networks_store import KnownNetworksStore
from consent import ConsentManager
from mac_manager import MacManager
import network_isolation

# Configure logging with structured output, configurable levels, and file support
# Set VASILI_LOG_LEVEL, VASILI_LOG_FILE, VASILI_LOG_FORMAT environment variables to customize
setup_logging()
logger = get_logger(__name__)

# Global config - loaded at startup
_config: Optional[VasiliConfig] = None


def get_config() -> VasiliConfig:
    """Get the current configuration."""
    global _config
    if _config is None:
        _config = load_config()
        apply_logging_config(_config)
    return _config


# Custom exceptions for better error handling
class VasiliError(Exception):
    """Base exception for Vasili errors."""

    pass


class NoWifiCardsError(VasiliError):
    """Raised when no WiFi cards are available."""

    pass


class NoWifiCardsAvailableError(VasiliError):
    """Raised when all WiFi cards are in use."""

    pass


class WifiConnectionError(VasiliError):
    """Raised when a connection operation fails."""

    pass


class ScanError(VasiliError):
    """Raised when a network scan operation fails."""

    pass


class BridgeError(VasiliError):
    """Raised when bridge setup fails."""

    pass


class ModuleLoadError(VasiliError):
    """Raised when a connection module fails to load."""

    pass


# System health status
class SystemHealth:
    """Tracks overall system health and degraded states."""

    def __init__(self):
        self.wifi_cards_available = False
        self.modules_loaded = False
        self.scanning_operational = False
        self.last_error: Optional[str] = None
        self.degraded_mode = False
        self.degradation_reasons: list[str] = []

    def update_card_status(self, cards_count: int):
        self.wifi_cards_available = cards_count > 0
        if not self.wifi_cards_available:
            self._add_degradation('No WiFi cards detected')
        else:
            self._remove_degradation('No WiFi cards detected')

    def update_module_status(self, modules_count: int):
        self.modules_loaded = modules_count > 0
        if not self.modules_loaded:
            self._add_degradation('No connection modules loaded')
        else:
            self._remove_degradation('No connection modules loaded')

    def update_scan_status(self, operational: bool, error: Optional[str] = None):
        self.scanning_operational = operational
        if not operational:
            reason = f'Scanning failed: {error}' if error else 'Scanning not operational'
            self._add_degradation(reason)
        else:
            # Remove any scanning-related degradation
            self.degradation_reasons = [
                r for r in self.degradation_reasons if not r.startswith('Scanning')
            ]
            self._update_degraded_mode()

    def set_error(self, error: str):
        self.last_error = error
        logger.error(f'System error: {error}')

    def clear_error(self):
        self.last_error = None

    def _add_degradation(self, reason: str):
        if reason not in self.degradation_reasons:
            self.degradation_reasons.append(reason)
        self._update_degraded_mode()

    def _remove_degradation(self, reason: str):
        if reason in self.degradation_reasons:
            self.degradation_reasons.remove(reason)
        self._update_degraded_mode()

    def _update_degraded_mode(self):
        self.degraded_mode = len(self.degradation_reasons) > 0

    def to_dict(self) -> dict:
        return {
            'wifi_cards_available': self.wifi_cards_available,
            'modules_loaded': self.modules_loaded,
            'scanning_operational': self.scanning_operational,
            'last_error': self.last_error,
            'degraded_mode': self.degraded_mode,
            'degradation_reasons': self.degradation_reasons,
        }

    def is_operational(self) -> bool:
        """Check if system can perform basic operations."""
        return self.wifi_cards_available and self.modules_loaded


@dataclass
class WifiNetwork:
    ssid: str
    bssid: str
    signal_strength: int
    channel: int
    encryption_type: str
    is_open: bool
    uncloaked: bool = False  # True if SSID was resolved from a hidden network


def network_group_key(network: 'WifiNetwork', group_by_ssid: bool = True) -> str:
    """Return the logical grouping key for a network.

    When ``group_by_ssid`` is True (the default) and the network has a
    non-empty SSID, every access point broadcasting that SSID shares a key
    (``ssid``) and is treated as one logical network for enabling actions and
    for pass/fail dedup. Otherwise the key is per access point
    (``"ssid|bssid"``).
    """
    ssid = getattr(network, 'ssid', '') or ''
    if group_by_ssid and ssid:
        return ssid
    bssid = getattr(network, 'bssid', '') or ''
    return f'{ssid}|{bssid}'


@dataclass
class ConnectionResult:
    network: WifiNetwork
    download_speed: float
    upload_speed: float
    ping: float
    connected: bool
    connection_method: str
    interface: str
    # Set when an operator force-bridges this network (Bridge Override). A
    # pinned entry is exempt from auto-selector switching, auto-bridge
    # replacement, and reconcile teardown until manually unbridged.
    pinned: bool = False

    def calculate_score(self) -> float:
        """
        Calculate a connection quality score (0-100) based on:
        - Download speed (40% weight)
        - Signal strength (30% weight)
        - Upload speed (20% weight)
        - Ping/latency (10% weight - lower is better)

        Returns:
            A score from 0 to 100, where higher is better
        """
        # Normalize download speed (assume 100 Mbps as reference excellent speed)
        download_score = min(100, (self.download_speed / 100.0) * 100)

        # Signal strength is already 0-100
        signal_score = self.network.signal_strength

        # Normalize upload speed (assume 50 Mbps as reference excellent speed)
        upload_score = min(100, (self.upload_speed / 50.0) * 100)

        # Normalize ping (lower is better, 0ms=100, 200ms+=0)
        ping_score = max(0, 100 - (self.ping / 2.0))

        # Weighted average
        total_score = (
            download_score * 0.4 + signal_score * 0.3 + upload_score * 0.2 + ping_score * 0.1
        )

        return round(total_score, 2)


@dataclass
class StageResult:
    """Result from a pipeline stage execution."""
    success: bool
    has_internet: bool  # True = full internet works, pipeline can stop
    context_updates: dict  # Data to pass to subsequent stages
    message: str = ''
    stop_pipeline: bool = False  # True = abort pipeline (e.g. no WiFi association)


@dataclass
class StrategyResult:
    """Outcome of a parallel strategy, optionally enriched with speedtest data."""
    stage_name: str
    stage_result: StageResult
    context_updates: dict
    download_speed: float = 0.0
    upload_speed: float = 0.0
    ping: float = 0.0


class PerformanceMetricsStore:
    """
    Store and retrieve WiFi connection performance metrics in MongoDB.
    Gracefully handles MongoDB unavailability.
    """

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/', db_name: str = 'vasili'):
        """
        Initialize the metrics store.

        Args:
            mongo_uri: MongoDB connection URI
            db_name: Database name to use
        """
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.client: Optional[MongoClient] = None
        self.db = None
        self.metrics_collection = None
        self._available = False

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            # Test connection
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.metrics_collection = self.db['connection_metrics']
            self._available = True
            logger.info(f'Connected to MongoDB at {mongo_uri}')

            # Create indexes for efficient queries
            self.metrics_collection.create_index([('ssid', 1), ('timestamp', DESCENDING)])
            self.metrics_collection.create_index([('bssid', 1)])
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available: {e}. Metrics storage disabled.')
            self._available = False
        except Exception as e:
            logger.error(f'Failed to initialize MongoDB: {e}. Metrics storage disabled.')
            self._available = False

    def is_available(self) -> bool:
        """Check if MongoDB is available."""
        return self._available

    def store_metrics(self, connection: ConnectionResult) -> bool:
        """
        Store connection metrics to MongoDB.

        Args:
            connection: ConnectionResult containing performance data

        Returns:
            True if stored successfully, False otherwise
        """
        if not self._available:
            return False

        try:
            metric = {
                'ssid': connection.network.ssid,
                'bssid': connection.network.bssid,
                'signal_strength': connection.network.signal_strength,
                'channel': connection.network.channel,
                'encryption_type': connection.network.encryption_type,
                'download_speed': connection.download_speed,
                'upload_speed': connection.upload_speed,
                'ping': connection.ping,
                'connection_method': connection.connection_method,
                'interface': connection.interface,
                'score': connection.calculate_score(),
                'timestamp': time.time(),
                'connected': connection.connected,
            }

            self.metrics_collection.insert_one(metric)
            logger.debug(f'Stored metrics for {connection.network.ssid} (score: {metric["score"]})')
            return True

        except Exception as e:
            logger.error(f'Failed to store metrics: {e}')
            return False

    def get_network_history(self, ssid: str, limit: int = 10) -> list[dict]:
        """
        Get historical metrics for a specific network.

        Args:
            ssid: Network SSID to query
            limit: Maximum number of records to return

        Returns:
            List of metric dictionaries, newest first
        """
        if not self._available:
            return []

        try:
            cursor = (
                self.metrics_collection.find({'ssid': ssid})
                .sort('timestamp', DESCENDING)
                .limit(limit)
            )
            return list(cursor)
        except Exception as e:
            logger.error(f'Failed to retrieve network history: {e}')
            return []

    def get_average_score(self, ssid: str) -> Optional[float]:
        """
        Calculate average score for a network based on historical data.

        Args:
            ssid: Network SSID

        Returns:
            Average score or None if no data available
        """
        if not self._available:
            return None

        try:
            pipeline = [
                {'$match': {'ssid': ssid, 'connected': True}},
                {'$group': {'_id': '$ssid', 'avg_score': {'$avg': '$score'}, 'count': {'$sum': 1}}},
            ]
            result = list(self.metrics_collection.aggregate(pipeline))
            if result:
                return round(result[0]['avg_score'], 2)
            return None
        except Exception as e:
            logger.error(f'Failed to calculate average score: {e}')
            return None

    def get_best_networks(self, limit: int = 5) -> list[dict]:
        """
        Get the best performing networks based on average scores.

        Args:
            limit: Number of networks to return

        Returns:
            List of networks with their average scores
        """
        if not self._available:
            return []

        try:
            pipeline = [
                {'$match': {'connected': True}},
                {
                    '$group': {
                        '_id': '$ssid',
                        'avg_score': {'$avg': '$score'},
                        'avg_download': {'$avg': '$download_speed'},
                        'avg_upload': {'$avg': '$upload_speed'},
                        'avg_ping': {'$avg': '$ping'},
                        'avg_signal': {'$avg': '$signal_strength'},
                        'connection_count': {'$sum': 1},
                    }
                },
                {'$sort': {'avg_score': -1}},
                {'$limit': limit},
            ]
            return list(self.metrics_collection.aggregate(pipeline))
        except Exception as e:
            logger.error(f'Failed to get best networks: {e}')
            return []

    def close(self):
        """Close the MongoDB connection."""
        if self.client:
            self.client.close()
            logger.info('MongoDB connection closed')


class CardLeaseStore:
    """MongoDB-backed card lease tracking.

    Persists which plugin holds which WiFi card so leases survive reboots
    and multiple processes can coordinate card access.
    """

    # Default lease TTL in seconds — leases older than this are considered stale
    DEFAULT_TTL = 300

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili', ttl: int = DEFAULT_TTL):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.ttl = ttl
        self._available = False

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db['card_leases']
            self._available = True

            # Unique index on interface — one lease per card
            self.collection.create_index('interface', unique=True)
            logger.info('CardLeaseStore connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for lease store: {e}')
        except Exception as e:
            logger.error(f'Failed to initialize CardLeaseStore: {e}')

    def is_available(self) -> bool:
        return self._available

    def acquire(self, interface: str, holder: str, role: str = 'connection') -> bool:
        """Attempt to acquire a lease on a card.

        Args:
            interface: Network interface name
            holder: Identifier of the plugin/module acquiring the card
            role: 'scanning' or 'connection'

        Returns:
            True if lease acquired, False if already held
        """
        if not self._available:
            return True  # fallback: always allow if DB down

        try:
            now = time.time()

            # Check if we already hold it — just refresh
            doc = self.collection.find_one({'interface': interface, 'holder': holder})
            if doc:
                self.collection.update_one(
                    {'interface': interface, 'holder': holder},
                    {'$set': {'leased_at': now, 'role': role}},
                )
                return True

            # Try to claim: insert new or reclaim stale lease
            try:
                result = self.collection.update_one(
                    {
                        'interface': interface,
                        '$or': [
                            {'holder': {'$exists': False}},
                            {'leased_at': {'$lt': now - self.ttl}},
                        ],
                    },
                    {
                        '$set': {
                            'interface': interface,
                            'holder': holder,
                            'role': role,
                            'leased_at': now,
                        }
                    },
                    upsert=True,
                )
                if result.upserted_id or result.modified_count > 0:
                    logger.debug(f'Lease acquired: {interface} -> {holder} ({role})')
                    return True
            except OperationFailure:
                # Duplicate key — interface already leased by another holder
                pass

            return False
        except Exception as e:
            logger.error(f'Failed to acquire lease on {interface}: {e}')
            return True  # fallback permissive

    def release(self, interface: str, holder: str) -> bool:
        """Release a lease on a card.

        Args:
            interface: Network interface name
            holder: Identifier of the holder releasing the card

        Returns:
            True if released, False if not held by this holder
        """
        if not self._available:
            return True

        try:
            result = self.collection.delete_one({'interface': interface, 'holder': holder})
            released = result.deleted_count > 0
            if released:
                logger.debug(f'Lease released: {interface} by {holder}')
            return released
        except Exception as e:
            logger.error(f'Failed to release lease on {interface}: {e}')
            return False

    def release_all(self, holder: str) -> int:
        """Release all leases held by a given holder (e.g. on shutdown)."""
        if not self._available:
            return 0

        try:
            result = self.collection.delete_many({'holder': holder})
            logger.info(f'Released {result.deleted_count} leases for {holder}')
            return result.deleted_count
        except Exception as e:
            logger.error(f'Failed to release leases for {holder}: {e}')
            return 0

    def get_lease(self, interface: str) -> dict | None:
        """Get the current lease for a card, if any."""
        if not self._available:
            return None

        try:
            doc = self.collection.find_one({'interface': interface}, {'_id': 0})
            if doc and (time.time() - doc.get('leased_at', 0)) > self.ttl:
                # Stale lease — clean it up
                self.collection.delete_one({'interface': interface})
                return None
            return doc
        except Exception as e:
            logger.error(f'Failed to get lease for {interface}: {e}')
            return None

    def get_all_leases(self) -> list[dict]:
        """Get all active (non-stale) leases."""
        if not self._available:
            return []

        try:
            now = time.time()
            # Clean up stale leases
            self.collection.delete_many({'leased_at': {'$lt': now - self.ttl}})
            return list(self.collection.find({}, {'_id': 0}))
        except Exception as e:
            logger.error(f'Failed to get leases: {e}')
            return []

    def clear_all(self):
        """Clear all leases (used on startup to reset stale state)."""
        if not self._available:
            return

        try:
            result = self.collection.delete_many({})
            logger.info(f'Cleared {result.deleted_count} stale leases on startup')
        except Exception as e:
            logger.error(f'Failed to clear leases: {e}')


class DnsmasqDHCP:
    """Run dnsmasq as a subprocess to serve DHCP on a single interface.

    Replaces ``pyarchops_dnsmasq.dnsmasq.DHCP`` (which never actually
    existed in upstream — that module only ships ``apply``/``suitable``
    Salt helpers, so the old import path failed at runtime and silently
    broke both HostAP and the ethernet bridge). DNS is disabled
    (``--port=0``) so multiple instances coexist with the system
    dnsmasq as long as each binds a distinct ``--interface``.
    """

    def __init__(self, interface: str, dhcp_range: tuple,
                 subnet_mask: str = '255.255.255.0',
                 router: Optional[str] = None,
                 dns: Optional[str] = None,
                 local_hostname: str = 'vasili.local',
                 upstream_dns: tuple = ('8.8.8.8', '1.1.1.1')):
        self.interface = interface
        self.dhcp_range = dhcp_range
        self.subnet_mask = subnet_mask
        if router is None:
            octets = dhcp_range[0].split('.')
            if len(octets) == 4:
                router = '.'.join(octets[:3] + ['1'])
        self.router = router
        # Clients are pointed at the router IP for DNS so our dnsmasq can
        # answer vasili.local; everything else is forwarded upstream.
        self.dns = dns if dns is not None else self.router
        self.local_hostname = local_hostname
        self.upstream_dns = tuple(upstream_dns)
        self._process: Optional[subprocess.Popen] = None
        self._pid_file = f'/tmp/vasili-dnsmasq-{interface}.pid'
        self._lease_file = f'/tmp/vasili-dnsmasq-{interface}.leases'

    def start(self):
        """Launch dnsmasq. Raises RuntimeError if it exits immediately."""
        self._kill_stale()

        cmd = [
            'dnsmasq',
            '--keep-in-foreground',
            '--bind-interfaces',
            f'--interface={self.interface}',
            '--except-interface=lo',
            # DNS on this interface only (bind-interfaces above scopes it).
            # Forward everything except vasili.local upstream.
            '--no-resolv',
            f'--dhcp-range={self.dhcp_range[0]},{self.dhcp_range[1]},'
            f'{self.subnet_mask},12h',
            f'--dhcp-leasefile={self._lease_file}',
            f'--pid-file={self._pid_file}',
        ]
        if self.local_hostname and self.router:
            cmd.append(f'--address=/{self.local_hostname}/{self.router}')
        for upstream in self.upstream_dns:
            cmd.append(f'--server={upstream}')
        if self.router:
            cmd.append(f'--dhcp-option=3,{self.router}')
        if self.dns:
            cmd.append(f'--dhcp-option=6,{self.dns}')

        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        if self._process.poll() is not None:
            stdout, stderr = self._process.communicate()
            msg = (stderr or stdout or b'').decode(errors='replace').strip()
            self._process = None
            raise RuntimeError(
                f'dnsmasq exited immediately: {msg[:300] or "no output"}')

    def stop(self):
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            except Exception as e:
                logger.warning(
                    f'dnsmasq stop error on {self.interface}: {e}')
        self._process = None

    def _kill_stale(self):
        """Reap a stale dnsmasq from a previous crash on this interface."""
        try:
            with open(self._pid_file) as f:
                pid = int(f.read().strip())
        except (FileNotFoundError, ValueError, OSError):
            return
        try:
            os.kill(pid, 15)
        except OSError:
            return
        for _ in range(10):
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.1)


class ConnectionShare:
    """Share an upstream WiFi connection with already-configured local surfaces.

    Each downstream surface (eth0 in management mode, usb0, future HostAP)
    is expected to have its own addressing already — this class only installs
    forwarding + MASQUERADE rules so traffic from those surfaces is NAT'd
    out through the active upstream.

    Reuses the VASILI-FWD / VASILI-NAT chains so coexists with HostAP's
    own VASILI-HOSTAP-* chains.
    """

    def __init__(self, wifi_interface: str, downstream_interfaces: list[str]):
        self.wifi_interface = wifi_interface
        self.downstream_interfaces = list(downstream_interfaces)
        self.is_active = False
        # Kept for callers that expect a single ``ethernet_interface``
        # attribute (e.g. status payload, HostAP fallback upstream).
        self.ethernet_interface = (
            self.downstream_interfaces[0] if self.downstream_interfaces else ''
        )
        self._original_ip_forward: Optional[str] = None

    def start(self) -> bool:
        try:
            try:
                with open('/proc/sys/net/ipv4/ip_forward', 'r') as f:
                    self._original_ip_forward = f.read().strip()
            except Exception:
                self._original_ip_forward = '0'
            try:
                with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
                    f.write('1')
            except PermissionError:
                logger.error('ConnectionShare: cannot enable ip_forward (need root)')
                return False

            for cmd in [
                ['iptables', '-N', 'VASILI-FWD'],
                ['iptables', '-t', 'nat', '-N', 'VASILI-NAT'],
            ]:
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0 and 'already exists' not in r.stderr.lower():
                    logger.debug(f'Chain creation: {r.stderr.strip()}')

            subprocess.run(['iptables', '-F', 'VASILI-FWD'], capture_output=True)
            subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-NAT'],
                           capture_output=True)

            for jump_cmd in [
                ['iptables', '-C', 'FORWARD', '-j', 'VASILI-FWD'],
                ['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-j', 'VASILI-NAT'],
            ]:
                r = subprocess.run(jump_cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    add = [jump_cmd[0]] + ['-A' if c == '-C' else c for c in jump_cmd[1:]]
                    subprocess.run(add, capture_output=True)

            r = subprocess.run([
                'iptables', '-t', 'nat', '-A', 'VASILI-NAT',
                '-o', self.wifi_interface, '-j', 'MASQUERADE',
            ], capture_output=True, text=True)
            if r.returncode != 0:
                logger.error(f'MASQUERADE on {self.wifi_interface} failed: {r.stderr}')
                self._teardown()
                return False

            for ds in self.downstream_interfaces:
                r1 = subprocess.run([
                    'iptables', '-A', 'VASILI-FWD',
                    '-i', ds, '-o', self.wifi_interface, '-j', 'ACCEPT',
                ], capture_output=True, text=True)
                r2 = subprocess.run([
                    'iptables', '-A', 'VASILI-FWD',
                    '-i', self.wifi_interface, '-o', ds,
                    '-m', 'state', '--state', 'RELATED,ESTABLISHED',
                    '-j', 'ACCEPT',
                ], capture_output=True, text=True)
                if r1.returncode != 0 or r2.returncode != 0:
                    logger.error(f'FORWARD rules for {ds} failed: '
                                 f'{r1.stderr.strip() or r2.stderr.strip()}')
                    self._teardown()
                    return False

            self.is_active = True
            logger.info(
                f'ConnectionShare active: {self.wifi_interface} -> '
                f'[{", ".join(self.downstream_interfaces) or "(no downstreams)"}]'
            )
            return True
        except Exception as e:
            logger.error(f'ConnectionShare start failed: {e}')
            self._teardown()
            return False

    def _teardown(self):
        subprocess.run(['iptables', '-F', 'VASILI-FWD'], capture_output=True)
        subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-NAT'],
                       capture_output=True)
        if self._original_ip_forward is not None:
            try:
                with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
                    f.write(self._original_ip_forward)
            except Exception:
                pass

    def stop(self):
        logger.info('Stopping ConnectionShare')
        self._teardown()
        self.is_active = False


class HostAP:
    """Manage a WiFi card as a local access point using hostapd."""

    CONF_PATH = '/tmp/vasili-hostapd.conf'
    AP_SUBNET = '192.168.11'
    AP_IP = '192.168.11.1'
    DHCP_RANGE = ('192.168.11.50', '192.168.11.150')

    def __init__(self, interface: str, ssid: str, security: str,
                 password: str, channel: int):
        self.interface = interface
        self.ssid = ssid
        self.security = security  # 'open', 'wpa2', 'wpa3'
        self.password = password
        self.channel = channel
        self._hostapd_process: Optional[subprocess.Popen] = None
        self._dhcp_server = None
        self._upstream_interface: Optional[str] = None
        self._ip_configured = False
        self._nm_unmanaged = False  # True once we set NM 'managed no' on the iface
        self.is_active = False
        # Captured failure reason from the most recent start attempt —
        # surfaced through ``WifiManager._hostap_last_error`` so the UI
        # can explain failures without sending users into the logs.
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    @staticmethod
    def check_hostapd_installed() -> bool:
        return shutil.which('hostapd') is not None

    @staticmethod
    def _phy_for(interface: str) -> Optional[str]:
        """Return the phy name (e.g. ``phy0``) backing an interface, or None."""
        phy_path = f'/sys/class/net/{interface}/phy80211/name'
        try:
            with open(phy_path) as f:
                return f.read().strip()
        except Exception:
            return None

    def _get_phy(self) -> Optional[str]:
        return self._phy_for(self.interface)

    @staticmethod
    def interface_supports_ap(interface: str) -> bool:
        """Return True if ``interface`` lists ``AP`` in its phy's modes.

        Callable without an instance so ``start_hostap`` can pre-flight
        before reserving the card — see notes on the retry loop in
        ``WifiManager._on_card_returned_for_hostap``.
        """
        phy = HostAP._phy_for(interface)
        if not phy:
            return False
        try:
            result = subprocess.run(
                ['iw', 'phy', phy, 'info'],
                capture_output=True, text=True, timeout=5,
            )
            in_modes = False
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith('Supported interface modes'):
                    in_modes = True
                    continue
                if in_modes:
                    if stripped.startswith('*'):
                        if 'AP' in stripped.split():
                            return True
                    else:
                        break
            return False
        except Exception:
            return False

    def check_ap_support(self) -> bool:
        """Check if this instance's interface supports AP mode."""
        return self.interface_supports_ap(self.interface)

    # ------------------------------------------------------------------
    # hostapd config generation
    # ------------------------------------------------------------------

    def _write_hostapd_conf(self) -> str:
        """Generate hostapd configuration file. Returns path."""
        # Determine hw_mode based on channel
        hw_mode = 'a' if self.channel > 14 else 'g'

        lines = [
            f'interface={self.interface}',
            f'ssid={self.ssid}',
            f'hw_mode={hw_mode}',
            f'channel={self.channel}',
            'ieee80211n=1',
            'wmm_enabled=1',
        ]

        if hw_mode == 'a':
            lines.append('ieee80211ac=1')

        if self.security == 'open':
            lines.append('auth_algs=1')
            lines.append('wpa=0')
        elif self.security == 'wpa2':
            lines.extend([
                'auth_algs=1',
                'wpa=2',
                'wpa_key_mgmt=WPA-PSK',
                'rsn_pairwise=CCMP',
                f'wpa_passphrase={self.password}',
            ])
        elif self.security == 'wpa3':
            lines.extend([
                'auth_algs=1',
                'wpa=2',
                'wpa_key_mgmt=SAE',
                'rsn_pairwise=CCMP',
                'ieee80211w=2',
                f'sae_password={self.password}',
            ])

        conf = '\n'.join(lines) + '\n'
        with open(self.CONF_PATH, 'w') as f:
            f.write(conf)
        return self.CONF_PATH

    # ------------------------------------------------------------------
    # Interface configuration
    # ------------------------------------------------------------------

    def _release_from_nm(self):
        """Hand the interface fully over to hostapd.

        hostapd's "nl80211: Could not configure driver mode" almost always
        means NetworkManager (via wpa_supplicant) still controls the radio.
        `nmcli device disconnect` alone isn't enough — NM keeps the device
        managed and wpa_supplicant can re-grab it. Setting the device
        'managed no' makes NM release it entirely so hostapd can switch it to
        AP mode. Restored to 'managed yes' in _cleanup_interface.
        """
        subprocess.run(
            ['nmcli', 'device', 'set', self.interface, 'managed', 'no'],
            capture_output=True, text=True,
        )
        self._nm_unmanaged = True
        subprocess.run(
            ['nmcli', 'device', 'disconnect', self.interface],
            capture_output=True, text=True,
        )

    def _reset_ap_interface(self):
        """Re-assert AP-ready interface state between hostapd retries.

        Bounces the link so any half-configured driver state from a failed
        attempt is cleared before the next one.
        """
        self._release_from_nm()
        subprocess.run(['ip', 'link', 'set', self.interface, 'down'],
                       capture_output=True)
        time.sleep(0.3)
        subprocess.run(['ip', 'link', 'set', self.interface, 'up'],
                       capture_output=True)

    def _configure_interface(self) -> bool:
        """Release the interface from NetworkManager and assign static IP."""
        # Take the radio away from NM/wpa_supplicant before hostapd needs it.
        self._release_from_nm()
        # Brief settle so the driver finishes releasing before we reconfigure.
        time.sleep(0.5)
        # Flush existing addresses
        subprocess.run(
            ['ip', 'addr', 'flush', 'dev', self.interface],
            capture_output=True, text=True,
        )
        # Assign static IP
        result = subprocess.run(
            ['ip', 'addr', 'add', f'{self.AP_IP}/24', 'dev', self.interface],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and 'File exists' not in result.stderr:
            self.last_error = f'interface setup failed: {result.stderr.strip()}'
            logger.error(f'HostAP: failed to assign IP: {result.stderr}')
            return False
        self._ip_configured = True
        # Bring interface up
        result = subprocess.run(
            ['ip', 'link', 'set', self.interface, 'up'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.last_error = f'interface setup failed: {result.stderr.strip()}'
            logger.error(f'HostAP: failed to bring up interface: {result.stderr}')
            return False
        return True

    def _cleanup_interface(self):
        """Remove the static IP and hand the interface back to NetworkManager."""
        if self._ip_configured:
            subprocess.run(
                ['ip', 'addr', 'del', f'{self.AP_IP}/24', 'dev', self.interface],
                capture_output=True,
            )
            self._ip_configured = False
        # Re-enable NM management so the card returns to the connection pool
        # usable again (we set 'managed no' during bring-up).
        if self._nm_unmanaged:
            subprocess.run(
                ['nmcli', 'device', 'set', self.interface, 'managed', 'yes'],
                capture_output=True, text=True,
            )
            self._nm_unmanaged = False

    # ------------------------------------------------------------------
    # hostapd process
    # ------------------------------------------------------------------

    # Substrings in hostapd output (and our sub-step errors) that indicate a
    # transient bring-up race — the radio hadn't finished being released — and
    # so are worth retrying rather than treating as a permanent failure.
    _TRANSIENT_HOSTAP_MARKERS = (
        'could not configure driver mode',
        'could not set interface',
        'device or resource busy',
        'resource temporarily unavailable',
        'interface initialization failed',
        'failed to set beacon',
        "wasn't started",
        'hostapd exited immediately',
        'interface setup failed',
        'dhcp failed',
    )

    @classmethod
    def _is_transient_hostapd_error(cls, output: str) -> bool:
        """True if a start failure looks like a retryable bring-up race."""
        if not output:
            return True  # empty/unknown — give it another try
        low = output.lower()
        return any(m in low for m in cls._TRANSIENT_HOSTAP_MARKERS)

    def _start_hostapd(self) -> bool:
        """Launch hostapd, retrying transient driver-mode/busy races.

        "nl80211: Could not configure driver mode" and "Device or resource
        busy" happen when NetworkManager/wpa_supplicant hasn't fully released
        the interface yet. A few short retries (re-asserting the interface
        between them) absorb that settling window instead of failing outright.
        """
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                self._hostapd_process = subprocess.Popen(
                    ['hostapd', self.CONF_PATH],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                # Give hostapd a moment to start
                time.sleep(2)
                if self._hostapd_process.poll() is None:
                    logger.info(
                        f'HostAP: hostapd started (pid {self._hostapd_process.pid})'
                    )
                    return True

                # Exited immediately — capture diagnostics.
                stdout, stderr = self._hostapd_process.communicate()
                # hostapd writes most of its diagnostics to stdout; fall
                # back to stderr if stdout is empty.
                msg = (stdout or b'').decode(errors='replace').strip()
                if not msg:
                    msg = (stderr or b'').decode(errors='replace').strip()
                self.last_error = (
                    self._summarise_hostapd_error(msg) or 'hostapd exited immediately'
                )
                self._hostapd_process = None

                if attempt < attempts and self._is_transient_hostapd_error(msg):
                    logger.warning(
                        'HostAP: hostapd transient failure '
                        f'(attempt {attempt}/{attempts}): {msg[:200]} — retrying'
                    )
                    self._reset_ap_interface()
                    time.sleep(1.5)
                    continue

                logger.error(f'HostAP: hostapd exited: {msg[:400]}')
                return False
            except FileNotFoundError:
                self.last_error = 'hostapd binary not found'
                logger.error('HostAP: hostapd binary not found in PATH')
                return False
            except Exception as e:
                self.last_error = f'failed to launch hostapd: {e}'
                logger.error(f'HostAP: failed to start hostapd: {e}')
                return False
        return False

    @staticmethod
    def _summarise_hostapd_error(output: str) -> Optional[str]:
        """Pick the most informative line out of hostapd's output.

        hostapd prints a stack of messages on failure — the last
        ``ERROR``/``Failed``/``not supported`` line is usually the
        actionable one. Falls back to the last non-empty line.
        """
        if not output:
            return None
        useful = None
        for line in output.splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if ('error' in low or 'failed' in low or 'not supported' in low
                    or 'unable' in low or 'could not' in low):
                useful = s
        if useful:
            # Trim hostapd's wlanN: prefix for readability.
            return useful.split(':', 2)[-1].strip()[:200] or useful[:200]
        # Last non-blank line as fallback
        lines = [s.strip() for s in output.splitlines() if s.strip()]
        return lines[-1][:200] if lines else None

    def _stop_hostapd(self):
        """Stop hostapd process."""
        if self._hostapd_process:
            try:
                self._hostapd_process.terminate()
                self._hostapd_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._hostapd_process.kill()
                self._hostapd_process.wait()
            except Exception as e:
                logger.warning(f'HostAP: error stopping hostapd: {e}')
            self._hostapd_process = None

    # ------------------------------------------------------------------
    # DHCP
    # ------------------------------------------------------------------

    def _start_dhcp(self) -> bool:
        """Start DHCP server on the AP interface."""
        try:
            self._dhcp_server = DnsmasqDHCP(
                interface=self.interface,
                dhcp_range=self.DHCP_RANGE,
                subnet_mask='255.255.255.0',
                router=self.AP_IP,
            )
            self._dhcp_server.start()
            logger.info('HostAP: DHCP server started')
            return True
        except Exception as e:
            # Carry the failure context up to WifiManager so the UI can
            # show "DHCP failed: ..." instead of the misleading
            # "hostapd failed to start" (hostapd actually launched).
            self.last_error = f'DHCP failed: {e}'
            logger.error(f'HostAP: failed to start DHCP: {e}')
            return False

    def _stop_dhcp(self):
        if self._dhcp_server:
            try:
                self._dhcp_server.stop()
            except Exception as e:
                logger.warning(f'HostAP: error stopping DHCP: {e}')
            self._dhcp_server = None

    # ------------------------------------------------------------------
    # NAT
    # ------------------------------------------------------------------

    def _setup_nat(self, upstream: str) -> bool:
        """Set up NAT from AP interface to upstream WiFi connection."""
        try:
            # Enable IP forwarding
            try:
                with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
                    f.write('1')
            except Exception as e:
                logger.error(f'HostAP: cannot enable IP forwarding: {e}')
                return False

            # Create hostap-specific iptables chains
            for cmd in [
                ['iptables', '-N', 'VASILI-HOSTAP-FWD'],
                ['iptables', '-t', 'nat', '-N', 'VASILI-HOSTAP-NAT'],
                ['iptables', '-t', 'nat', '-N', 'VASILI-HOSTAP-PRE'],
            ]:
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0 and 'already exists' not in result.stderr.lower():
                    logger.debug(f'HostAP chain creation: {result.stderr.strip()}')

            # Flush chains
            subprocess.run(['iptables', '-F', 'VASILI-HOSTAP-FWD'], capture_output=True)
            subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-HOSTAP-NAT'], capture_output=True)
            subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-HOSTAP-PRE'], capture_output=True)

            # Jump into hostap chains from main chains (idempotent)
            for jump_cmd in [
                ['iptables', '-C', 'FORWARD', '-j', 'VASILI-HOSTAP-FWD'],
                ['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-j', 'VASILI-HOSTAP-NAT'],
                ['iptables', '-t', 'nat', '-C', 'PREROUTING', '-j', 'VASILI-HOSTAP-PRE'],
            ]:
                result = subprocess.run(jump_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    add_cmd = [jump_cmd[0]] + ['-A' if c == '-C' else c for c in jump_cmd[1:]]
                    subprocess.run(add_cmd, capture_output=True)

            # Redirect tcp/80 on the AP iface to Vasili's web UI on tcp/5000
            # so clients can reach http://vasili.local/ without a port.
            result = subprocess.run([
                'iptables', '-t', 'nat', '-A', 'VASILI-HOSTAP-PRE',
                '-i', self.interface, '-p', 'tcp', '--dport', '80',
                '-j', 'REDIRECT', '--to-ports', '5000',
            ], capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(f'HostAP port 80 redirect failed: {result.stderr.strip()}')

            # Masquerade AP traffic going out upstream
            result = subprocess.run([
                'iptables', '-t', 'nat', '-A', 'VASILI-HOSTAP-NAT',
                '-s', f'{self.AP_SUBNET}.0/24', '-o', upstream, '-j', 'MASQUERADE',
            ], capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f'HostAP NAT masquerade failed: {result.stderr}')
                self._cleanup_nat()
                return False

            # Forward AP -> upstream
            result = subprocess.run([
                'iptables', '-A', 'VASILI-HOSTAP-FWD',
                '-i', self.interface, '-o', upstream, '-j', 'ACCEPT',
            ], capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f'HostAP forward rule failed: {result.stderr}')
                self._cleanup_nat()
                return False

            # Forward upstream -> AP (established/related)
            result = subprocess.run([
                'iptables', '-A', 'VASILI-HOSTAP-FWD',
                '-i', upstream, '-o', self.interface,
                '-m', 'state', '--state', 'RELATED,ESTABLISHED', '-j', 'ACCEPT',
            ], capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f'HostAP reverse forward rule failed: {result.stderr}')
                self._cleanup_nat()
                return False

            self._upstream_interface = upstream
            logger.info(f'HostAP: NAT configured ({self.interface} -> {upstream})')
            return True
        except Exception as e:
            logger.error(f'HostAP NAT setup failed: {e}')
            self._cleanup_nat()
            return False

    def _cleanup_nat(self):
        """Flush hostap iptables chains."""
        subprocess.run(['iptables', '-F', 'VASILI-HOSTAP-FWD'], capture_output=True)
        subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-HOSTAP-NAT'], capture_output=True)
        subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-HOSTAP-PRE'], capture_output=True)
        self._upstream_interface = None

    def update_upstream(self, new_upstream: str):
        """Re-point NAT to a different upstream interface."""
        if not self.is_active:
            return
        if new_upstream == self._upstream_interface:
            return
        logger.info(f'HostAP: switching upstream {self._upstream_interface} -> {new_upstream}')
        self._cleanup_nat()
        self._setup_nat(new_upstream)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, upstream_interface: Optional[str] = None) -> bool:
        """Start the full HostAP stack."""
        if not self.check_hostapd_installed():
            logger.error('HostAP: hostapd not installed')
            return False

        if not self.check_ap_support():
            logger.error(f'HostAP: {self.interface} does not support AP mode')
            return False

        self._write_hostapd_conf()

        if not self._configure_interface():
            # _configure_interface already set NM 'managed no'; restore it so
            # the card is usable again when returned to the connection pool.
            self._cleanup_interface()
            return False

        if not self._start_hostapd():
            self._cleanup_interface()
            return False

        if not self._start_dhcp():
            self._stop_hostapd()
            self._cleanup_interface()
            return False

        if upstream_interface:
            if not self._setup_nat(upstream_interface):
                logger.warning('HostAP: NAT failed, AP running without internet')

        self.is_active = True
        logger.info(f'HostAP: active on {self.interface} (SSID: {self.ssid})')
        return True

    def stop(self):
        """Stop HostAP and clean up all resources."""
        logger.info('HostAP: stopping...')
        self._cleanup_nat()
        self._stop_dhcp()
        self._stop_hostapd()
        self._cleanup_interface()
        # Clean up config file
        try:
            os.remove(self.CONF_PATH)
        except OSError:
            pass
        self.is_active = False
        logger.info('HostAP: stopped')

    def get_client_count(self) -> int:
        """Count connected stations via hostapd_cli."""
        try:
            result = subprocess.run(
                ['hostapd_cli', '-i', self.interface, 'all_sta'],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode != 0:
                return 0
            # Each station block starts with a MAC address line
            count = 0
            for line in result.stdout.splitlines():
                line = line.strip()
                if len(line) == 17 and line.count(':') == 5:
                    count += 1
            return count
        except Exception:
            return 0

    def get_status(self) -> dict:
        return {
            'is_active': self.is_active,
            'interface': self.interface,
            'ssid': self.ssid,
            'security': self.security,
            'channel': self.channel,
            'upstream_interface': self._upstream_interface,
            'client_count': self.get_client_count() if self.is_active else 0,
        }


def _get_device_ssid_map() -> dict[str, str]:
    """Return {interface: on-the-air SSID} from one `iw dev` call.

    Uses iw rather than nmcli because nmcli's CONNECTION field reports
    the *profile name* (e.g. "Will there be a piano? 1"), which diverges
    from the actual SSID once duplicate profiles exist. iw reports the
    SSID the radio is currently associated to, which is what vasili's
    in-memory ConnectionResult.network.ssid is compared against.
    """
    out: dict[str, str] = {}
    try:
        r = subprocess.run(
            ['iw', 'dev'], capture_output=True, text=True, timeout=5,
        )
        current_iface: Optional[str] = None
        for raw in r.stdout.splitlines():
            line = raw.strip()
            if line.startswith('Interface '):
                current_iface = line.split(None, 1)[1]
            elif line.startswith('ssid ') and current_iface:
                out[current_iface] = line[len('ssid '):]
    except Exception as e:
        logger.debug(f'_get_device_ssid_map failed: {e}')
    return out


def _classify_nmcli_connect_error(stderr: str) -> str:
    """Classify `nmcli device wifi connect` stderr into a failure kind.

    Returns one of:
      - 'auth'            — wrong/missing secrets; retrying with the same
                            password won't help
      - 'ssid_not_found'  — SSID not in NM's current scan list; usually
                            permanent within the lifetime of one connect()
                            (the calling pipeline does its own re-scans)
      - 'transient'       — anything else worth a retry
    """
    if not stderr:
        return 'transient'
    s = stderr.lower()
    if ('secrets were required' in s
            or 'secrets were not provided' in s
            or '802.1x supplicant' in s
            or '(7)' in s):
        return 'auth'
    if 'no network with ssid' in s or 'ssid was not found' in s:
        return 'ssid_not_found'
    return 'transient'


def _nm_disable_autoconnect_for_interface(iface: str) -> int:
    """Disable autoconnect on every NM profile bound to this iface.

    Used post-connect to disable the profile NM just created so the
    interface won't be auto-re-associated after vasili repurposes the
    card. Returns the number of profiles modified.
    """
    if not iface:
        return 0
    modified = 0
    try:
        r = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,DEVICE', 'connection', 'show'],
            capture_output=True, text=True, timeout=5,
        )
        targets: list[str] = []
        for line in r.stdout.splitlines():
            if not line:
                continue
            parts = line.rsplit(':', 1)
            if len(parts) != 2:
                continue
            name, device = parts
            if device == iface:
                targets.append(name)
        for name in targets:
            mr = subprocess.run(
                ['nmcli', 'connection', 'modify', name,
                 'connection.autoconnect', 'no'],
                capture_output=True, text=True, timeout=5,
            )
            if mr.returncode == 0:
                modified += 1
    except Exception as e:
        logger.debug(f'_nm_disable_autoconnect_for_interface({iface}): {e}')
    if modified:
        logger.info(
            f'Disabled NM autoconnect on {modified} profile(s) bound to {iface}'
        )
    return modified


def _nm_disable_autoconnect_all_wifi() -> int:
    """Disable autoconnect on every NM wifi profile.

    Per-interface cleanup only catches profiles currently bound to a
    device. Inactive sandbag profiles (from previous sessions, no
    current DEVICE column) still have autoconnect=yes and will hijack
    cards as soon as vasili releases them. Vasili owns wifi orchestration
    on this host, so we disable autoconnect on every wifi profile at
    startup. Idempotent.
    """
    modified = 0
    try:
        # Query UUID (never contains ':'), TYPE, and AUTOCONNECT in one shot so
        # we only touch profiles that actually need changing. On hosts with many
        # accumulated sandbag profiles, blindly re-modifying every wifi profile
        # was the dominant startup cost (~0.17s/profile × hundreds = tens of
        # seconds); filtering to autoconnect=yes collapses that to near-zero.
        r = subprocess.run(
            ['nmcli', '-t', '-f', 'UUID,TYPE,AUTOCONNECT', 'connection', 'show'],
            capture_output=True, text=True, timeout=5,
        )
        targets: list[str] = []
        for line in r.stdout.splitlines():
            if not line:
                continue
            # UUID/TYPE/AUTOCONNECT are colon-free, so a plain split is safe.
            parts = line.split(':')
            if len(parts) < 3:
                continue
            uuid, ctype, autoconnect = parts[0], parts[1], parts[2]
            if ctype == '802-11-wireless' and autoconnect == 'yes':
                targets.append(uuid)

        # Disable in parallel — independent nmcli calls, so wall-time stays low
        # even if a batch of profiles still has autoconnect on.
        def _disable(uuid: str) -> bool:
            try:
                mr = subprocess.run(
                    ['nmcli', 'connection', 'modify', uuid,
                     'connection.autoconnect', 'no'],
                    capture_output=True, text=True, timeout=5,
                )
                return mr.returncode == 0
            except Exception:
                return False

        if targets:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=8) as pool:
                modified = sum(pool.map(_disable, targets))
    except Exception as e:
        logger.debug(f'_nm_disable_autoconnect_all_wifi: {e}')
    if modified:
        logger.info(f'Disabled NM autoconnect on {modified} wifi profile(s)')
    return modified


class WifiCard:
    def __init__(self, interface_name: str, mac_manager: MacManager = None):
        """Initialize a wifi card with the given interface name"""
        self.interface = interface_name
        self.in_use = False
        self._connected_network: Optional[WifiNetwork] = None
        self._connection_password: Optional[str] = None
        self._routing_info: Optional[dict] = None
        self._mac_manager: Optional[MacManager] = mac_manager
        self._original_mac: Optional[str] = None
        self.current_task: Optional[dict] = None
        self.current_mode: str = 'managed'  # managed, monitor, etc.

        # Verify the interface exists and is a wireless device
        if not os.path.isdir(f'/sys/class/net/{interface_name}/wireless'):
            raise ValueError(f'Interface {interface_name} is not a valid wireless device')

    def scan(self) -> list[WifiNetwork]:
        """Scan for available networks using this card via nmcli"""
        try:
            # Put interface up
            subprocess.run(
                ['ip', 'link', 'set', self.interface, 'up'],
                check=True,
                capture_output=True,
            )

            # Trigger a fresh scan
            subprocess.run(
                ['nmcli', 'device', 'wifi', 'rescan', 'ifname', self.interface],
                capture_output=True,
                text=True,
            )
            # Brief pause to let scan results populate
            time.sleep(1)

            # Get scan results in machine-readable format
            result = subprocess.run(
                [
                    'nmcli', '-t', '-f', 'SSID,BSSID,SIGNAL,CHAN,SECURITY',
                    'device', 'wifi', 'list', 'ifname', self.interface,
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            networks = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                # nmcli -t uses : as delimiter, but BSSID contains escaped colons (\:)
                # Replace escaped colons temporarily to split correctly
                line_clean = line.replace('\\:', '\x00')
                parts = line_clean.split(':')
                if len(parts) < 5:
                    continue

                ssid = parts[0]
                bssid = parts[1].replace('\x00', ':')
                try:
                    signal = int(parts[2])
                except (ValueError, IndexError):
                    signal = 0
                try:
                    channel = int(parts[3])
                except (ValueError, IndexError):
                    channel = 0
                security = parts[4].replace('\x00', ':')

                # Determine encryption type and openness
                is_open = security == '' or security == '--'
                if 'WPA3' in security or 'SAE' in security:
                    encryption_type = 'WPA3'
                elif 'WPA2' in security:
                    encryption_type = 'WPA2'
                elif 'WPA' in security:
                    encryption_type = 'WPA'
                elif 'WEP' in security:
                    encryption_type = 'WEP'
                else:
                    encryption_type = ''

                network = WifiNetwork(
                    ssid=ssid,
                    bssid=bssid,
                    signal_strength=signal,
                    channel=channel,
                    encryption_type=encryption_type,
                    is_open=is_open,
                )
                networks.append(network)

            return networks

        except subprocess.CalledProcessError as e:
            logger.error(f'Scan failed on interface {self.interface}: {e}')
            return []

    def connect(
        self,
        network: WifiNetwork,
        password: Optional[str] = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        attempt_timeout: float = 10.0,
    ) -> bool:
        """
        Connect to a WiFi network using this card with automatic retry logic.

        Args:
            network: The WifiNetwork to connect to
            password: Optional password for encrypted networks
            max_retries: Maximum number of connection attempts (default: 3)
            base_delay: Base delay in seconds between retries, doubles each attempt (default: 1.0)
            attempt_timeout: Per-attempt nmcli subprocess timeout in seconds (default: 10.0)

        Returns:
            True if connection successful, False otherwise
        """
        attempt = 0
        last_error = None

        # Apply per-network MAC before connecting (privacy + session consistency)
        if self._mac_manager and network.bssid:
            self._apply_network_mac(network.bssid)

        while attempt < max_retries:
            attempt += 1
            logger.info(
                f'Connection attempt {attempt}/{max_retries} to {network.ssid} on {self.interface}'
            )

            try:
                # Bring interface up
                subprocess.run(['ip', 'link', 'set', self.interface, 'up'], check=True)

                # Disconnect from any current network first
                subprocess.run(
                    ['nmcli', 'device', 'disconnect', self.interface], capture_output=True
                )

                # Build the nmcli command
                cmd = ['nmcli', 'device', 'wifi', 'connect', network.ssid]

                # Add password if provided (for encrypted networks)
                if password:
                    cmd.extend(['password', password])
                elif not network.is_open:
                    # For encrypted networks without a password, try connecting anyway
                    # nmcli may have saved credentials from a previous connection
                    logger.info(
                        f'Attempting to connect to encrypted network {network.ssid} using saved credentials'
                    )

                # Specify the interface to use
                cmd.extend(['ifname', self.interface])

                # Optionally specify BSSID for more precise connection
                if network.bssid:
                    cmd.extend(['bssid', network.bssid])

                # Execute the connection command
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=attempt_timeout
                )

                if result.returncode == 0:
                    logger.info(f'Successfully connected to {network.ssid} on {self.interface}')
                    self._connected_network = network
                    self._connection_password = password

                    # Set up routing isolation so speedtest/connectivity
                    # checks use this interface, not eth0
                    self._routing_info = self._setup_isolation()

                    # NM creates per-interface profiles with autoconnect=yes
                    # by default. Without this call, NM will keep re-binding
                    # the interface to this SSID even after vasili releases
                    # it for scanning/other modules.
                    _nm_disable_autoconnect_for_interface(self.interface)
                    return True
                else:
                    last_error = f'nmcli error: {result.stderr}'
                    kind = _classify_nmcli_connect_error(result.stderr)
                    logger.warning(
                        f'Attempt {attempt}/{max_retries} failed for {network.ssid} '
                        f'[{kind}]: {result.stderr.strip()}'
                    )
                    if kind in ('auth', 'ssid_not_found'):
                        logger.error(
                            f'Permanent failure ({kind}) for {network.ssid} on '
                            f'{self.interface}; skipping remaining retries.'
                        )
                        break

            except subprocess.TimeoutExpired:
                last_error = 'Connection timed out'
                logger.warning(f'Attempt {attempt}/{max_retries} timed out for {network.ssid}')
            except subprocess.CalledProcessError as e:
                last_error = str(e)
                logger.warning(f'Attempt {attempt}/{max_retries} failed on {self.interface}: {e}')
            except Exception as e:
                last_error = str(e)
                logger.warning(f'Attempt {attempt}/{max_retries} unexpected error: {e}')

            # Apply exponential backoff before next retry (except on last attempt)
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(f'Waiting {delay:.1f}s before retry...')
                time.sleep(delay)

        logger.error(
            f'Failed to connect to {network.ssid} after {attempt} attempt(s). Last error: {last_error}'
        )
        # NB: do NOT clear self.in_use here. Lease ownership belongs to
        # WifiCardManager.lease_card / return_card; a failed connect attempt
        # within an already-leased pipeline must not surrender the lease,
        # or the card races into another worker mid-pipeline.
        return False

    def disconnect(self) -> bool:
        """Disconnect from the current network."""
        try:
            # Tear down routing isolation before disconnecting
            if self._routing_info:
                network_isolation.teardown_interface_routing(
                    self.interface, self._routing_info
                )
                self._routing_info = None

            result = subprocess.run(
                ['nmcli', 'device', 'disconnect', self.interface], capture_output=True, text=True
            )
            if result.returncode == 0:
                logger.info(f'Disconnected {self.interface}')
                # Lease ownership stays with WifiCardManager; disconnecting
                # only clears the network state, not the lease.
                self._connected_network = None
                self._connection_password = None
                return True
            else:
                logger.error(f'Failed to disconnect {self.interface}: {result.stderr}')
                return False
        except Exception as e:
            logger.error(f'Error disconnecting {self.interface}: {e}')
            return False

    def is_connected(self) -> bool:
        """Check if this card is currently connected to a WiFi network."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'DEVICE,STATE', 'device', 'status'],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.strip().split('\n'):
                if ':' in line:
                    device, state = line.split(':', 1)
                    if device == self.interface and state == 'connected':
                        return True
            return False
        except Exception as e:
            logger.error(f'Error checking connection status for {self.interface}: {e}')
            return False

    def get_connected_ssid(self) -> Optional[str]:
        """Get the SSID of the currently connected network, if any.

        Reads the on-air SSID via `iw` rather than nmcli's CONNECTION field,
        which returns the *profile name* (e.g. "Will there be a piano? 1"
        after duplicate profiles accumulate). See _get_device_ssid_map for
        the same rationale — comparing profile name to SSID makes the
        ConnectionMonitor flap forever.
        """
        return _get_device_ssid_map().get(self.interface)

    def reconnect(self, max_retries: int = 3, base_delay: float = 1.0) -> bool:
        """
        Attempt to reconnect to the last known network.

        Args:
            max_retries: Maximum number of reconnection attempts
            base_delay: Base delay in seconds between retries

        Returns:
            True if reconnection successful, False otherwise
        """
        if self._connected_network is None:
            logger.warning(f'No previous network to reconnect to on {self.interface}')
            return False

        logger.info(
            f'Attempting to reconnect to {self._connected_network.ssid} on {self.interface}'
        )
        return self.connect(
            self._connected_network,
            password=self._connection_password,
            max_retries=max_retries,
            base_delay=base_delay,
        )

    def get_status(self) -> dict:
        """Get current status of the wifi card"""
        return {
            'interface': self.interface,
            'in_use': self.in_use,
            'is_up': self._is_interface_up(),
        }

    def _is_interface_up(self) -> bool:
        """Check if the interface is currently up.

        In monitor mode, operstate may report 'unknown' even when active,
        so we also check the IFF_UP flag via sysfs.
        """
        try:
            with open(f'/sys/class/net/{self.interface}/operstate', 'r') as f:
                state = f.read().strip()
            if state == 'up':
                return True
            # In monitor mode, operstate is often 'unknown' — check flags
            if self.current_mode == 'monitor':
                with open(f'/sys/class/net/{self.interface}/flags', 'r') as f:
                    flags = int(f.read().strip(), 16)
                    return bool(flags & 0x1)  # IFF_UP
            return False
        except Exception:
            return False

    def get_mode(self) -> Optional[str]:
        """Get current interface mode (managed, monitor, etc.)."""
        try:
            result = subprocess.run(
                ['iw', 'dev', self.interface, 'info'],
                capture_output=True, text=True, timeout=5,
            )
            import re as _re
            match = _re.search(r'type\s+(\w+)', result.stdout)
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    def set_mode(self, mode: str) -> bool:
        """Set interface mode (managed, monitor, etc.).

        Brings the interface down, changes mode, brings it back up.
        The card must be leased before calling this.
        """
        try:
            subprocess.run(
                ['ip', 'link', 'set', self.interface, 'down'],
                check=True, capture_output=True, timeout=5,
            )
            subprocess.run(
                ['iw', 'dev', self.interface, 'set', 'type', mode],
                check=True, capture_output=True, timeout=5,
            )
            subprocess.run(
                ['ip', 'link', 'set', self.interface, 'up'],
                check=True, capture_output=True, timeout=5,
            )
            self.current_mode = mode
            logger.info(f'Set {self.interface} mode to {mode}')
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f'Failed to set {self.interface} to {mode}: {e}')
            return False

    def ensure_managed(self) -> bool:
        """Ensure the card is in managed mode (for nmcli operations)."""
        if self.current_mode != 'managed':
            return self.set_mode('managed')
        return True

    def run_scan(self, ssids: list[str] = None, passive: bool = False) -> str:
        """Run a direct iw scan on this card.

        Unlike nmcli scans (which use the scanning card), this runs a scan
        directly on THIS card. Useful for directed probes and hidden network
        discovery.

        Args:
            ssids: Optional list of SSIDs to probe for (directed scan)
            passive: If True, only listen (no probe requests)

        Returns:
            Raw scan output text
        """
        try:
            # Ensure interface is up
            subprocess.run(
                ['ip', 'link', 'set', self.interface, 'up'],
                capture_output=True, timeout=5,
            )

            cmd = ['iw', 'dev', self.interface, 'scan']
            if passive:
                cmd.append('passive')
            elif ssids:
                for ssid in ssids:
                    cmd.extend(['ssid', ssid])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.warning(f'Scan timed out on {self.interface}')
            return ''
        except Exception as e:
            logger.error(f'Scan failed on {self.interface}: {e}')
            return ''

    def get_ip_address(self) -> Optional[str]:
        """Get the IPv4 address assigned to this interface."""
        return network_isolation.get_interface_ip(self.interface)

    def get_gateway(self) -> Optional[str]:
        """Get the default gateway for this interface."""
        return network_isolation.get_interface_gateway(self.interface)

    def get_frequency_info(self) -> dict:
        """Get current frequency and supported bands from iw.

        Returns dict with:
            current_freq: int MHz or None
            current_band: '2.4GHz' / '5GHz' / '6GHz' / None
            current_channel: int or None
            supported_bands: list of '2.4GHz' / '5GHz' / '6GHz'
        """
        info = {
            'current_freq': None,
            'current_band': None,
            'current_channel': None,
            'supported_bands': [],
        }

        try:
            # Get current channel/freq
            result = subprocess.run(
                ['iw', 'dev', self.interface, 'info'],
                capture_output=True, text=True, timeout=5,
            )
            import re as _re
            ch_match = _re.search(
                r'channel\s+(\d+)\s+\((\d+)\s+MHz\)', result.stdout
            )
            if ch_match:
                info['current_channel'] = int(ch_match.group(1))
                freq = int(ch_match.group(2))
                info['current_freq'] = freq
                info['current_band'] = self._freq_to_band(freq)

            # Get phy name
            phy_match = _re.search(r'wiphy\s+(\d+)', result.stdout)
            if phy_match:
                phy = f'phy{phy_match.group(1)}'
                # Get supported bands
                phy_result = subprocess.run(
                    ['iw', 'phy', phy, 'info'],
                    capture_output=True, text=True, timeout=5,
                )
                bands = set()
                for line in phy_result.stdout.split('\n'):
                    line = line.strip()
                    if line.startswith('Band '):
                        band_num = line.split()[1].rstrip(':')
                        if band_num == '1':
                            bands.add('2.4GHz')
                        elif band_num == '2':
                            bands.add('5GHz')
                        elif band_num == '4':
                            bands.add('6GHz')
                info['supported_bands'] = sorted(bands)

        except Exception as e:
            logger.debug(f'Failed to get frequency info for {self.interface}: {e}')

        return info

    @staticmethod
    def _freq_to_band(freq_mhz: int) -> str:
        """Convert frequency in MHz to band name."""
        if 2400 <= freq_mhz <= 2500:
            return '2.4GHz'
        elif 5000 <= freq_mhz <= 5900:
            return '5GHz'
        elif 5925 <= freq_mhz <= 7125:
            return '6GHz'
        return f'{freq_mhz}MHz'

    def _setup_isolation(self) -> Optional[dict]:
        """Set up routing isolation after a successful WiFi connection.

        Polls for a DHCP lease (up to 10s) then configures policy routing
        so traffic from this interface uses its own routing table.

        Returns:
            Routing info dict on success, None on failure.
        """
        # Poll for IP — DHCP may not be instant after nmcli returns
        ip = None
        for _ in range(20):
            ip = self.get_ip_address()
            if ip:
                break
            time.sleep(0.5)

        if not ip:
            logger.warning(f'No DHCP lease on {self.interface} after 10s')
            return None

        return network_isolation.setup_interface_routing(self.interface)

    def _apply_network_mac(self, bssid: str):
        """Apply a per-network randomized MAC before connecting.

        The same MAC is reused for the same BSSID across sessions,
        providing privacy while maintaining session continuity.
        """
        try:
            # Save original MAC on first use
            if not self._original_mac:
                self._original_mac = MacManager.get_current_mac(self.interface)

            target_mac = self._mac_manager.get_mac_for_network(bssid)
            current_mac = MacManager.get_current_mac(self.interface)

            if current_mac and current_mac.lower() == target_mac.lower():
                return  # Already set

            logger.info(
                f'Setting MAC for {self.interface}: {current_mac} -> {target_mac} '
                f'(network {bssid})'
            )
            MacManager.set_mac(self.interface, target_mac)
        except Exception as e:
            logger.warning(f'MAC randomization failed on {self.interface}: {e}')


class WifiCardManager:
    def __init__(self):
        self.cards: list[WifiCard] = []
        self._lock = threading.Lock()
        self.initialization_errors: list[str] = []
        self._scanning_card: Optional[WifiCard] = None
        self._hostap_card: Optional[WifiCard] = None
        self._on_card_returned_callbacks: list = []

        # Initialize MongoDB-backed stores
        config = get_config()
        self.lease_store = CardLeaseStore(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )
        self.mac_manager = MacManager(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )
        # Thread-local storage for pending task info — each worker thread
        # sets its own pending task before calling module.connect()
        self._pending_tasks = threading.local()
        # Clear stale leases from previous run (e.g. after reboot)
        self.lease_store.clear_all()

        self.scan_for_cards()

    def scan_for_cards(self) -> int:
        """
        Scan for available wifi cards and add them to the list.

        Returns the number of cards found. Gracefully handles systems
        with no WiFi hardware by continuing without error.
        """
        with self._lock:
            config = get_config()

            # Clear existing cards
            self.cards = []
            self.initialization_errors = []

            # Get list of network interfaces
            try:
                interfaces = netifaces.interfaces()
            except Exception as e:
                error_msg = f'Failed to enumerate network interfaces: {e}'
                logger.error(error_msg)
                self.initialization_errors.append(error_msg)
                return 0

            # Find wifi interfaces
            wifi_interfaces = []
            for interface in interfaces:
                if os.path.isdir(f'/sys/class/net/{interface}/wireless'):
                    # Skip excluded interfaces
                    if interface in config.interfaces.excluded:
                        logger.debug(f'Skipping excluded interface: {interface}')
                        continue
                    wifi_interfaces.append(interface)

            # Sort by preference if preferred list is configured
            if config.interfaces.preferred:
                # Put preferred interfaces first, in order
                def sort_key(iface):
                    try:
                        return config.interfaces.preferred.index(iface)
                    except ValueError:
                        return len(config.interfaces.preferred) + 1

                wifi_interfaces.sort(key=sort_key)

            # Initialize cards
            for interface in wifi_interfaces:
                try:
                    card = WifiCard(interface, mac_manager=self.mac_manager)
                    self.cards.append(card)
                    logger.info(f'Initialized WiFi card: {interface}')
                except ValueError as e:
                    # Interface exists but isn't a valid wireless device
                    error_msg = f'Skipping {interface}: {e}'
                    logger.warning(error_msg)
                    self.initialization_errors.append(error_msg)
                except Exception as e:
                    error_msg = f'Failed to initialize wifi card {interface}: {e}'
                    logger.error(error_msg)
                    self.initialization_errors.append(error_msg)

            if not self.cards:
                logger.warning(
                    'No WiFi cards detected. System will operate in degraded mode. '
                    'Scanning and connection features will be unavailable.'
                )
                return 0

            # Designate scanning card based on config or first available
            scan_interface = config.interfaces.scan_interface
            if scan_interface:
                # Use configured scan interface if available
                for card in self.cards:
                    if card.interface == scan_interface:
                        self._scanning_card = card
                        logger.info(f'Designated {card.interface} as scanning card (from config)')
                        break
                if not self._scanning_card:
                    logger.warning(
                        f'Configured scan_interface {scan_interface} not found, '
                        'using first card as scanning card'
                    )

            # If no configured scan interface or it wasn't found, use first card
            if not self._scanning_card and self.cards:
                self._scanning_card = self.cards[0]
                logger.info(f'Designated {self._scanning_card.interface} as scanning card (auto)')

            return len(self.cards)

    def lease_card(self, for_scanning: bool = False,
                   holder: str = 'vasili') -> Optional[WifiCard]:
        """
        Get an available wifi card and mark it as in use.

        Lease state is persisted in MongoDB so it survives reboots and
        multiple processes can coordinate card access.

        Args:
            for_scanning: If True, returns the dedicated scanning card.
                         If False, returns an available connection card only.
            holder: Identifier of the plugin/module requesting the card.

        Returns:
            Available WifiCard or None if no cards available
        """
        role = 'scanning' if for_scanning else 'connection'
        with self._lock:
            if for_scanning:
                if self._scanning_card and not self._scanning_card.in_use:
                    if self.lease_store.acquire(
                        self._scanning_card.interface, holder, role='scanning'
                    ):
                        self._scanning_card.in_use = True
                        return self._scanning_card
                return None
            else:
                for card in self.cards:
                    if card == self._scanning_card:
                        continue
                    if card == self._hostap_card:
                        continue
                    if not card.in_use:
                        if self.lease_store.acquire(
                            card.interface, holder, role='connection'
                        ):
                            card.in_use = True
                            card.current_task = getattr(self._pending_tasks, 'task', None)
                            return card
                return None

    def get_card(self) -> Optional[WifiCard]:
        """Alias for lease_card() for backwards compatibility with modules."""
        return self.lease_card()

    def on_card_returned(self, callback):
        """Register a callback invoked after a card is returned to the pool.

        The callback receives the returned WifiCard as its sole argument and
        runs *outside* the card-manager lock so it may safely call
        ``set_hostap_card`` or ``lease_card``.
        """
        self._on_card_returned_callbacks.append(callback)

    def return_card(self, card: WifiCard, holder: str = 'vasili'):
        """Return a card to the pool of available cards."""
        returned_card = None
        with self._lock:
            if card in self.cards:
                # Defensive: clean up routing isolation if still active
                routing_info = getattr(card, '_routing_info', None)
                if routing_info:
                    network_isolation.teardown_interface_routing(
                        card.interface, routing_info
                    )
                    card._routing_info = None
                # Ensure card is back in managed mode for next user
                card.ensure_managed()
                card.in_use = False
                card.current_task = None
                self.lease_store.release(card.interface, holder)
                returned_card = card

        # Fire callbacks outside the lock
        if returned_card:
            for cb in self._on_card_returned_callbacks:
                try:
                    cb(returned_card)
                except Exception as e:
                    logger.error(f'Card-returned callback error: {e}')

    def get_all_cards(self) -> list[WifiCard]:
        """Get list of all wifi cards."""
        with self._lock:
            return list(self.cards)

    def get_available_count(self) -> int:
        """Get count of cards not currently in use."""
        with self._lock:
            return sum(1 for card in self.cards if not card.in_use)

    def audit_lease_state(self) -> list[str]:
        """Compare in-memory ``card.in_use`` flags against persisted leases.

        Detects two drift classes that have historically caused scheduler
        bugs (see commit 1d449d1 ``Bug in card lease logic``):

          - **in_use without lease**: ``card.in_use=True`` but no live lease
            row exists in MongoDB. Means we think we hold it but the store
            doesn't agree — a held-forever orphan that blocks all future
            ``lease_card`` calls for that interface.
          - **lease without in_use**: a live lease row exists for a card
            whose ``in_use=False``. Means ``return_card`` partially failed
            (DB release didn't happen, e.g. transient Mongo error) and the
            row will block other holders until the TTL expires.

        Returns the list of human-readable violations (empty = healthy).
        Violations are also logged at ERROR so periodic callers needn't
        check the return value to surface problems.

        The HostAP slot (``_hostap_card``) is deliberately excluded: it
        manages ``in_use`` directly without going through the lease store,
        and reporting it would be a permanent false positive.
        """
        violations: list[str] = []
        if not self.lease_store.is_available():
            return violations

        with self._lock:
            hostap_iface = self._hostap_card.interface if self._hostap_card else None
            in_memory = {c.interface: c.in_use for c in self.cards
                         if c.interface != hostap_iface}

        try:
            live_leases = {row['interface']: row
                           for row in self.lease_store.get_all_leases()}
        except Exception as e:
            logger.error(f'audit_lease_state: failed to read leases: {e}')
            return violations

        for iface, in_use in in_memory.items():
            has_lease = iface in live_leases
            if in_use and not has_lease:
                msg = (
                    f'LEASE INVARIANT VIOLATION: {iface}.in_use=True but no '
                    f'live lease row exists. Orphaned in-memory lease — card '
                    f'will never be re-leased until vasili restarts.'
                )
                logger.error(msg)
                violations.append(msg)
            elif (not in_use) and has_lease:
                holder = live_leases[iface].get('holder', '<unknown>')
                msg = (
                    f'LEASE INVARIANT VIOLATION: {iface}.in_use=False but DB '
                    f"lease row held by '{holder}' is still live. Orphan "
                    f'lease row blocks other holders until TTL expiry.'
                )
                logger.error(msg)
                violations.append(msg)
        return violations

    def has_cards(self) -> bool:
        """Check if any WiFi cards are available."""
        with self._lock:
            return len(self.cards) > 0

    def get_scanning_card(self) -> Optional[WifiCard]:
        """
        Get the dedicated scanning card.

        Returns:
            The WifiCard designated for scanning, or None if not assigned
        """
        with self._lock:
            return self._scanning_card

    def get_connection_cards(self) -> list[WifiCard]:
        """
        Get all cards available for connections (excludes scanning card).

        Returns:
            List of WifiCards available for connections
        """
        with self._lock:
            return [card for card in self.cards
                    if card != self._scanning_card and card != self._hostap_card]

    def get_status(self) -> dict:
        """Get status information about WiFi cards."""
        with self._lock:
            return {
                'total_cards': len(self.cards),
                'available_cards': sum(1 for c in self.cards if not c.in_use),
                'in_use_cards': sum(1 for c in self.cards if c.in_use),
                'card_interfaces': [c.interface for c in self.cards],
                'initialization_errors': self.initialization_errors,
                'scanning_card': self._scanning_card.interface if self._scanning_card else None,
                'hostap_card': self._hostap_card.interface if self._hostap_card else None,
                'connection_cards': [c.interface for c in self.cards
                                     if c != self._scanning_card and c != self._hostap_card],
                'active_leases': self.lease_store.get_all_leases(),
            }

    def set_hostap_card(self, interface: str) -> Optional[WifiCard]:
        """Reserve a card for HostAP use. Returns the card or None."""
        with self._lock:
            for card in self.cards:
                if card.interface == interface:
                    if card == self._scanning_card:
                        return None
                    if card.in_use:
                        return None
                    card.in_use = True
                    self._hostap_card = card
                    card.current_task = {'module': 'HostAP', 'ssid': '', 'started_at': time.time()}
                    return card
            return None

    def clear_hostap_card(self, fire_callbacks: bool = True) -> Optional[WifiCard]:
        """Return the hostap card to the pool.

        Re-hands the interface to NetworkManager (managed mode) and
        optionally fires card-returned callbacks.

        Args:
            fire_callbacks: If False, skip card-returned callbacks.
                This prevents infinite retry loops when ``start_hostap``
                fails and ``clear_hostap_card`` is called from within
                the same callback chain.
        """
        card = None
        with self._lock:
            if self._hostap_card:
                # Bring the card back under NM control
                self._hostap_card.ensure_managed()
                self._hostap_card.in_use = False
                self._hostap_card.current_task = None
                card = self._hostap_card
                self._hostap_card = None

        # Fire callbacks outside the lock (same as return_card)
        if card and fire_callbacks:
            for cb in self._on_card_returned_callbacks:
                try:
                    cb(card)
                except Exception as e:
                    logger.error(f'Card-returned callback error: {e}')

        return card


class ProbeHistory:
    """Stores observed BSSID→SSID mappings from scan results.

    When the scanning card sees a BSSID with a non-empty SSID, it records
    the observation. Later, if the same BSSID appears as a hidden network
    (empty SSID), the HiddenNetworkModule can look up the SSID from history.

    This works because some APs alternate between broadcasting and hiding
    their SSID, or because directed probe responses were captured in a
    previous scan cycle.
    """

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili'):
        self._available = False
        self._cache: dict[str, str] = {}  # bssid -> ssid (in-memory)

        try:
            from pymongo import MongoClient
            from pymongo.errors import ConnectionFailure
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            client.admin.command('ping')
            self.collection = client[db_name]['probe_history']
            self.collection.create_index('bssid', unique=True)
            self._available = True

            # Load existing history into cache
            for doc in self.collection.find({}, {'_id': 0, 'bssid': 1, 'ssid': 1}):
                self._cache[doc['bssid'].lower()] = doc['ssid']

            logger.debug(f'ProbeHistory loaded {len(self._cache)} entries')
        except Exception as e:
            logger.debug(f'ProbeHistory MongoDB unavailable: {e}')

    def record(self, bssid: str, ssid: str):
        """Record an observed BSSID→SSID mapping."""
        if not ssid:
            return
        bssid_lower = bssid.lower()
        self._cache[bssid_lower] = ssid

        if self._available:
            try:
                self.collection.update_one(
                    {'bssid': bssid_lower},
                    {'$set': {
                        'bssid': bssid_lower,
                        'ssid': ssid,
                        'last_seen': time.time(),
                    }},
                    upsert=True,
                )
            except Exception:
                pass

    def record_batch(self, networks: list):
        """Record all non-hidden networks from a scan batch."""
        for net in networks:
            if net.ssid and net.bssid:
                self.record(net.bssid, net.ssid)

    def lookup(self, bssid: str) -> Optional[str]:
        """Look up a previously observed SSID for a BSSID."""
        return self._cache.get(bssid.lower())


class NetworkScanner:
    def __init__(self, card_manager, probe_history: ProbeHistory = None):
        self.card_manager = card_manager
        self.scan_results: list[WifiNetwork] = []
        self.scanning = False
        self.scan_thread = None
        self.scan_queue = queue.Queue()
        self.probe_history = probe_history

    def start_scan(self):
        """Start scanning for wifi networks in a background thread"""
        if self.scanning:
            return

        self.scanning = True
        self.scan_thread = threading.Thread(target=self._scan_worker)
        self.scan_thread.daemon = True
        self.scan_thread.start()

    def stop_scan(self):
        """Stop the background scanning thread"""
        self.scanning = False
        if self.scan_thread:
            self.scan_thread.join()
            self.scan_thread = None

    def _scan_worker(self):
        """
        Background worker that continuously scans for networks.

        Uses the dedicated scanning card to prevent interference with
        connection operations on other cards.
        """
        config = get_config()
        scan_interval = config.scanner.scan_interval

        while self.scanning:
            card = None
            try:
                # Get the dedicated scanning card
                card = self.card_manager.lease_card(for_scanning=True)
                if not card:
                    logger.warning('Scanning card not available, waiting...')
                    time.sleep(1)
                    continue

                logger.debug(f'Starting scan on dedicated card: {card.interface}')

                # Scan for networks
                networks = card.scan()
                self.scan_results = networks
                self.scan_queue.put(networks)

                # Record BSSID→SSID observations for hidden network resolution
                if self.probe_history:
                    self.probe_history.record_batch(networks)

                logger.debug(f'Scan completed, found {len(networks)} networks')

                # Wait before scanning again
                time.sleep(scan_interval)

            except Exception as e:
                logger.error(f'Error during network scan: {e}')
                time.sleep(1)
            finally:
                # Always return the card if we leased one
                if card:
                    self.card_manager.return_card(card)

    def get_scan_results(self) -> list[WifiNetwork]:
        """Get the most recent scan results"""
        return self.scan_results

    def get_next_scan(self) -> list[WifiNetwork]:
        """Wait for and return the next scan results"""
        return self.scan_queue.get()


class ConnectionMonitor:
    """
    Monitors active WiFi connections and triggers reconnection when drops are detected.

    Two delivery paths exist:
      - **D-Bus path** (preferred): subscribes to NetworkManager's
        `org.freedesktop.NetworkManager.Device.StateChanged` and reacts on
        transitions out of ACTIVATED in sub-second time, with no nmcli polling.
      - **Poll path** (fallback): runs the legacy `is_connected()` / `get_connected_ssid()`
        check every `check_interval` seconds. Used when dbus-python / GLib aren't
        importable (CI, slim dev environments) or when NM isn't on the bus.
    """

    # NetworkManager NMDeviceState values; see
    # https://developer.gnome.org/NetworkManager/stable/nm-dbus-types.html
    _NM_STATE_DISCONNECTED = 30
    _NM_STATE_ACTIVATED = 100
    _NM_STATE_FAILED = 120

    def __init__(self, check_interval: float = 10.0, max_reconnect_attempts: int = 5):
        """
        Initialize the connection monitor.

        Args:
            check_interval: Poll-path tick interval in seconds (default: 10).
                Unused on the D-Bus path (signals are pushed).
            max_reconnect_attempts: Maximum reconnection attempts before giving up (default: 5)
        """
        self.check_interval = check_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        self._monitored_cards: list[WifiCard] = []
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._reconnect_callbacks: list = []
        # Shared between the poll and D-Bus paths so they can't race on the same card.
        self._reconnect_attempts: dict[str, int] = {}
        self._handler_locks: dict[str, threading.Lock] = {}
        # D-Bus path state — populated only when GLib mainloop starts cleanly.
        self._dbus_loop = None
        self._dbus_thread: Optional[threading.Thread] = None
        self._using_dbus = False

    def add_card(self, card: WifiCard):
        """Add a card to be monitored for connection drops."""
        with self._lock:
            if card not in self._monitored_cards:
                self._monitored_cards.append(card)
                self._handler_locks.setdefault(card.interface, threading.Lock())
                logger.info(f'Added {card.interface} to connection monitor')

    def remove_card(self, card: WifiCard):
        """Remove a card from monitoring."""
        with self._lock:
            if card in self._monitored_cards:
                self._monitored_cards.remove(card)
                logger.info(f'Removed {card.interface} from connection monitor')

    def on_reconnect(self, callback):
        """Register a callback to be called after reconnection attempts."""
        self._reconnect_callbacks.append(callback)

    def start(self):
        """Start the connection monitoring thread.

        Tries the D-Bus signal path first; falls back to polling if dbus-python /
        GLib / NetworkManager on the bus aren't all available.
        """
        if self._monitoring:
            return

        self._monitoring = True
        if self._try_start_dbus():
            self._using_dbus = True
            logger.info('Connection monitor started (D-Bus signal path)')
        else:
            self._using_dbus = False
            self._monitor_thread = threading.Thread(target=self._poll_worker, daemon=True)
            self._monitor_thread.start()
            logger.info('Connection monitor started (poll path)')

    def stop(self):
        """Stop the connection monitoring thread."""
        self._monitoring = False
        if self._dbus_loop is not None:
            try:
                self._dbus_loop.quit()
            except Exception as e:
                logger.debug(f'Error stopping D-Bus mainloop: {e}')
        if self._dbus_thread:
            self._dbus_thread.join(timeout=2.0)
            self._dbus_thread = None
        if self._monitor_thread:
            self._monitor_thread.join(timeout=self.check_interval + 1)
            self._monitor_thread = None
        self._dbus_loop = None
        self._using_dbus = False
        logger.info('Connection monitor stopped')

    def _try_start_dbus(self) -> bool:
        """Wire up NM D-Bus signal subscription. Returns False on any failure."""
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib
        except ImportError as e:
            logger.info(f'D-Bus monitor unavailable ({e}); will poll instead')
            return False

        try:
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            # Probe NM exists; this raises if NetworkManager isn't on the bus.
            bus.get_object('org.freedesktop.NetworkManager', '/org/freedesktop/NetworkManager')
            bus.add_signal_receiver(
                self._on_device_state_changed,
                signal_name='StateChanged',
                dbus_interface='org.freedesktop.NetworkManager.Device',
                path_keyword='path',
            )
            self._dbus_loop = GLib.MainLoop()
            self._dbus_thread = threading.Thread(target=self._dbus_loop.run, daemon=True)
            self._dbus_thread.start()
            return True
        except Exception as e:
            logger.warning(f'D-Bus monitor init failed ({e}); falling back to polling')
            return False

    def _on_device_state_changed(self, new_state, old_state, reason, path=None):
        """NM Device StateChanged handler — runs on the GLib mainloop thread."""
        try:
            new_s = int(new_state)
            old_s = int(old_state)
        except Exception:
            return
        # Only react when we just *lost* an active connection. Transitions
        # into ACTIVATED, or churn between non-active states, aren't drops.
        if old_s != self._NM_STATE_ACTIVATED:
            return
        if new_s not in (self._NM_STATE_DISCONNECTED, self._NM_STATE_FAILED):
            return
        iface = self._resolve_iface_from_path(path)
        if not iface:
            return
        card = self._find_card_by_iface(iface)
        if card is None or card._connected_network is None:
            return
        # Don't block the GLib thread; reconnect() takes seconds.
        threading.Thread(
            target=self._handle_drop, args=(card,), daemon=True
        ).start()

    def _resolve_iface_from_path(self, path) -> Optional[str]:
        """Look up the Linux iface name (wlan0, …) for an NM Device object path."""
        if not path:
            return None
        try:
            import dbus
            bus = dbus.SystemBus()
            dev = bus.get_object('org.freedesktop.NetworkManager', path)
            props = dbus.Interface(dev, 'org.freedesktop.DBus.Properties')
            return str(props.Get('org.freedesktop.NetworkManager.Device', 'Interface'))
        except Exception as e:
            logger.debug(f'Failed to resolve device path {path}: {e}')
            return None

    def _find_card_by_iface(self, iface: str) -> Optional[WifiCard]:
        with self._lock:
            for card in self._monitored_cards:
                if card.interface == iface:
                    return card
        return None

    def _handle_drop(self, card: WifiCard) -> None:
        """Run the reconnect-attempt state machine for one dropped card.

        Shared between the poll path and the D-Bus signal path. The per-card
        lock serialises overlapping triggers (e.g. flapping causes multiple
        StateChanged signals in quick succession).
        """
        lock = self._handler_locks.setdefault(card.interface, threading.Lock())
        if not lock.acquire(blocking=False):
            # Another drop is already being handled for this card; skip.
            return
        try:
            if card._connected_network is None:
                return
            expected_ssid = card._connected_network.ssid
            attempts = self._reconnect_attempts.get(card.interface, 0)

            if attempts >= self.max_reconnect_attempts:
                logger.error(
                    f'Max reconnect attempts ({self.max_reconnect_attempts}) reached for '
                    f'{card.interface}. Giving up on {expected_ssid}.'
                )
                card._connected_network = None
                card._connection_password = None
                self._reconnect_attempts[card.interface] = 0
                self._notify_callbacks(card, success=False)
                return

            logger.warning(
                f'Connection dropped on {card.interface} (expected: {expected_ssid}). '
                f'Attempting reconnect ({attempts + 1}/{self.max_reconnect_attempts})...'
            )
            self._reconnect_attempts[card.interface] = attempts + 1
            success = card.reconnect(max_retries=2, base_delay=0.5)
            if success:
                logger.info(f'Successfully reconnected {card.interface} to {expected_ssid}')
                self._reconnect_attempts[card.interface] = 0
                self._notify_callbacks(card, success=True)
            else:
                logger.warning(
                    f'Reconnection attempt {attempts + 1} failed for {card.interface}'
                )
        finally:
            lock.release()

    def _poll_worker(self):
        """Fallback worker: periodically check health and dispatch drops.

        Used only when the D-Bus path can't start (no dbus-python, no GLib,
        or NetworkManager not on the system bus).
        """
        while self._monitoring:
            with self._lock:
                cards_to_check = list(self._monitored_cards)

            for card in cards_to_check:
                if not self._monitoring:
                    break
                if card._connected_network is None:
                    continue

                expected_ssid = card._connected_network.ssid
                is_connected = card.is_connected()
                current_ssid = card.get_connected_ssid() if is_connected else None

                if is_connected and current_ssid == expected_ssid:
                    self._reconnect_attempts[card.interface] = 0
                    continue

                self._handle_drop(card)

            time.sleep(self.check_interval)

    def _notify_callbacks(self, card: WifiCard, success: bool):
        """Notify registered callbacks of reconnection result."""
        for callback in self._reconnect_callbacks:
            try:
                callback(card, success)
            except Exception as e:
                logger.error(f'Error in reconnect callback: {e}')

    def get_monitored_cards(self) -> list[WifiCard]:
        """Get list of currently monitored cards."""
        with self._lock:
            return list(self._monitored_cards)


class AutoSelector:
    """
    Automatically selects and uses the best available connection.

    This class periodically evaluates all available connections and automatically
    switches to a better connection if the score improvement exceeds the configured
    threshold.
    """

    def __init__(
        self,
        wifi_manager,
        evaluation_interval: int = 30,
        min_score_improvement: float = 10.0,
        initial_delay: int = 10,
    ):
        """
        Initialize the auto-selector.

        Args:
            wifi_manager: Reference to the WifiManager instance
            evaluation_interval: Seconds between connection evaluations (default: 30)
            min_score_improvement: Minimum score improvement to trigger switch (default: 10.0)
            initial_delay: Seconds to wait before first evaluation (default: 10)
        """
        self.wifi_manager = wifi_manager
        self.evaluation_interval = evaluation_interval
        self.min_score_improvement = min_score_improvement
        self.initial_delay = initial_delay
        self._enabled = False
        self._running = False
        self._selector_thread: Optional[threading.Thread] = None
        self._last_switch_time: float = 0
        self._evaluation_count = 0

    def enable(self):
        """Enable auto-selection mode."""
        if self._enabled:
            return
        self._enabled = True
        logger.info('Auto-selection mode enabled')
        emit_status_update()

    def disable(self):
        """Disable auto-selection mode."""
        if not self._enabled:
            return
        self._enabled = False
        logger.info('Auto-selection mode disabled')
        emit_status_update()

    def is_enabled(self) -> bool:
        """Check if auto-selection is enabled."""
        return self._enabled

    def start(self):
        """Start the auto-selection thread."""
        if self._running:
            return

        self._running = True
        self._selector_thread = threading.Thread(target=self._selector_worker, daemon=True)
        self._selector_thread.start()
        logger.info('Auto-selector thread started')

    def stop(self):
        """Stop the auto-selection thread."""
        self._running = False
        if self._selector_thread:
            self._selector_thread.join(timeout=self.evaluation_interval + 5)
            self._selector_thread = None
        logger.info('Auto-selector thread stopped')

    def _selector_worker(self):
        """Background worker that evaluates and switches connections."""
        # Wait for initial delay to allow connections to stabilize
        if self.initial_delay > 0:
            logger.info(f'Auto-selector waiting {self.initial_delay}s before first evaluation')
            time.sleep(self.initial_delay)

        while self._running:
            if self._enabled:
                try:
                    self._evaluate_and_switch()
                except Exception as e:
                    logger.error(f'Error in auto-selector evaluation: {e}')

            # Wait before next evaluation
            time.sleep(self.evaluation_interval)

    def _evaluate_and_switch(self):
        """Evaluate available connections and switch if a better one is found."""
        self._evaluation_count += 1

        # A Bridge Override pins the bridge — never auto-switch away from it.
        if self.wifi_manager._bridge_override_iface:
            logger.debug('Auto-selector idle: bridge override active')
            return

        # Get current bridge info
        current_bridge = self.wifi_manager.status.get('current_bridge')
        if not current_bridge:
            # No active bridge, try to select best available connection
            logger.debug('No active connection, attempting to select best available')
            self._select_best_connection()
            return

        current_ssid = current_bridge.get('ssid')
        current_interface = current_bridge.get('wifi_interface')

        # Find current connection in suitable_connections
        current_connection = None
        for conn in self.wifi_manager.suitable_connections:
            if conn.network.ssid == current_ssid and conn.interface == current_interface:
                current_connection = conn
                break

        if not current_connection:
            logger.warning(f'Current connection {current_ssid} not found in suitable_connections')
            return

        current_score = current_connection.calculate_score()

        # Get all available connections sorted by score
        sorted_connections = self.wifi_manager.get_sorted_connections()

        if not sorted_connections:
            logger.debug('No connections available for evaluation')
            return

        # Find the best connection
        best_connection = sorted_connections[0]
        best_score = best_connection.calculate_score()

        # Check if we should switch
        score_improvement = best_score - current_score

        logger.debug(
            f'Auto-selector evaluation #{self._evaluation_count}: '
            f'Current={current_ssid} (score={current_score:.2f}), '
            f'Best={best_connection.network.ssid} (score={best_score:.2f}), '
            f'Improvement={score_improvement:.2f}'
        )

        if score_improvement >= self.min_score_improvement:
            # Find the index of the best connection
            try:
                best_index = self.wifi_manager.suitable_connections.index(best_connection)
            except ValueError:
                logger.error('Best connection not found in suitable_connections list')
                return

            logger.info(
                f'Auto-selector switching from {current_ssid} (score={current_score:.2f}) '
                f'to {best_connection.network.ssid} (score={best_score:.2f}), '
                f'improvement={score_improvement:.2f}'
            )

            # Perform the switch
            success = self.wifi_manager.use_connection(best_index)

            if success:
                self._last_switch_time = time.time()
                logger.info(
                    f'Auto-selector successfully switched to {best_connection.network.ssid}'
                )
                emit_status_update()
                emit_connections_update()
            else:
                logger.error(f'Auto-selector failed to switch to {best_connection.network.ssid}')
        else:
            logger.debug(
                f'Current connection is optimal (improvement={score_improvement:.2f} < '
                f'threshold={self.min_score_improvement})'
            )

    def _select_best_connection(self):
        """Select and use the best available connection when none is active."""
        # A Bridge Override owns the bridge; don't let the selector claim one.
        if self.wifi_manager._bridge_override_iface:
            return
        sorted_connections = self.wifi_manager.get_sorted_connections()

        if not sorted_connections:
            logger.debug('No connections available to select')
            return

        best_connection = sorted_connections[0]
        best_score = best_connection.calculate_score()

        try:
            best_index = self.wifi_manager.suitable_connections.index(best_connection)
        except ValueError:
            logger.error('Best connection not found in suitable_connections list')
            return

        logger.info(
            f'Auto-selector selecting best connection: {best_connection.network.ssid} '
            f'(score={best_score:.2f})'
        )

        success = self.wifi_manager.use_connection(best_index)

        if success:
            self._last_switch_time = time.time()
            logger.info(f'Auto-selector activated connection to {best_connection.network.ssid}')
            emit_status_update()
            emit_connections_update()
        else:
            logger.error(f'Auto-selector failed to activate {best_connection.network.ssid}')

    def get_stats(self) -> dict:
        """Get auto-selector statistics."""
        return {
            'enabled': self._enabled,
            'running': self._running,
            'evaluation_count': self._evaluation_count,
            'last_switch_time': self._last_switch_time,
            'evaluation_interval': self.evaluation_interval,
            'min_score_improvement': self.min_score_improvement,
        }


def run_interface_speedtest(interface: str) -> tuple[float, float, float]:
    """Run a speedtest bound to a specific interface's IP.

    Verifies actual internet connectivity through the interface before
    running the speedtest. Binding to the interface IP (rather than using
    the device's default route) prevents false-positive results from
    traffic routing through another connection on the device — e.g. a
    USB-C / ethernet NIC.

    Args:
        interface: Network interface to bind the speedtest to.

    Returns:
        Tuple of (download_mbps, upload_mbps, ping_ms).

    Raises:
        ConnectionError: If the interface has no IP or no internet.
    """
    ip = network_isolation.get_interface_ip(interface)
    if not ip:
        raise ConnectionError(f'No IP address on {interface}')

    if not network_isolation.verify_connectivity(interface):
        raise ConnectionError(f'No internet connectivity via {interface}')

    st = speedtest.Speedtest(source_address=ip)
    st.get_best_server()
    download = st.download() / 1_000_000
    upload = st.upload() / 1_000_000
    ping = st.results.ping
    return download, upload, ping


class ConnectionModule:
    # Base class for connection modules
    priority = 50  # Lower = runs first in scan loop

    def __init__(self, card_manager, module_config=None, **kwargs):
        self.card_manager = card_manager
        self._module_config = module_config

    def get_module_config(self) -> dict:
        """Get this module's current config values from the config store."""
        if self._module_config:
            name = getattr(self, 'name', self.__class__.__name__)
            return self._module_config.get_config(name)
        # Fall back to schema defaults
        schema = self.get_config_schema()
        return {k: v.get('default') for k, v in schema.items()}

    def can_connect(self, network: WifiNetwork) -> bool:
        raise NotImplementedError()

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        raise NotImplementedError()

    def run_speedtest(self, card, interface_override=None) -> tuple[float, float, float]:
        """Run a speedtest bound to the card's (or tunnel's) interface IP.

        Verifies actual internet connectivity through the interface
        before running the speedtest, preventing false results from
        traffic routing through other interfaces (e.g. eth0).

        Args:
            card: WifiCard that is connected to a network
            interface_override: Use this interface instead of card.interface
                (e.g. a tunnel interface like dns0)

        Returns:
            Tuple of (download_mbps, upload_mbps, ping_ms)

        Raises:
            ConnectionError: If no IP or no internet on the interface
        """
        interface = interface_override or card.interface
        return run_interface_speedtest(interface)


class PipelineStage:
    """A stage within a pipeline that runs against an already-connected card.

    Stages communicate via a shared context dict. Each stage can read
    context set by previous stages and add its own findings.
    """
    name: str = 'unnamed'
    requires_consent: bool = False

    # Cached/overridden config. Left None in production so _get_stage_config
    # reads fresh from the store each call (picks up live edits/imports);
    # tests set it directly as a seam. Injected store reference set by the
    # owning PipelineModule.
    _stage_config: dict | None = None
    _module_config = None

    def can_run(self, network: WifiNetwork, card, context: dict) -> bool:
        """Check if this stage should run given current context."""
        raise NotImplementedError()

    def run(self, network: WifiNetwork, card, context: dict) -> StageResult:
        """Execute this stage. Card is already connected to the network."""
        raise NotImplementedError()

    def get_config_schema(self) -> dict:
        """Return config schema for this stage.

        Returns dict of {key: {type, default, description}}.
        """
        return {}

    def _get_stage_config(self) -> dict:
        """Effective config: schema defaults overlaid with stored overrides.

        Stored overrides live in the ``module_config`` store keyed by the
        stage's ``name`` (set via the settings UI or the helper-config
        importer). A directly-set ``_stage_config`` (used by tests) wins and
        short-circuits the store lookup. Recomputed each call so config edits
        and helper imports take effect without a restart.
        """
        if self._stage_config is not None:
            return self._stage_config
        schema = self.get_config_schema()
        cfg = {k: v.get('default') for k, v in schema.items()}
        store = getattr(self, '_module_config', None)
        if store is not None:
            try:
                stored = store.get_config(self.name)
                cfg.update({k: v for k, v in stored.items() if v is not None})
            except Exception as exc:
                logger.debug('Stage %s config load failed: %s', self.name, exc)
        return cfg


class PipelineModule(ConnectionModule):
    """Orchestrates a pipeline of phases for a network type.

    Each phase is either a single ``PipelineStage`` (runs sequentially) or
    a **list** of stages (runs in parallel — best result wins).  This lets
    discovery stages build context sequentially while exploitation strategies
    (e.g. captive-portal bypass vs DNS tunnel) race against each other.

    Backward compatible: passing ``stages=[...]`` wraps each stage as a
    sequential phase.
    """
    priority = 10  # Pipelines run before simple modules

    # If True, pipeline connects to the network before running stages.
    # Set to False for encrypted networks where stages handle credentials.
    auto_connect = True

    def __init__(self, card_manager, stages: list[PipelineStage] = None,
                 phases: list = None,
                 consent_manager=None, module_config=None,
                 pipeline_config=None, **kwargs):
        super().__init__(card_manager, module_config=module_config)
        if phases is not None:
            default_phases = phases
        else:
            default_phases = list(stages or [])

        # Record hard-coded defaults so the pipeline-builder UI can offer
        # a "reset" action, then apply any user-customised layout on top.
        self.default_phases = default_phases
        self.pipeline_config = pipeline_config
        if pipeline_config is not None:
            pipeline_config.register_defaults(
                self.__class__.__name__, default_phases,
            )
            custom = pipeline_config.get_layout(self.__class__.__name__)
            if custom:
                self.phases = self._hydrate_phases(custom) or default_phases
            else:
                self.phases = default_phases
        else:
            self.phases = default_phases

        # Flat list of all stages for API introspection (/api/modules)
        self.stages = self._flatten_phases(self.phases)
        # Wire the config store into each stage so user/helper overrides from
        # the module_config store (keyed by stage name) reach the stage at
        # runtime. Without this, stages only ever see schema defaults.
        for stage in self.stages:
            stage._module_config = module_config
        self.consent_manager = consent_manager
        self.last_stage_log: list[dict] = []

    def _hydrate_phases(self, layout: list) -> list:
        """Rebuild ``phases`` from a saved ``[name | [name, ...]]`` layout.

        Unknown stage names are dropped with a warning rather than
        crashing the module — the UI surfaces the same registry so this
        should only happen when a saved layout outlives a stage.
        """
        from modules.stages import get_stage_registry
        registry = get_stage_registry()
        rebuilt: list = []
        for phase in layout:
            if isinstance(phase, list):
                instances = []
                for name in phase:
                    cls = registry.get(name)
                    if cls is None:
                        logger.warning(
                            f'Pipeline layout for {self.__class__.__name__} '
                            f'references unknown stage {name!r} — skipping'
                        )
                        continue
                    instances.append(cls())
                if len(instances) >= 2:
                    rebuilt.append(instances)
                elif len(instances) == 1:
                    rebuilt.append(instances[0])
            else:
                cls = registry.get(phase)
                if cls is None:
                    logger.warning(
                        f'Pipeline layout for {self.__class__.__name__} '
                        f'references unknown stage {phase!r} — skipping'
                    )
                    continue
                rebuilt.append(cls())
        return rebuilt

    @staticmethod
    def _flatten_phases(phases: list) -> list[PipelineStage]:
        flat = []
        for phase in phases:
            if isinstance(phase, list):
                flat.extend(phase)
            else:
                flat.append(phase)
        return flat

    def _has_consent(self, stage_name: str, network: WifiNetwork = None) -> bool:
        if self.consent_manager:
            bssid = network.bssid if network else None
            ssid = network.ssid if network else None
            group_by_ssid = getattr(
                self.consent_manager, 'group_by_ssid', True
            )
            return self.consent_manager.has_consent(
                stage_name, bssid=bssid, ssid=ssid,
                group_by_ssid=group_by_ssid,
            )
        return False

    def _get_connect_context(self) -> dict:
        """Return initial context for a connect() call.

        Override in subclasses to inject data like password lists.
        """
        return {}

    @staticmethod
    def _teardown_tunnel(context: dict):
        """Tear down any active tunnel stored in the pipeline context."""
        helper = context.get('_tunnel_helper')
        if helper is None:
            return
        try:
            helper.teardown()
        except Exception as exc:
            logger.warning(f'Tunnel teardown error: {exc}')

    # ------------------------------------------------------------------
    # Stage execution helpers
    # ------------------------------------------------------------------

    def _is_stage_eligible(self, stage: PipelineStage,
                           network: WifiNetwork, card, context: dict) -> bool:
        """Check consent and can_run; log skips. Returns True if stage should run."""
        if stage.requires_consent and not self._has_consent(stage.name, network):
            self.last_stage_log.append({
                'stage': stage.name, 'status': 'skipped',
                'reason': 'no_consent', 'timestamp': time.time(),
            })
            return False
        try:
            if not stage.can_run(network, card, context):
                self.last_stage_log.append({
                    'stage': stage.name, 'status': 'skipped',
                    'reason': 'can_run=False', 'timestamp': time.time(),
                })
                return False
        except Exception:
            return False
        return True

    def _run_single_stage(self, stage: PipelineStage,
                          network: WifiNetwork, card,
                          context: dict) -> Optional[StageResult]:
        """Run one stage sequentially. Returns None if skipped."""
        if not self._is_stage_eligible(stage, network, card, context):
            return None

        try:
            logger.info(f'Pipeline stage: {stage.name} on {network.ssid}')
            t0 = time.time()
            result = stage.run(network, card, context)
            elapsed = round(time.time() - t0, 2)
            context.update(result.context_updates)

            self.last_stage_log.append({
                'stage': stage.name,
                'status': (
                    'internet' if result.has_internet else
                    'stopped' if result.stop_pipeline else
                    'success' if result.success else 'failed'
                ),
                'message': result.message,
                'duration': elapsed,
                'context': dict(result.context_updates),
                'timestamp': time.time(),
            })
            return result

        except Exception as e:
            logger.error(f'Stage {stage.name} error: {e}')
            self.last_stage_log.append({
                'stage': stage.name, 'status': 'error',
                'message': str(e)[:200], 'timestamp': time.time(),
            })
            return None

    def _run_parallel_phase(self, stages: list[PipelineStage],
                            network: WifiNetwork, card,
                            context: dict) -> Optional[StrategyResult]:
        """Run multiple stages in parallel. Returns best StrategyResult or None."""
        eligible = [s for s in stages
                    if self._is_stage_eligible(s, network, card, context)]
        if not eligible:
            return None

        ctx_snapshot = context.copy()
        results: list[tuple[PipelineStage, StageResult]] = []

        logger.info(
            'Parallel phase: running %s on %s',
            [s.name for s in eligible], network.ssid,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(eligible)) as pool:
            futs = {
                pool.submit(s.run, network, card, ctx_snapshot.copy()): s
                for s in eligible
            }
            for fut in concurrent.futures.as_completed(futs):
                stage = futs[fut]
                t0 = time.time()
                try:
                    result = fut.result()
                    results.append((stage, result))
                    self.last_stage_log.append({
                        'stage': stage.name,
                        'status': 'internet' if result.has_internet else
                                  'success' if result.success else 'failed',
                        'message': result.message,
                        'context': dict(result.context_updates),
                        'parallel': True,
                        'timestamp': time.time(),
                    })
                except Exception as e:
                    logger.error(f'Parallel stage {stage.name} error: {e}')
                    self.last_stage_log.append({
                        'stage': stage.name, 'status': 'error',
                        'message': str(e)[:200], 'parallel': True,
                        'timestamp': time.time(),
                    })

        winners = [(s, r) for s, r in results if r.has_internet]

        if not winners:
            # No internet — merge discovery context from all results
            for _, r in results:
                context.update(r.context_updates)
            return None

        # Single winner — tear down losers, return immediately
        if len(winners) == 1:
            best_s, best_r = winners[0]
            for s, r in results:
                if r is not best_r:
                    self._teardown_tunnel(r.context_updates)
            context.update(best_r.context_updates)
            return StrategyResult(
                stage_name=best_s.name,
                stage_result=best_r,
                context_updates=best_r.context_updates,
            )

        # Multiple winners — speedtest each to pick best
        logger.info('Multiple strategies succeeded — comparing speeds')
        candidates: list[StrategyResult] = []
        for stage, result in winners:
            tunnel_iface = result.context_updates.get('tunnel_interface')
            try:
                dl, ul, pg = self.run_speedtest(
                    card, interface_override=tunnel_iface,
                )
            except Exception:
                dl, ul, pg = 0.0, 0.0, 999.0
            candidates.append(StrategyResult(
                stage_name=stage.name,
                stage_result=result,
                context_updates=result.context_updates,
                download_speed=dl, upload_speed=ul, ping=pg,
            ))

        candidates.sort(key=lambda c: c.download_speed, reverse=True)
        best = candidates[0]
        logger.info(
            'Best strategy: %s (%.1f Mbps)',
            best.stage_name, best.download_speed,
        )

        # Tear down losing strategies and non-winners
        for c in candidates[1:]:
            self._teardown_tunnel(c.context_updates)
        for s, r in results:
            if not r.has_internet:
                self._teardown_tunnel(r.context_updates)

        context.update(best.context_updates)
        return best

    # ------------------------------------------------------------------
    # Main connect orchestrator
    # ------------------------------------------------------------------

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        card = self.card_manager.get_card()
        if not card:
            logger.error('No wifi cards available for pipeline')
            return ConnectionResult(
                network=network, download_speed=0, upload_speed=0,
                ping=0, connected=False, connection_method='pipeline',
                interface='',
            )

        # For open networks, connect before running stages.
        # For encrypted networks, stages handle credentials and connection.
        if self.auto_connect:
            if not card.connect(network):
                self.card_manager.return_card(card)
                return ConnectionResult(
                    network=network, download_speed=0, upload_speed=0,
                    ping=0, connected=False, connection_method='pipeline',
                    interface=card.interface,
                )

        context: dict = self._get_connect_context()
        context['card_manager'] = self.card_manager
        if self.auto_connect:
            context['wifi_associated'] = True
        successful_stage = None
        pre_tested = None  # (dl, ul, ping) if parallel phase already ran speedtest
        self.last_stage_log = []

        for phase in self.phases:
            if isinstance(phase, list):
                # --- Parallel strategy group ---
                winner = self._run_parallel_phase(phase, network, card, context)
                if winner:
                    successful_stage = winner.stage_name
                    if winner.download_speed > 0:
                        pre_tested = (winner.download_speed,
                                      winner.upload_speed,
                                      winner.ping)
                    break
            else:
                # --- Sequential stage ---
                result = self._run_single_stage(phase, network, card, context)
                if result is None:
                    continue
                if result.stop_pipeline:
                    logger.info(
                        f'Stage {phase.name} stopped pipeline: {result.message}'
                    )
                    break
                if result.has_internet:
                    logger.info(
                        f'Stage {phase.name} achieved internet on {network.ssid}'
                    )
                    successful_stage = phase.name
                    break

        if successful_stage:
            if pre_tested:
                dl, ul, ping = pre_tested
            else:
                try:
                    tunnel_iface = context.get('tunnel_interface')
                    dl, ul, ping = self.run_speedtest(
                        card, interface_override=tunnel_iface,
                    )
                except Exception as e:
                    logger.warning(f'Speedtest failed after pipeline success: {e}')
                    dl = ul = ping = 0

            if dl > 0 or ul > 0:
                return ConnectionResult(
                    network=network, download_speed=dl, upload_speed=ul,
                    ping=ping, connected=True,
                    connection_method=f'pipeline:{successful_stage}',
                    interface=card.interface,
                )

        # Tear down any active tunnel before disconnecting
        self._teardown_tunnel(context)

        # All stages exhausted — no internet achieved
        card.disconnect()
        self.card_manager.return_card(card)
        return ConnectionResult(
            network=network, download_speed=0, upload_speed=0,
            ping=0, connected=False, connection_method='pipeline',
            interface=card.interface,
        )


class ReconModule:
    """Base class for background reconnaissance modules.

    ReconModules run continuously, collecting intelligence about the
    wireless environment. They have a different lifecycle from
    ConnectionModules — start/stop rather than per-network connect.
    """
    name: str = 'unnamed'
    requires_consent: bool = False

    def start(self):
        """Start background data collection."""
        raise NotImplementedError()

    def stop(self):
        """Stop background data collection."""
        raise NotImplementedError()

    def get_data(self) -> dict:
        """Get current collected data."""
        return {}

    def get_config_schema(self) -> dict:
        """Return config schema for this module."""
        return {}


class WifiManager:
    def __init__(self):
        self.card_manager = WifiCardManager()
        config = get_config()
        self.probe_history = ProbeHistory(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )
        self.scanner = NetworkScanner(self.card_manager, probe_history=self.probe_history)
        self.connection_monitor = ConnectionMonitor()

        # Create consent manager early — modules need it during loading
        yaml_consent = getattr(config, 'consent', {})
        if not isinstance(yaml_consent, dict):
            yaml_consent = {}
        self.consent_manager = ConsentManager(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
            yaml_consent=yaml_consent,
        )

        # Module config store — must be created before loading modules
        self.module_config = ModuleConfigStore(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )

        # Pipeline-builder store — captures hard-coded defaults during
        # module load and applies any user-customised layout on top.
        from pipeline_config import PipelineConfigStore
        self.pipeline_config = PipelineConfigStore(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )

        kn_cfg = getattr(config, 'known_networks', None)
        key_path = getattr(kn_cfg, 'master_key_path', None) if kn_cfg else None
        self.known_networks_store = KnownNetworksStore(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
            key_path=key_path,
        )

        self.modules = self._load_connection_modules()
        self.disabled_modules: set[str] = self._load_disabled_modules()
        # Network grouping: when True, all APs broadcasting the same SSID are
        # treated as one logical network for enabling actions and pass/fail.
        self.group_networks_by_ssid: bool = self._load_group_by_ssid()
        # Make the flag reachable from the consent path (PipelineModule._has_consent
        # reads it off the shared consent_manager rather than importing globals).
        if self.consent_manager is not None:
            self.consent_manager.group_by_ssid = self.group_networks_by_ssid

        # Speedtest is a post-connection action, not a connection module:
        # it runs after a module connects and is bound to that module's
        # interface, so it can't false-positive off another connection.
        from modules.speedtest import SpeedtestAction
        self.speedtest_action = SpeedtestAction(self.card_manager)
        self.suitable_connections: list[ConnectionResult] = []
        self.nearby_networks: list[WifiNetwork] = []
        self.metrics_store = PerformanceMetricsStore(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )

        # Load auto-selection config
        self.auto_selector = AutoSelector(
            wifi_manager=self,
            evaluation_interval=config.auto_selection.evaluation_interval,
            min_score_improvement=config.auto_selection.min_score_improvement,
            initial_delay=config.auto_selection.initial_delay,
        )

        # Enable auto-selection if configured
        if config.auto_selection.enabled:
            self.auto_selector.enable()

        # P3 features
        self.connection_store = ConnectionStore()
        self.bandwidth_monitor = BandwidthMonitor()
        self.notification_manager = NotificationManager()

        # Register config schemas from loaded modules and their pipeline
        # stages. Stage schemas are keyed by stage name so per-stage config
        # (set via the UI or the helper-config importer) round-trips through
        # the same store the stages read at runtime.
        for mod in self.modules:
            schema_method = getattr(mod, 'get_config_schema', None)
            if schema_method:
                schema = schema_method()
                if schema:
                    name = getattr(mod, 'name', mod.__class__.__name__)
                    self.module_config.register_schema(name, schema)
            for stage in getattr(mod, 'stages', []):
                stage_schema = stage.get_config_schema()
                if stage_schema:
                    self.module_config.register_schema(stage.name, stage_schema)

        # Activity log for power-user UI — last 100 events
        self.activity_log: collections.deque = collections.deque(maxlen=100)
        # Detailed per-attempt logs keyed by attempt_id (UUID)
        self.attempt_details: dict[str, dict] = {}
        # Locks for thread-safe parallel connection testing
        self._connections_lock = threading.Lock()
        self._details_lock = threading.Lock()

        self.status = {
            'scanning': False,
            'monitoring': False,
            'cards_in_use': 0,
            'active_modules': 0,
            'networks_found': 0,
            'current_bridge': None,
            'reconnect_events': 0,
            'metrics_available': self.metrics_store.is_available(),
            'auto_selection_enabled': self.auto_selector.is_enabled(),
            'auto_selection_running': False,
        }
        self.active_bridge = None
        # Bridge Override: when set, the interface of an operator-forced bridge.
        # Non-empty ⇒ an override is active; the automatic selector/auto-bridge/
        # reconcile paths must leave it (and its pinned connection) alone until
        # the operator unbridges. See start_bridge_override/stop_bridge_override.
        self._bridge_override_iface: Optional[str] = None
        self.hostap: Optional[HostAP] = None
        self._hostap_lazy_pending = False  # True when AP is confirmed but waiting for card
        self._hostap_claiming = False  # Re-entrancy guard for card-returned callback
        # Serialize start attempts so a background retry and a card-return
        # callback can't both try to bring the AP up at once.
        self._hostap_start_lock = threading.Lock()
        self._hostap_retry_thread: Optional[threading.Thread] = None
        # Last failure reason — shown in /api/hostap/status so the UI can
        # explain failures (and whether auto-start is still retrying).
        self._hostap_last_error: Optional[str] = None

        # Ethernet mode: 'management' (default — static IP, DHCP, NAT)
        #                 'pool' (available for future ethernet-based modules)
        self._ethernet_mode = self._load_ethernet_mode()

        # Auto-bridge: when true, the first successful connection (and any
        # subsequent success while no bridge is active) is immediately bridged
        # without waiting for the auto-selector's evaluation interval. Picks
        # the highest-scored entry in suitable_connections each time.
        self._auto_bridge_enabled = self._load_auto_bridge_enabled()

        # Register callback to track reconnection events
        self.connection_monitor.on_reconnect(self._on_reconnect)

        # Register card-return hook for lazy hostap
        self.card_manager.on_card_returned(self._on_card_returned_for_hostap)

        # Probe each card for AP-mode support at startup. The result is
        # also re-checked live in get_hostap_status, but logging it once
        # upfront makes "why can't I start HostAP?" answerable from the
        # journal without opening the UI.
        self._log_hostap_capabilities()

        # Neutralise pre-existing NM profiles bound to vasili interfaces.
        # NM defaults to autoconnect=yes for every per-device profile, so
        # without this an interface vasili repurposes (e.g. as the scan
        # card) gets re-associated to its old SSID behind our back.
        self._neutralize_nm_autoconnect()

    def _neutralize_nm_autoconnect(self):
        """Disable autoconnect on every NM wifi profile at startup.

        Catches both active (device-bound) and dormant (no DEVICE column)
        wifi profiles — the latter are sandbags from previous sessions
        that would hijack a card as soon as vasili releases it.
        """
        _nm_disable_autoconnect_all_wifi()

    def _reconcile_suitable_connections(self):
        """Drop / repoint suitable_connections entries to match OS reality.

        The ConnectionResult.interface field is a snapshot taken at the
        moment of success. If NM later moves the SSID to a different card
        (e.g. via autoconnect) or the card drops the connection, the
        entry is stale. One nmcli call per scan cycle keeps the in-memory
        view aligned with the OS without per-card polling.
        """
        if not self.suitable_connections:
            return
        ssid_by_iface = _get_device_ssid_map()
        iface_by_ssid: dict[str, str] = {}
        for iface, ssid in ssid_by_iface.items():
            iface_by_ssid.setdefault(ssid, iface)

        managed_ifaces = {c.interface for c in self.card_manager.get_all_cards()}
        changes_made = False
        to_remove: list[ConnectionResult] = []

        with self._connections_lock:
            for conn in self.suitable_connections:
                # A pinned (Bridge Override) entry is never repointed or
                # dropped — it lives until the operator unbridges, even if
                # the card momentarily drops association.
                if conn.pinned:
                    continue
                ssid = conn.network.ssid
                if ssid_by_iface.get(conn.interface) == ssid:
                    continue
                replacement = iface_by_ssid.get(ssid)
                if replacement and replacement in managed_ifaces:
                    logger.info(
                        f'Reconcile: {ssid} moved {conn.interface} -> {replacement}'
                    )
                    conn.interface = replacement
                    changes_made = True
                    continue
                logger.info(
                    f'Reconcile: dropping {ssid} '
                    f'(was on {conn.interface}, no card holds it now)'
                )
                to_remove.append(conn)
            for c in to_remove:
                self.suitable_connections.remove(c)

        if to_remove or changes_made:
            # If the active bridge points at a now-missing upstream, tear it
            # down so the next auto-bridge tick can pick a fresh one. A Bridge
            # Override is exempt — it stays up until manually unbridged.
            if (not self._bridge_override_iface
                    and self.active_bridge and self.active_bridge.is_active
                    and self.active_bridge.wifi_interface not in ssid_by_iface):
                logger.info(
                    'Reconcile: tearing down bridge — upstream '
                    f'{self.active_bridge.wifi_interface} no longer connected'
                )
                self.active_bridge.stop()
                self.status['current_bridge'] = None
            try:
                emit_connections_update()
                emit_status_update()
            except Exception:
                pass

    def _log_hostap_capabilities(self):
        """Log per-card AP-mode support so missing capability is obvious."""
        cards = self.card_manager.get_all_cards()
        if not cards:
            logger.warning('HostAP capability check: no WiFi cards present')
            return
        capable = []
        non_capable = []
        for c in cards:
            if HostAP.interface_supports_ap(c.interface):
                capable.append(c.interface)
            else:
                non_capable.append(c.interface)
        if capable:
            logger.info(
                f'HostAP capability: {len(capable)}/{len(cards)} cards support AP mode '
                f'(capable: {", ".join(capable)}'
                + (f'; not capable: {", ".join(non_capable)}' if non_capable else '')
                + ')'
            )
        else:
            logger.warning(
                'HostAP capability: no installed card supports AP mode '
                f'(checked: {", ".join(c.interface for c in cards)}). '
                'HostAP will not be startable until an AP-capable adapter is plugged in.'
            )

    def _load_connection_modules(self) -> list[ConnectionModule]:
        config = get_config()
        modules_dir = os.path.join(os.path.dirname(__file__), 'modules')
        modules = []

        # Create modules directory if it doesn't exist
        if not os.path.exists(modules_dir):
            os.makedirs(modules_dir)

        # Get enabled modules from config (None means all modules)
        enabled_modules = config.modules.enabled

        # Import all modules from the modules directory
        for filename in os.listdir(modules_dir):
            if filename.endswith('.py') and filename != '__init__.py':
                module_name = filename[:-3]

                # Skip if module is not in enabled list (when enabled list is specified)
                if enabled_modules is not None and module_name not in enabled_modules:
                    logger.debug(f'Skipping disabled module: {module_name}')
                    continue

                try:
                    module = importlib.import_module(f'modules.{module_name}')
                    # Find all ConnectionModule subclasses in the module
                    for name, obj in inspect.getmembers(module):
                        if (
                            inspect.isclass(obj)
                            and any(
                                base.__name__ == 'ConnectionModule'
                                for base in inspect.getmro(obj)
                            )
                            and obj.__name__ not in ('ConnectionModule', 'PipelineModule')
                        ):
                            # Pass optional params to modules that accept them
                            sig = inspect.signature(obj.__init__)
                            kwargs = {}
                            if 'mongodb_uri' in sig.parameters:
                                kwargs['mongodb_uri'] = config.database.mongodb_uri
                            if 'consent_manager' in sig.parameters:
                                kwargs['consent_manager'] = self.consent_manager
                            if 'module_config' in sig.parameters:
                                kwargs['module_config'] = self.module_config
                            if 'pipeline_config' in sig.parameters:
                                kwargs['pipeline_config'] = self.pipeline_config
                            if 'probe_history' in sig.parameters:
                                kwargs['probe_history'] = self.probe_history
                            if 'known_networks_store' in sig.parameters:
                                kwargs['known_networks_store'] = self.known_networks_store
                            modules.append(obj(self.card_manager, **kwargs))
                            logger.info(f'Loaded module: {module_name}')
                except Exception as e:
                    logger.error(f'Failed to load module {module_name}: {e}')

        # Sort by priority (lower = runs first)
        modules.sort(key=lambda m: getattr(m, 'priority', 50))
        return modules

    def _load_disabled_modules(self) -> set[str]:
        """Load set of disabled module names from MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            db = client[config.database.db_name]
            doc = db['module_state'].find_one({'_id': 'disabled_modules'})
            if doc and 'modules' in doc:
                return set(doc['modules'])
        except Exception:
            pass
        return set()

    def _save_disabled_modules(self):
        """Persist disabled module set to MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            db = client[config.database.db_name]
            db['module_state'].update_one(
                {'_id': 'disabled_modules'},
                {'$set': {'modules': list(self.disabled_modules)}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f'Failed to save disabled modules: {e}')

    def set_module_enabled(self, module_name: str, enabled: bool) -> bool:
        """Enable or disable a module by name. Returns True on success."""
        # Verify module exists
        if not any(
            getattr(m, 'name', m.__class__.__name__) == module_name
            for m in self.modules
        ):
            return False
        if enabled:
            self.disabled_modules.discard(module_name)
        else:
            self.disabled_modules.add(module_name)
        self._save_disabled_modules()
        return True

    def is_module_enabled(self, module_name: str) -> bool:
        return module_name not in self.disabled_modules

    def _load_group_by_ssid(self) -> bool:
        """Load the network-grouping setting from MongoDB (default True)."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            db = client[config.database.db_name]
            doc = db['module_state'].find_one({'_id': 'network_grouping'})
            if doc and 'group_by_ssid' in doc:
                return bool(doc['group_by_ssid'])
        except Exception:
            pass
        return True

    def _save_group_by_ssid(self):
        """Persist the network-grouping setting to MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            db = client[config.database.db_name]
            db['module_state'].update_one(
                {'_id': 'network_grouping'},
                {'$set': {'group_by_ssid': bool(self.group_networks_by_ssid)}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f'Failed to save network grouping setting: {e}')

    def get_group_by_ssid(self) -> bool:
        """Return whether networks are grouped by SSID."""
        return self.group_networks_by_ssid

    def set_group_by_ssid(self, value: bool) -> bool:
        """Enable or disable SSID-based network grouping. Returns True."""
        self.group_networks_by_ssid = bool(value)
        # Keep the consent path's view of the flag in sync.
        if self.consent_manager is not None:
            self.consent_manager.group_by_ssid = self.group_networks_by_ssid
        self._save_group_by_ssid()
        return True

    def use_connection(self, connection_index: int) -> bool:
        if connection_index >= len(self.suitable_connections):
            return False

        # Stop any existing share
        if self.active_bridge:
            self.active_bridge.stop()

        connection = self.suitable_connections[connection_index]
        upstream = connection.interface

        downstreams = self._discover_downstream_surfaces(exclude=upstream)
        if not downstreams:
            logger.warning(
                'No downstream surfaces available for connection share '
                '(ethernet not in management mode, no usb0, HostAP off). '
                'NAT will be set up so HostAP can still attach later.'
            )

        self.active_bridge = ConnectionShare(
            wifi_interface=upstream,
            downstream_interfaces=downstreams,
        )

        if self.active_bridge.start():
            self.status['current_bridge'] = {
                'wifi_interface': upstream,
                'ethernet_interface': downstreams[0] if downstreams else '',
                'downstream_interfaces': downstreams,
                'ssid': connection.network.ssid,
            }
            # HostAP runs its own NAT chain; point it at the new upstream.
            if self.hostap and self.hostap.is_active:
                self.hostap.update_upstream(upstream)
            return True

        return False

    def _discover_downstream_surfaces(self, exclude: str = '') -> list[str]:
        """Local interfaces that should receive NAT'd internet from the
        active upstream. Includes eth0 only when in management mode, any
        usb0 with an IPv4 address, and skips ``exclude`` (the upstream).
        Does NOT include the HostAP interface — HostAP installs its own
        NAT chain via ``HostAP._setup_nat``.
        """
        downstreams: list[str] = []
        try:
            all_ifaces = netifaces.interfaces()
        except Exception:
            return downstreams

        if (
            self._ethernet_mode == 'management'
            and 'eth0' in all_ifaces
            and 'eth0' != exclude
        ):
            downstreams.append('eth0')

        for iface in all_ifaces:
            if iface == exclude:
                continue
            if iface.startswith('usb'):
                try:
                    addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                    if addrs:
                        downstreams.append(iface)
                except Exception:
                    pass

        # De-dupe while preserving order
        seen = set()
        ordered = []
        for d in downstreams:
            if d not in seen:
                seen.add(d)
                ordered.append(d)
        return ordered

    def stop_current_connection(self):
        if self.active_bridge:
            self.active_bridge.stop()
            self.status['current_bridge'] = None

    # ------------------------------------------------------------------
    # Bridge Override — operator force-bridges a chosen network
    # ------------------------------------------------------------------

    def start_bridge_override(self, bssid: str,
                              ssid: Optional[str] = None) -> dict:
        """Force-bridge an operator-chosen network for portal work / investigation.

        Connects a free card to the network (open, or encrypted only if a
        saved credential exists) and makes it the bridged upstream *even with
        no internet*, pinned so the auto-selector / auto-bridge / reconcile
        paths leave it alone until ``stop_bridge_override``. If a card already
        holds this network, that entry is pinned and bridged instead of
        leasing a new one.

        Returns ``{'success': bool, 'error'?: str, 'ssid'?, 'interface'?}``.
        Error codes: network_not_found, no_saved_credentials, no_free_card,
        connect_failed, bridge_failed.
        """
        bssid = (bssid or '').strip()
        ssid = (ssid or '').strip()

        # Resolve the target network from the latest scan.
        target = None
        for net in self.nearby_networks:
            if bssid and net.bssid and net.bssid.lower() == bssid.lower():
                target = net
                break
            if not bssid and ssid and net.ssid == ssid:
                target = net
                break
        if target is None:
            return {'success': False, 'error': 'network_not_found'}

        # If a card already holds this network, pin and bridge that entry.
        with self._connections_lock:
            existing_index = next(
                (i for i, c in enumerate(self.suitable_connections)
                 if c.network.bssid == target.bssid),
                -1,
            )
            if existing_index >= 0:
                self.suitable_connections[existing_index].pinned = True
                existing_iface = self.suitable_connections[existing_index].interface

        if existing_index >= 0:
            self._bridge_override_iface = existing_iface
            if self.use_connection(existing_index):
                logger.info(
                    f'Bridge override: pinned existing connection {target.ssid} '
                    f'on {existing_iface}'
                )
                return {'success': True, 'ssid': target.ssid,
                        'interface': existing_iface, 'reused': True}
            self._bridge_override_iface = None
            with self._connections_lock:
                if 0 <= existing_index < len(self.suitable_connections):
                    self.suitable_connections[existing_index].pinned = False
            return {'success': False, 'error': 'bridge_failed'}

        # Encrypted networks: require a saved credential (no inline prompt).
        password = None
        if not target.is_open:
            password = self.known_networks_store.reveal(target.ssid)
            if not password:
                return {'success': False, 'error': 'no_saved_credentials'}

        # Lease a free connection card — fail cleanly if none is available.
        card = self.card_manager.lease_card(holder='bridge_override')
        if card is None:
            return {'success': False, 'error': 'no_free_card'}

        if not card.connect(target, password=password):
            self.card_manager.return_card(card, holder='bridge_override')
            return {'success': False, 'error': 'connect_failed'}

        result = ConnectionResult(
            network=target, download_speed=0, upload_speed=0, ping=0,
            connected=True, connection_method='override',
            interface=card.interface, pinned=True,
        )
        with self._connections_lock:
            self.suitable_connections.append(result)
            index = len(self.suitable_connections) - 1
        self.connection_monitor.add_card(card)

        self._bridge_override_iface = card.interface
        if self.use_connection(index):
            logger.info(
                f'Bridge override: forced + bridged {target.ssid} '
                f'on {card.interface} (no-internet OK)'
            )
            return {'success': True, 'ssid': target.ssid,
                    'interface': card.interface}

        # Bridge setup failed — unwind everything.
        self._bridge_override_iface = None
        with self._connections_lock:
            if result in self.suitable_connections:
                self.suitable_connections.remove(result)
        self.connection_monitor.remove_card(card)
        try:
            card.disconnect()
        except Exception:
            pass
        self.card_manager.return_card(card, holder='bridge_override')
        return {'success': False, 'error': 'bridge_failed'}

    def stop_bridge_override(self) -> dict:
        """Tear down an active Bridge Override and restore normal bridging.

        Stops the forced bridge, disconnects + returns the leased card, drops
        the pinned entry, clears the override flag, and (if auto-bridge is on)
        lets the best real connection take over so downstream clients regain
        internet. Idempotent.
        """
        iface = self._bridge_override_iface
        if not iface:
            return {'success': True, 'changed': False}

        if self.active_bridge:
            self.active_bridge.stop()
            self.status['current_bridge'] = None

        removed = None
        with self._connections_lock:
            for c in list(self.suitable_connections):
                if c.interface == iface and c.pinned:
                    self.suitable_connections.remove(c)
                    removed = c
                    break

        card = self._get_card_for_interface(iface)
        if card:
            self.connection_monitor.remove_card(card)
            try:
                card.disconnect()
            except Exception:
                pass
            self.card_manager.return_card(card, holder='bridge_override')

        self._bridge_override_iface = None

        # Let auto-bridge restore a real internet upstream for downstream users.
        if self._auto_bridge_enabled and not (
            self.active_bridge and self.active_bridge.is_active
        ):
            try:
                with self._connections_lock:
                    sorted_conns = sorted(
                        self.suitable_connections,
                        key=lambda c: c.calculate_score(), reverse=True,
                    )
                    best = sorted_conns[0] if sorted_conns else None
                    best_index = (
                        self.suitable_connections.index(best)
                        if best is not None else -1
                    )
                if best is not None:
                    self.use_connection(best_index)
            except Exception as e:
                logger.error(f'Restore auto-bridge after override failed: {e}')

        emit_connections_update()
        emit_status_update()
        return {'success': True, 'changed': True,
                'ssid': removed.network.ssid if removed else None}

    # ------------------------------------------------------------------
    # HostAP management
    # ------------------------------------------------------------------

    def start_hostap(self, conf: dict) -> dict:
        """Start the host access point (serialized).

        Returns ``{'success': bool, 'error': str, 'permanent': bool}``.
        A ``permanent`` error (missing hostapd, no AP-capable card) means
        there is no point retrying — callers must NOT set
        ``_hostap_lazy_pending`` on these. Transient errors (driver-mode /
        resource-busy races, DHCP/interface settling) return
        ``permanent: False`` so "Always Start" stays armed and the AP is
        retried.

        A non-blocking lock serializes attempts so a background retry and a
        card-return callback can't try to bring the AP up at the same time.
        """
        if not self._hostap_start_lock.acquire(blocking=False):
            return {'success': False,
                    'error': 'HostAP start already in progress',
                    'permanent': False}
        try:
            return self._do_start_hostap(conf)
        finally:
            self._hostap_start_lock.release()

    def _do_start_hostap(self, conf: dict) -> dict:
        if self.hostap and self.hostap.is_active:
            return {'success': False, 'error': 'HostAP already running',
                    'permanent': False}

        if not HostAP.check_hostapd_installed():
            return {'success': False, 'error': 'hostapd is not installed',
                    'permanent': True}

        interface = conf.get('interface')
        if not interface:
            # Pick an AP-capable connection card (prefer the *last* one,
            # matching the old behaviour, but only consider supported ones).
            conn_cards = self.card_manager.get_connection_cards()
            if not conn_cards:
                return {'success': False, 'error': 'No cards available for HostAP',
                        'permanent': False}
            capable = [c for c in conn_cards
                       if HostAP.interface_supports_ap(c.interface)]
            if not capable:
                return {
                    'success': False,
                    'error': ('No connection card supports AP mode '
                              '(check `iw phy ... info` → Supported interface modes)'),
                    'permanent': True,
                }
            interface = capable[-1].interface

        # Pre-flight AP-mode support BEFORE reserving the card.
        # Reserving first and failing later briefly steals the card from
        # the connection pool every time the lazy retry runs, which is
        # what made HostAP look like a "scheduled" network rather than a
        # permanent reservation.
        if not HostAP.interface_supports_ap(interface):
            err = f'{interface} does not support AP mode'
            self._hostap_last_error = err
            return {'success': False, 'error': err, 'permanent': True}

        card = self.card_manager.set_hostap_card(interface)
        if not card:
            return {'success': False,
                    'error': f'Cannot reserve {interface} (busy or scanning card)',
                    'permanent': False}

        # Determine upstream: use active bridge's wifi interface
        upstream = None
        if self.active_bridge and self.active_bridge.is_active:
            upstream = self.active_bridge.wifi_interface

        self.hostap = HostAP(
            interface=interface,
            ssid=conf.get('ssid', 'Vasili-AP'),
            security=conf.get('security', 'wpa2'),
            password=conf.get('password', ''),
            channel=conf.get('channel', 6),
        )

        if self.hostap.start(upstream_interface=upstream):
            self.status['hostap_active'] = True
            self.status['hostap_ssid'] = self.hostap.ssid
            self._hostap_lazy_pending = False
            self._hostap_last_error = None
            self._save_hostap_config(conf)
            return {'success': True, 'interface': interface, 'permanent': False}
        else:
            # Capture the failure reason set by whichever sub-step bailed
            # (hostapd / DHCP / interface IP) so the UI can show the real
            # cause instead of always blaming hostapd.
            detail = getattr(self.hostap, 'last_error', None)
            # fire_callbacks=False prevents clear_hostap_card from
            # triggering _on_card_returned_for_hostap which would
            # immediately re-seize the card in an infinite loop.
            self.card_manager.clear_hostap_card(fire_callbacks=False)
            self.hostap = None
            err = detail or 'HostAP failed to start'
            self._hostap_last_error = err
            # A bring-up race ("Could not configure driver mode", resource
            # busy, DHCP/interface settling) is retryable — keep it
            # non-permanent so "Always Start" persists and we retry, rather
            # than disabling auto-start on a transient hiccup.
            transient = HostAP._is_transient_hostapd_error(err)
            return {'success': False, 'error': err, 'permanent': not transient}

    def stop_hostap(self) -> dict:
        if not self.hostap or not self.hostap.is_active:
            return {'success': False, 'error': 'HostAP not running'}
        self.hostap.stop()
        self.hostap = None
        self.status['hostap_active'] = False
        self.status['hostap_ssid'] = None
        # A manual stop is not a failure — drop any stale error.
        self._hostap_last_error = None

        # Set lazy pending *before* clear_hostap_card so the card-returned
        # callback can immediately re-acquire it if lazy is enabled.
        saved = self._load_hostap_config()
        if saved.get('enabled'):
            self._hostap_lazy_pending = True
            logger.info('HostAP stopped but lazy-enabled — will restart when card is free')

        # This fires card-returned callbacks (including lazy hostap pickup)
        self.card_manager.clear_hostap_card()

        return {'success': True}

    def get_hostap_status(self) -> dict:
        # Capability info has to be included in *both* branches — when AP
        # is active the UI still asks for status, and an empty
        # ap_capable_interfaces would trigger the misleading "no AP card"
        # banner over a working AP.
        # Scanning card is excluded because it's reserved for scanning and
        # is never offered as a HostAP candidate; counting it makes the
        # UI's X/Y display nonsensical (e.g. 4/3).
        scanning = self.card_manager.get_scanning_card()
        candidate_cards = [
            c for c in self.card_manager.get_all_cards() if c != scanning
        ]
        ap_capable = [
            c.interface for c in candidate_cards
            if HostAP.interface_supports_ap(c.interface)
        ]
        capability = {
            'ap_capable_interfaces': ap_capable,
            'hostapd_installed': HostAP.check_hostapd_installed(),
        }

        if self.hostap and self.hostap.is_active:
            status = self.hostap.get_status()
            status['lazy_enabled'] = True  # Must be enabled if running
            status['lazy_pending'] = False
            status['last_error'] = None
            status.update(capability)
            return status
        saved = self._load_hostap_config()
        return {
            'is_active': False,
            'lazy_enabled': bool(saved.get('enabled')),
            'lazy_pending': self._hostap_lazy_pending,
            'last_error': self._hostap_last_error,
            **capability,
        }

    def _load_hostap_config(self) -> dict:
        """Load saved HostAP config from MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            doc = mdb['hostap_config'].find_one({'_id': 'hostap'})
            return doc.get('config', {}) if doc else {}
        except Exception:
            return {}

    def _save_hostap_config(self, conf: dict):
        """Merge ``conf`` into the saved HostAP document.

        This is a **merge**, not a replace. The form posts only the
        editable fields (interface/ssid/security/password/channel) so a
        replace would silently drop sticky flags like ``enabled`` that
        live on the same document — making the settings page appear to
        reset every time the user touches a field. Callers that want to
        flip a flag pass only that key, e.g. ``{'enabled': False}``.
        """
        if not conf:
            return
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            update = {f'config.{k}': v for k, v in conf.items()}
            mdb['hostap_config'].update_one(
                {'_id': 'hostap'},
                {'$set': update},
                upsert=True,
            )
        except Exception as e:
            logger.error(f'Failed to save hostap config: {e}')

    # ------------------------------------------------------------------
    # Lazy HostAP — confirm once, start when card is free / on reboot
    # ------------------------------------------------------------------

    def confirm_hostap(self, conf: dict) -> dict:
        """Confirm (enable) hostap config for lazy startup.

        Saves the config with ``enabled: true`` so the AP will start
        automatically when the chosen card is free and on every reboot.
        If the card is available right now the AP starts immediately.
        A permanent failure (no AP-capable card, hostapd missing) does
        not queue a lazy retry — that would just thrash a card the
        kernel can't use for AP mode.
        """
        conf['enabled'] = True
        self._save_hostap_config(conf)

        # Already running?
        if self.hostap and self.hostap.is_active:
            return {'success': True, 'state': 'running'}

        # Try to start right now
        result = self.start_hostap(conf)
        if result.get('success'):
            return {'success': True, 'state': 'running'}

        if result.get('permanent'):
            # Clear the saved enabled flag so reboots don't replay the
            # same failure, and make sure the retry loop is off.
            self._hostap_lazy_pending = False
            self._save_hostap_config({'enabled': False})
            logger.warning(
                'HostAP cannot start: %s — auto-start disabled',
                result.get('error', 'unknown error'),
            )
            return {'success': False, 'state': 'unsupported',
                    'error': result.get('error', 'HostAP unavailable')}

        # Transient failure (card busy elsewhere, or a bring-up race) — queue
        # lazy start AND kick off a bounded background retry so a free-card
        # race (no card-return event) still gets retried.
        self._hostap_lazy_pending = True
        self._start_hostap_retry_loop()
        logger.info(
            'HostAP confirmed but start failed transiently — retrying; will '
            'also start lazily when %s is free', conf.get('interface', 'any card')
        )
        return {'success': True, 'state': 'pending',
                'message': 'AP will start shortly (retrying) or when the card is free'}

    def disable_hostap_lazy(self) -> dict:
        """Disable lazy/auto hostap. Stops AP if running and clears enabled flag."""
        # Stop if currently active
        if self.hostap and self.hostap.is_active:
            self.stop_hostap()

        self._hostap_lazy_pending = False
        # User explicitly turned it off — clear any sticky error too.
        self._hostap_last_error = None
        self._save_hostap_config({'enabled': False})

        logger.info('HostAP lazy/auto disabled')
        return {'success': True}

    def _start_hostap_retry_loop(self):
        """Bounded background retry for transient HostAP start failures.

        When the AP card is already free (boot, or an immediate confirm) a
        transient hostapd race produces no card-return event, so the lazy
        card-return path alone would never retry. This re-attempts with
        backoff, then leaves ``_hostap_lazy_pending`` armed so a later organic
        card-return still tries. Guarded so only one loop runs at a time.
        """
        existing = self._hostap_retry_thread
        if existing is not None and existing.is_alive():
            return

        def _worker():
            for delay in (2, 3, 5, 8, 13):
                time.sleep(delay)
                if self.hostap and self.hostap.is_active:
                    return
                if not self._hostap_lazy_pending:
                    return  # cleared elsewhere (disabled, or started already)
                saved = self._load_hostap_config()
                if not saved or not saved.get('enabled'):
                    return
                logger.info('HostAP background retry (after %ss)...', delay)
                result = self.start_hostap(saved)
                if result.get('success'):
                    self._hostap_lazy_pending = False
                    logger.info('HostAP background retry succeeded')
                    return
                if result.get('permanent'):
                    self._hostap_lazy_pending = False
                    self._save_hostap_config({'enabled': False})
                    logger.warning(
                        'HostAP background retry hit permanent failure: %s',
                        result.get('error'),
                    )
                    return
            logger.info(
                'HostAP background retry exhausted — staying armed; will '
                'retry on next card free / reboot'
            )

        t = threading.Thread(target=_worker, name='hostap-retry', daemon=True)
        self._hostap_retry_thread = t
        t.start()

    def _on_card_returned_for_hostap(self, card: WifiCard):
        """Card-return callback: try to claim the card for lazy hostap.

        Permanent failures (this interface doesn't support AP mode,
        hostapd refused) clear the lazy flag so we don't flap the card
        on every return. Without this, freeing a card during a connect
        cycle would briefly grab it for a doomed AP start each time —
        which is what made the AP look like a scheduled connection
        rather than a permanent reservation.
        """
        if not self._hostap_lazy_pending:
            return
        # Guard against re-entrancy (start_hostap failure → clear → callback)
        if getattr(self, '_hostap_claiming', False):
            return
        if self.hostap and self.hostap.is_active:
            self._hostap_lazy_pending = False
            return

        saved = self._load_hostap_config()
        if not saved or not saved.get('enabled'):
            self._hostap_lazy_pending = False
            return

        wanted_iface = saved.get('interface')
        # Only grab the specific card the user chose (or any if unset)
        if wanted_iface and card.interface != wanted_iface:
            return

        # Pre-flight: if the returned card can't do AP mode, don't even
        # reserve it. With no wanted_iface this also lets the next
        # AP-capable card-return succeed instead of every return
        # thrashing.
        if not HostAP.interface_supports_ap(card.interface):
            if wanted_iface:
                # User chose this specific interface — it will never
                # work. Stop the retry loop and clear the enabled flag.
                self._hostap_lazy_pending = False
                self._save_hostap_config({'enabled': False})
                self._hostap_last_error = (
                    f'{card.interface} does not support AP mode'
                )
                logger.warning(
                    'HostAP disabled: %s does not support AP mode',
                    card.interface,
                )
            return

        self._hostap_claiming = True
        try:
            logger.info('Card %s now free — attempting lazy HostAP start',
                        card.interface)
            # Pass the specific returned card's interface so start_hostap
            # doesn't grab a different card from the pool.
            conf = dict(saved)
            conf['interface'] = card.interface
            result = self.start_hostap(conf)
            if result.get('success'):
                self._hostap_lazy_pending = False
                logger.info('Lazy HostAP started on %s', card.interface)
            elif result.get('permanent'):
                # No point retrying — clear the lazy flag and the
                # saved enabled bit so we stop grabbing cards on return.
                self._hostap_lazy_pending = False
                self._save_hostap_config({'enabled': False})
                logger.warning(
                    'Lazy HostAP disabled (permanent failure on %s): %s',
                    card.interface, result.get('error'),
                )
            else:
                logger.debug('Lazy HostAP start failed on %s: %s',
                             card.interface, result.get('error'))
        finally:
            self._hostap_claiming = False

    def _boot_hostap_check(self):
        """Called once at startup to auto-start AP from saved config.

        Runs the actual bring-up in a daemon thread so a transiently-failing
        AP (with internal hostapd retries) never delays startup or scanning.
        """
        saved = self._load_hostap_config()
        if not saved or not saved.get('enabled'):
            return

        threading.Thread(
            target=self._do_boot_hostap_check, args=(saved,),
            name='hostap-boot', daemon=True,
        ).start()

    def _do_boot_hostap_check(self, saved: dict):
        logger.info('Saved HostAP config has enabled=true — attempting boot start')
        result = self.start_hostap(saved)
        if result.get('success'):
            logger.info('HostAP auto-started on boot')
            return

        if result.get('permanent'):
            # Disable in saved config so the next boot doesn't replay
            # this and so the card-return callback stays off.
            self._hostap_lazy_pending = False
            self._save_hostap_config({'enabled': False})
            logger.warning(
                'HostAP auto-start disabled at boot (permanent): %s',
                result.get('error'),
            )
            return

        # Transient at boot (card busy/scanning, or a bring-up race) — queue
        # lazy start AND run a bounded background retry, since at boot the card
        # is often already free and no card-return event will fire.
        self._hostap_lazy_pending = True
        self._start_hostap_retry_loop()
        logger.info(
            'HostAP start failed transiently at boot — retrying; queued for '
            'lazy start too: %s', result.get('error'),
        )

    # ------------------------------------------------------------------
    # Ethernet mode management
    # ------------------------------------------------------------------

    def _load_ethernet_mode(self) -> str:
        """Load ethernet mode from MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            doc = mdb['ethernet_config'].find_one({'_id': 'mode'})
            return doc.get('mode', 'management') if doc else 'management'
        except Exception:
            return 'management'

    def _save_ethernet_mode(self, mode: str):
        """Save ethernet mode to MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            mdb['ethernet_config'].update_one(
                {'_id': 'mode'},
                {'$set': {'mode': mode}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f'Failed to save ethernet mode: {e}')

    def _load_auto_bridge_enabled(self) -> bool:
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            doc = mdb['runtime_settings'].find_one({'_id': 'auto_bridge'})
            return bool(doc.get('enabled', False)) if doc else False
        except Exception:
            return False

    def _save_auto_bridge_enabled(self, enabled: bool):
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            mdb['runtime_settings'].update_one(
                {'_id': 'auto_bridge'},
                {'$set': {'enabled': bool(enabled)}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f'Failed to save auto-bridge setting: {e}')

    def set_auto_bridge_enabled(self, enabled: bool) -> bool:
        self._auto_bridge_enabled = bool(enabled)
        self._save_auto_bridge_enabled(self._auto_bridge_enabled)
        return True

    def get_auto_bridge_enabled(self) -> bool:
        return self._auto_bridge_enabled

    def get_ethernet_status(self) -> dict:
        """Return current ethernet port status and mode."""
        iface = 'eth0'
        has_link = False
        ip_addr = None
        try:
            with open(f'/sys/class/net/{iface}/carrier') as f:
                has_link = f.read().strip() == '1'
        except Exception:
            pass
        try:
            ip_addr = network_isolation.get_interface_ip(iface)
        except Exception:
            pass

        return {
            'interface': iface,
            'mode': self._ethernet_mode,
            'has_link': has_link,
            'ip_address': ip_addr,
        }

    def set_ethernet_mode(self, mode: str) -> dict:
        """Switch ethernet port between management and pool modes."""
        if mode not in ('management', 'pool'):
            return {'success': False, 'error': f'Invalid mode: {mode}'}

        old_mode = self._ethernet_mode
        if mode == old_mode:
            return {'success': True, 'mode': mode, 'changed': False}

        self._ethernet_mode = mode
        self._save_ethernet_mode(mode)

        iface = 'eth0'
        if mode == 'pool':
            # Remove management IP and stop DHCP serving on eth0
            # The interface becomes available for future ethernet modules
            logger.info('Switching eth0 to pool mode')
            subprocess.run(
                ['ip', 'addr', 'flush', 'dev', iface],
                capture_output=True, timeout=5,
            )
        elif mode == 'management':
            # Restore management IP
            logger.info('Switching eth0 to management mode')
            subprocess.run(
                ['ip', 'addr', 'add', '10.55.0.2/24', 'dev', iface],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ['ip', 'link', 'set', iface, 'up'],
                capture_output=True, timeout=5,
            )

        return {'success': True, 'mode': mode, 'changed': True}

    def _on_reconnect(self, card: WifiCard, success: bool):
        """Callback invoked when reconnection is attempted."""
        self.status['reconnect_events'] = self.status.get('reconnect_events', 0) + 1
        if success:
            logger.info(f'Reconnection event: {card.interface} successfully reconnected')
        else:
            logger.warning(f'Reconnection event: {card.interface} failed to reconnect')
            # Remove failed connection from suitable_connections
            self.suitable_connections = [
                conn for conn in self.suitable_connections if conn.interface != card.interface
            ]

    def _get_card_for_interface(self, interface: str) -> Optional[WifiCard]:
        """Get the WifiCard instance for a given interface name."""
        for card in self.card_manager.get_all_cards():
            if card.interface == interface:
                return card
        return None

    def get_sorted_connections(self) -> list[ConnectionResult]:
        """
        Get connections sorted by their calculated score (best first).

        Returns:
            List of ConnectionResult objects sorted by score descending
        """
        return sorted(
            self.suitable_connections, key=lambda conn: conn.calculate_score(), reverse=True
        )

    def enable_auto_selection(self):
        """Enable auto-selection mode."""
        self.auto_selector.enable()
        self.status['auto_selection_enabled'] = True
        emit_status_update()

    def disable_auto_selection(self):
        """Disable auto-selection mode."""
        self.auto_selector.disable()
        self.status['auto_selection_enabled'] = False
        emit_status_update()

    def get_auto_selection_status(self) -> dict:
        """Get auto-selection status and statistics."""
        stats = self.auto_selector.get_stats()
        stats['running'] = self.status.get('auto_selection_running', False)
        return stats

    def _log_activity(self, event_type: str, attempt_id: str = None, **kwargs):
        """Log an activity event for the power-user UI.

        Args:
            event_type: 'attempt', 'connected', 'failed', 'error', 'stage'
            attempt_id: UUID linking related events. Generated for 'attempt' type.
            **kwargs: Type-specific fields.
        """
        if event_type == 'attempt' and not attempt_id:
            attempt_id = str(uuid.uuid4())

        entry = {
            'id': attempt_id,
            'type': event_type,
            'timestamp': time.time(),
            **kwargs,
        }
        self.activity_log.append(entry)

        # Initialize/update detail records (thread-safe)
        with self._details_lock:
            if event_type == 'attempt' and attempt_id:
                self.attempt_details[attempt_id] = {
                    'id': attempt_id,
                    'ssid': kwargs.get('ssid', ''),
                    'bssid': kwargs.get('bssid', ''),
                    'module': kwargs.get('module', ''),
                    'encryption': kwargs.get('encryption', ''),
                    'signal': kwargs.get('signal', 0),
                    'started_at': time.time(),
                    'status': 'in_progress',
                    'stages': [],
                    'result': None,
                }
                if len(self.attempt_details) > 200:
                    oldest = sorted(self.attempt_details.keys(),
                                    key=lambda k: self.attempt_details[k].get('started_at', 0))
                    for k in oldest[:50]:
                        del self.attempt_details[k]

            if attempt_id and attempt_id in self.attempt_details:
                detail = self.attempt_details[attempt_id]
                if event_type == 'connected':
                    detail['status'] = 'connected'
                    detail['result'] = {k: v for k, v in kwargs.items()}
                    detail['finished_at'] = time.time()
                elif event_type == 'failed':
                    detail['status'] = 'failed'
                    detail['result'] = {k: v for k, v in kwargs.items()}
                    detail['finished_at'] = time.time()
                elif event_type == 'error':
                    detail['status'] = 'error'
                    detail['result'] = {k: v for k, v in kwargs.items()}
                    detail['finished_at'] = time.time()
                elif event_type == 'stage':
                    detail['stages'].append({
                        'timestamp': time.time(),
                        **kwargs,
                    })

        try:
            emit_activity_update(entry)
        except Exception:
            pass

        return attempt_id

    def _try_network(self, network: WifiNetwork, module) -> Optional[ConnectionResult]:
        """Worker: attempt one network with one module on one card.

        Called from thread pool — must be thread-safe.
        """
        module_name = module.__class__.__name__
        attempt_id = None

        try:
            logger.info(f'Module {module_name} attempting connection to {network.ssid}')
            attempt_id = self._log_activity(
                'attempt',
                ssid=network.ssid,
                bssid=network.bssid,
                module=module_name,
                encryption=network.encryption_type,
                signal=network.signal_strength,
                uncloaked=getattr(network, 'uncloaked', False),
            )

            # Set thread-local pending task for lease_card to pick up
            self.card_manager._pending_tasks.task = {
                'ssid': network.ssid,
                'module': module_name,
                'started_at': time.time(),
            }
            result = module.connect(network)
            self.card_manager._pending_tasks.task = None

            # Get the MAC address that was used for this attempt
            used_mac = None
            used_interface = result.interface if result else None
            if used_interface:
                card = self._get_card_for_interface(used_interface)
                if card:
                    from mac_manager import MacManager
                    used_mac = MacManager.get_current_mac(used_interface)

            # Capture pipeline stage details
            stage_log = getattr(module, 'last_stage_log', [])
            if attempt_id:
                with self._details_lock:
                    if attempt_id in self.attempt_details:
                        if stage_log:
                            self.attempt_details[attempt_id]['stages'] = list(stage_log)
                        if used_mac:
                            self.attempt_details[attempt_id]['mac'] = used_mac

            if result.connected:
                # Post-connection action: measure speed bound to the
                # interface this module connected on (no-op if the module
                # already produced metrics).
                result = self.speedtest_action.run(result)
                score = result.calculate_score()
                logger.info(
                    f'Successfully connected to {network.ssid} '
                    f'via {module_name} (score: {score})'
                )
                self._log_activity(
                    'connected', attempt_id=attempt_id,
                    ssid=result.network.ssid or network.ssid,
                    module=module_name,
                    interface=result.interface,
                    mac=used_mac or '',
                    score=round(score, 1),
                    download=round(result.download_speed, 1),
                    upload=round(result.upload_speed, 1),
                    ping=round(result.ping, 1),
                    uncloaked=getattr(result.network, 'uncloaked', False),
                )
                return result
            else:
                logger.warning(f'Module {module_name} failed on {network.ssid}')
                self._log_activity(
                    'failed', attempt_id=attempt_id,
                    ssid=network.ssid, module=module_name,
                    mac=used_mac or '',
                    reason='connection_failed',
                )
                store_connection_history(network, False, failure_reason='connection_failed')
                return None

        except Exception as e:
            logger.error(f'Error with {module_name} on {network.ssid}: {e}')
            self._log_activity(
                'error', attempt_id=attempt_id,
                ssid=network.ssid, module=module_name,
                reason=str(e)[:100],
            )
            store_connection_history(network, False, failure_reason=str(e)[:100])
            return None

    def _handle_successful_connection(self, network: WifiNetwork,
                                       result: ConnectionResult):
        """Process a successful connection result (thread-safe)."""
        with self._connections_lock:
            # Parallel workers can both succeed on the same logical network;
            # only the first one keeps its card. With SSID grouping on, all
            # BSSIDs of an SSID collapse to one key; with grouping off, dedup
            # is per (SSID, BSSID).
            group_by_ssid = self.group_networks_by_ssid
            result_key = network_group_key(result.network, group_by_ssid)
            duplicate = any(
                c.connected
                and network_group_key(c.network, group_by_ssid) == result_key
                for c in self.suitable_connections
            ) if result.network.ssid else False
            if not duplicate:
                self.suitable_connections.append(result)

        if duplicate:
            logger.info(
                f'Duplicate SSID {result.network.ssid} on {result.interface} '
                '— another card already holds this network, releasing card'
            )
            card = self._get_card_for_interface(result.interface)
            if card:
                card.disconnect()
                self.card_manager.return_card(card)
            return

        # Use result.network which has the resolved SSID for hidden networks
        rnet = result.network
        score = result.calculate_score()
        self.metrics_store.store_metrics(result)
        self.connection_store.store_network(
            ssid=rnet.ssid, bssid=rnet.bssid,
            encryption_type=rnet.encryption_type, score=score,
            download_speed=result.download_speed,
            upload_speed=result.upload_speed, ping=result.ping, success=True,
        )
        self.notification_manager.connection_established(
            ssid=rnet.ssid, interface=result.interface, score=score,
        )
        store_connection_history(rnet, True, {
            'download': result.download_speed,
            'upload': result.upload_speed,
            'ping': result.ping,
        }, result.interface)

        card = self._get_card_for_interface(result.interface)
        if card:
            self.connection_monitor.add_card(card)

        emit_connections_update()

        # Auto-bridge: if enabled and no bridge is currently active, pick the
        # best entry from suitable_connections (by score) and bridge it now
        # instead of waiting for the auto-selector's evaluation tick. Never
        # bridge over an active Bridge Override.
        if self._auto_bridge_enabled and not self._bridge_override_iface and not (
            self.active_bridge and self.active_bridge.is_active
        ):
            try:
                with self._connections_lock:
                    sorted_conns = sorted(
                        self.suitable_connections,
                        key=lambda c: c.calculate_score(),
                        reverse=True,
                    )
                    best = sorted_conns[0] if sorted_conns else None
                    best_index = (
                        self.suitable_connections.index(best) if best else -1
                    )
                if best is not None:
                    logger.info(
                        f'Auto-bridge: bridging best connection '
                        f'{best.network.ssid} (score={best.calculate_score():.1f})'
                    )
                    if self.use_connection(best_index):
                        emit_status_update()
                        emit_connections_update()
            except Exception as e:
                logger.error(f'Auto-bridge failed: {e}')

    def scan_and_connect(self):
        """
        Main loop that scans for networks and tests connections in parallel.

        Uses a thread pool with one worker per available connection card,
        allowing simultaneous network testing across multiple WiFi interfaces.
        """
        logger.info('Starting scan_and_connect loop')
        self.status['scanning'] = True
        self.status['monitoring'] = True
        self.status['auto_selection_running'] = True
        emit_status_update()

        self.scanner.start_scan()
        self.connection_monitor.start()
        self.auto_selector.start()
        self.bandwidth_monitor.start()

        try:
            while True:
                try:
                    networks = self.scanner.get_next_scan()
                    logger.info(f'Scan found {len(networks)} networks')
                except Exception as e:
                    logger.error(f'Error getting scan results: {e}')
                    time.sleep(5)
                    continue

                # Re-poll OS to stay aligned with NM before making decisions
                # about which networks to attempt.
                self._reconcile_suitable_connections()

                self.nearby_networks = sorted(
                    networks, key=lambda n: n.signal_strength, reverse=True
                )
                self.status['networks_found'] = len(networks)
                self.status['cards_in_use'] = sum(
                    1 for card in self.card_manager.get_all_cards() if card.in_use
                )
                self.status['active_modules'] = len(self.modules)
                emit_status_update()
                emit_scan_update()

                # Build work queue: (network, module) pairs to test
                work_items = []
                group_by_ssid = self.group_networks_by_ssid
                connected_bssids = set()
                # Grouping keys already connected: SSID-scoped when grouping is
                # on, (SSID,BSSID)-scoped when off.
                connected_keys = set()
                with self._connections_lock:
                    for conn in self.suitable_connections:
                        if conn.connected:
                            connected_bssids.add(conn.network.bssid)
                            if conn.network.ssid:
                                connected_keys.add(
                                    network_group_key(conn.network, group_by_ssid)
                                )

                # Don't schedule connect attempts to our own HostAP SSID —
                # the scanner will see it on the air just like any other
                # network, but attempting to associate is wasted work and
                # produces ssid_not_found churn on connection cards.
                hostap_ssid = (
                    self.hostap.ssid
                    if self.hostap and self.hostap.is_active
                    else None
                )

                # In-flight grouping keys queued so far this cycle — prevents
                # two members of the same logical network being submitted to
                # parallel workers. SSID-scoped when grouping is on, otherwise
                # per (SSID, BSSID).
                queued_keys: set[str] = set()
                for network in networks:
                    if network.bssid in connected_bssids:
                        continue
                    if hostap_ssid and network.ssid == hostap_ssid:
                        continue
                    net_key = network_group_key(network, group_by_ssid)
                    if network.ssid and (
                        net_key in connected_keys
                        or net_key in queued_keys
                    ):
                        continue
                    for module in self.modules:
                        mod_name = getattr(module, 'name', module.__class__.__name__)
                        if not self.is_module_enabled(mod_name):
                            continue
                        # Hidden networks (empty SSID) must only go to
                        # HiddenNetworkModule — skip all other modules
                        if not network.ssid:
                            if module.__class__.__name__ != 'HiddenNetworkModule':
                                continue
                        if module.can_connect(network):
                            work_items.append((network, module))
                            if network.ssid:
                                queued_keys.add(net_key)
                            break  # First matching module per network

                if not work_items:
                    continue

                # Number of parallel workers = available connection cards
                num_connection_cards = len(self.card_manager.get_connection_cards())
                max_workers = max(1, num_connection_cards)

                logger.info(
                    f'Processing {len(work_items)} network candidates '
                    f'with {max_workers} parallel workers'
                )

                # Submit work to thread pool
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers
                ) as pool:
                    futures = {}
                    for network, module in work_items:
                        # Check card availability before submitting
                        if self.card_manager.get_available_count() <= 1:
                            break  # Reserve scanning card
                        future = pool.submit(self._try_network, network, module)
                        futures[future] = network

                    # Collect results as they complete
                    for future in concurrent.futures.as_completed(futures):
                        network = futures[future]
                        try:
                            result = future.result()
                            if result and result.connected:
                                self._handle_successful_connection(network, result)
                        except Exception as e:
                            logger.error(f'Worker error for {network.ssid}: {e}')

        except Exception as e:
            logger.error(f'scan_and_connect loop error: {e}')
        finally:
            self.scanner.stop_scan()
            self.connection_monitor.stop()
            self.auto_selector.stop()
            self.bandwidth_monitor.stop()
            # Clean up routing isolation on all cards
            for card in self.card_manager.get_all_cards():
                if card._routing_info:
                    network_isolation.teardown_interface_routing(
                        card.interface, card._routing_info
                    )
                    card._routing_info = None
            self.status['scanning'] = False
            self.status['monitoring'] = False
            self.status['auto_selection_running'] = False
            logger.info('scan_and_connect loop stopped')


# Flask web interface
app = Flask(__name__)
app.config['SECRET_KEY'] = 'vasili-secret-key-change-in-production'
socketio = SocketIO(app, cors_allowed_origins='*')
wifi_manager: Optional[WifiManager] = None

# MongoDB setup
mongo_client = None
db = None
history_collection = None


def app_is_ready() -> bool:
    """True once _init_app() has constructed the WifiManager.

    The web server starts in its own thread before the (slow) hardware/DB
    initialization, so routes must tolerate wifi_manager being None for the
    first moments after a restart.
    """
    return wifi_manager is not None


@app.before_request
def _guard_until_ready():
    """While initializing, answer API calls with 503 'initializing' instead of
    crashing on a None wifi_manager. Page routes still render their shell so the
    browser can poll and show a starting-up state."""
    if app_is_ready():
        return None
    path = request.path or ''
    if path.startswith('/api/'):
        return jsonify({
            'status': 'initializing',
            'message': 'Vasili is starting up — hardware and database are coming online.',
        }), 503
    return None


def _init_app():
    """Initialize the application — called once from main()."""
    global wifi_manager, mongo_client, db, history_collection

    wifi_manager = WifiManager()
    wifi_manager.notification_manager._socketio_emit = socketio.emit

    config = get_config()
    try:
        mongo_client = MongoClient(
            config.database.mongodb_uri, serverSelectionTimeoutMS=2000
        )
        mongo_client.admin.command('ping')
        db = mongo_client[config.database.db_name]
        history_collection = db['connection_history']
        logger.info('Connected to MongoDB successfully')
    except ConnectionFailure:
        logger.warning('MongoDB not available - history features will be disabled')
    except Exception as e:
        logger.warning(f'MongoDB connection error: {e} - history features will be disabled')

    # Auto-start HostAP if saved config has enabled=true
    wifi_manager._boot_hostap_check()


def store_connection_history(
    network: WifiNetwork, success: bool, speed_test: dict = None,
    interface: str = None, failure_reason: str = None,
):
    """Store connection attempt in MongoDB history."""
    if history_collection is None:
        return

    try:
        history_entry = {
            'timestamp': datetime.now(tz=None),
            'ssid': network.ssid,
            'bssid': network.bssid,
            'signal_strength': network.signal_strength,
            'channel': network.channel,
            'encryption_type': network.encryption_type,
            'success': success,
            'interface': interface,
            'uncloaked': getattr(network, 'uncloaked', False),
        }

        if failure_reason:
            history_entry['failure_reason'] = failure_reason

        if speed_test:
            history_entry['download_speed'] = speed_test.get('download', 0)
            history_entry['upload_speed'] = speed_test.get('upload', 0)
            history_entry['ping'] = speed_test.get('ping', 0)

        history_collection.insert_one(history_entry)
        logger.debug(f'Stored connection history for {network.ssid}')
    except Exception as e:
        logger.error(f'Failed to store connection history: {e}')


def emit_status_update():
    """Emit current status to all connected clients."""
    # During startup the WifiManager may still be under construction (it can
    # trigger an emit, e.g. via auto-selection enable) before the global is
    # assigned. No-op until it's ready rather than logging a spurious error.
    if wifi_manager is None:
        return
    try:
        # Recalculate cards_in_use so the status bar reflects current state
        wifi_manager.status['cards_in_use'] = sum(
            1 for card in wifi_manager.card_manager.get_all_cards() if card.in_use
        )
        wifi_manager.status['hostap_active'] = (
            wifi_manager.hostap is not None and wifi_manager.hostap.is_active
        )
        socketio.emit('status_update', wifi_manager.status)
    except Exception as e:
        logger.error(f'Failed to emit status update: {e}')


def emit_scan_update():
    """Emit current scan results to all connected clients."""
    if wifi_manager is None:
        return
    try:
        scan_data = []
        for net in wifi_manager.nearby_networks:
            scan_data.append({
                'ssid': net.ssid,
                'bssid': net.bssid,
                'signal_strength': net.signal_strength,
                'channel': net.channel,
                'encryption_type': net.encryption_type,
                'is_open': net.is_open,
                'uncloaked': getattr(net, 'uncloaked', False),
            })
        socketio.emit('scan_update', {'networks': scan_data})
    except Exception as e:
        logger.error(f'Failed to emit scan update: {e}')


def emit_activity_update(entry: dict):
    """Emit a single activity event to all connected clients."""
    try:
        socketio.emit('activity_update', entry)
    except Exception as e:
        logger.error(f'Failed to emit activity update: {e}')


def emit_connections_update():
    """Emit current connections to all connected clients."""
    if wifi_manager is None:
        return
    try:
        connections_data = []
        with wifi_manager._connections_lock:
            conns_snapshot = list(wifi_manager.suitable_connections)
        bridge = wifi_manager.active_bridge
        bridged_iface = (
            bridge.wifi_interface if bridge and bridge.is_active else None
        )
        override_iface = wifi_manager._bridge_override_iface
        for conn in conns_snapshot:
            conn_dict = {
                'network': {
                    'ssid': conn.network.ssid,
                    'bssid': conn.network.bssid,
                    'signal_strength': conn.network.signal_strength,
                    'channel': conn.network.channel,
                    'encryption_type': conn.network.encryption_type,
                    'is_open': conn.network.is_open,
                    'uncloaked': getattr(conn.network, 'uncloaked', False),
                },
                'download_speed': conn.download_speed,
                'upload_speed': conn.upload_speed,
                'ping': conn.ping,
                'connected': conn.connected,
                'connection_method': conn.connection_method,
                'interface': conn.interface,
                'bridged': (conn.interface == bridged_iface),
                'override': bool(override_iface) and conn.interface == override_iface,
            }
            connections_data.append(conn_dict)
        socketio.emit('connections_update', {'connections': connections_data})
    except Exception as e:
        logger.error(f'Failed to emit connections update: {e}')


@app.route('/')
def index():
    # During the brief startup window wifi_manager may not exist yet — render
    # the shell with safe defaults so the page loads and polls until ready.
    if not app_is_ready():
        return render_template(
            'index.html',
            status={'initializing': True},
            connections=[],
            nearby_networks=[],
        )
    return render_template(
        'index.html',
        status=wifi_manager.status,
        connections=wifi_manager.suitable_connections,
        nearby_networks=wifi_manager.nearby_networks,
    )


@app.route('/config')
def config_page():
    return render_template('config.html')


@app.route('/builder')
def pipeline_builder_page():
    return render_template('builder.html')


@app.route('/api/status')
def get_status():
    return jsonify(wifi_manager.status)


@app.route('/api/connections')
def get_connections():
    bridge = wifi_manager.active_bridge
    bridged_iface = (
        bridge.wifi_interface if bridge and bridge.is_active else None
    )
    override_iface = wifi_manager._bridge_override_iface
    out = []
    for conn in wifi_manager.suitable_connections:
        d = vars(conn).copy()
        d['bridged'] = (conn.interface == bridged_iface)
        d['override'] = bool(override_iface) and conn.interface == override_iface
        out.append(d)
    return jsonify(out)


@app.route('/api/scan_results')
def get_scan_results():
    return jsonify([
        {
            'ssid': net.ssid,
            'bssid': net.bssid,
            'signal_strength': net.signal_strength,
            'channel': net.channel,
            'encryption_type': net.encryption_type,
            'is_open': net.is_open,
            'uncloaked': getattr(net, 'uncloaked', False),
        }
        for net in wifi_manager.nearby_networks
    ])


@app.route('/api/cards')
def get_cards():
    """Detailed per-card status for power-user UI."""
    cards = []
    for card in wifi_manager.card_manager.get_all_cards():
        freq_info = card.get_frequency_info()
        card_info = {
            'interface': card.interface,
            'in_use': card.in_use,
            'is_up': card._is_interface_up(),
            'role': (
                'hostap' if card == wifi_manager.card_manager._hostap_card
                else 'scanning' if card == wifi_manager.card_manager.get_scanning_card()
                else 'connection'
            ),
            'mode': card.current_mode,
            'connected_network': None,
            'ip_address': card.get_ip_address(),
            'gateway': None,
            'routing_table': None,
            'current_task': card.current_task,
            'current_freq': freq_info.get('current_freq'),
            'current_band': freq_info.get('current_band'),
            'current_channel': freq_info.get('current_channel'),
            'supported_bands': freq_info.get('supported_bands', []),
            'supports_ap': HostAP.interface_supports_ap(card.interface),
        }
        if card._connected_network:
            card_info['connected_network'] = {
                'ssid': card._connected_network.ssid,
                'bssid': card._connected_network.bssid,
                'signal_strength': card._connected_network.signal_strength,
                'encryption_type': card._connected_network.encryption_type,
            }
        routing = getattr(card, '_routing_info', None)
        if routing:
            card_info['gateway'] = routing.get('gateway')
            card_info['routing_table'] = routing.get('table')
        if card == wifi_manager.card_manager._hostap_card and wifi_manager.hostap:
            card_info['hostap_info'] = wifi_manager.hostap.get_status()
        cards.append(card_info)
    return jsonify(cards)


@app.route('/api/activity')
def get_activity():
    """Recent activity log."""
    return jsonify(list(wifi_manager.activity_log))


@app.route('/api/activity/<attempt_id>')
def get_attempt_detail(attempt_id):
    """Detailed log for a specific connection attempt."""
    detail = wifi_manager.attempt_details.get(attempt_id)
    if detail:
        return jsonify(detail)
    return jsonify({'error': 'Attempt not found'}), 404


@app.route('/api/modules')
def get_modules():
    """List all modules with config schemas, current values, and consent status."""
    modules = []
    for mod in wifi_manager.modules:
        name = getattr(mod, 'name', mod.__class__.__name__)
        schema_method = getattr(mod, 'get_config_schema', None)
        schema = schema_method() if schema_method else {}
        needs_consent = getattr(mod, 'requires_consent', False)

        # For PipelineModules, include stage info (flat) and phase structure
        stages_info = []
        if hasattr(mod, 'stages'):
            for stage in mod.stages:
                stages_info.append({
                    'name': stage.name,
                    'requires_consent': stage.requires_consent,
                    'config_schema': stage.get_config_schema(),
                    'config_values': wifi_manager.module_config.get_config(stage.name),
                })

        # Build phase structure: list of items, each is either
        # a stage name (sequential) or a list of stage names (parallel)
        phases_info = []
        if hasattr(mod, 'phases'):
            for phase in mod.phases:
                if isinstance(phase, list):
                    phases_info.append([s.name for s in phase])
                else:
                    phases_info.append(phase.name)

        modules.append({
            'name': name,
            'class': mod.__class__.__name__,
            'priority': getattr(mod, 'priority', 50),
            'enabled': wifi_manager.is_module_enabled(name),
            'requires_consent': needs_consent,
            'consent_mode': wifi_manager.consent_manager.get_mode(name) if needs_consent else None,
            'config_schema': schema,
            'config_values': wifi_manager.module_config.get_config(name),
            'stages': stages_info,
            'phases': phases_info,
        })
    return jsonify(modules)


@app.route('/api/pipeline-builder/stages')
def get_pipeline_stages():
    """Catalog of all stages that can appear in a pipeline layout."""
    from modules.stages import get_stage_registry
    out = []
    for name, cls in sorted(get_stage_registry().items()):
        out.append({
            'name': name,
            'class': cls.__name__,
            'requires_consent': getattr(cls, 'requires_consent', False),
            'description': (cls.__doc__ or '').strip().split('\n')[0],
        })
    return jsonify(out)


@app.route('/api/pipeline-builder/pipelines')
def get_pipeline_layouts():
    """All pipeline modules with their default and effective layouts.

    The ``connectivity_gate`` field marks where the
    ``connectivity_check`` stage sits in the effective layout so the UI
    can render pre-gate / post-gate sections; ``-1`` means it isn't in
    the layout at all (the user will need to add it).
    """
    pcfg = wifi_manager.pipeline_config
    out = []
    for mod in wifi_manager.modules:
        if not hasattr(mod, 'phases'):
            continue
        cls_name = mod.__class__.__name__
        defaults = pcfg.get_defaults(cls_name)
        custom = pcfg.get_layout(cls_name)
        effective = custom if custom is not None else defaults
        gate_idx = next(
            (i for i, p in enumerate(effective)
             if (isinstance(p, str) and p == 'connectivity_check')
                or (isinstance(p, list) and 'connectivity_check' in p)),
            -1,
        )
        out.append({
            'name': getattr(mod, 'name', cls_name),
            'class': cls_name,
            'priority': getattr(mod, 'priority', 50),
            'auto_connect': getattr(mod, 'auto_connect', True),
            'default_phases': defaults,
            'phases': effective,
            'customised': custom is not None,
            'connectivity_gate': gate_idx,
        })
    return jsonify(out)


@app.route('/api/pipeline-builder/pipelines/<cls_name>', methods=['PUT'])
def set_pipeline_layout(cls_name):
    """Save a custom layout. Body: ``{"phases": [str | [str, ...], ...]}``."""
    data = request.get_json() or {}
    phases = data.get('phases')
    if not isinstance(phases, list):
        return jsonify({'error': 'phases must be a list'}), 400

    from modules.stages import get_stage_registry
    registry = get_stage_registry()
    for phase in phases:
        items = phase if isinstance(phase, list) else [phase]
        for name in items:
            if not isinstance(name, str) or name not in registry:
                return jsonify({'error': f'unknown stage {name!r}'}), 400

    ok = wifi_manager.pipeline_config.set_layout(cls_name, phases)
    if not ok:
        return jsonify({'error': 'persistence unavailable'}), 503

    # Apply the new layout to the live pipeline instance so the change
    # takes effect immediately without a process restart.
    for mod in wifi_manager.modules:
        if mod.__class__.__name__ == cls_name and hasattr(mod, '_hydrate_phases'):
            rebuilt = mod._hydrate_phases(phases)
            if rebuilt:
                mod.phases = rebuilt
                mod.stages = PipelineModule._flatten_phases(rebuilt)
            break
    return jsonify({'success': True, 'class': cls_name, 'phases': phases})


@app.route('/api/pipeline-builder/pipelines/<cls_name>', methods=['DELETE'])
def reset_pipeline_layout(cls_name):
    """Drop a custom layout so the module reverts to its hard-coded defaults."""
    ok = wifi_manager.pipeline_config.reset_layout(cls_name)
    # Restore defaults on the live instance too.
    for mod in wifi_manager.modules:
        if mod.__class__.__name__ == cls_name and hasattr(mod, 'default_phases'):
            mod.phases = mod.default_phases
            mod.stages = PipelineModule._flatten_phases(mod.default_phases)
            break
    return jsonify({'success': ok, 'class': cls_name})


@app.route('/api/modules/<name>/config', methods=['GET'])
def get_module_config(name):
    return jsonify({
        'schema': wifi_manager.module_config.get_schema(name),
        'values': wifi_manager.module_config.get_config(name),
    })


@app.route('/api/modules/<name>/config', methods=['PUT'])
def set_module_config(name):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    success = wifi_manager.module_config.set_config_bulk(name, data)
    return jsonify({'success': success})


@app.route('/api/known-networks', methods=['GET'])
def list_known_networks():
    """Return all known-network entries (passwords redacted)."""
    return jsonify(wifi_manager.known_networks_store.list_all())


@app.route('/api/known-networks', methods=['POST'])
def add_known_network():
    """Add or update a known-network credential. Body: {ssid, password, security?, notes?}."""
    data = request.get_json() or {}
    ssid = (data.get('ssid') or '').strip()
    password = data.get('password') or ''
    if not ssid or not password:
        return jsonify({'error': 'ssid and password required'}), 400
    ok = wifi_manager.known_networks_store.add(
        ssid=ssid,
        password=password,
        security=data.get('security', 'WPA2'),
        notes=data.get('notes', ''),
    )
    if not ok:
        return jsonify({'error': 'Store unavailable'}), 503
    return jsonify({'success': True, 'ssid': ssid})


@app.route('/api/known-networks/<ssid>', methods=['DELETE'])
def remove_known_network(ssid):
    ok = wifi_manager.known_networks_store.remove(ssid)
    if not ok:
        return jsonify({'error': 'Not found or store unavailable'}), 404
    return jsonify({'success': True, 'ssid': ssid})


@app.route('/api/known-networks/<ssid>/reveal', methods=['GET'])
def reveal_known_network(ssid):
    password = wifi_manager.known_networks_store.reveal(ssid)
    if password is None:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'ssid': ssid, 'password': password})


@app.route('/api/modules/<name>/enabled', methods=['PUT'])
def set_module_enabled(name):
    """Enable or disable a module at runtime."""
    data = request.get_json()
    if data is None or 'enabled' not in data:
        return jsonify({'error': 'enabled field required'}), 400
    success = wifi_manager.set_module_enabled(name, bool(data['enabled']))
    if not success:
        return jsonify({'error': 'Module not found'}), 404
    return jsonify({'success': True, 'name': name, 'enabled': data['enabled']})


@app.route('/api/modules/<name>/consent', methods=['POST'])
def set_module_consent(name):
    """Set consent mode for a module: off, on, or by_ssid."""
    data = request.get_json()
    mode = data.get('mode') if data else None

    # Legacy boolean support
    if mode is None and data:
        if data.get('consented'):
            mode = 'on'
        else:
            mode = 'off'

    if mode not in ('off', 'on', 'by_ssid'):
        return jsonify({'error': 'Invalid mode. Use off, on, or by_ssid'}), 400

    success = wifi_manager.consent_manager.set_mode(name, mode)
    return jsonify({'success': success, 'mode': mode})


@app.route('/api/modules/<name>/consent/ssid', methods=['POST'])
def approve_ssid_consent(name):
    """Approve or revoke a specific network for a by_ssid consent module."""
    data = request.get_json()
    if not data or 'bssid' not in data:
        return jsonify({'error': 'bssid required'}), 400

    bssid = data['bssid']
    ssid = data.get('ssid', '')

    group_by_ssid = wifi_manager.get_group_by_ssid()
    if data.get('approved', True):
        success = wifi_manager.consent_manager.approve_ssid(
            name, bssid, ssid, group_by_ssid=group_by_ssid,
        )
    else:
        success = wifi_manager.consent_manager.revoke_ssid(
            name, bssid, ssid=ssid, group_by_ssid=group_by_ssid,
        )

    return jsonify({'success': success})


@app.route('/api/modules/<name>/consent/ssids')
def get_approved_ssids(name):
    """Get all approved networks for a module."""
    return jsonify(wifi_manager.consent_manager.get_approved_ssids(name))


@app.route('/api/modules/consent')
def get_all_consent():
    return jsonify(wifi_manager.consent_manager.get_all())


@app.route('/api/config/network-grouping', methods=['GET'])
def get_network_grouping():
    """Return whether networks are grouped by SSID."""
    return jsonify({'group_by_ssid': wifi_manager.get_group_by_ssid()})


@app.route('/api/config/network-grouping', methods=['PUT'])
def set_network_grouping():
    """Enable or disable SSID-based network grouping."""
    data = request.get_json()
    if data is None or 'group_by_ssid' not in data:
        return jsonify({'error': 'group_by_ssid field required'}), 400
    wifi_manager.set_group_by_ssid(bool(data['group_by_ssid']))
    return jsonify({
        'success': True,
        'group_by_ssid': wifi_manager.get_group_by_ssid(),
    })


# Maps each key the helper's /api/client-config block emits to the pipeline
# stage that consumes it. Keys not in this map are reported back as unknown.
HELPER_CONFIG_KEY_STAGE = {
    'ssh_server': 'dns_port_tunnel',
    'ssh_user': 'dns_port_tunnel',
    'ssh_key_path': 'dns_port_tunnel',
    'wg_config_path': 'dns_port_tunnel',
    'server_domain': 'dns_tunnel',
    'tunnel_password': 'dns_tunnel',
    'tunnel_type': 'dns_tunnel',
    'offload_domain': 'dns_offload_crack',
    'offload_secret': 'dns_offload_crack',
}


def parse_helper_config(text: str):
    """Parse the helper's client-config copy-paste block.

    The block is a list of ``key: value`` lines grouped under ``#`` comment
    headers (see the helper's ``/api/client-config``). Comment and blank
    lines are ignored; each setting is split on its first colon and routed
    to its stage via ``HELPER_CONFIG_KEY_STAGE``.

    Returns ``(by_stage, unknown)`` where ``by_stage`` maps stage name to a
    ``{key: value}`` dict and ``unknown`` lists unrecognised keys.
    """
    by_stage: dict[str, dict] = {}
    unknown: list[str] = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()
        stage = HELPER_CONFIG_KEY_STAGE.get(key)
        if stage is None:
            unknown.append(key)
            continue
        by_stage.setdefault(stage, {})[key] = value
    return by_stage, unknown


@app.route('/api/helper-import', methods=['POST'])
def helper_import():
    """Import a helper client-config block into the relevant stage configs.

    Body: ``{text: "<paste from the helper's Client Config block>"}``.
    Parses each ``key: value`` line, maps it to its pipeline stage, and
    persists it so the stages pick it up at runtime. Does not enable modules
    or grant consent — that stays an explicit operator action.
    """
    data = request.get_json(silent=True) or {}
    text = data.get('text', '')
    if not isinstance(text, str) or not text.strip():
        return jsonify({'error': 'No config text provided'}), 400

    by_stage, unknown = parse_helper_config(text)
    if not by_stage:
        return jsonify({
            'error': 'No recognised settings found in the pasted block.',
            'unknown_keys': sorted(set(unknown)),
        }), 400

    applied: dict[str, list] = {}
    failed: list[str] = []
    for stage, values in by_stage.items():
        if wifi_manager.module_config.set_config_bulk(stage, values):
            applied[stage] = sorted(values.keys())
        else:
            failed.append(stage)

    return jsonify({
        'success': not failed,
        'applied': applied,
        'failed': sorted(failed),
        'unknown_keys': sorted(set(unknown)),
        'store_available': wifi_manager.module_config.is_available(),
    })


@app.route('/api/hostap/status')
def get_hostap_status():
    """Get HostAP status."""
    return jsonify(wifi_manager.get_hostap_status())


@app.route('/api/hostap/config', methods=['GET'])
def get_hostap_config():
    """Get saved HostAP configuration."""
    return jsonify(wifi_manager._load_hostap_config())


@app.route('/api/hostap/config', methods=['PUT'])
def save_hostap_config():
    """Save HostAP configuration."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    wifi_manager._save_hostap_config(data)
    return jsonify({'success': True})


@app.route('/api/hostap/start', methods=['POST'])
def start_hostap():
    """Start the host access point."""
    data = request.get_json() or {}
    result = wifi_manager.start_hostap(data)
    emit_status_update()
    return jsonify(result)


@app.route('/api/hostap/stop', methods=['POST'])
def stop_hostap():
    """Stop the host access point."""
    result = wifi_manager.stop_hostap()
    emit_status_update()
    return jsonify(result)


@app.route('/api/hostap/confirm', methods=['POST'])
def confirm_hostap():
    """Confirm HostAP config for lazy/auto start."""
    data = request.get_json() or {}
    result = wifi_manager.confirm_hostap(data)
    emit_status_update()
    return jsonify(result)


@app.route('/api/hostap/disable', methods=['POST'])
def disable_hostap():
    """Disable lazy/auto HostAP and stop if running."""
    result = wifi_manager.disable_hostap_lazy()
    emit_status_update()
    return jsonify(result)


@app.route('/api/hostap/clear-error', methods=['POST'])
def clear_hostap_error():
    """Dismiss the sticky ``last_error`` shown in the status banner."""
    wifi_manager._hostap_last_error = None
    return jsonify({'success': True})


@app.route('/api/ethernet/status')
def get_ethernet_status():
    """Get wired ethernet port status and mode."""
    return jsonify(wifi_manager.get_ethernet_status())


@app.route('/api/ethernet/mode', methods=['PUT'])
def set_ethernet_mode():
    """Switch ethernet between management and pool modes."""
    data = request.get_json()
    if not data or 'mode' not in data:
        return jsonify({'error': 'Missing mode'}), 400
    result = wifi_manager.set_ethernet_mode(data['mode'])
    emit_status_update()
    return jsonify(result)


@app.route('/api/use_connection/<int:index>', methods=['POST'])
def use_connection(index):
    success = wifi_manager.use_connection(index)
    emit_status_update()
    emit_connections_update()
    return jsonify({'success': success})


@app.route('/api/stop_connection', methods=['POST'])
def stop_connection():
    wifi_manager.stop_current_connection()
    emit_status_update()
    emit_connections_update()
    return jsonify({'success': True})


# Human-readable messages for start_bridge_override error codes.
_BRIDGE_OVERRIDE_ERRORS = {
    'network_not_found': 'Network not found in the latest scan — rescan and retry.',
    'no_saved_credentials': 'Encrypted network: add its password under Saved Wi-Fi '
                            'Credentials first (override does not prompt).',
    'no_free_card': 'No free WiFi card — stop another connection to free one, then retry.',
    'connect_failed': 'Could not associate to the network.',
    'bridge_failed': 'Associated, but failed to set up the bridge.',
}


@app.route('/api/bridge_override', methods=['POST'])
def bridge_override():
    """Force-bridge a chosen network even with no internet (Bridge Override).

    Body: ``{bssid, ssid?}``. Pins the connection so automatic switching /
    reconcile won't disturb it until /api/bridge_override/stop.
    """
    data = request.get_json(silent=True) or {}
    bssid = (data.get('bssid') or '').strip()
    ssid = (data.get('ssid') or '').strip()
    if not bssid and not ssid:
        return jsonify({'success': False, 'error': 'bssid or ssid required'}), 400
    result = wifi_manager.start_bridge_override(bssid, ssid)
    emit_status_update()
    emit_connections_update()
    if not result.get('success'):
        result['message'] = _BRIDGE_OVERRIDE_ERRORS.get(
            result.get('error'), 'Bridge override failed.'
        )
    return jsonify(result)


@app.route('/api/bridge_override/stop', methods=['POST'])
def bridge_override_stop():
    """Tear down the active Bridge Override and restore normal bridging."""
    result = wifi_manager.stop_bridge_override()
    emit_status_update()
    emit_connections_update()
    return jsonify(result)


@app.route('/api/connections/sorted')
def get_sorted_connections():
    """Get connections sorted by score (best first)."""
    sorted_conns = wifi_manager.get_sorted_connections()
    return jsonify(
        [
            {
                'ssid': conn.network.ssid,
                'bssid': conn.network.bssid,
                'score': conn.calculate_score(),
                'download_speed': conn.download_speed,
                'upload_speed': conn.upload_speed,
                'ping': conn.ping,
                'signal_strength': conn.network.signal_strength,
                'interface': conn.interface,
                'connection_method': conn.connection_method,
            }
            for conn in sorted_conns
        ]
    )


@app.route('/api/metrics/network/<ssid>')
def get_network_metrics(ssid):
    """Get historical metrics for a specific network."""
    history = wifi_manager.metrics_store.get_network_history(ssid)
    avg_score = wifi_manager.metrics_store.get_average_score(ssid)
    return jsonify(
        {
            'ssid': ssid,
            'average_score': avg_score,
            'history': [
                {
                    'score': h.get('score'),
                    'download_speed': h.get('download_speed'),
                    'upload_speed': h.get('upload_speed'),
                    'ping': h.get('ping'),
                    'signal_strength': h.get('signal_strength'),
                    'timestamp': h.get('timestamp'),
                }
                for h in history
            ],
        }
    )


@app.route('/api/metrics/best')
def get_best_networks():
    """Get the best performing networks based on historical data."""
    best = wifi_manager.metrics_store.get_best_networks()
    return jsonify(best)


@app.route('/api/history')
def get_history():
    """Get connection history from MongoDB."""
    if history_collection is None:
        return jsonify({'history': [], 'available': False})

    try:
        # Get last 50 entries, newest first
        history = list(history_collection.find().sort('timestamp', -1).limit(50))

        # Convert ObjectId to string for JSON serialization
        for entry in history:
            entry['_id'] = str(entry['_id'])
            entry['timestamp'] = entry['timestamp'].isoformat()

        return jsonify({'history': history, 'available': True})
    except Exception as e:
        logger.error(f'Failed to fetch history: {e}')
        return jsonify({'history': [], 'available': False, 'error': str(e)})


@app.route('/api/auto_select/enable', methods=['POST'])
def enable_auto_selection():
    """Enable auto-selection mode."""
    wifi_manager.enable_auto_selection()
    return jsonify({'success': True, 'enabled': True})


@app.route('/api/auto_select/disable', methods=['POST'])
def disable_auto_selection():
    """Disable auto-selection mode."""
    wifi_manager.disable_auto_selection()
    return jsonify({'success': True, 'enabled': False})


@app.route('/api/auto_select/status')
def get_auto_selection_status():
    """Get auto-selection status and statistics."""
    status = wifi_manager.get_auto_selection_status()
    return jsonify(status)


@app.route('/api/auto-bridge', methods=['GET'])
def get_auto_bridge():
    """Return whether auto-bridge of the best successful connection is on."""
    return jsonify({'enabled': wifi_manager.get_auto_bridge_enabled()})


@app.route('/api/auto-bridge', methods=['PUT'])
def set_auto_bridge():
    """Enable or disable auto-bridge. Body: {enabled: bool}."""
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        return jsonify({'error': 'enabled field required'}), 400
    wifi_manager.set_auto_bridge_enabled(bool(data['enabled']))
    return jsonify({'success': True,
                    'enabled': wifi_manager.get_auto_bridge_enabled()})


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info('Client connected to websocket')
    # The UI may connect before initialization finishes — send a minimal
    # 'initializing' status and skip the helpers that need wifi_manager.
    if not app_is_ready():
        emit('status_update', {'initializing': True})
        return
    # Send initial status to the newly connected client
    emit('status_update', wifi_manager.status)
    emit_connections_update()
    emit_scan_update()


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.info('Client disconnected from websocket')


# === P3: Connection Persistence API ===


@app.route('/api/saved_networks')
def get_saved_networks():
    """Get all saved/known networks."""
    networks = wifi_manager.connection_store.get_known_networks()
    return jsonify(networks)


@app.route('/api/saved_networks/best')
def get_best_saved_networks():
    """Get top performing saved networks."""
    limit = request.args.get('limit', 10, type=int)
    networks = wifi_manager.connection_store.get_best_networks(limit=limit)
    return jsonify(networks)


@app.route('/api/saved_networks/<ssid>', methods=['DELETE'])
def delete_saved_network(ssid):
    """Delete a saved network by SSID."""
    deleted = wifi_manager.connection_store.delete_network(ssid)
    if deleted:
        return jsonify({'status': 'deleted', 'ssid': ssid})
    return jsonify({'status': 'not_found', 'ssid': ssid}), 404


@app.route('/api/saved_networks', methods=['DELETE'])
def clear_all_saved_networks():
    """Delete all saved networks."""
    count = wifi_manager.connection_store.clear_all()
    return jsonify({'status': 'cleared', 'count': count})


@app.route('/api/probes')
def get_probes():
    """Get all cached probe history entries (BSSID→SSID mappings)."""
    ph = wifi_manager.probe_history
    if not ph:
        return jsonify([])

    # If MongoDB is available, return full docs with last_seen timestamps
    if ph._available:
        try:
            docs = list(ph.collection.find(
                {}, {'_id': 0, 'bssid': 1, 'ssid': 1, 'last_seen': 1}
            ).sort('last_seen', -1))
            return jsonify(docs)
        except Exception:
            pass

    # Fallback: return from in-memory cache (no timestamps)
    return jsonify([
        {'bssid': b, 'ssid': s, 'last_seen': None}
        for b, s in ph._cache.items()
    ])


@app.route('/api/probes/<path:bssid>', methods=['DELETE'])
def delete_probe(bssid):
    """Delete a single probe entry by BSSID."""
    ph = wifi_manager.probe_history
    if not ph:
        return jsonify({'status': 'error', 'message': 'Probe history unavailable'}), 503

    bssid_lower = bssid.lower()
    removed = bssid_lower in ph._cache
    ph._cache.pop(bssid_lower, None)

    if ph._available:
        try:
            ph.collection.delete_one({'bssid': bssid_lower})
        except Exception:
            pass

    if removed:
        return jsonify({'status': 'deleted', 'bssid': bssid_lower})
    return jsonify({'status': 'not_found', 'bssid': bssid_lower}), 404


@app.route('/api/probes', methods=['DELETE'])
def clear_probes():
    """Clear all cached probe history."""
    ph = wifi_manager.probe_history
    if not ph:
        return jsonify({'status': 'error', 'message': 'Probe history unavailable'}), 503

    count = len(ph._cache)
    ph._cache.clear()

    if ph._available:
        try:
            result = ph.collection.delete_many({})
            count = max(count, result.deleted_count)
        except Exception:
            pass

    return jsonify({'status': 'cleared', 'count': count})


@app.route('/api/mac_assignments', methods=['DELETE'])
def clear_mac_assignments():
    """Clear all stored MAC-to-network mappings."""
    try:
        result = wifi_manager.card_manager.mac_manager.collection.delete_many({})
        wifi_manager.card_manager.mac_manager._cache.clear()
        return jsonify({'status': 'cleared', 'count': result.deleted_count})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/wipe_data', methods=['DELETE'])
def wipe_all_data():
    """Drop all data/history collections but preserve app config.

    Preserved (config): module_state, module_config, module_consent,
                        ssid_consent, hostap_config
    Wiped (data):       connection_metrics, connection_history, card_leases,
                        probe_history, bandwidth, mac_assignments,
                        known_networks, portal_patterns
    """
    if db is None:
        return jsonify({'status': 'error', 'message': 'Database not available'}), 503

    data_collections = [
        'connection_metrics', 'connection_history', 'card_leases',
        'probe_history', 'bandwidth', 'mac_assignments',
        'known_networks', 'portal_patterns',
    ]
    total = 0
    dropped = []
    try:
        for name in data_collections:
            col = db[name]
            result = col.delete_many({})
            total += result.deleted_count
            dropped.append(name)

        # Clear in-memory caches that mirror wiped collections
        if wifi_manager.probe_history:
            wifi_manager.probe_history._cache.clear()
        if hasattr(wifi_manager.card_manager, 'mac_manager'):
            wifi_manager.card_manager.mac_manager._cache.clear()
        wifi_manager.card_manager.lease_store.clear_all()

        return jsonify({
            'status': 'cleared',
            'collections': dropped,
            'total_documents': total,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# === P3: Notification API ===


@app.route('/api/notifications')
def get_notifications():
    """Get recent notification history."""
    limit = request.args.get('limit', 50, type=int)
    history = wifi_manager.notification_manager.get_history(limit=limit)
    return jsonify(history)


# === P3: Bandwidth Monitoring API ===


@app.route('/api/bandwidth/current')
def get_bandwidth_current():
    """Get current bandwidth rates per interface."""
    rates = wifi_manager.bandwidth_monitor.get_current_rates()
    return jsonify(rates)


@app.route('/api/bandwidth/history')
def get_bandwidth_history():
    """Get historical bandwidth data."""
    hours = request.args.get('hours', 24, type=int)
    interface = request.args.get('interface', None)
    history = wifi_manager.bandwidth_monitor.get_history(hours=hours, interface=interface)
    return jsonify(history)


@app.route('/api/bandwidth/total')
def get_bandwidth_total():
    """Get total bandwidth usage."""
    hours = request.args.get('hours', 24, type=int)
    interface = request.args.get('interface', None)
    total = wifi_manager.bandwidth_monitor.get_total_usage(hours=hours, interface=interface)
    return jsonify(total)


# === P3: API Documentation ===


@app.route('/api/docs')
def get_api_docs():
    """Serve the OpenAPI specification."""
    docs_path = os.path.join(os.path.dirname(__file__), 'docs', 'openapi.yaml')
    try:
        with open(docs_path) as f:
            from flask import Response
            return Response(f.read(), mimetype='text/yaml')
    except FileNotFoundError:
        return jsonify({'error': 'API documentation not found'}), 404


def _run_web_server():
    """Serve the Flask/SocketIO UI.

    Runs in its own thread so the interface binds the port and responds
    immediately on restart, instead of waiting on the slower WiFi-card
    enumeration, MongoDB connections, and HostAP boot in _init_app(). Until
    that finishes, wifi_manager is None and the routes report an 'initializing'
    state (see _guard_until_ready / app_is_ready).
    """
    config = get_config()
    socketio.run(
        app, host=config.web.host, port=config.web.port,
        allow_unsafe_werkzeug=True,
    )


def main():
    config = get_config()
    logger.info('Vasili starting with configuration loaded')

    # Start the web UI FIRST, in its own thread, so a restart serves the
    # interface near-instantly. The slow initialization below runs while the
    # UI is already up; it reports "initializing" until ready.
    web_thread = None
    if config.web.enabled:
        logger.info(f'Starting web interface on {config.web.host}:{config.web.port}')
        web_thread = threading.Thread(
            target=_run_web_server, name='web-ui', daemon=True,
        )
        web_thread.start()
    else:
        logger.info('Web interface disabled, running in headless mode')

    # Heavy initialization (WiFi cards, MongoDB, HostAP) — the slow part of a
    # restart. Creates the WifiManager and flips the UI from 'initializing'.
    _init_app()
    logger.info('Initialization complete — UI fully live')

    # Start scanning in a separate thread
    scan_thread = threading.Thread(
        target=wifi_manager.scan_and_connect, name='scan',
    )
    scan_thread.start()

    # Keep the main thread alive (and responsive to Ctrl+C).
    try:
        if web_thread is not None:
            web_thread.join()
        else:
            scan_thread.join()
    except KeyboardInterrupt:
        logger.info('Shutting down...')


if __name__ == '__main__':
    main()

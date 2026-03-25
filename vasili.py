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
from pyarchops_dnsmasq import dnsmasq
from datetime import datetime

from config import VasiliConfig, apply_logging_config, load_config
from logging_config import setup_logging, get_logger
from persistence import ConnectionStore
from notifications import NotificationManager, NotificationEvent
from bandwidth import BandwidthMonitor
from module_config import ModuleConfigStore
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


@dataclass
class ConnectionResult:
    network: WifiNetwork
    download_speed: float
    upload_speed: float
    ping: float
    connected: bool
    connection_method: str
    interface: str

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


class NetworkBridge:
    def __init__(self, wifi_interface: str, ethernet_interface: str):
        self.wifi_interface = wifi_interface
        self.ethernet_interface = ethernet_interface
        self.dhcp_server = None
        self.is_active = False
        self._nat_configured = False
        self._ip_configured = False
        self._original_ip_forward: Optional[str] = None

    def setup_nat(self) -> bool:
        """Set up NAT for traffic forwarding."""
        try:
            # Save original IP forwarding state
            try:
                with open('/proc/sys/net/ipv4/ip_forward', 'r') as f:
                    self._original_ip_forward = f.read().strip()
            except Exception as e:
                logger.warning(f'Could not read original IP forward state: {e}')
                self._original_ip_forward = '0'

            # Enable IP forwarding
            try:
                with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
                    f.write('1')
            except PermissionError:
                logger.error('Permission denied: cannot enable IP forwarding. Run as root.')
                return False
            except Exception as e:
                logger.error(f'Failed to enable IP forwarding: {e}')
                return False

            # Set up vasili-specific iptables chains (avoid flushing all rules)
            for cmd in [
                ['iptables', '-N', 'VASILI-FWD'],
                ['iptables', '-t', 'nat', '-N', 'VASILI-NAT'],
            ]:
                result = subprocess.run(cmd, capture_output=True, text=True)
                # Chain may already exist from a previous run — that's fine
                if result.returncode != 0 and 'already exists' not in result.stderr.lower():
                    logger.debug(f'Chain creation note: {result.stderr.strip()}')

            # Flush only vasili chains
            subprocess.run(['iptables', '-F', 'VASILI-FWD'], capture_output=True, text=True)
            subprocess.run(
                ['iptables', '-t', 'nat', '-F', 'VASILI-NAT'], capture_output=True, text=True
            )

            # Jump into vasili chains from main chains (idempotent)
            for jump_cmd in [
                ['iptables', '-C', 'FORWARD', '-j', 'VASILI-FWD'],
                ['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-j', 'VASILI-NAT'],
            ]:
                result = subprocess.run(jump_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    # Rule doesn't exist yet — add it
                    add_cmd = [jump_cmd[0]] + ['-A' if c == '-C' else c for c in jump_cmd[1:]]
                    subprocess.run(add_cmd, capture_output=True, text=True)

            # Set up NAT masquerade in vasili chain
            result = subprocess.run(
                [
                    'iptables',
                    '-t',
                    'nat',
                    '-A',
                    'VASILI-NAT',
                    '-o',
                    self.wifi_interface,
                    '-j',
                    'MASQUERADE',
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f'Failed to set up NAT masquerade: {result.stderr}')
                self._cleanup_nat()
                return False

            # Allow forwarding in vasili chain
            result = subprocess.run(
                [
                    'iptables',
                    '-A',
                    'VASILI-FWD',
                    '-i',
                    self.ethernet_interface,
                    '-o',
                    self.wifi_interface,
                    '-j',
                    'ACCEPT',
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f'Failed to set up forward rule: {result.stderr}')
                self._cleanup_nat()
                return False

            result = subprocess.run(
                [
                    'iptables',
                    '-A',
                    'VASILI-FWD',
                    '-i',
                    self.wifi_interface,
                    '-o',
                    self.ethernet_interface,
                    '-m',
                    'state',
                    '--state',
                    'RELATED,ESTABLISHED',
                    '-j',
                    'ACCEPT',
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f'Failed to set up reverse forward rule: {result.stderr}')
                self._cleanup_nat()
                return False

            self._nat_configured = True
            logger.info('NAT setup completed successfully')
            return True

        except Exception as e:
            logger.error(f'Failed to set up NAT: {e}')
            self._cleanup_nat()
            return False

    def _cleanup_nat(self):
        """Clean up NAT configuration on failure."""
        try:
            subprocess.run(['iptables', '-F', 'VASILI-FWD'], capture_output=True)
            subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-NAT'], capture_output=True)
            if self._original_ip_forward is not None:
                try:
                    with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
                        f.write(self._original_ip_forward)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f'Error during NAT cleanup: {e}')
        self._nat_configured = False

    def setup_dhcp(self) -> bool:
        """Set up DHCP server on ethernet interface."""
        try:
            # Configure ethernet interface with static IP
            result = subprocess.run(
                ['ip', 'addr', 'add', '192.168.10.1/24', 'dev', self.ethernet_interface],
                capture_output=True,
                text=True,
            )
            # Ignore "already exists" errors
            if result.returncode != 0 and 'RTNETLINK answers: File exists' not in result.stderr:
                logger.error(f'Failed to configure IP: {result.stderr}')
                return False

            self._ip_configured = True

            result = subprocess.run(
                ['ip', 'link', 'set', self.ethernet_interface, 'up'], capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.error(f'Failed to bring up interface: {result.stderr}')
                self._cleanup_dhcp()
                return False

            # Start DHCP server
            self.dhcp_server = dnsmasq.DHCP(
                interface=self.ethernet_interface,
                dhcp_range=('192.168.10.50', '192.168.10.150'),
                subnet_mask='255.255.255.0',
            )
            self.dhcp_server.start()
            logger.info('DHCP server started successfully')
            return True

        except Exception as e:
            logger.error(f'Failed to set up DHCP: {e}')
            self._cleanup_dhcp()
            return False

    def _cleanup_dhcp(self):
        """Clean up DHCP configuration on failure."""
        try:
            if self.dhcp_server:
                try:
                    self.dhcp_server.stop()
                except Exception as e:
                    logger.warning(f'Error stopping DHCP server: {e}')
                self.dhcp_server = None

            if self._ip_configured:
                subprocess.run(
                    ['ip', 'addr', 'del', '192.168.10.1/24', 'dev', self.ethernet_interface],
                    capture_output=True,
                )
                self._ip_configured = False
        except Exception as e:
            logger.warning(f'Error during DHCP cleanup: {e}')

    def start(self) -> bool:
        """Start the network bridge. Cleans up on partial failure."""
        nat_ok = self.setup_nat()
        if not nat_ok:
            logger.error('Bridge start failed: NAT setup failed')
            return False

        dhcp_ok = self.setup_dhcp()
        if not dhcp_ok:
            logger.error('Bridge start failed: DHCP setup failed, cleaning up NAT')
            self._cleanup_nat()
            return False

        self.is_active = True
        logger.info(f'Network bridge active: {self.wifi_interface} -> {self.ethernet_interface}')
        return True

    def stop(self):
        """Stop the network bridge and clean up all resources."""
        logger.info('Stopping network bridge...')

        # Stop DHCP server
        self._cleanup_dhcp()

        # Clean up NAT
        self._cleanup_nat()

        self.is_active = False
        logger.info('Network bridge stopped')

    def get_status(self) -> dict:
        """Get bridge status information."""
        return {
            'is_active': self.is_active,
            'wifi_interface': self.wifi_interface,
            'ethernet_interface': self.ethernet_interface,
            'nat_configured': self._nat_configured,
            'dhcp_running': self.dhcp_server is not None,
        }


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
        self.is_active = False

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    @staticmethod
    def check_hostapd_installed() -> bool:
        return shutil.which('hostapd') is not None

    def _get_phy(self) -> Optional[str]:
        """Get the phy name for the interface (e.g. phy0)."""
        phy_path = f'/sys/class/net/{self.interface}/phy80211/name'
        try:
            with open(phy_path) as f:
                return f.read().strip()
        except Exception:
            return None

    def check_ap_support(self) -> bool:
        """Check if the interface supports AP mode."""
        phy = self._get_phy()
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

    def _configure_interface(self) -> bool:
        """Disconnect from NetworkManager and assign static IP."""
        # Disconnect any NM-managed connection on this interface
        subprocess.run(
            ['nmcli', 'device', 'disconnect', self.interface],
            capture_output=True, text=True,
        )
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
            logger.error(f'HostAP: failed to assign IP: {result.stderr}')
            return False
        self._ip_configured = True
        # Bring interface up
        result = subprocess.run(
            ['ip', 'link', 'set', self.interface, 'up'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error(f'HostAP: failed to bring up interface: {result.stderr}')
            return False
        return True

    def _cleanup_interface(self):
        """Remove the static IP from the AP interface."""
        if self._ip_configured:
            subprocess.run(
                ['ip', 'addr', 'del', f'{self.AP_IP}/24', 'dev', self.interface],
                capture_output=True,
            )
            self._ip_configured = False

    # ------------------------------------------------------------------
    # hostapd process
    # ------------------------------------------------------------------

    def _start_hostapd(self) -> bool:
        """Launch hostapd as a subprocess."""
        try:
            self._hostapd_process = subprocess.Popen(
                ['hostapd', self.CONF_PATH],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            # Give hostapd a moment to start
            time.sleep(2)
            if self._hostapd_process.poll() is not None:
                _, stderr = self._hostapd_process.communicate()
                logger.error(f'HostAP: hostapd exited: {stderr.decode()[:200]}')
                self._hostapd_process = None
                return False
            logger.info(f'HostAP: hostapd started (pid {self._hostapd_process.pid})')
            return True
        except Exception as e:
            logger.error(f'HostAP: failed to start hostapd: {e}')
            return False

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
            self._dhcp_server = dnsmasq.DHCP(
                interface=self.interface,
                dhcp_range=self.DHCP_RANGE,
                subnet_mask='255.255.255.0',
            )
            self._dhcp_server.start()
            logger.info('HostAP: DHCP server started')
            return True
        except Exception as e:
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
            ]:
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0 and 'already exists' not in result.stderr.lower():
                    logger.debug(f'HostAP chain creation: {result.stderr.strip()}')

            # Flush chains
            subprocess.run(['iptables', '-F', 'VASILI-HOSTAP-FWD'], capture_output=True)
            subprocess.run(['iptables', '-t', 'nat', '-F', 'VASILI-HOSTAP-NAT'], capture_output=True)

            # Jump into hostap chains from main chains (idempotent)
            for jump_cmd in [
                ['iptables', '-C', 'FORWARD', '-j', 'VASILI-HOSTAP-FWD'],
                ['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-j', 'VASILI-HOSTAP-NAT'],
            ]:
                result = subprocess.run(jump_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    add_cmd = [jump_cmd[0]] + ['-A' if c == '-C' else c for c in jump_cmd[1:]]
                    subprocess.run(add_cmd, capture_output=True)

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
    ) -> bool:
        """
        Connect to a WiFi network using this card with automatic retry logic.

        Args:
            network: The WifiNetwork to connect to
            password: Optional password for encrypted networks
            max_retries: Maximum number of connection attempts (default: 3)
            base_delay: Base delay in seconds between retries, doubles each attempt (default: 1.0)

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
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                if result.returncode == 0:
                    logger.info(f'Successfully connected to {network.ssid} on {self.interface}')
                    self.in_use = True
                    self._connected_network = network
                    self._connection_password = password

                    # Set up routing isolation so speedtest/connectivity
                    # checks use this interface, not eth0
                    self._routing_info = self._setup_isolation()

                    return True
                else:
                    last_error = f'nmcli error: {result.stderr}'
                    logger.warning(
                        f'Attempt {attempt}/{max_retries} failed for {network.ssid}: {result.stderr}'
                    )

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
            f'Failed to connect to {network.ssid} after {max_retries} attempts. Last error: {last_error}'
        )
        self.in_use = False
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
                self.in_use = False
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
        """Get the SSID of the currently connected network, if any."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'DEVICE,CONNECTION', 'device', 'status'],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.strip().split('\n'):
                if ':' in line:
                    device, connection = line.split(':', 1)
                    if device == self.interface and connection and connection != '--':
                        return connection
            return None
        except Exception as e:
            logger.error(f'Error getting connected SSID for {self.interface}: {e}')
            return None

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

    def return_card(self, card: WifiCard, holder: str = 'vasili'):
        """Return a card to the pool of available cards."""
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

    def get_all_cards(self) -> list[WifiCard]:
        """Get list of all wifi cards."""
        with self._lock:
            return list(self.cards)

    def get_available_count(self) -> int:
        """Get count of cards not currently in use."""
        with self._lock:
            return sum(1 for card in self.cards if not card.in_use)

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

    def clear_hostap_card(self) -> Optional[WifiCard]:
        """Return the hostap card to the pool."""
        with self._lock:
            if self._hostap_card:
                self._hostap_card.in_use = False
                self._hostap_card.current_task = None
                card = self._hostap_card
                self._hostap_card = None
                return card
            return None


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

    This class runs a background thread that periodically checks the status of
    monitored connections and automatically attempts to reconnect when a
    connection is lost.
    """

    def __init__(self, check_interval: float = 10.0, max_reconnect_attempts: int = 5):
        """
        Initialize the connection monitor.

        Args:
            check_interval: How often to check connections in seconds (default: 10)
            max_reconnect_attempts: Maximum reconnection attempts before giving up (default: 5)
        """
        self.check_interval = check_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        self._monitored_cards: list[WifiCard] = []
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._reconnect_callbacks: list = []

    def add_card(self, card: WifiCard):
        """Add a card to be monitored for connection drops."""
        with self._lock:
            if card not in self._monitored_cards:
                self._monitored_cards.append(card)
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
        """Start the connection monitoring thread."""
        if self._monitoring:
            return

        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_worker, daemon=True)
        self._monitor_thread.start()
        logger.info('Connection monitor started')

    def stop(self):
        """Stop the connection monitoring thread."""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=self.check_interval + 1)
            self._monitor_thread = None
        logger.info('Connection monitor stopped')

    def _monitor_worker(self):
        """Background worker that monitors connections and triggers reconnection."""
        reconnect_attempts: dict[str, int] = {}

        while self._monitoring:
            with self._lock:
                cards_to_check = list(self._monitored_cards)

            for card in cards_to_check:
                if not self._monitoring:
                    break

                # Only check cards that should be connected
                if card._connected_network is None:
                    continue

                expected_ssid = card._connected_network.ssid
                is_connected = card.is_connected()
                current_ssid = card.get_connected_ssid() if is_connected else None

                if is_connected and current_ssid == expected_ssid:
                    # Connection is healthy, reset reconnect counter
                    reconnect_attempts[card.interface] = 0
                    continue

                # Connection dropped or connected to wrong network
                attempts = reconnect_attempts.get(card.interface, 0)

                if attempts >= self.max_reconnect_attempts:
                    logger.error(
                        f'Max reconnect attempts ({self.max_reconnect_attempts}) reached for '
                        f'{card.interface}. Giving up on {expected_ssid}.'
                    )
                    # Clear the network so we stop trying
                    card._connected_network = None
                    card._connection_password = None
                    reconnect_attempts[card.interface] = 0
                    self._notify_callbacks(card, success=False)
                    continue

                logger.warning(
                    f'Connection dropped on {card.interface} (expected: {expected_ssid}, '
                    f'current: {current_ssid}). Attempting reconnect ({attempts + 1}/'
                    f'{self.max_reconnect_attempts})...'
                )

                reconnect_attempts[card.interface] = attempts + 1

                # Attempt reconnection
                success = card.reconnect(max_retries=2, base_delay=0.5)

                if success:
                    logger.info(f'Successfully reconnected {card.interface} to {expected_ssid}')
                    reconnect_attempts[card.interface] = 0
                    self._notify_callbacks(card, success=True)
                else:
                    logger.warning(
                        f'Reconnection attempt {attempts + 1} failed for {card.interface}'
                    )

            # Wait before next check
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

    def run_speedtest(self, card) -> tuple[float, float, float]:
        """Run a speedtest bound to the card's interface IP.

        Verifies actual internet connectivity through the WiFi interface
        before running the speedtest, preventing false results from
        traffic routing through other interfaces (e.g. eth0).

        Args:
            card: WifiCard that is connected to a network

        Returns:
            Tuple of (download_mbps, upload_mbps, ping_ms)

        Raises:
            ConnectionError: If no IP or no internet on the interface
        """
        wifi_ip = card.get_ip_address()
        if not wifi_ip:
            raise ConnectionError(f'No IP address on {card.interface}')

        if not network_isolation.verify_connectivity(card.interface):
            raise ConnectionError(
                f'No internet connectivity via {card.interface}'
            )

        st = speedtest.Speedtest(source_address=wifi_ip)
        st.get_best_server()
        download = st.download() / 1_000_000
        upload = st.upload() / 1_000_000
        ping = st.results.ping
        return download, upload, ping


class PipelineStage:
    """A stage within a pipeline that runs against an already-connected card.

    Stages communicate via a shared context dict. Each stage can read
    context set by previous stages and add its own findings.
    """
    name: str = 'unnamed'
    requires_consent: bool = False

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


class PipelineModule(ConnectionModule):
    """Orchestrates a pipeline of stages for a network type.

    The pipeline connects to a network once, then runs stages sequentially.
    Each stage can check/modify the context dict. If a stage achieves
    internet connectivity (has_internet=True), the pipeline runs a speedtest
    and returns success. If all stages exhaust without internet, the card
    is disconnected and failure is returned.
    """
    priority = 10  # Pipelines run before simple modules

    # If True, pipeline connects to the network before running stages.
    # Set to False for encrypted networks where stages handle credentials.
    auto_connect = True

    def __init__(self, card_manager, stages: list[PipelineStage] = None,
                 consent_manager=None, module_config=None, **kwargs):
        super().__init__(card_manager, module_config=module_config)
        self.stages = stages or []
        self.consent_manager = consent_manager
        self.last_stage_log: list[dict] = []  # Stage results from last connect()

    def _has_consent(self, stage_name: str, network: WifiNetwork = None) -> bool:
        if self.consent_manager:
            bssid = network.bssid if network else None
            ssid = network.ssid if network else None
            return self.consent_manager.has_consent(stage_name, bssid=bssid, ssid=ssid)
        return False

    def _get_connect_context(self) -> dict:
        """Return initial context for a connect() call.

        Override in subclasses to inject data like password lists.
        """
        return {}

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

        # Run stages sequentially
        context: dict = self._get_connect_context()
        if self.auto_connect:
            context['wifi_associated'] = True
        successful_stage = None
        self.last_stage_log = []

        for stage in self.stages:
            if stage.requires_consent and not self._has_consent(stage.name, network):
                self.last_stage_log.append({
                    'stage': stage.name,
                    'status': 'skipped',
                    'reason': 'no_consent',
                    'timestamp': time.time(),
                })
                continue

            try:
                if not stage.can_run(network, card, context):
                    self.last_stage_log.append({
                        'stage': stage.name,
                        'status': 'skipped',
                        'reason': 'can_run=False',
                        'timestamp': time.time(),
                    })
                    continue

                logger.info(f'Pipeline stage: {stage.name} on {network.ssid}')
                t0 = time.time()
                result = stage.run(network, card, context)
                elapsed = round(time.time() - t0, 2)
                context.update(result.context_updates)

                stage_entry = {
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
                }
                self.last_stage_log.append(stage_entry)

                if result.stop_pipeline:
                    logger.info(
                        f'Stage {stage.name} stopped pipeline: {result.message}'
                    )
                    break

                if result.has_internet:
                    logger.info(
                        f'Stage {stage.name} achieved internet on {network.ssid}'
                    )
                    successful_stage = stage.name
                    break

            except Exception as e:
                logger.error(f'Stage {stage.name} error: {e}')
                self.last_stage_log.append({
                    'stage': stage.name,
                    'status': 'error',
                    'message': str(e)[:200],
                    'timestamp': time.time(),
                })
                continue

        if successful_stage:
            try:
                dl, ul, ping = self.run_speedtest(card)
                return ConnectionResult(
                    network=network, download_speed=dl, upload_speed=ul,
                    ping=ping, connected=True,
                    connection_method=f'pipeline:{successful_stage}',
                    interface=card.interface,
                )
            except Exception as e:
                logger.warning(f'Speedtest failed after pipeline success: {e}')

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

        self.modules = self._load_connection_modules()
        self.disabled_modules: set[str] = self._load_disabled_modules()
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

        # Register config schemas from loaded modules
        for mod in self.modules:
            schema_method = getattr(mod, 'get_config_schema', None)
            if schema_method:
                schema = schema_method()
                if schema:
                    name = getattr(mod, 'name', mod.__class__.__name__)
                    self.module_config.register_schema(name, schema)

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
        self.hostap: Optional[HostAP] = None

        # Register callback to track reconnection events
        self.connection_monitor.on_reconnect(self._on_reconnect)

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
                            if 'probe_history' in sig.parameters:
                                kwargs['probe_history'] = self.probe_history
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

    def use_connection(self, connection_index: int) -> bool:
        if connection_index >= len(self.suitable_connections):
            return False

        # Stop any existing bridge
        if self.active_bridge:
            self.active_bridge.stop()

        connection = self.suitable_connections[connection_index]

        # Find an available ethernet interface
        ethernet_interfaces = [
            iface
            for iface in netifaces.interfaces()
            if iface.startswith('eth') or iface.startswith('enp')
        ]

        if not ethernet_interfaces:
            logger.error('No ethernet interfaces available')
            return False

        # Create and start new bridge
        self.active_bridge = NetworkBridge(
            wifi_interface=connection.interface, ethernet_interface=ethernet_interfaces[0]
        )

        if self.active_bridge.start():
            self.status['current_bridge'] = {
                'wifi_interface': connection.interface,
                'ethernet_interface': ethernet_interfaces[0],
                'ssid': connection.network.ssid,
            }
            # Update HostAP NAT upstream if running
            if self.hostap and self.hostap.is_active:
                self.hostap.update_upstream(connection.interface)
            return True

        return False

    def stop_current_connection(self):
        if self.active_bridge:
            self.active_bridge.stop()
            self.status['current_bridge'] = None

    # ------------------------------------------------------------------
    # HostAP management
    # ------------------------------------------------------------------

    def start_hostap(self, conf: dict) -> dict:
        """Start the host access point."""
        if self.hostap and self.hostap.is_active:
            return {'success': False, 'error': 'HostAP already running'}

        if not HostAP.check_hostapd_installed():
            return {'success': False, 'error': 'hostapd is not installed'}

        interface = conf.get('interface')
        if not interface:
            conn_cards = self.card_manager.get_connection_cards()
            if not conn_cards:
                return {'success': False, 'error': 'No cards available for HostAP'}
            interface = conn_cards[-1].interface

        card = self.card_manager.set_hostap_card(interface)
        if not card:
            return {'success': False, 'error': f'Cannot reserve {interface} (busy or scanning card)'}

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
            self._save_hostap_config(conf)
            return {'success': True, 'interface': interface}
        else:
            self.card_manager.clear_hostap_card()
            self.hostap = None
            return {'success': False, 'error': 'hostapd failed to start (check AP support and logs)'}

    def stop_hostap(self) -> dict:
        if not self.hostap or not self.hostap.is_active:
            return {'success': False, 'error': 'HostAP not running'}
        self.hostap.stop()
        self.card_manager.clear_hostap_card()
        self.hostap = None
        self.status['hostap_active'] = False
        self.status['hostap_ssid'] = None
        return {'success': True}

    def get_hostap_status(self) -> dict:
        if self.hostap and self.hostap.is_active:
            return self.hostap.get_status()
        return {'is_active': False}

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
        """Save HostAP config to MongoDB."""
        try:
            config = get_config()
            client = MongoClient(
                config.database.mongodb_uri, serverSelectionTimeoutMS=2000
            )
            mdb = client[config.database.db_name]
            mdb['hostap_config'].update_one(
                {'_id': 'hostap'},
                {'$set': {'config': conf}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f'Failed to save hostap config: {e}')

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
            self.suitable_connections.append(result)

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
                connected_bssids = set()
                with self._connections_lock:
                    for conn in self.suitable_connections:
                        if conn.connected:
                            connected_bssids.add(conn.network.bssid)

                for network in networks:
                    if network.bssid in connected_bssids:
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
    try:
        socketio.emit('status_update', wifi_manager.status)
    except Exception as e:
        logger.error(f'Failed to emit status update: {e}')


def emit_scan_update():
    """Emit current scan results to all connected clients."""
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
    try:
        connections_data = []
        with wifi_manager._connections_lock:
            conns_snapshot = list(wifi_manager.suitable_connections)
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
            }
            connections_data.append(conn_dict)
        socketio.emit('connections_update', {'connections': connections_data})
    except Exception as e:
        logger.error(f'Failed to emit connections update: {e}')


@app.route('/')
def index():
    return render_template(
        'index.html',
        status=wifi_manager.status,
        connections=wifi_manager.suitable_connections,
        nearby_networks=wifi_manager.nearby_networks,
    )


@app.route('/config')
def config_page():
    return render_template('config.html')


@app.route('/api/status')
def get_status():
    return jsonify(wifi_manager.status)


@app.route('/api/connections')
def get_connections():
    return jsonify([vars(conn) for conn in wifi_manager.suitable_connections])


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

        # For PipelineModules, include stage info
        stages_info = []
        if hasattr(mod, 'stages'):
            for stage in mod.stages:
                stages_info.append({
                    'name': stage.name,
                    'requires_consent': stage.requires_consent,
                    'config_schema': stage.get_config_schema(),
                })

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
        })
    return jsonify(modules)


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

    if data.get('approved', True):
        success = wifi_manager.consent_manager.approve_ssid(name, bssid, ssid)
    else:
        success = wifi_manager.consent_manager.revoke_ssid(name, bssid)

    return jsonify({'success': success})


@app.route('/api/modules/<name>/consent/ssids')
def get_approved_ssids(name):
    """Get all approved networks for a module."""
    return jsonify(wifi_manager.consent_manager.get_approved_ssids(name))


@app.route('/api/modules/consent')
def get_all_consent():
    return jsonify(wifi_manager.consent_manager.get_all())


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


@app.route('/api/use_connection/<int:index>', methods=['POST'])
def use_connection(index):
    success = wifi_manager.use_connection(index)
    emit_status_update()
    return jsonify({'success': success})


@app.route('/api/stop_connection', methods=['POST'])
def stop_connection():
    wifi_manager.stop_current_connection()
    emit_status_update()
    return jsonify({'success': True})


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


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info('Client connected to websocket')
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


@app.route('/api/mac_assignments', methods=['DELETE'])
def clear_mac_assignments():
    """Clear all stored MAC-to-network mappings."""
    try:
        result = wifi_manager.card_manager.mac_manager.collection.delete_many({})
        wifi_manager.card_manager.mac_manager._cache.clear()
        return jsonify({'status': 'cleared', 'count': result.deleted_count})
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


def main():
    # Initialize the app (creates WifiManager, connects to MongoDB)
    _init_app()

    config = get_config()
    logger.info('Vasili starting with configuration loaded')

    # Start scanning in a separate thread
    scan_thread = threading.Thread(target=wifi_manager.scan_and_connect)
    scan_thread.start()

    # Start web interface if enabled
    if config.web.enabled:
        logger.info(f'Starting web interface on {config.web.host}:{config.web.port}')
        socketio.run(app, host=config.web.host, port=config.web.port, allow_unsafe_werkzeug=True)
    else:
        logger.info('Web interface disabled, running in headless mode')
        # Keep the main thread alive
        try:
            scan_thread.join()
        except KeyboardInterrupt:
            logger.info('Shutting down...')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# Main application entry point
# Modules are loaded dynamically from the modules directory

import collections
import importlib
import inspect
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
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


class WifiCard:
    def __init__(self, interface_name: str):
        """Initialize a wifi card with the given interface name"""
        self.interface = interface_name
        self.in_use = False
        self._connected_network: Optional[WifiNetwork] = None
        self._connection_password: Optional[str] = None
        self._routing_info: Optional[dict] = None

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
        """Check if the interface is currently up"""
        try:
            with open(f'/sys/class/net/{self.interface}/operstate', 'r') as f:
                state = f.read().strip()
                return state == 'up'
        except Exception:
            return False

    def get_ip_address(self) -> Optional[str]:
        """Get the IPv4 address assigned to this interface."""
        return network_isolation.get_interface_ip(self.interface)

    def get_gateway(self) -> Optional[str]:
        """Get the default gateway for this interface."""
        return network_isolation.get_interface_gateway(self.interface)

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


class WifiCardManager:
    def __init__(self):
        self.cards: list[WifiCard] = []
        self._lock = threading.Lock()
        self.initialization_errors: list[str] = []
        self._scanning_card: Optional[WifiCard] = None

        # Initialize MongoDB-backed lease store
        config = get_config()
        self.lease_store = CardLeaseStore(
            mongo_uri=config.database.mongodb_uri,
            db_name=config.database.db_name,
        )
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
                    card = WifiCard(interface)
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
                    if not card.in_use:
                        if self.lease_store.acquire(
                            card.interface, holder, role='connection'
                        ):
                            card.in_use = True
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
                card.in_use = False
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
            return [card for card in self.cards if card != self._scanning_card]

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
                'connection_cards': [c.interface for c in self.cards if c != self._scanning_card],
                'active_leases': self.lease_store.get_all_leases(),
            }


class NetworkScanner:
    def __init__(self, card_manager):
        self.card_manager = card_manager
        self.scan_results: list[WifiNetwork] = []
        self.scanning = False
        self.scan_thread = None
        self.scan_queue = queue.Queue()

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
    def __init__(self, card_manager):
        self.card_manager = card_manager

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


class WifiManager:
    def __init__(self):
        self.card_manager = WifiCardManager()
        self.scanner = NetworkScanner(self.card_manager)
        self.connection_monitor = ConnectionMonitor()
        self.modules = self._load_connection_modules()
        self.suitable_connections: list[ConnectionResult] = []
        self.nearby_networks: list[WifiNetwork] = []
        config = get_config()
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

        # Activity log for power-user UI — last 100 events
        self.activity_log: collections.deque = collections.deque(maxlen=100)

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
                            and obj.__name__ != 'ConnectionModule'
                        ):
                            # Pass mongodb_uri to modules that accept it
                            sig = inspect.signature(obj.__init__)
                            if 'mongodb_uri' in sig.parameters:
                                modules.append(obj(
                                    self.card_manager,
                                    mongodb_uri=config.database.mongodb_uri,
                                ))
                            else:
                                modules.append(obj(self.card_manager))
                            logger.info(f'Loaded module: {module_name}')
                except Exception as e:
                    logger.error(f'Failed to load module {module_name}: {e}')

        return modules

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
            return True

        return False

    def stop_current_connection(self):
        if self.active_bridge:
            self.active_bridge.stop()
            self.status['current_bridge'] = None

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

    def _log_activity(self, event_type: str, **kwargs):
        """Log an activity event for the power-user UI."""
        entry = {
            'type': event_type,
            'timestamp': time.time(),
            **kwargs,
        }
        self.activity_log.append(entry)
        try:
            emit_activity_update(entry)
        except Exception:
            pass

    def scan_and_connect(self):
        """
        Main loop that scans for networks and attempts connections via modules.

        This method runs continuously in a background thread. It:
        1. Starts the network scanner and connection monitor
        2. Waits for scan results
        3. For each discovered network, checks which modules can connect
        4. Attempts connections and stores successful results
        5. Adds connected cards to the connection monitor for auto-reconnect
        """
        logger.info('Starting scan_and_connect loop')
        self.status['scanning'] = True
        self.status['monitoring'] = True
        self.status['auto_selection_running'] = True
        emit_status_update()

        # Start the background scanner, connection monitor, auto-selector, and bandwidth monitor
        self.scanner.start_scan()
        self.connection_monitor.start()
        self.auto_selector.start()
        self.bandwidth_monitor.start()

        try:
            while True:
                # Wait for scan results
                try:
                    networks = self.scanner.get_next_scan()
                    logger.info(f'Scan found {len(networks)} networks')
                except Exception as e:
                    logger.error(f'Error getting scan results: {e}')
                    time.sleep(5)
                    continue

                # Store nearby networks for UI display
                self.nearby_networks = sorted(
                    networks, key=lambda n: n.signal_strength, reverse=True
                )
                self.status['networks_found'] = len(networks)

                # Update status with cards in use
                self.status['cards_in_use'] = sum(
                    1 for card in self.card_manager.get_all_cards() if card.in_use
                )
                self.status['active_modules'] = len(self.modules)
                emit_status_update()
                emit_scan_update()

                # Try to connect to each network using available modules
                for network in networks:
                    # Skip networks we've already successfully connected to
                    already_connected = any(
                        conn.network.bssid == network.bssid and conn.connected
                        for conn in self.suitable_connections
                    )
                    if already_connected:
                        continue

                    # Check if any connection cards are available
                    available_cards = self.card_manager.get_available_count()
                    # Subtract 1 for scanning card reservation
                    if available_cards <= 1:
                        logger.debug(
                            f'No connection cards available, skipping {network.ssid}'
                        )
                        break

                    # Find modules that can connect to this network
                    matched_module = False
                    for module in self.modules:
                        try:
                            if module.can_connect(network):
                                matched_module = True
                                module_name = module.__class__.__name__
                                logger.info(
                                    f'Module {module_name} attempting '
                                    f'connection to {network.ssid}'
                                )
                                self._log_activity(
                                    'attempt',
                                    ssid=network.ssid,
                                    bssid=network.bssid,
                                    module=module_name,
                                    encryption=network.encryption_type,
                                    signal=network.signal_strength,
                                )
                                result = module.connect(network)

                                if result.connected:
                                    score = result.calculate_score()
                                    logger.info(
                                        f'Successfully connected to {network.ssid} '
                                        f'via {module_name} (score: {score})'
                                    )
                                    self._log_activity(
                                        'connected',
                                        ssid=network.ssid,
                                        module=module_name,
                                        interface=result.interface,
                                        score=round(score, 1),
                                        download=round(result.download_speed, 1),
                                        upload=round(result.upload_speed, 1),
                                        ping=round(result.ping, 1),
                                    )
                                    self.suitable_connections.append(result)

                                    # Store metrics to MongoDB (scoring system)
                                    self.metrics_store.store_metrics(result)

                                    # Store to connection persistence (P3)
                                    score = result.calculate_score()
                                    self.connection_store.store_network(
                                        ssid=network.ssid,
                                        bssid=network.bssid,
                                        encryption_type=network.encryption_type,
                                        score=score,
                                        download_speed=result.download_speed,
                                        upload_speed=result.upload_speed,
                                        ping=result.ping,
                                        success=True,
                                    )

                                    # Send notification (P3)
                                    self.notification_manager.connection_established(
                                        ssid=network.ssid,
                                        interface=result.interface,
                                        score=score,
                                    )

                                    # Store connection history (history tracking)
                                    speed_data = {
                                        'download': result.download_speed,
                                        'upload': result.upload_speed,
                                        'ping': result.ping,
                                    }
                                    store_connection_history(
                                        network, True, speed_data, result.interface
                                    )

                                    # Add the connected card to monitoring for auto-reconnect
                                    card = self._get_card_for_interface(result.interface)
                                    if card:
                                        self.connection_monitor.add_card(card)

                                    # Emit update to all connected clients
                                    emit_connections_update()

                                    # Only need one successful connection per network
                                    break
                                else:
                                    logger.warning(
                                        f'Module {module_name} failed to '
                                        f'connect to {network.ssid}'
                                    )
                                    self._log_activity(
                                        'failed',
                                        ssid=network.ssid,
                                        module=module_name,
                                        reason='connection_failed',
                                    )
                                    # Store failed connection attempt
                                    store_connection_history(network, False)
                        except Exception as e:
                            logger.error(
                                f'Error with module {module.__class__.__name__} '
                                f'on network {network.ssid}: {e}'
                            )
                            self._log_activity(
                                'error',
                                ssid=network.ssid,
                                module=module.__class__.__name__,
                                reason=str(e)[:100],
                            )

                    if not matched_module:
                        logger.debug(
                            f'No module can handle {network.ssid} '
                            f'(encryption={network.encryption_type}, open={network.is_open})'
                        )

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
    network: WifiNetwork, success: bool, speed_test: dict = None, interface: str = None
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
        }

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
        for conn in wifi_manager.suitable_connections:
            conn_dict = {
                'network': {
                    'ssid': conn.network.ssid,
                    'bssid': conn.network.bssid,
                    'signal_strength': conn.network.signal_strength,
                    'channel': conn.network.channel,
                    'encryption_type': conn.network.encryption_type,
                    'is_open': conn.network.is_open,
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
        }
        for net in wifi_manager.nearby_networks
    ])


@app.route('/api/cards')
def get_cards():
    """Detailed per-card status for power-user UI."""
    cards = []
    for card in wifi_manager.card_manager.get_all_cards():
        card_info = {
            'interface': card.interface,
            'in_use': card.in_use,
            'is_up': card._is_interface_up(),
            'role': 'scanning' if card == wifi_manager.card_manager.get_scanning_card() else 'connection',
            'connected_network': None,
            'ip_address': card.get_ip_address(),
            'gateway': None,
            'routing_table': None,
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
        cards.append(card_info)
    return jsonify(cards)


@app.route('/api/activity')
def get_activity():
    """Recent activity log."""
    return jsonify(list(wifi_manager.activity_log))


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

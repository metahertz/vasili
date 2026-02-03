#!/usr/bin/env python3
# Main application entry point
# Modules are loaded dynamically from the modules directory

import importlib
import inspect
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import iptc
import netifaces
import speedtest
import wifi
from flask import Flask, jsonify, render_template, request
from pyarchops_dnsmasq import dnsmasq

from config import VasiliConfig, apply_logging_config, load_config
from logging_config import setup_logging, get_logger
from card_state_manager import CardStateManager, CardRole, CardState

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

            # Clear existing iptables rules (with error checking)
            result = subprocess.run(['iptables', '-F'], capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(f'iptables flush failed: {result.stderr}')

            result = subprocess.run(['iptables', '-t', 'nat', '-F'], capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(f'iptables NAT flush failed: {result.stderr}')

            # Set up NAT
            result = subprocess.run(
                [
                    'iptables',
                    '-t',
                    'nat',
                    '-A',
                    'POSTROUTING',
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

            # Allow forwarding
            result = subprocess.run(
                [
                    'iptables',
                    '-A',
                    'FORWARD',
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
                    'FORWARD',
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
            subprocess.run(['iptables', '-F'], capture_output=True)
            subprocess.run(['iptables', '-t', 'nat', '-F'], capture_output=True)
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

        # Verify the interface exists and is a wireless device
        try:
            subprocess.run(['iwconfig', self.interface], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise ValueError(f'Interface {interface_name} is not a valid wireless device')

    def scan(self) -> list[WifiNetwork]:
        """Scan for available networks using this card"""
        try:
            # Put interface in scanning mode
            subprocess.run(['ip', 'link', 'set', self.interface, 'up'], check=True)

            # Run iwlist scan
            result = subprocess.run(
                ['iwlist', self.interface, 'scan'], capture_output=True, text=True, check=True
            )

            networks = []
            current_network = None

            # Parse iwlist output
            for line in result.stdout.split('\n'):
                line = line.strip()

                if 'Cell' in line:
                    if current_network:
                        networks.append(current_network)
                    current_network = WifiNetwork(
                        ssid='',
                        bssid='',
                        signal_strength=0,
                        channel=0,
                        encryption_type='',
                        is_open=True,
                    )
                    current_network.bssid = line.split('Address: ')[1]

                elif current_network:
                    if 'ESSID:' in line:
                        current_network.ssid = line.split('ESSID:')[1].strip('"')
                    elif 'Channel:' in line:
                        current_network.channel = int(line.split(':')[1])
                    elif 'Quality=' in line or 'Signal level=' in line:
                        # Handle both quality format (X/100) and dBm format
                        try:
                            if 'Quality=' in line:
                                # Format: "Quality=51/100  Signal level=-59 dBm"
                                quality_str = line.split('Quality=')[1].split()[0]
                                if '/' in quality_str:
                                    numerator, denominator = quality_str.split('/')
                                    current_network.signal_strength = int(
                                        (int(numerator) / int(denominator)) * 100
                                    )
                                else:
                                    current_network.signal_strength = int(quality_str)
                            elif 'Signal level=' in line:
                                # Format: "Signal level=-59 dBm" or "Signal level=51/100"
                                signal_str = line.split('Signal level=')[1].split()[0]
                                if '/' in signal_str:
                                    # Quality format
                                    numerator, denominator = signal_str.split('/')
                                    current_network.signal_strength = int(
                                        (int(numerator) / int(denominator)) * 100
                                    )
                                else:
                                    # dBm format - convert to percentage
                                    dbm = int(signal_str)
                                    current_network.signal_strength = min(
                                        100, max(0, (dbm + 100) * 2)
                                    )
                        except (ValueError, IndexError) as e:
                            logger.warning(
                                f'Failed to parse signal strength from line: {line} - {e}'
                            )
                            current_network.signal_strength = 0
                    elif 'Encryption key:' in line:
                        current_network.is_open = 'off' in line.lower()
                    elif 'IE: IEEE 802.11i/WPA2' in line:
                        current_network.encryption_type = 'WPA2'
                    elif 'IE: WPA Version' in line:
                        current_network.encryption_type = 'WPA'

            # Add the last network if exists
            if current_network:
                networks.append(current_network)

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


class WifiCardManager:
    def __init__(self):
        self.cards: list[WifiCard] = []
        self._lock = threading.Lock()
        self.initialization_errors: list[str] = []
        self._card_state_manager: Optional[CardStateManager] = None
        self._scanning_card: Optional[WifiCard] = None
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
                if interface.startswith(('wlan', 'wifi')):
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

            # Initialize card state manager and assign roles
            self._card_state_manager = CardStateManager(config)
            interface_list = [card.interface for card in self.cards]
            roles = self._card_state_manager.assign_roles(interface_list)

            # Identify the scanning card
            scanning_interface = self._card_state_manager.get_scanning_interface()
            for card in self.cards:
                if card.interface == scanning_interface:
                    self._scanning_card = card
                    logger.info(f'Designated {card.interface} as dedicated scanning card')
                    break

            return len(self.cards)

    def lease_card(self, for_scanning: bool = False) -> Optional[WifiCard]:
        """
        Get an available wifi card and mark it as in use.

        Args:
            for_scanning: If True, returns the dedicated scanning card.
                         If False, returns an available connection card only.

        Returns:
            Available WifiCard or None if no cards available
        """
        with self._lock:
            if for_scanning:
                # Return the dedicated scanning card if available
                if self._scanning_card and not self._scanning_card.in_use:
                    self._scanning_card.in_use = True
                    if self._card_state_manager:
                        self._card_state_manager.update_card_state(
                            self._scanning_card.interface, CardState.SCANNING, in_use=True
                        )
                    return self._scanning_card
                return None
            else:
                # Return an available connection card (not the scanning card)
                for card in self.cards:
                    if card == self._scanning_card:
                        # Skip the scanning card - it's reserved for scanning only
                        continue
                    if not card.in_use:
                        card.in_use = True
                        if self._card_state_manager:
                            self._card_state_manager.update_card_state(
                                card.interface, CardState.CONNECTING, in_use=True
                            )
                        return card
                return None

    def get_card(self) -> Optional[WifiCard]:
        """Alias for lease_card() for backwards compatibility with modules."""
        return self.lease_card()

    def return_card(self, card: WifiCard):
        """Return a card to the pool of available cards."""
        with self._lock:
            if card in self.cards:
                card.in_use = False
                if self._card_state_manager:
                    # Determine the appropriate state based on connection status
                    state = CardState.CONNECTED if card.is_connected() else CardState.IDLE
                    connected_ssid = card.get_connected_ssid() if card.is_connected() else None
                    self._card_state_manager.update_card_state(
                        card.interface, state, in_use=False, connected_ssid=connected_ssid
                    )

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
            status = {
                'total_cards': len(self.cards),
                'available_cards': sum(1 for c in self.cards if not c.in_use),
                'in_use_cards': sum(1 for c in self.cards if c.in_use),
                'card_interfaces': [c.interface for c in self.cards],
                'initialization_errors': self.initialization_errors,
                'scanning_card': self._scanning_card.interface if self._scanning_card else None,
                'connection_cards': [c.interface for c in self.cards if c != self._scanning_card],
            }

            # Add card state manager status if available
            if self._card_state_manager:
                status['card_state_manager'] = self._card_state_manager.get_status()

            return status


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
                    logger.error('Scanning card not available')
                    time.sleep(1)
                    continue

                logger.debug(f'Starting scan on dedicated scanning card: {card.interface}')

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


class ConnectionModule:
    # Base class for connection modules
    def __init__(self, card_manager):
        self.card_manager = card_manager

    def can_connect(self, network: WifiNetwork) -> bool:
        raise NotImplementedError()

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        raise NotImplementedError()


class WifiManager:
    def __init__(self):
        self.card_manager = WifiCardManager()
        self.scanner = NetworkScanner(self.card_manager)
        self.connection_monitor = ConnectionMonitor()
        self.modules = self._load_connection_modules()
        self.suitable_connections: list[ConnectionResult] = []
        self.status = {
            'scanning': False,
            'monitoring': False,
            'cards_in_use': 0,
            'active_modules': 0,
            'current_bridge': None,
            'reconnect_events': 0,
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
                            and issubclass(obj, ConnectionModule)
                            and obj != ConnectionModule
                        ):
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

        # Start the background scanner and connection monitor
        self.scanner.start_scan()
        self.connection_monitor.start()

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

                # Update status with cards in use
                self.status['cards_in_use'] = sum(
                    1 for card in self.card_manager.get_all_cards() if card.in_use
                )
                self.status['active_modules'] = len(self.modules)

                # Try to connect to each network using available modules
                for network in networks:
                    # Skip networks we've already successfully connected to
                    already_connected = any(
                        conn.network.bssid == network.bssid and conn.connected
                        for conn in self.suitable_connections
                    )
                    if already_connected:
                        continue

                    # Find modules that can connect to this network
                    for module in self.modules:
                        try:
                            if module.can_connect(network):
                                logger.info(
                                    f'Module {module.__class__.__name__} attempting '
                                    f'connection to {network.ssid}'
                                )
                                result = module.connect(network)

                                if result.connected:
                                    logger.info(
                                        f'Successfully connected to {network.ssid} '
                                        f'via {module.__class__.__name__}'
                                    )
                                    self.suitable_connections.append(result)

                                    # Add the connected card to monitoring for auto-reconnect
                                    card = self._get_card_for_interface(result.interface)
                                    if card:
                                        self.connection_monitor.add_card(card)

                                    # Only need one successful connection per network
                                    break
                                else:
                                    logger.warning(
                                        f'Module {module.__class__.__name__} failed to '
                                        f'connect to {network.ssid}'
                                    )
                        except Exception as e:
                            logger.error(
                                f'Error with module {module.__class__.__name__} '
                                f'on network {network.ssid}: {e}'
                            )

        except Exception as e:
            logger.error(f'scan_and_connect loop error: {e}')
        finally:
            self.scanner.stop_scan()
            self.connection_monitor.stop()
            self.status['scanning'] = False
            self.status['monitoring'] = False
            logger.info('scan_and_connect loop stopped')


# Flask web interface
app = Flask(__name__)
wifi_manager = WifiManager()


@app.route('/')
def index():
    return render_template(
        'index.html', status=wifi_manager.status, connections=wifi_manager.suitable_connections
    )


@app.route('/api/status')
def get_status():
    return jsonify(wifi_manager.status)


@app.route('/api/connections')
def get_connections():
    return jsonify([vars(conn) for conn in wifi_manager.suitable_connections])


@app.route('/api/use_connection/<int:index>', methods=['POST'])
def use_connection(index):
    success = wifi_manager.use_connection(index)
    return jsonify({'success': success})


@app.route('/api/stop_connection', methods=['POST'])
def stop_connection():
    wifi_manager.stop_current_connection()
    return jsonify({'success': True})


def main():
    # Load configuration
    config = get_config()
    logger.info('Vasili starting with configuration loaded')

    # Start scanning in a separate thread
    scan_thread = threading.Thread(target=wifi_manager.scan_and_connect)
    scan_thread.start()

    # Start web interface if enabled
    if config.web.enabled:
        logger.info(f'Starting web interface on {config.web.host}:{config.web.port}')
        app.run(host=config.web.host, port=config.web.port)
    else:
        logger.info('Web interface disabled, running in headless mode')
        # Keep the main thread alive
        try:
            scan_thread.join()
        except KeyboardInterrupt:
            logger.info('Shutting down...')


if __name__ == '__main__':
    main()

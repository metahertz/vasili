#!/usr/bin/env python3
# Main application entry point
# Modules are loaded dynamically from the modules directory

import subprocess
import wifi
import threading
import queue
import time
import speedtest
import netifaces
import os
import importlib
import inspect
from flask import Flask, render_template, jsonify, request
from dataclasses import dataclass
from typing import List, Dict, Optional
import logging
from pyarchops_dnsmasq import dnsmasq
import iptc

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

    def setup_nat(self):
        try:
            # Enable IP forwarding
            with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
                f.write('1')

            # Clear existing iptables rules
            subprocess.run(['iptables', '-F'])
            subprocess.run(['iptables', '-t', 'nat', '-F'])

            # Set up NAT
            subprocess.run([
                'iptables', '-t', 'nat', '-A', 'POSTROUTING',
                '-o', self.wifi_interface,
                '-j', 'MASQUERADE'
            ])

            # Allow forwarding
            subprocess.run([
                'iptables', '-A', 'FORWARD',
                '-i', self.ethernet_interface,
                '-o', self.wifi_interface,
                '-j', 'ACCEPT'
            ])
            subprocess.run([
                'iptables', '-A', 'FORWARD',
                '-i', self.wifi_interface,
                '-o', self.ethernet_interface,
                '-m', 'state', '--state', 'RELATED,ESTABLISHED',
                '-j', 'ACCEPT'
            ])

            return True
        except Exception as e:
            logger.error(f"Failed to set up NAT: {e}")
            return False

    def setup_dhcp(self):
        try:
            # Configure ethernet interface with static IP
            subprocess.run([
                'ip', 'addr', 'add',
                '192.168.10.1/24',
                'dev', self.ethernet_interface
            ])
            subprocess.run(['ip', 'link', 'set', self.ethernet_interface, 'up'])

            # Start DHCP server
            self.dhcp_server = dnsmasq.DHCP(
                interface=self.ethernet_interface,
                dhcp_range=('192.168.10.50', '192.168.10.150'),
                subnet_mask='255.255.255.0'
            )
            self.dhcp_server.start()
            return True
        except Exception as e:
            logger.error(f"Failed to set up DHCP: {e}")
            return False

    def start(self) -> bool:
        if self.setup_nat() and self.setup_dhcp():
            self.is_active = True
            return True
        return False

    def stop(self):
        if self.dhcp_server:
            self.dhcp_server.stop()
        
        # Disable IP forwarding
        with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
            f.write('0')
        
        # Clear iptables rules
        subprocess.run(['iptables', '-F'])
        subprocess.run(['iptables', '-t', 'nat', '-F'])
        
        self.is_active = False

class WifiCard:
    def __init__(self, interface_name: str):
        """Initialize a wifi card with the given interface name"""
        self.interface = interface_name
        self.in_use = False
        
        # Verify the interface exists and is a wireless device
        try:
            subprocess.run(['iwconfig', self.interface], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Interface {interface_name} is not a valid wireless device")

    def scan(self) -> List[WifiNetwork]:
        """Scan for available networks using this card"""
        try:
            # Put interface in scanning mode
            subprocess.run(['ip', 'link', 'set', self.interface, 'up'], check=True)
            
            # Run iwlist scan
            result = subprocess.run(
                ['iwlist', self.interface, 'scan'],
                capture_output=True,
                text=True,
                check=True
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
                        is_open=True
                    )
                    current_network.bssid = line.split('Address: ')[1]
                
                elif current_network:
                    if 'ESSID:' in line:
                        current_network.ssid = line.split('ESSID:')[1].strip('"')
                    elif 'Channel:' in line:
                        current_network.channel = int(line.split(':')[1])
                    elif 'Signal level=' in line:
                        # Convert dBm to percentage (approximate)
                        dbm = int(line.split('Signal level=')[1].split(' ')[0])
                        current_network.signal_strength = min(100, max(0, (dbm + 100) * 2))
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
            logger.error(f"Scan failed on interface {self.interface}: {e}")
            return []

    def get_status(self) -> Dict:
        """Get current status of the wifi card"""
        return {
            'interface': self.interface,
            'in_use': self.in_use,
            'is_up': self._is_interface_up()
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
        self.cards = []
        self.scan_for_cards()

    def scan_for_cards(self):
        """Scan for available wifi cards and add them to the list"""
        # Clear existing cards
        self.cards = []
        
        # Get list of network interfaces
        interfaces = netifaces.interfaces()
        
        # Find wifi interfaces
        for interface in interfaces:
            if interface.startswith(('wlan', 'wifi')):
                try:
                    card = WifiCard(interface)
                    self.cards.append(card)
                except Exception as e:
                    logger.error(f"Failed to initialize wifi card {interface}: {e}")

    def lease_card(self) -> Optional[WifiCard]:
        """Get an available wifi card and mark it as in use"""
        for card in self.cards:
            if not card.in_use:
                card.in_use = True
                return card
        return None

    def return_card(self, card: WifiCard):
        """Return a card to the pool of available cards"""
        if card in self.cards:
            card.in_use = False

    def get_all_cards(self) -> List[WifiCard]:
        """Get list of all wifi cards"""
        return self.cards

class NetworkScanner:
    def __init__(self, card_manager):
        self.card_manager = card_manager
        self.scan_results: List[WifiNetwork] = []
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
        """Background worker that continuously scans for networks"""
        while self.scanning:
            try:
                # Get an available wifi card
                card = self.card_manager.lease_card()
                if not card:
                    logger.error("No wifi cards available for scanning")
                    time.sleep(1)
                    continue

                # Scan for networks
                networks = card.scan()
                self.scan_results = networks
                self.scan_queue.put(networks)

                # Return the card
                self.card_manager.return_card(card)

                # Wait before scanning again
                time.sleep(5)

            except Exception as e:
                logger.error(f"Error during network scan: {e}")
                time.sleep(1)

    def get_scan_results(self) -> List[WifiNetwork]:
        """Get the most recent scan results"""
        return self.scan_results

    def get_next_scan(self) -> List[WifiNetwork]:
        """Wait for and return the next scan results"""
        return self.scan_queue.get()

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
        self.modules = self._load_connection_modules()
        self.suitable_connections: List[ConnectionResult] = []
        self.status = {
            'scanning': False,
            'cards_in_use': 0,
            'active_modules': 0,
            'current_bridge': None
        }
        self.active_bridge = None

    def _load_connection_modules(self) -> List[ConnectionModule]:
        modules_dir = os.path.join(os.path.dirname(__file__), 'modules')
        modules = []

        # Create modules directory if it doesn't exist
        if not os.path.exists(modules_dir):
            os.makedirs(modules_dir)

        # Import all modules from the modules directory
        for filename in os.listdir(modules_dir):
            if filename.endswith('.py'):
                module_name = filename[:-3]
                try:
                    module = importlib.import_module(f'modules.{module_name}')
                    # Find all ConnectionModule subclasses in the module
                    for name, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and 
                            issubclass(obj, ConnectionModule) and 
                            obj != ConnectionModule):
                            modules.append(obj(self.card_manager))
                except Exception as e:
                    logger.error(f"Failed to load module {module_name}: {e}")

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
            iface for iface in netifaces.interfaces()
            if iface.startswith('eth') or iface.startswith('enp')
        ]
        
        if not ethernet_interfaces:
            logger.error("No ethernet interfaces available")
            return False

        # Create and start new bridge
        self.active_bridge = NetworkBridge(
            wifi_interface=connection.interface,
            ethernet_interface=ethernet_interfaces[0]
        )
        
        if self.active_bridge.start():
            self.status['current_bridge'] = {
                'wifi_interface': connection.interface,
                'ethernet_interface': ethernet_interfaces[0],
                'ssid': connection.network.ssid
            }
            return True
        
        return False

    def stop_current_connection(self):
        if self.active_bridge:
            self.active_bridge.stop()
            self.status['current_bridge'] = None

    # ... [Rest of WifiManager methods remain the same] ...

# Flask web interface
app = Flask(__name__)
wifi_manager = WifiManager()

@app.route('/')
def index():
    return render_template('index.html',
                         status=wifi_manager.status,
                         connections=wifi_manager.suitable_connections)

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
    # Start scanning in a separate thread
    scan_thread = threading.Thread(target=wifi_manager.scan_and_connect)
    scan_thread.start()
    
    # Start web interface
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    main()

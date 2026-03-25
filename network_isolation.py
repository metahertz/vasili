"""Network isolation via policy routing.

Ensures each WiFi card's traffic uses only that card's network path,
preventing contamination from other interfaces (e.g. eth0 with a default gateway).

Uses Linux policy routing (ip rule + per-interface routing tables) to create
separate routing paths per WiFi interface. Traffic sourced from a WiFi IP
is directed to a dedicated routing table with a default route via that
WiFi interface's gateway.
"""

import subprocess
import threading
from subprocess import TimeoutExpired
from typing import Optional

import netifaces

from logging_config import get_logger

logger = get_logger('network_isolation')

# Routing table numbers: 100+ range, one per interface
_TABLE_BASE = 100
_table_map: dict[str, int] = {}
_next_table = _TABLE_BASE
_table_lock = threading.Lock()


def _get_table_for_interface(interface: str) -> int:
    """Get or assign a routing table number for an interface (thread-safe)."""
    global _next_table
    with _table_lock:
        if interface not in _table_map:
            _table_map[interface] = _next_table
            _next_table += 1
        return _table_map[interface]


def get_interface_ip(interface: str) -> Optional[str]:
    """Get the IPv4 address assigned to a network interface.

    Args:
        interface: Network interface name (e.g. 'wlan0')

    Returns:
        IPv4 address string or None if no address assigned
    """
    try:
        addrs = netifaces.ifaddresses(interface)
        ipv4_list = addrs.get(netifaces.AF_INET, [])
        if ipv4_list:
            return ipv4_list[0]['addr']
    except (ValueError, KeyError, OSError) as e:
        logger.debug(f'Could not get IP for {interface}: {e}')
    return None


def get_interface_gateway(interface: str) -> Optional[str]:
    """Get the default gateway for an interface from NetworkManager.

    Args:
        interface: Network interface name

    Returns:
        Gateway IP string or None if not available
    """
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'IP4.GATEWAY', 'device', 'show', interface],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split('\n'):
            if line.startswith('IP4.GATEWAY:'):
                gw = line.split(':', 1)[1].strip()
                if gw and gw != '--' and gw != '':
                    return gw
    except Exception as e:
        logger.debug(f'Could not get gateway for {interface}: {e}')
    return None


def setup_interface_routing(interface: str) -> Optional[dict]:
    """Set up policy routing for a WiFi interface.

    Creates a dedicated routing table with a default route via the interface's
    gateway, and an ip rule to direct traffic from the interface's IP to that table.

    Args:
        interface: Network interface name

    Returns:
        Dict with routing info on success, None on failure.
        Dict keys: 'ip', 'gateway', 'table', 'interface'
    """
    ip = get_interface_ip(interface)
    if not ip:
        logger.warning(f'Cannot set up routing for {interface}: no IP address')
        return None

    gateway = get_interface_gateway(interface)
    if not gateway:
        logger.warning(f'Cannot set up routing for {interface}: no gateway')
        return None

    table = _get_table_for_interface(interface)

    # Idempotent cleanup before adding
    subprocess.run(
        ['ip', 'route', 'flush', 'table', str(table)],
        capture_output=True, check=False,
    )
    subprocess.run(
        ['ip', 'rule', 'del', 'from', ip, 'lookup', str(table)],
        capture_output=True, check=False,
    )

    # Add default route in the per-interface table
    result = subprocess.run(
        ['ip', 'route', 'add', 'default', 'via', gateway, 'dev', interface,
         'table', str(table)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(
            f'Failed to add route for {interface} table {table}: {result.stderr}'
        )
        return None

    # Add ip rule to direct traffic from this IP to the interface's table
    result = subprocess.run(
        ['ip', 'rule', 'add', 'from', ip, 'lookup', str(table), 'priority', '100'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(f'Failed to add rule for {ip} table {table}: {result.stderr}')
        # Clean up the route we just added
        subprocess.run(
            ['ip', 'route', 'flush', 'table', str(table)],
            capture_output=True, check=False,
        )
        return None

    routing_info = {
        'ip': ip,
        'gateway': gateway,
        'table': table,
        'interface': interface,
    }
    logger.info(
        f'Routing isolation set up for {interface}: '
        f'{ip} -> table {table} via {gateway}'
    )
    return routing_info


def teardown_interface_routing(interface: str, routing_info: dict):
    """Remove policy routing for a WiFi interface.

    Args:
        interface: Network interface name
        routing_info: Dict returned by setup_interface_routing
    """
    if not routing_info:
        return

    ip = routing_info.get('ip')
    table = routing_info.get('table')

    if ip and table:
        subprocess.run(
            ['ip', 'rule', 'del', 'from', ip, 'lookup', str(table)],
            capture_output=True, check=False,
        )

    if table:
        subprocess.run(
            ['ip', 'route', 'flush', 'table', str(table)],
            capture_output=True, check=False,
        )

    logger.info(f'Routing isolation torn down for {interface}')


def verify_connectivity(interface: str) -> bool:
    """Verify actual internet connectivity through a specific interface.

    Uses curl bound to the interface to make an HTTP request to a
    connectivity check endpoint. This ensures we're testing the WiFi
    path, not eth0 or another interface.

    Args:
        interface: Network interface name to test

    Returns:
        True if internet is reachable via this interface
    """
    try:
        result = subprocess.run(
            [
                'curl', '--interface', interface,
                '--connect-timeout', '5',
                '-s', '-o', '/dev/null',
                '-w', '%{http_code}',
                'http://connectivitycheck.gstatic.com/generate_204',
            ],
            capture_output=True, text=True, timeout=10,
        )
        http_code = result.stdout.strip()
        if http_code == '204':
            logger.debug(f'Connectivity verified on {interface}')
            return True
        else:
            logger.debug(
                f'Connectivity check on {interface} returned HTTP {http_code}'
            )
            return False
    except TimeoutExpired:
        logger.debug(f'Connectivity check timed out on {interface}')
        return False
    except Exception as e:
        logger.debug(f'Connectivity check failed on {interface}: {e}')
        return False

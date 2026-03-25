"""MAC address management — generate and persist per-network randomized MACs.

Each network (identified by BSSID) gets a consistent randomized MAC so that
reconnections use the same address. This provides privacy while maintaining
session continuity with captive portals and DHCP leases.

The MAC clone stage in the open network pipeline can override this when
it intentionally clones an authenticated client's MAC.
"""

import random
import re
import subprocess

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('mac_manager')


class MacManager:
    """Generate and persist per-network randomized MAC addresses."""

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili'):
        self._available = False
        self._cache: dict[str, str] = {}  # bssid -> mac

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db['mac_assignments']
            self._available = True
            self.collection.create_index('bssid', unique=True)
            logger.info('MacManager connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for MAC manager: {e}')
        except Exception as e:
            logger.error(f'Failed to initialize MacManager: {e}')

    def get_mac_for_network(self, bssid: str) -> str:
        """Get or generate a randomized MAC for a network.

        Returns the same MAC for the same BSSID across sessions.
        Generates a new one if never seen before.
        """
        bssid_lower = bssid.lower()

        # Check cache
        if bssid_lower in self._cache:
            return self._cache[bssid_lower]

        # Check MongoDB
        if self._available:
            try:
                doc = self.collection.find_one({'bssid': bssid_lower})
                if doc:
                    mac = doc['mac']
                    self._cache[bssid_lower] = mac
                    return mac
            except Exception as e:
                logger.error(f'Failed to look up MAC for {bssid}: {e}')

        # Generate new MAC
        mac = self._generate_random_mac()
        self._cache[bssid_lower] = mac

        # Persist
        if self._available:
            try:
                self.collection.update_one(
                    {'bssid': bssid_lower},
                    {'$set': {'bssid': bssid_lower, 'mac': mac}},
                    upsert=True,
                )
            except Exception as e:
                logger.error(f'Failed to store MAC for {bssid}: {e}')

        return mac

    @staticmethod
    def _generate_random_mac() -> str:
        """Generate a random locally-administered unicast MAC address.

        Sets the locally-administered bit (bit 1 of first octet) and
        clears the multicast bit (bit 0 of first octet).
        """
        octets = [random.randint(0x00, 0xff) for _ in range(6)]
        # Set locally administered bit, clear multicast bit
        octets[0] = (octets[0] | 0x02) & 0xfe
        return ':'.join(f'{b:02x}' for b in octets)

    @staticmethod
    def get_current_mac(interface: str) -> str | None:
        """Read the current MAC address of an interface."""
        try:
            result = subprocess.run(
                ['ip', 'link', 'show', interface],
                capture_output=True, text=True, timeout=5,
            )
            match = re.search(r'link/ether\s+([0-9a-f:]{17})', result.stdout, re.I)
            if match:
                return match.group(1)
        except Exception as e:
            logger.error(f'Failed to get MAC for {interface}: {e}')
        return None

    @staticmethod
    def set_mac(interface: str, mac: str) -> bool:
        """Set the MAC address of an interface (brings it down/up)."""
        try:
            subprocess.run(
                ['ip', 'link', 'set', interface, 'down'],
                check=True, capture_output=True, timeout=5,
            )
            subprocess.run(
                ['ip', 'link', 'set', 'dev', interface, 'address', mac],
                check=True, capture_output=True, timeout=5,
            )
            subprocess.run(
                ['ip', 'link', 'set', interface, 'up'],
                check=True, capture_output=True, timeout=5,
            )
            logger.debug(f'Set {interface} MAC to {mac}')
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f'Failed to set MAC on {interface}: {e}')
            return False

    @staticmethod
    def get_original_mac(interface: str) -> str | None:
        """Read the permanent/hardware MAC from sysfs."""
        try:
            path = f'/sys/class/net/{interface}/address'
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return None

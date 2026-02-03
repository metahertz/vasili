"""
Card State Manager for MongoDB-based card orchestration.

Manages WiFi card roles (scanning vs connection) and state coordination
across the system using MongoDB as a persistent store.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from logging_config import get_logger

logger = get_logger(__name__)

# Try to import pymongo - make it optional for backward compatibility
try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

    MONGODB_AVAILABLE = True
except ImportError:
    MONGODB_AVAILABLE = False
    logger.warning('pymongo not available - MongoDB features will be disabled')


class CardRole(Enum):
    """WiFi card role types."""

    SCANNING = 'scanning'
    CONNECTION = 'connection'
    UNASSIGNED = 'unassigned'


class CardState(Enum):
    """WiFi card operational states."""

    IDLE = 'idle'
    SCANNING = 'scanning'
    CONNECTING = 'connecting'
    CONNECTED = 'connected'
    ERROR = 'error'


@dataclass
class CardInfo:
    """Information about a WiFi card."""

    interface: str
    role: CardRole
    state: CardState
    in_use: bool
    connected_ssid: Optional[str] = None
    last_updated: float = 0.0
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for MongoDB storage."""
        return {
            'interface': self.interface,
            'role': self.role.value,
            'state': self.state.value,
            'in_use': self.in_use,
            'connected_ssid': self.connected_ssid,
            'last_updated': self.last_updated,
            'error_message': self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'CardInfo':
        """Create from dictionary retrieved from MongoDB."""
        return cls(
            interface=data['interface'],
            role=CardRole(data['role']),
            state=CardState(data['state']),
            in_use=data['in_use'],
            connected_ssid=data.get('connected_ssid'),
            last_updated=data.get('last_updated', 0.0),
            error_message=data.get('error_message'),
        )


class CardStateManager:
    """
    Manages WiFi card state coordination using MongoDB.

    Provides role assignment, state tracking, and prevents scanning interference
    by ensuring the scanning card is not used for connections.
    """

    def __init__(self, config):
        """
        Initialize the card state manager.

        Args:
            config: VasiliConfig instance with MongoDB settings
        """
        self.config = config
        self.mongodb_enabled = config.mongodb.enabled and MONGODB_AVAILABLE
        self._client: Optional[MongoClient] = None
        self._db = None
        self._collection = None
        self._scanning_interface: Optional[str] = None

        if self.mongodb_enabled:
            self._connect_mongodb()
        else:
            logger.info('MongoDB disabled - using in-memory state management')
            self._memory_store: dict[str, CardInfo] = {}

    def _connect_mongodb(self):
        """Connect to MongoDB and set up collections."""
        try:
            connection_string = f'mongodb://{self.config.mongodb.host}:{self.config.mongodb.port}/'
            self._client = MongoClient(
                connection_string, serverSelectionTimeoutMS=5000  # 5 second timeout
            )

            # Test the connection
            self._client.admin.command('ping')

            self._db = self._client[self.config.mongodb.database]
            self._collection = self._db[self.config.mongodb.collection]

            # Create index on interface for fast lookups
            self._collection.create_index('interface', unique=True)

            logger.info(
                f'Connected to MongoDB at {self.config.mongodb.host}:{self.config.mongodb.port}'
            )

        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.warning(f'Failed to connect to MongoDB: {e}. Falling back to in-memory store.')
            self.mongodb_enabled = False
            self._memory_store: dict[str, CardInfo] = {}
        except Exception as e:
            logger.error(f'Unexpected error connecting to MongoDB: {e}. Using in-memory store.')
            self.mongodb_enabled = False
            self._memory_store: dict[str, CardInfo] = {}

    def assign_roles(self, interfaces: list[str]) -> dict[str, CardRole]:
        """
        Assign roles to WiFi cards based on configuration and available interfaces.

        The first interface (or configured scan_interface) becomes the scanning card.
        All other interfaces become connection cards.

        Args:
            interfaces: List of available WiFi interface names

        Returns:
            Dictionary mapping interface names to their assigned roles
        """
        if not interfaces:
            logger.warning('No interfaces provided for role assignment')
            return {}

        roles = {}

        # Determine scanning interface
        if self.config.interfaces.scan_interface in interfaces:
            scan_iface = self.config.interfaces.scan_interface
        else:
            # Use first interface as scanning card
            scan_iface = interfaces[0]

        self._scanning_interface = scan_iface
        roles[scan_iface] = CardRole.SCANNING
        logger.info(f'Assigned SCANNING role to {scan_iface}')

        # Assign CONNECTION role to remaining interfaces
        for iface in interfaces:
            if iface != scan_iface:
                roles[iface] = CardRole.CONNECTION
                logger.info(f'Assigned CONNECTION role to {iface}')

        # Initialize state for all cards
        for iface, role in roles.items():
            card_info = CardInfo(
                interface=iface,
                role=role,
                state=CardState.IDLE,
                in_use=False,
                last_updated=time.time(),
            )
            self._save_card_info(card_info)

        return roles

    def get_scanning_interface(self) -> Optional[str]:
        """Get the interface assigned to scanning role."""
        return self._scanning_interface

    def get_connection_interfaces(self) -> list[str]:
        """Get all interfaces assigned to connection role."""
        if self.mongodb_enabled:
            try:
                cursor = self._collection.find({'role': CardRole.CONNECTION.value})
                return [doc['interface'] for doc in cursor]
            except Exception as e:
                logger.error(f'Error querying connection interfaces: {e}')
                return []
        else:
            return [
                iface
                for iface, info in self._memory_store.items()
                if info.role == CardRole.CONNECTION
            ]

    def update_card_state(
        self,
        interface: str,
        state: CardState,
        in_use: bool = None,
        connected_ssid: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """
        Update the state of a WiFi card.

        Args:
            interface: Interface name
            state: New state
            in_use: Whether the card is currently in use (optional)
            connected_ssid: SSID if connected (optional)
            error_message: Error message if in error state (optional)
        """
        card_info = self.get_card_info(interface)
        if not card_info:
            logger.warning(f'Cannot update state for unknown interface: {interface}')
            return

        card_info.state = state
        if in_use is not None:
            card_info.in_use = in_use
        if connected_ssid is not None:
            card_info.connected_ssid = connected_ssid
        if error_message is not None:
            card_info.error_message = error_message

        card_info.last_updated = time.time()
        self._save_card_info(card_info)

        logger.debug(f'Updated {interface}: state={state.value}, in_use={card_info.in_use}')

    def get_card_info(self, interface: str) -> Optional[CardInfo]:
        """
        Get information about a specific card.

        Args:
            interface: Interface name

        Returns:
            CardInfo if found, None otherwise
        """
        if self.mongodb_enabled:
            try:
                doc = self._collection.find_one({'interface': interface})
                if doc:
                    return CardInfo.from_dict(doc)
            except Exception as e:
                logger.error(f'Error retrieving card info for {interface}: {e}')
        else:
            return self._memory_store.get(interface)

        return None

    def get_all_cards(self) -> list[CardInfo]:
        """Get information about all cards."""
        if self.mongodb_enabled:
            try:
                cursor = self._collection.find()
                return [CardInfo.from_dict(doc) for doc in cursor]
            except Exception as e:
                logger.error(f'Error retrieving all card info: {e}')
                return []
        else:
            return list(self._memory_store.values())

    def get_available_connection_card(self) -> Optional[str]:
        """
        Get an available connection card (not in use).

        Returns:
            Interface name of available connection card, or None if all are busy
        """
        if self.mongodb_enabled:
            try:
                doc = self._collection.find_one(
                    {'role': CardRole.CONNECTION.value, 'in_use': False}
                )
                if doc:
                    return doc['interface']
            except Exception as e:
                logger.error(f'Error finding available connection card: {e}')
        else:
            for iface, info in self._memory_store.items():
                if info.role == CardRole.CONNECTION and not info.in_use:
                    return iface

        return None

    def is_scanning_card(self, interface: str) -> bool:
        """
        Check if an interface is the scanning card.

        Args:
            interface: Interface name

        Returns:
            True if this is the scanning card
        """
        card_info = self.get_card_info(interface)
        return card_info is not None and card_info.role == CardRole.SCANNING

    def _save_card_info(self, card_info: CardInfo):
        """
        Save card info to storage (MongoDB or memory).

        Args:
            card_info: CardInfo to save
        """
        if self.mongodb_enabled:
            try:
                self._collection.replace_one(
                    {'interface': card_info.interface}, card_info.to_dict(), upsert=True
                )
            except Exception as e:
                logger.error(f'Error saving card info to MongoDB: {e}')
        else:
            self._memory_store[card_info.interface] = card_info

    def cleanup(self):
        """Clean up resources (close MongoDB connection)."""
        if self._client:
            try:
                self._client.close()
                logger.info('Closed MongoDB connection')
            except Exception as e:
                logger.error(f'Error closing MongoDB connection: {e}')

    def get_status(self) -> dict:
        """Get status information about card state management."""
        cards = self.get_all_cards()
        return {
            'mongodb_enabled': self.mongodb_enabled,
            'total_cards': len(cards),
            'scanning_interface': self._scanning_interface,
            'connection_cards': len(self.get_connection_interfaces()),
            'cards_in_use': sum(1 for c in cards if c.in_use),
            'cards': [
                {
                    'interface': c.interface,
                    'role': c.role.value,
                    'state': c.state.value,
                    'in_use': c.in_use,
                    'connected_ssid': c.connected_ssid,
                }
                for c in cards
            ],
        }

# Multi-Card Orchestration Implementation

## Overview

This document describes the multi-card orchestration feature that enables Vasili to use one WiFi card exclusively for scanning while other cards are used for testing and establishing connections.

## Architecture

The multi-card orchestration system consists of three main components:

### 1. CardStateManager (`card_state_manager.py`)

A new module that manages WiFi card roles and state coordination using MongoDB (or in-memory storage as fallback).

**Key Features:**
- **Role Assignment**: Assigns one card as the scanning card and others as connection cards
- **State Tracking**: Tracks each card's operational state (idle, scanning, connecting, connected, error)
- **MongoDB Integration**: Persists card state in MongoDB for coordination across system components
- **Fallback Mode**: Automatically falls back to in-memory storage if MongoDB is unavailable

**Card Roles:**
- `SCANNING`: Card dedicated to network scanning operations
- `CONNECTION`: Card available for connection attempts
- `UNASSIGNED`: Card without assigned role

**Card States:**
- `IDLE`: Card is available but not currently in use
- `SCANNING`: Card is actively scanning for networks
- `CONNECTING`: Card is attempting to connect to a network
- `CONNECTED`: Card has successfully connected to a network
- `ERROR`: Card has encountered an error

### 2. Enhanced WifiCardManager (`vasili.py`)

The `WifiCardManager` class has been enhanced to integrate with `CardStateManager`:

**New Methods:**
- `lease_card(for_scanning=False)`: Lease a card for scanning or connection
  - When `for_scanning=True`: Returns the dedicated scanning card
  - When `for_scanning=False`: Returns an available connection card (never the scanning card)
- `get_scanning_card()`: Returns the WiFi card designated for scanning
- `get_connection_cards()`: Returns all cards available for connections

**Key Changes:**
- Role assignment during initialization
- Scanning card is never leased for connection operations
- Card state updates are synchronized with CardStateManager
- Status reporting includes card state information

### 3. Updated NetworkScanner (`vasili.py`)

The `NetworkScanner` has been updated to use the dedicated scanning card:

**Changes:**
- `_scan_worker()` now explicitly requests the scanning card via `lease_card(for_scanning=True)`
- Ensures no interference with connection operations on other cards
- Added debug logging for scan operations

## Configuration

### MongoDB Settings

Add to your `config.yaml`:

```yaml
mongodb:
  enabled: true
  host: "localhost"
  port: 27017
  database: "vasili"
  collection: "card_states"
```

### Scanning Interface Preference

Optionally specify which interface should be the scanning card:

```yaml
interfaces:
  scan_interface: wlan0  # Use wlan0 as dedicated scanning card
```

If not specified, the first available interface is used for scanning.

## Usage

### Automatic Role Assignment

When the system starts, `WifiCardManager` automatically:
1. Detects all available WiFi interfaces
2. Designates one as the scanning card (first interface or configured `scan_interface`)
3. Assigns remaining interfaces as connection cards
4. Initializes state tracking in MongoDB

### Example: 3 WiFi Cards

With interfaces `wlan0`, `wlan1`, `wlan2`:
- `wlan0`: Dedicated scanning card (used by NetworkScanner)
- `wlan1`: Connection card (available to modules)
- `wlan2`: Connection card (available to modules)

### Example: 1 WiFi Card

With a single interface `wlan0`:
- `wlan0`: Dedicated scanning card (cannot be used for connections)
- No connection cards available
- System can scan but cannot test connections

## Benefits

1. **No Scanning Interference**: Connection attempts don't interfere with network discovery
2. **Parallel Operations**: Scanning continues while connections are being tested
3. **Better Resource Management**: Clear separation of concerns between scanning and connecting
4. **State Coordination**: MongoDB provides centralized state management
5. **Scalability**: Can support many WiFi cards with clear role definitions

## Testing

### Unit Tests

Tests for `CardStateManager` are in `tests/unit/test_card_state_manager.py`:
- Role assignment with single and multiple cards
- State updates and retrieval
- Preferred scan interface configuration
- Card availability checks
- MongoDB fallback to in-memory storage

### Integration Tests

Tests for multi-card orchestration are in `tests/integration/test_multi_card_orchestration.py`:
- Role assignment across multiple cards
- Scanning card isolation from connection operations
- Connection cards remain available during scanning
- Single card scenarios
- Card state manager integration

Run tests with:
```bash
pytest tests/unit/test_card_state_manager.py -v
pytest tests/integration/test_multi_card_orchestration.py -v
```

## Backward Compatibility

The implementation maintains backward compatibility:
- `get_card()` method still works (aliases to `lease_card(for_scanning=False)`)
- Existing modules don't need changes
- MongoDB is optional (falls back to in-memory storage)
- System works with any number of WiFi cards (1 or more)

## Future Enhancements

Possible future improvements:
- Dynamic role reassignment based on card performance
- Load balancing across multiple connection cards
- Connection card pooling with priority queues
- Health monitoring and automatic failover
- Remote state coordination across multiple Vasili instances

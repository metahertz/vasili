# Connection Scoring System

## Overview

Vasili now includes an intelligent connection scoring algorithm that ranks WiFi connections by quality. The scoring system considers multiple factors to determine the best available connection.

## Scoring Algorithm

Connections are scored on a 0-100 scale based on four key metrics:

### Weights
- **Download Speed** (40%): Primary factor for overall connection quality
- **Signal Strength** (30%): Critical for stability and reliability
- **Upload Speed** (20%): Important for interactive applications
- **Ping/Latency** (10%): Affects responsiveness

### Calculation

```python
score = (
    download_score * 0.4 +  # Normalized against 100 Mbps reference
    signal_score * 0.3 +    # Already 0-100
    upload_score * 0.2 +    # Normalized against 50 Mbps reference
    ping_score * 0.1        # Normalized, lower is better (0ms=100, 200ms+=0)
)
```

## Performance Metrics Storage

Connection performance data is stored in MongoDB for historical analysis and trend tracking.

### MongoDB Schema

```json
{
    "ssid": "NetworkName",
    "bssid": "AA:BB:CC:DD:EE:FF",
    "signal_strength": 75,
    "channel": 6,
    "encryption_type": "WPA2",
    "download_speed": 45.5,
    "upload_speed": 12.3,
    "ping": 28.5,
    "connection_method": "wpa2Network",
    "interface": "wlan0",
    "score": 72.45,
    "timestamp": 1706982345.678,
    "connected": true
}
```

### MongoDB Configuration

By default, Vasili connects to MongoDB at `mongodb://localhost:27017/` using the `vasili` database.

To configure MongoDB:
1. Install MongoDB: `sudo apt-get install mongodb`
2. Start MongoDB: `sudo systemctl start mongodb`
3. Vasili will automatically connect on startup

**Graceful Degradation**: If MongoDB is unavailable, Vasili continues to operate normally but without metrics storage.

## API Endpoints

### Get Sorted Connections
```
GET /api/connections/sorted
```

Returns all connections sorted by score (best first).

**Response:**
```json
[
    {
        "ssid": "FastNetwork",
        "bssid": "00:11:22:33:44:55",
        "score": 87.25,
        "download_speed": 95.5,
        "upload_speed": 45.2,
        "ping": 12.3,
        "signal_strength": 85,
        "interface": "wlan0",
        "connection_method": "wpa2Network"
    }
]
```

### Get Network Metrics
```
GET /api/metrics/network/<ssid>
```

Returns historical metrics for a specific network.

**Response:**
```json
{
    "ssid": "MyNetwork",
    "average_score": 78.5,
    "history": [
        {
            "score": 80.2,
            "download_speed": 55.0,
            "upload_speed": 25.0,
            "ping": 20.0,
            "signal_strength": 75,
            "timestamp": 1706982345.678
        }
    ]
}
```

### Get Best Networks
```
GET /api/metrics/best
```

Returns the top 5 networks by average historical score.

**Response:**
```json
[
    {
        "_id": "FastNetwork",
        "avg_score": 92.3,
        "avg_download": 98.5,
        "avg_upload": 48.2,
        "avg_ping": 10.5,
        "avg_signal": 88,
        "connection_count": 15
    }
]
```

## Usage Examples

### Programmatic Access

```python
from vasili import WifiManager

manager = WifiManager()

# Get connections sorted by score
sorted_connections = manager.get_sorted_connections()

for conn in sorted_connections:
    print(f"{conn.network.ssid}: Score {conn.calculate_score()}")

# Check metrics availability
if manager.metrics_store.is_available():
    # Get network history
    history = manager.metrics_store.get_network_history("MyNetwork", limit=5)

    # Get average score
    avg_score = manager.metrics_store.get_average_score("MyNetwork")
    print(f"Average score: {avg_score}")

    # Get best networks
    best = manager.metrics_store.get_best_networks(limit=10)
```

### CLI Usage

```bash
# Query metrics via API
curl http://localhost:5000/api/connections/sorted

# Get network history
curl http://localhost:5000/api/metrics/network/MyNetwork

# Get best performing networks
curl http://localhost:5000/api/metrics/best
```

## Future Enhancements

Potential improvements to the scoring system:

1. **Adaptive Weights**: Learn optimal weights based on usage patterns
2. **Stability Scoring**: Track connection drops and reconnection frequency
3. **Time-based Scoring**: Consider time of day and historical performance
4. **Smart Selection**: Automatically switch to better connections when available
5. **Custom Scoring Profiles**: User-defined weights for different use cases

## Testing

Run the scoring tests:

```bash
pytest tests/unit/test_connection_scoring.py -v
```

The test suite includes:
- Perfect/poor/medium connection scoring
- Weight verification (download speed priority)
- Signal strength impact testing
- MongoDB integration tests with mocking

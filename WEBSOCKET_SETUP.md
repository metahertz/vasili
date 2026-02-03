# Web UI Real-Time Updates & Connection History

This document describes the new real-time status updates and connection history features added to Vasili.

## Features

### 1. Real-Time Status Updates
- **WebSocket-based updates** using Flask-SocketIO
- Live status changes without page refresh
- Instant connection updates when new networks are discovered
- Real-time scanning status and card usage information

### 2. Connection History
- **MongoDB-based storage** of all connection attempts
- Tracks successful and failed connections
- Stores speed test results (download, upload, ping)
- Records signal strength, encryption type, and timestamps

## Setup

### Prerequisites

1. **Python Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

   New dependencies added:
   - `flask-socketio` - WebSocket support
   - `pymongo` - MongoDB client

2. **MongoDB (Optional)**

   Connection history requires MongoDB. If MongoDB is not available, Vasili will work normally but without history tracking.

   **Install MongoDB on Ubuntu/Debian:**
   ```bash
   sudo apt-get install -y mongodb
   sudo systemctl start mongodb
   sudo systemctl enable mongodb
   ```

   **Install MongoDB on other systems:**
   See https://docs.mongodb.com/manual/installation/

   **Default Configuration:**
   - Host: `localhost`
   - Port: `27017`
   - Database: `vasili`
   - Collection: `connection_history`

### Running Vasili

Start Vasili normally:

```bash
sudo python3 vasili.py
```

Access the web interface at `http://localhost:5000`

## Web Interface Changes

### What's New

1. **No More Polling**: Replaced 30-second page refresh with instant WebSocket updates
2. **Live Status Panel**: Shows real-time scanning status, cards in use, and active modules
3. **Dynamic Connection List**: Updates automatically when new networks are found
4. **Connection History Panel**: Displays recent connection attempts with success/failure status

### UI Sections

#### Status Panel
- **Scanning**: Shows if the system is actively scanning (Active/Idle)
- **Cards In Use**: Number of WiFi cards currently in use
- **Active Modules**: Number of connection modules loaded
- **Active Bridge**: Displays current active connection with SSID and interfaces

#### Available Connections
- Live-updated list of available WiFi connections
- Signal strength indicators
- Speed test results (for connected networks)
- One-click connect buttons

#### Connection History
- Most recent 50 connection attempts
- Timestamp of each attempt
- Success/failure status
- Speed test results for successful connections
- Signal strength and encryption details

## MongoDB Schema

Connection history entries are stored with the following structure:

```json
{
  "_id": ObjectId("..."),
  "timestamp": ISODate("2024-02-03T10:30:00Z"),
  "ssid": "NetworkName",
  "bssid": "aa:bb:cc:dd:ee:ff",
  "signal_strength": 75,
  "channel": 6,
  "encryption_type": "WPA2",
  "success": true,
  "interface": "wlan0",
  "download_speed": 45.2,
  "upload_speed": 12.8,
  "ping": 23
}
```

### Querying History

You can query the MongoDB directly:

```bash
# Connect to MongoDB
mongosh vasili

# View recent connections
db.connection_history.find().sort({timestamp: -1}).limit(10)

# View only successful connections
db.connection_history.find({success: true})

# View connections to a specific SSID
db.connection_history.find({ssid: "NetworkName"})
```

## API Endpoints

New endpoints added:

### GET /api/history
Returns connection history from MongoDB.

**Response:**
```json
{
  "history": [
    {
      "_id": "...",
      "timestamp": "2024-02-03T10:30:00",
      "ssid": "NetworkName",
      "success": true,
      ...
    }
  ],
  "available": true
}
```

### WebSocket Events

**Client → Server:**
- `connect`: Initial connection
- `disconnect`: Client disconnection

**Server → Client:**
- `status_update`: Sends current system status
- `connections_update`: Sends available connections list

## Troubleshooting

### WebSockets Not Working

1. **Check browser console** for connection errors
2. **Verify Socket.IO library** loads correctly (check network tab)
3. **Check firewall rules** - port 5000 must be accessible
4. **Try different browser** - some browsers block WebSockets

### MongoDB Connection Issues

1. **Check if MongoDB is running:**
   ```bash
   sudo systemctl status mongodb
   ```

2. **Check MongoDB logs:**
   ```bash
   sudo journalctl -u mongodb
   ```

3. **Test MongoDB connection:**
   ```bash
   mongosh --host localhost --port 27017
   ```

4. **If MongoDB is not needed**, Vasili will function normally without it (history will be disabled)

### Performance Considerations

- **WebSocket connections**: Limited only by server resources
- **MongoDB storage**: History is capped at 50 recent entries in the UI, but all entries are stored
- **Memory usage**: Each WebSocket connection uses minimal memory (~50KB)

## Development

### Testing Locally

Without actual WiFi hardware, you can test the web interface:

1. Start MongoDB (or run without it for basic testing)
2. Start Vasili: `python3 vasili.py`
3. Open browser to `http://localhost:5000`
4. Open browser console to see WebSocket messages
5. Status updates will emit even without WiFi hardware detected

### Adding Custom History Queries

You can extend the `/api/history` endpoint to support filtering:

```python
@app.route('/api/history')
def get_history():
    ssid = request.args.get('ssid')
    success_only = request.args.get('success') == 'true'

    query = {}
    if ssid:
        query['ssid'] = ssid
    if success_only:
        query['success'] = True

    history = list(history_collection.find(query).sort('timestamp', -1).limit(50))
    # ... rest of the code
```

## Security Considerations

1. **Secret Key**: Change the Flask secret key in production:
   ```python
   app.config['SECRET_KEY'] = 'your-secure-random-key-here'
   ```

2. **CORS**: Currently allows all origins (`cors_allowed_origins="*"`). Restrict in production:
   ```python
   socketio = SocketIO(app, cors_allowed_origins=["http://your-domain.com"])
   ```

3. **MongoDB Authentication**: Consider enabling MongoDB authentication in production:
   ```python
   mongo_client = MongoClient('mongodb://username:password@localhost:27017/')
   ```

## Future Enhancements

Potential improvements for the real-time features:

- [ ] Add filters to history view (by SSID, date range, success/failure)
- [ ] Add export functionality for connection history
- [ ] Add real-time notifications for connection drops
- [ ] Add connection quality graphs/charts
- [ ] Add mobile-responsive design improvements
- [ ] Add authentication for the web interface

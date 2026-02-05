# Systemd Service Configuration

This document explains the systemd service configuration for running vasili as a daemon.

## Service File

The `vasili.service` file configures vasili to run as a systemd service with automatic startup on boot.

### Location

- **Source:** `vasili.service` (in project root)
- **Installed:** `/etc/systemd/system/vasili.service` (on target system)

### Key Features

1. **Automatic Startup:** Starts on boot via `WantedBy=multi-user.target`
2. **MongoDB Dependency:** Ensures MongoDB (`mongod.service`) starts before vasili
3. **Network Dependency:** Waits for network to be online before starting
4. **Auto-Restart:** Automatically restarts on failure with 10-second delay
5. **Logging:** Integrates with systemd journal for centralized logging

### Service Configuration

```ini
[Unit]
Description=Vasili WiFi Connection Manager
After=network-online.target mongod.service
Wants=network-online.target
Requires=mongod.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vasili
ExecStart=/opt/vasili/venv/bin/python3 /opt/vasili/vasili.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Deployment

The `deploy.sh` script automatically:
1. Transfers `vasili.service` to the target system
2. Substitutes the installation directory path
3. Installs it to `/etc/systemd/system/vasili.service`
4. Reloads systemd to recognize the new service

## Manual Installation

If deploying manually without `deploy.sh`:

```bash
# Copy service file to systemd directory
sudo cp vasili.service /etc/systemd/system/

# Edit paths if not using /opt/vasili
sudo sed -i 's|/opt/vasili|/your/install/path|g' /etc/systemd/system/vasili.service

# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable vasili

# Start service immediately
sudo systemctl start vasili
```

## Managing the Service

### Start vasili
```bash
sudo systemctl start vasili
```

### Stop vasili
```bash
sudo systemctl stop vasili
```

### Restart vasili
```bash
sudo systemctl restart vasili
```

### Enable automatic startup on boot
```bash
sudo systemctl enable vasili
```

### Disable automatic startup
```bash
sudo systemctl disable vasili
```

### Check service status
```bash
sudo systemctl status vasili
```

### View logs
```bash
# View all logs
sudo journalctl -u vasili

# View recent logs (last 50 lines)
sudo journalctl -u vasili -n 50

# Follow logs in real-time
sudo journalctl -u vasili -f

# View logs since last boot
sudo journalctl -u vasili -b
```

## Customization

### Environment Variables

Add environment variables to the service by editing the `[Service]` section:

```ini
[Service]
Environment="VASILI_LOG_LEVEL=DEBUG"
Environment="VASILI_LOG_FILE=/var/log/vasili.log"
Environment="VASILI_CONFIG=/etc/vasili/config.yaml"
```

After editing, reload and restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart vasili
```

### Resource Limits

Uncomment and adjust resource limits in the service file:

```ini
[Service]
MemoryLimit=512M
CPUQuota=50%
```

### Security Hardening

For enhanced security, uncomment security options (note: requires testing as vasili needs network access):

```ini
[Service]
PrivateTmp=yes
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/vasili
```

## Troubleshooting

### Service fails to start

Check status and logs:
```bash
sudo systemctl status vasili -l
sudo journalctl -u vasili -n 100 --no-pager
```

### MongoDB dependency issues

Verify MongoDB is running:
```bash
sudo systemctl status mongod
```

If MongoDB is not installed or using a different service name, edit the service file:
```bash
sudo systemctl edit vasili
```

Remove or modify the `Requires=mongod.service` line.

### Service starts but crashes

Check for:
- Missing Python dependencies: `ls -la /opt/vasili/venv/`
- Permissions issues: Service runs as root but venv might have wrong ownership
- Network interface availability: vasili requires WiFi adapters

### Logs not appearing

Verify journal is running:
```bash
sudo systemctl status systemd-journald
```

View service output directly:
```bash
sudo journalctl -u vasili --since "5 minutes ago"
```

## Dependencies

The service requires:
- **MongoDB:** Must be installed and running (`mongod.service`)
- **Network:** Waits for network-online.target
- **Python 3:** Installed at `/opt/vasili/venv/bin/python3`
- **vasili.py:** Main application at `/opt/vasili/vasili.py`

## Best Practices

1. **Always use `systemctl`** commands rather than running vasili manually
2. **Enable the service** for production deployments to ensure automatic startup
3. **Monitor logs** regularly: `journalctl -u vasili -f`
4. **Test changes** in a development environment before production
5. **Backup configuration** before making changes to the service file

## Integration with deploy.sh

The `deploy.sh` script handles service installation automatically. To update the service configuration:

1. Edit `vasili.service` in the project repository
2. Run `./deploy.sh <host>` to redeploy
3. The script will update the service file and reload systemd

## See Also

- [DEPLOYMENT.md](../DEPLOYMENT.md) - Full deployment guide
- [README.MD](../README.MD) - Project overview
- [ROADMAP.md](../ROADMAP.md) - Development roadmap

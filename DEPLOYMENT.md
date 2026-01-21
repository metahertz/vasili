# Vasili Deployment Guide

This guide covers deploying vasili to an Ubuntu-based micro router via SSH.

## Prerequisites

### On Your Local Machine

- SSH client installed
- SSH key-based authentication configured for the target router
- The vasili project files

### On the Target Router

- Ubuntu or Ubuntu-based Linux distribution
- SSH server running and accessible
- Root or sudo access
- At least one WiFi adapter

## Quick Start

Deploy vasili to a router at `192.168.1.1`:

```bash
./deploy.sh 192.168.1.1
```

This will:
1. Test SSH connectivity
2. Transfer all project files to `/opt/vasili`
3. Install system dependencies (Python, NetworkManager, iptables, dnsmasq, etc.)
4. Install Python dependencies from `requirements.txt`
5. Create a systemd service for automatic startup

## Usage

```bash
./deploy.sh <host> [options]
```

### Arguments

- `host` - Remote host IP address or hostname (required)

### Options

- `-u, --user USER` - SSH username (default: `root`)
- `-p, --port PORT` - SSH port (default: `22`)
- `-d, --dir DIR` - Remote installation directory (default: `/opt/vasili`)
- `-h, --help` - Show help message

### Examples

Deploy with custom user:
```bash
./deploy.sh 192.168.1.1 -u admin
```

Deploy on non-standard SSH port:
```bash
./deploy.sh router.local -p 2222
```

Deploy to custom directory:
```bash
./deploy.sh 10.0.0.1 --dir /home/admin/vasili
```

Combined options:
```bash
./deploy.sh 192.168.1.1 -u admin -p 2222 -d /opt/vasili
```

## Post-Deployment

After successful deployment, connect to your router and manage the vasili service:

### Start vasili
```bash
ssh root@192.168.1.1 'systemctl start vasili'
```

### Enable automatic startup on boot
```bash
ssh root@192.168.1.1 'systemctl enable vasili'
```

### Check service status
```bash
ssh root@192.168.1.1 'systemctl status vasili'
```

### View real-time logs
```bash
ssh root@192.168.1.1 'journalctl -u vasili -f'
```

### Stop the service
```bash
ssh root@192.168.1.1 'systemctl stop vasili'
```

## Accessing the Web Interface

Once vasili is running, access the web interface at:

```
http://<router-ip>:5000
```

For example: `http://192.168.1.1:5000`

## Troubleshooting

### SSH Connection Fails

Ensure:
1. SSH server is running on the target router
2. SSH key authentication is configured (password authentication is not supported by this script)
3. The host, port, and username are correct
4. Firewall allows SSH connections

Test SSH manually:
```bash
ssh -p 22 root@192.168.1.1 echo "Connection successful"
```

### Dependencies Fail to Install

The script requires root access to install system packages. If you're using a non-root user, ensure they have sudo privileges configured.

### Service Fails to Start

Check logs for errors:
```bash
ssh root@192.168.1.1 'journalctl -u vasili -n 50'
```

Common issues:
- No WiFi adapters detected - ensure `wlan*` or `wifi*` interfaces exist
- Missing dependencies - manually run: `cd /opt/vasili && python3 -m pip install -r requirements.txt`
- Permission errors - vasili requires root privileges for network management

### Re-deploying / Updating

Simply run the deployment script again. It will overwrite existing files and update dependencies.

```bash
./deploy.sh 192.168.1.1
```

After re-deployment, restart the service:
```bash
ssh root@192.168.1.1 'systemctl restart vasili'
```

## Security Considerations

- The deployment script requires SSH key authentication (more secure than passwords)
- vasili runs as root (required for network management operations)
- The web interface listens on all interfaces (0.0.0.0) - consider firewall rules if needed
- No authentication is currently implemented on the web interface

## Manual Installation

If you prefer to deploy manually or need to customize the process:

1. Copy files to the target:
   ```bash
   scp -r vasili.py modules/ templates/ requirements.txt root@192.168.1.1:/opt/vasili/
   ```

2. SSH into the router:
   ```bash
   ssh root@192.168.1.1
   ```

3. Install system dependencies:
   ```bash
   apt-get update
   apt-get install python3 python3-pip wireless-tools network-manager iptables dnsmasq iw
   ```

4. Install Python dependencies:
   ```bash
   cd /opt/vasili
   pip3 install -r requirements.txt
   ```

5. Run vasili:
   ```bash
   python3 /opt/vasili/vasili.py
   ```

## Next Steps

After deployment, see [ROADMAP.md](ROADMAP.md) for the project's development priorities and [README.MD](README.MD) for how vasili works.

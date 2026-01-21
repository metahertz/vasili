#!/bin/bash
# Vasili SSH Deployment Script
# Deploy vasili to an Ubuntu-based micro router via SSH

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
REMOTE_USER="root"
REMOTE_PORT="22"
REMOTE_DIR="/opt/vasili"

# Usage message
usage() {
    cat <<EOF
Usage: $0 <host> [options]

Deploy vasili to an Ubuntu-based micro router via SSH.

Arguments:
    host                Remote host IP or hostname (required)

Options:
    -u, --user USER     SSH user (default: root)
    -p, --port PORT     SSH port (default: 22)
    -d, --dir DIR       Remote installation directory (default: /opt/vasili)
    -h, --help          Show this help message

Examples:
    $0 192.168.1.1
    $0 router.local -u admin -p 2222
    $0 10.0.0.1 --dir /home/admin/vasili

EOF
    exit 1
}

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Parse command line arguments
if [ $# -eq 0 ]; then
    usage
fi

# Check for help flag first
if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage
fi

REMOTE_HOST="$1"
shift

while [ $# -gt 0 ]; do
    case "$1" in
        -u|--user)
            REMOTE_USER="$2"
            shift 2
            ;;
        -p|--port)
            REMOTE_PORT="$2"
            shift 2
            ;;
        -d|--dir)
            REMOTE_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            ;;
    esac
done

# Verify we're in the vasili project directory
if [ ! -f "vasili.py" ] || [ ! -f "requirements.txt" ]; then
    log_error "This script must be run from the vasili project root directory"
    exit 1
fi

log_info "Deploying vasili to $REMOTE_USER@$REMOTE_HOST:$REMOTE_PORT"
log_info "Remote directory: $REMOTE_DIR"

# Test SSH connection
log_info "Testing SSH connection..."
if ! ssh -p "$REMOTE_PORT" -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE_USER@$REMOTE_HOST" "echo 'SSH connection successful'" 2>/dev/null; then
    log_error "Failed to connect to $REMOTE_HOST via SSH"
    log_error "Please ensure:"
    log_error "  1. SSH is enabled on the remote host"
    log_error "  2. SSH key authentication is configured"
    log_error "  3. Host, port, and user are correct"
    exit 1
fi

# Create remote directory
log_info "Creating remote directory structure..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" "mkdir -p $REMOTE_DIR/{modules,templates}"

# Transfer files
log_info "Transferring project files..."
scp -P "$REMOTE_PORT" vasili.py "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -P "$REMOTE_PORT" requirements.txt "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -P "$REMOTE_PORT" modules/*.py "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/modules/" 2>/dev/null || log_warn "No module files found"
scp -P "$REMOTE_PORT" templates/*.html "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/templates/" 2>/dev/null || log_warn "No template files found"

# Install system dependencies
log_info "Installing system dependencies..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" bash <<'ENDSSH'
    set -e
    export DEBIAN_FRONTEND=noninteractive

    echo "[INFO] Updating package lists..."
    apt-get update -qq

    echo "[INFO] Installing system packages..."
    apt-get install -y -qq \
        python3 \
        python3-pip \
        python3-dev \
        wireless-tools \
        network-manager \
        iptables \
        dnsmasq \
        iw \
        build-essential \
        libnetfilter-queue-dev

    echo "[INFO] System packages installed successfully"
ENDSSH

# Install Python dependencies
log_info "Installing Python dependencies..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_DIR && python3 -m pip install --upgrade pip && python3 -m pip install -r requirements.txt"

# Create systemd service file
log_info "Creating systemd service..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" bash <<ENDSSH
    cat > /etc/systemd/system/vasili.service <<'EOF'
[Unit]
Description=Vasili WiFi Connection Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_DIR
ExecStart=/usr/bin/python3 $REMOTE_DIR/vasili.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    echo "[INFO] Systemd service created"
ENDSSH

# Final instructions
log_info "Deployment complete!"
echo ""
log_info "Next steps:"
echo "  1. Start vasili:    ssh $REMOTE_USER@$REMOTE_HOST 'systemctl start vasili'"
echo "  2. Enable on boot:  ssh $REMOTE_USER@$REMOTE_HOST 'systemctl enable vasili'"
echo "  3. Check status:    ssh $REMOTE_USER@$REMOTE_HOST 'systemctl status vasili'"
echo "  4. View logs:       ssh $REMOTE_USER@$REMOTE_HOST 'journalctl -u vasili -f'"
echo "  5. Access web UI:   http://$REMOTE_HOST:5000"
echo ""
log_warn "Note: vasili requires root privileges for network management"
log_warn "Ensure the remote system has WiFi adapters available"

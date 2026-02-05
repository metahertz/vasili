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

# Detect if remote user needs sudo
log_info "Checking remote user privileges..."
NEEDS_SUDO=$(ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" 'if [ "$(id -u)" -eq 0 ]; then echo "no"; else echo "yes"; fi')
if [ "$NEEDS_SUDO" = "yes" ]; then
    log_info "Non-root user detected, checking sudo configuration..."

    # Check if passwordless sudo is configured
    if ! ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" 'sudo -n true 2>/dev/null'; then
        log_error "Sudo requires a password for user $REMOTE_USER"
        log_error ""
        log_error "Please configure passwordless sudo on the remote host:"
        log_error "  1. SSH to the remote host: ssh $REMOTE_USER@$REMOTE_HOST"
        log_error "  2. Run: sudo visudo"
        log_error "  3. Add this line: $REMOTE_USER ALL=(ALL) NOPASSWD: ALL"
        log_error "  4. Save and exit"
        log_error ""
        log_error "Alternatively, run this script as root user: $0 $REMOTE_HOST -u root"
        exit 1
    fi

    log_info "Passwordless sudo confirmed, will use sudo for privileged operations"
    SUDO="sudo"
else
    log_info "Running as root user"
    SUDO=""
fi

# Create remote directory
log_info "Creating remote directory structure..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" "$SUDO mkdir -p $REMOTE_DIR/{modules,templates} && $SUDO chown -R $REMOTE_USER:$REMOTE_USER $REMOTE_DIR"

# Transfer files
log_info "Transferring project files..."
scp -P "$REMOTE_PORT" vasili.py "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -P "$REMOTE_PORT" requirements.txt "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -P "$REMOTE_PORT" vasili.service "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -P "$REMOTE_PORT" modules/*.py "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/modules/" 2>/dev/null || log_warn "No module files found"
scp -P "$REMOTE_PORT" templates/*.html "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/templates/" 2>/dev/null || log_warn "No template files found"

# Install system dependencies
log_info "Installing system dependencies..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" bash <<ENDSSH
    set -e
    export DEBIAN_FRONTEND=noninteractive
    SUDO="$SUDO"

    echo "[INFO] Updating package lists..."
    \$SUDO apt-get update -qq

    echo "[INFO] Installing system packages..."
    \$SUDO apt-get install -y -qq \\
        python3 \\
        python3-pip \\
        python3-dev \\
        python3-venv \\
        pipx \\
        wireless-tools \\
        network-manager \\
        iptables \\
        dnsmasq \\
        iw \\
        build-essential \\
        libnetfilter-queue-dev \\
        gnupg \\
        curl

    echo "[INFO] System packages installed successfully"

    # Install MongoDB
    echo "[INFO] Installing MongoDB..."

    # Import MongoDB public GPG key
    curl -fsSL https://www.mongodb.org/static/pgp/server-4.4.asc | \$SUDO gpg --dearmor -o /usr/share/keyrings/mongodb-server-4.4.gpg 2>/dev/null || true

    # Add MongoDB repository (using focal as 4.4 doesn't have jammy packages)
    echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-4.4.gpg ] https://repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4 multiverse" | \$SUDO tee /etc/apt/sources.list.d/mongodb-org-4.4.list > /dev/null

    # Update and install MongoDB
    \$SUDO apt-get update -qq
    \$SUDO apt-get install -y -qq mongodb-org

    # Configure MongoDB to listen on localhost:27017
    \$SUDO tee /etc/mongod.conf > /dev/null <<'MONGOCONF'
# MongoDB configuration file
storage:
  dbPath: /var/lib/mongodb
  journal:
    enabled: true

systemLog:
  destination: file
  logAppend: true
  path: /var/log/mongodb/mongod.log

net:
  port: 27017
  bindIp: 127.0.0.1

processManagement:
  timeZoneInfo: /usr/share/zoneinfo
MONGOCONF

    # Start and enable MongoDB service
    \$SUDO systemctl daemon-reload
    \$SUDO systemctl start mongod
    \$SUDO systemctl enable mongod

    echo "[INFO] MongoDB installed and configured on localhost:27017"
ENDSSH

# Install Python dependencies using pipx
log_info "Installing Python dependencies using pipx..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" bash <<ENDSSH
    set -e

    # Ensure pipx is in PATH
    export PATH="\$HOME/.local/bin:\$PATH"

    # Ensure pipx is set up
    pipx ensurepath || true

    # Install dependencies from requirements.txt into a venv
    echo "[INFO] Creating virtual environment for vasili..."
    python3 -m venv $REMOTE_DIR/venv

    echo "[INFO] Installing dependencies via pipx-style isolated environment..."
    $REMOTE_DIR/venv/bin/pip install --upgrade pip
    $REMOTE_DIR/venv/bin/pip install -r $REMOTE_DIR/requirements.txt

    echo "[INFO] Python dependencies installed successfully"
ENDSSH

# Install systemd service file
log_info "Installing systemd service..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" bash <<ENDSSH
    SUDO="$SUDO"
    REMOTE_DIR="$REMOTE_DIR"

    # Install the service file, substituting the installation directory
    \$SUDO sed "s|/opt/vasili|\$REMOTE_DIR|g" "\$REMOTE_DIR/vasili.service" | \$SUDO tee /etc/systemd/system/vasili.service > /dev/null

    \$SUDO systemctl daemon-reload
    echo "[INFO] Systemd service installed"
ENDSSH

# Final instructions
log_info "Deployment complete!"
echo ""
log_info "Next steps:"
if [ "$NEEDS_SUDO" = "yes" ]; then
    echo "  1. Start vasili:    ssh $REMOTE_USER@$REMOTE_HOST 'sudo systemctl start vasili'"
    echo "  2. Enable on boot:  ssh $REMOTE_USER@$REMOTE_HOST 'sudo systemctl enable vasili'"
    echo "  3. Check status:    ssh $REMOTE_USER@$REMOTE_HOST 'sudo systemctl status vasili'"
    echo "  4. View logs:       ssh $REMOTE_USER@$REMOTE_HOST 'sudo journalctl -u vasili -f'"
else
    echo "  1. Start vasili:    ssh $REMOTE_USER@$REMOTE_HOST 'systemctl start vasili'"
    echo "  2. Enable on boot:  ssh $REMOTE_USER@$REMOTE_HOST 'systemctl enable vasili'"
    echo "  3. Check status:    ssh $REMOTE_USER@$REMOTE_HOST 'systemctl status vasili'"
    echo "  4. View logs:       ssh $REMOTE_USER@$REMOTE_HOST 'journalctl -u vasili -f'"
fi
echo "  5. Access web UI:   http://$REMOTE_HOST:5000"
echo ""
log_warn "Note: vasili requires root privileges for network management"
log_warn "Ensure the remote system has WiFi adapters available"

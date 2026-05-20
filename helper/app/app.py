#!/usr/bin/env python3
"""vasili-helper UI — Flask app that drives the bundled servers."""

import functools
import json
import os
import secrets
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

from flask import Flask, abort, jsonify, request, render_template, send_file

CONFIG_DIR = Path(os.environ.get('HELPER_CONFIG_DIR', '/etc/vasili-helper'))
STATE_DIR = Path(os.environ.get('HELPER_STATE_DIR', '/var/lib/vasili-helper'))
CONFIG_FILE = CONFIG_DIR / 'config.json'
JOBS_DB = STATE_DIR / 'crack-jobs.db'
WORDLIST = STATE_DIR / 'rockyou.txt'
SSH_CLIENT_KEY = STATE_DIR / 'ssh_client_key'
WG_SERVER_PUB = STATE_DIR / 'wg_server_public'

ROCKYOU_URL = (
    'https://github.com/brannondorsey/naive-hashcat/releases/download/'
    'data/rockyou.txt'
)

app = Flask(__name__, static_folder='static', template_folder='templates')


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def save_config(conf: dict):
    CONFIG_FILE.write_text(json.dumps(conf, indent=2))


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        conf = load_config()
        token = conf.get('auth_token') or os.environ.get('HELPER_TOKEN', '')
        if not token:
            abort(503, 'auth token not set')
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            abort(401, 'missing bearer token')
        if not secrets.compare_digest(header[7:], token):
            abort(401, 'bad token')
        return fn(*args, **kwargs)
    return wrapper


def supervisorctl(action: str, target: str):
    try:
        subprocess.run(
            ['supervisorctl', '-s', 'unix:///run/supervisor.sock', action, target],
            check=False, capture_output=True, timeout=15,
        )
    except Exception:
        pass


def detect_public_ip(saved: str) -> str:
    if saved and saved != 'auto':
        return saved
    for url in ('https://ifconfig.me', 'https://icanhazip.com'):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return ''


def service_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ['supervisorctl', '-s', 'unix:///run/supervisor.sock', 'status', name],
            capture_output=True, text=True, timeout=5,
        )
        return 'RUNNING' in out.stdout
    except Exception:
        return False


def port_listening(proto: str, port: int) -> bool:
    """Return True if anything is bound on the given proto/port."""
    flag = '-tlnp' if proto == 'tcp' else '-ulnp'
    try:
        out = subprocess.run(['ss', flag], capture_output=True,
                             text=True, timeout=3)
        return f':{port} ' in out.stdout
    except Exception:
        return False


# ----- Routes ----------------------------------------------------------


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
@require_auth
def api_status():
    conf = load_config()
    public_ip = detect_public_ip(conf.get('public_ip', 'auto'))
    return jsonify({
        'public_ip': public_ip,
        'services': {
            'helper-ui':      service_running('helper-ui'),
            'dns-proxy':      service_running('dns-proxy'),
            'crack-server':   service_running('crack-server') and bool(conf.get('crack', {}).get('enabled')),
            'iodine-backend': service_running('iodine-backend') and bool(conf.get('iodine', {}).get('enabled')),
            'wg-backend':     service_running('wg-backend') and bool(conf.get('wireguard', {}).get('enabled')),
            'ssh-tunnel':     service_running('ssh-tunnel') and bool(conf.get('ssh', {}).get('enabled')),
        },
        'ports': {
            'udp_53': port_listening('udp', 53),
            'tcp_53': port_listening('tcp', 53),
            'tcp_8080': port_listening('tcp', 8080),
        },
        'wordlist_present': WORDLIST.exists() and WORDLIST.stat().st_size > 0,
        'wordlist_size': WORDLIST.stat().st_size if WORDLIST.exists() else 0,
        'ssh_key_present': SSH_CLIENT_KEY.exists(),
        'wg_server_pub': WG_SERVER_PUB.read_text().strip() if WG_SERVER_PUB.exists() else '',
    })


@app.route('/api/config', methods=['GET'])
@require_auth
def api_config_get():
    conf = load_config()
    # Never expose auth_token over the wire.
    conf.pop('auth_token', None)
    return jsonify(conf)


@app.route('/api/config', methods=['PUT'])
@require_auth
def api_config_put():
    body = request.get_json(force=True, silent=True) or {}
    current = load_config()
    affected = set()

    # Merge per-section. The UI sends only the sections it touched.
    for section in ('ssh', 'iodine', 'wireguard', 'crack'):
        if section in body:
            current.setdefault(section, {}).update(body[section])
            if section == 'ssh':
                affected.add('ssh-tunnel')
            elif section == 'iodine':
                affected.add('iodine-backend')
                affected.add('dns-proxy')
            elif section == 'wireguard':
                affected.add('wg-backend')
                affected.add('dns-proxy')
            elif section == 'crack':
                affected.add('crack-server')
                affected.add('dns-proxy')

    if 'public_ip' in body:
        current['public_ip'] = body['public_ip']

    # Auto-generate the crack secret if enabling without one.
    if current.get('crack', {}).get('enabled') and not current['crack'].get('secret'):
        current['crack']['secret'] = secrets.token_hex(8)

    # Reject overlapping iodine/crack domains — the demux relies on
    # suffix routing, so neither domain can be a suffix of the other.
    iod = (current.get('iodine', {}).get('domain') or '').lower().rstrip('.')
    crk = (current.get('crack', {}).get('domain') or '').lower().rstrip('.')
    if iod and crk and (iod == crk
                        or iod.endswith('.' + crk)
                        or crk.endswith('.' + iod)):
        return jsonify({'ok': False,
                        'error': 'iodine and crack domains must be disjoint '
                                 f'(got iodine={iod!r}, crack={crk!r})'}), 400

    save_config(current)

    for target in affected:
        supervisorctl('restart', target)

    return jsonify({'ok': True, 'restarted': sorted(affected)})


@app.route('/api/jobs')
@require_auth
def api_jobs():
    if not JOBS_DB.exists():
        return jsonify([])
    try:
        with sqlite3.connect(str(JOBS_DB)) as conn:
            rows = conn.execute(
                'SELECT job_id, status, password, progress, submitted_at, '
                'completed_at FROM jobs ORDER BY submitted_at DESC LIMIT 50'
            ).fetchall()
    except sqlite3.Error:
        return jsonify([])
    return jsonify([
        {
            'job_id': r[0], 'status': r[1], 'password': r[2],
            'progress': r[3], 'submitted_at': r[4], 'completed_at': r[5],
        } for r in rows
    ])


@app.route('/api/wordlist/download', methods=['POST'])
@require_auth
def api_wordlist_download():
    if WORDLIST.exists() and WORDLIST.stat().st_size > 0:
        return jsonify({'ok': True, 'size': WORDLIST.stat().st_size,
                        'message': 'already present'})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WORDLIST.with_suffix('.tmp')
    try:
        with urllib.request.urlopen(ROCKYOU_URL, timeout=60) as r, open(tmp, 'wb') as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        tmp.rename(WORDLIST)
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        return jsonify({'ok': False, 'error': str(exc)}), 502
    return jsonify({'ok': True, 'size': WORDLIST.stat().st_size})


@app.route('/api/ssh-client-key')
@require_auth
def api_ssh_client_key():
    if not SSH_CLIENT_KEY.exists():
        abort(404, 'ssh client key not yet generated (enable SSH first)')
    return send_file(str(SSH_CLIENT_KEY), as_attachment=True,
                     download_name='vasili-ssh-client')


@app.route('/api/wg-client-config')
@require_auth
def api_wg_client_config():
    conf = load_config()
    wg = conf.get('wireguard', {})
    public_ip = detect_public_ip(conf.get('public_ip', 'auto'))
    srv_pub = WG_SERVER_PUB.read_text().strip() if WG_SERVER_PUB.exists() else ''
    client_priv_file = STATE_DIR / 'wg_client_private'
    client_pub_file = STATE_DIR / 'wg_client_public'
    if not client_priv_file.exists():
        # Generate a client keypair so the operator can hand its public
        # key back into the UI to register the peer.
        subprocess.run(
            f'umask 077; wg genkey | tee {client_priv_file} | wg pubkey > {client_pub_file}',
            shell=True, check=True,
        )
    subnet = wg.get('subnet', '10.53.1.0/24').split('/')[0]
    parts = subnet.split('.')
    client_ip = '.'.join(parts[:3] + ['2'])
    body = (
        '[Interface]\n'
        f'PrivateKey = {client_priv_file.read_text().strip()}\n'
        f'Address = {client_ip}/24\n'
        'DNS = 8.8.8.8\n'
        '\n'
        '[Peer]\n'
        f'PublicKey = {srv_pub}\n'
        f'Endpoint = {public_ip}:53\n'
        'AllowedIPs = 0.0.0.0/0\n'
        'PersistentKeepalive = 25\n'
    )
    # Also update saved config with the generated client_pubkey so the
    # server-side peer list matches.
    cli_pub = client_pub_file.read_text().strip()
    conf.setdefault('wireguard', {})['client_pubkey'] = cli_pub
    save_config(conf)
    supervisorctl('restart', 'wg-backend')
    return (body, 200, {
        'Content-Type': 'text/plain',
        'Content-Disposition': 'attachment; filename="wg-vasili-client.conf"',
    })


@app.route('/api/client-config')
@require_auth
def api_client_config():
    """Copy-paste block ready to drop into vasili's module/pipeline config."""
    conf = load_config()
    public_ip = detect_public_ip(conf.get('public_ip', 'auto'))
    lines = []
    if conf.get('ssh', {}).get('enabled'):
        lines += [
            '# dns_port_tunnel stage (SSH path)',
            f'ssh_server: {public_ip}',
            'ssh_user: root',
            'ssh_key_path: /etc/vasili/ssh_client_key',
            '',
        ]
    if conf.get('iodine', {}).get('enabled'):
        iod = conf.get('iodine', {})
        lines += [
            '# dns_tunnel stage (iodine)',
            f'server_domain: {iod.get("domain", "")}',
            f'tunnel_password: {iod.get("password", "")}',
            'tunnel_type: iodine',
            '',
            f'# DNS delegation required for {iod.get("domain", "")}:',
            f'#   {iod.get("domain","")}.   NS   ns-vasili.{iod.get("domain","").split(".",1)[-1] if "." in iod.get("domain","") else ""}',
            f'#   ns-vasili...   A   {public_ip}',
            '',
        ]
    if conf.get('wireguard', {}).get('enabled'):
        lines += [
            '# dns_port_tunnel stage (WireGuard) — fetch wg config from',
            '# /api/wg-client-config and place at /etc/wireguard/wg-vasili-client.conf',
            'wg_config_path: /etc/wireguard/wg-vasili-client.conf',
            '',
        ]
    if conf.get('crack', {}).get('enabled'):
        cr = conf['crack']
        lines += [
            '# dns_offload_crack stage',
            f'offload_domain: {cr.get("domain", "")}',
            f'offload_secret: {cr.get("secret", "")}',
            '',
            f'# DNS delegation required for {cr.get("domain","")}:',
            f'#   {cr.get("domain","")}.   NS   ns-crack.{cr.get("domain","").split(".",1)[-1] if "." in cr.get("domain","") else ""}',
            f'#   ns-crack...   A   {public_ip}',
            '',
        ]
    return jsonify({'text': '\n'.join(lines) if lines else
                    '# No services enabled yet — toggle them on above.'})


if __name__ == '__main__':
    # Plain Flask dev server is fine — single-operator UI on a port the
    # operator firewalls themselves. supervisord auto-restarts on crash.
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)

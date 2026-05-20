// vasili-helper UI front-end.
const TOKEN_KEY = 'vasili-helper-token';
let cfg = {};

function el(id) { return document.getElementById(id); }

function toast(msg, isError) {
    const t = el('toast');
    t.textContent = msg;
    t.classList.toggle('error', !!isError);
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2500);
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ''; }
function saveToken() {
    const v = el('token-input').value.trim();
    if (!v) return;
    sessionStorage.setItem(TOKEN_KEY, v);
    el('token-prompt').classList.add('hidden');
    boot();
}

async function api(path, opts = {}) {
    const headers = Object.assign(
        { 'Content-Type': 'application/json' },
        opts.headers || {},
        { Authorization: 'Bearer ' + getToken() },
    );
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    if (resp.status === 401) {
        sessionStorage.removeItem(TOKEN_KEY);
        el('token-prompt').classList.remove('hidden');
        throw new Error('unauthorized');
    }
    return resp;
}

async function downloadAuth(path, filename) {
    try {
        const resp = await api(path);
        if (!resp.ok) { toast('download failed: ' + resp.status, true); return; }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
    } catch (e) { /* token prompt re-shown */ }
}

function badge(ok) {
    return ok
        ? '<span class="badge badge-up">RUNNING</span>'
        : '<span class="badge badge-down">STOPPED</span>';
}

function fmtTime(t) {
    if (!t) return '—';
    return new Date(t * 1000).toLocaleString();
}

async function refreshStatus() {
    try {
        const resp = await api('/api/status');
        if (!resp.ok) return;
        const s = await resp.json();
        el('public-ip').textContent = s.public_ip ? ('public ip: ' + s.public_ip) : '(public ip unknown)';
        const grid = el('status-grid');
        const items = [
            ['helper-ui',      s.services['helper-ui']],
            ['dns-proxy',      s.services['dns-proxy']],
            ['crack-server',   s.services['crack-server']],
            ['iodine-backend', s.services['iodine-backend']],
            ['wg-backend',     s.services['wg-backend']],
            ['ssh-tunnel',     s.services['ssh-tunnel']],
            ['UDP/53 bound',   s.ports.udp_53],
            ['TCP/53 bound',   s.ports.tcp_53],
            ['Wordlist',       s.wordlist_present],
        ];
        grid.innerHTML = items.map(([name, ok]) =>
            `<div class="status-card"><div class="name">${name}</div><div class="value">${badge(ok)}</div></div>`
        ).join('');

        // SSH key download enabled only when key is present.
        el('ssh-download-btn').disabled = !s.ssh_key_present;
        // WG server public key (shown in the WG fields).
        el('wg-server-pub').textContent = s.wg_server_pub || '(generated on first Apply with WireGuard selected)';

        // Wordlist hint
        if (s.wordlist_present) {
            const mb = (s.wordlist_size / 1048576).toFixed(1);
            el('wordlist-state').textContent = `present (${mb} MB)`;
            el('rockyou-btn').disabled = true;
            el('rockyou-btn').textContent = 'rockyou.txt installed';
        } else {
            el('wordlist-state').textContent = 'not yet downloaded — click below.';
            el('rockyou-btn').disabled = false;
        }
    } catch (e) { /* shrug */ }
}

async function loadConfig() {
    const resp = await api('/api/config');
    if (!resp.ok) return;
    cfg = await resp.json();

    el('ssh-enabled').checked = !!(cfg.ssh && cfg.ssh.enabled);

    el('iodine-enabled').checked = !!(cfg.iodine && cfg.iodine.enabled);
    el('iodine-domain').value    = (cfg.iodine || {}).domain || '';
    el('iodine-password').value  = (cfg.iodine || {}).password || '';
    el('iodine-subnet').value    = (cfg.iodine || {}).subnet || '10.53.53.1/24';
    el('wg-enabled').checked     = !!(cfg.wireguard && cfg.wireguard.enabled);
    el('wg-subnet').value        = (cfg.wireguard || {}).subnet || '10.53.1.0/24';

    el('crack-enabled').checked  = !!(cfg.crack && cfg.crack.enabled);
    el('crack-domain').value     = (cfg.crack || {}).domain || '';
    el('crack-secret').value     = (cfg.crack || {}).secret || '';
    el('crack-wordlist').value   = (cfg.crack || {}).wordlist || '/var/lib/vasili-helper/rockyou.txt';
}

async function applySSH() {
    const resp = await api('/api/config', {
        method: 'PUT',
        body: JSON.stringify({ ssh: { enabled: el('ssh-enabled').checked } }),
    });
    const d = await resp.json();
    toast(d.ok ? 'SSH config saved' : (d.error || 'save failed'), !d.ok);
    setTimeout(() => { loadConfig(); refreshStatus(); refreshClient(); }, 500);
}

async function applyIodine() {
    const body = {
        iodine: {
            enabled: el('iodine-enabled').checked,
            domain: el('iodine-domain').value.trim(),
            password: el('iodine-password').value,
            subnet: el('iodine-subnet').value.trim(),
        },
    };
    const resp = await api('/api/config', {
        method: 'PUT', body: JSON.stringify(body),
    });
    const d = await resp.json();
    toast(d.ok ? 'iodine config saved' : (d.error || 'save failed'), !d.ok);
    setTimeout(() => { loadConfig(); refreshStatus(); refreshClient(); }, 500);
}

async function applyWireguard() {
    const body = {
        wireguard: {
            enabled: el('wg-enabled').checked,
            subnet: el('wg-subnet').value.trim(),
        },
    };
    const resp = await api('/api/config', {
        method: 'PUT', body: JSON.stringify(body),
    });
    const d = await resp.json();
    toast(d.ok ? 'WireGuard config saved' : (d.error || 'save failed'), !d.ok);
    setTimeout(() => { loadConfig(); refreshStatus(); refreshClient(); }, 500);
}

async function applyCrack() {
    const body = {
        crack: {
            enabled: el('crack-enabled').checked,
            domain: el('crack-domain').value.trim(),
            secret: el('crack-secret').value.trim(),
            wordlist: el('crack-wordlist').value.trim(),
        },
    };
    const resp = await api('/api/config', {
        method: 'PUT', body: JSON.stringify(body),
    });
    const d = await resp.json();
    toast(d.ok ? 'Crack config saved' : 'save failed', !d.ok);
    setTimeout(() => { loadConfig(); refreshStatus(); refreshClient(); }, 500);
}

async function downloadWordlist() {
    el('rockyou-btn').disabled = true;
    el('rockyou-btn').textContent = 'downloading…';
    try {
        const resp = await api('/api/wordlist/download', { method: 'POST' });
        const d = await resp.json();
        if (d.ok) toast(`wordlist ready (${(d.size/1048576).toFixed(1)} MB)`);
        else toast('download failed: ' + (d.error || ''), true);
    } finally {
        refreshStatus();
    }
}

async function refreshJobs() {
    try {
        const resp = await api('/api/jobs');
        if (!resp.ok) return;
        const jobs = await resp.json();
        const tbody = el('jobs-table').querySelector('tbody');
        if (jobs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="color:#484f58">no jobs yet</td></tr>';
            return;
        }
        tbody.innerHTML = jobs.map(j =>
            `<tr><td>${j.job_id}</td><td>${j.status}</td>` +
            `<td>${j.progress || 0}%</td>` +
            `<td>${j.password ? '<code>'+j.password+'</code>' : '—'}</td>` +
            `<td>${fmtTime(j.submitted_at)}</td></tr>`
        ).join('');
    } catch (e) { /* token */ }
}

async function refreshClient() {
    try {
        const resp = await api('/api/client-config');
        if (!resp.ok) return;
        const d = await resp.json();
        el('client-config').textContent = d.text;
    } catch (e) {}
}

function copyClientConfig() {
    navigator.clipboard.writeText(el('client-config').textContent)
        .then(() => toast('copied'))
        .catch(() => toast('copy failed', true));
}

async function boot() {
    if (!getToken()) {
        el('token-prompt').classList.remove('hidden');
        return;
    }
    el('token-prompt').classList.add('hidden');
    await loadConfig();
    refreshStatus();
    refreshClient();
    refreshJobs();
    setInterval(refreshStatus, 3000);
    setInterval(refreshJobs, 5000);
}

window.addEventListener('load', boot);

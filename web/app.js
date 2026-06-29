// VasiliBLE Web Bluetooth client.
//
// Talks to the Pi-side BLE peripheral (../ble_peripheral.py) from a desktop
// Chromium browser (Chrome / Edge / Opera). The UUIDs, the Response framing,
// and the command/response JSON shapes here MUST match ble_peripheral.py — it
// is the single source of truth for the wire contract.
//
// Web Bluetooth needs a secure context: serve this over https:// or from
// http://localhost (see README.md). Safari and Firefox don't support it.

'use strict';

// --- GATT UUIDs (lowercase; match ble_peripheral.py) ---------------------
const SERVICE_UUID = '56415349-4c49-0000-0000-000000000001';
const STATUS_UUID = '56415349-4c49-0000-0000-000000000002';
const COMMAND_UUID = '56415349-4c49-0000-0000-000000000003';
const RESPONSE_UUID = '56415349-4c49-0000-0000-000000000004';

// --- Response framing (mirrors frame_payload/Reassembler in Python) ------
class Reassembler {
  constructor() { this.reset(); }
  reset() { this.buf = new Uint8Array(0); this.total = null; }

  // Feed one notification (DataView). Returns a Uint8Array payload once
  // complete, else null.
  feed(view) {
    const bytes = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
    if (bytes.length === 0) return null;
    if (bytes[0] === 0x01) {
      const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      this.total = dv.getUint32(1, false); // big-endian total length
      this.buf = bytes.slice(5);
    } else {
      const merged = new Uint8Array(this.buf.length + bytes.length - 1);
      merged.set(this.buf, 0);
      merged.set(bytes.slice(1), this.buf.length);
      this.buf = merged;
    }
    if (this.total !== null && this.buf.length >= this.total) {
      const payload = this.buf.slice(0, this.total);
      this.reset();
      return payload;
    }
    return null;
  }
}

// --- BLE client ----------------------------------------------------------
class VasiliClient {
  constructor() {
    this.device = null;
    this.commandChar = null;
    this.responseChar = null;
    this.statusChar = null;
    this.reassembler = new Reassembler();
    this.pending = 'none'; // 'scan' | 'command' | 'none'
    this.onStatus = () => {};
    this.onNetworks = () => {};
    this.onState = () => {};
    this.onError = () => {};
  }

  get supported() {
    return typeof navigator !== 'undefined' && !!navigator.bluetooth;
  }

  async connect() {
    if (!this.supported) {
      this.onError('Web Bluetooth is not available in this browser. ' +
        'Use Chrome, Edge, or Opera on desktop.');
      return;
    }
    this.onState('connecting');
    try {
      this.device = await navigator.bluetooth.requestDevice({
        filters: [{ services: [SERVICE_UUID] }],
      });
      this.device.addEventListener('gattserverdisconnected',
        () => this._onDisconnected());
      const server = await this.device.gatt.connect();
      const service = await server.getPrimaryService(SERVICE_UUID);

      this.commandChar = await service.getCharacteristic(COMMAND_UUID);
      this.responseChar = await service.getCharacteristic(RESPONSE_UUID);
      this.statusChar = await service.getCharacteristic(STATUS_UUID);

      await this.responseChar.startNotifications();
      this.responseChar.addEventListener('characteristicvaluechanged',
        (e) => this._onResponse(e.target.value));

      await this.statusChar.startNotifications();
      this.statusChar.addEventListener('characteristicvaluechanged',
        (e) => this._onStatusValue(e.target.value));

      // First read of an encrypted characteristic triggers OS pairing.
      const initial = await this.statusChar.readValue();
      this._onStatusValue(initial);

      this.onState('connected');
      await this.refreshScan();
    } catch (err) {
      this.onState('disconnected');
      if (err && err.name === 'NotFoundError') return; // user cancelled chooser
      this.onError(this._describe(err));
    }
  }

  disconnect() {
    if (this.device && this.device.gatt.connected) {
      this.device.gatt.disconnect();
    }
  }

  _onDisconnected() {
    this.commandChar = this.responseChar = this.statusChar = null;
    this.reassembler.reset();
    this.onState('disconnected');
  }

  // -- commands -----------------------------------------------------------
  async _send(obj, pending) {
    if (!this.commandChar) { this.onError('Not connected'); return; }
    this.pending = pending || 'command';
    this.reassembler.reset();
    const data = new TextEncoder().encode(JSON.stringify(obj));
    try {
      await this.commandChar.writeValueWithResponse(data);
    } catch (err) {
      this.onError(this._describe(err));
    }
  }

  refreshStatus() { return this._send({ cmd: 'status' }, 'command'); }
  refreshScan() { return this._send({ cmd: 'scan' }, 'scan'); }
  setHostAP(on) {
    return this._send({ cmd: on ? 'hostap_start' : 'hostap_stop' }, 'command')
      .then(() => this._delayedStatus());
  }
  selectNetwork(bssid, ssid, password) {
    const cmd = { cmd: 'select', bssid, ssid };
    if (password) cmd.password = password;
    return this._send(cmd, 'command').then(() => this._delayedStatus());
  }
  unbridge() {
    return this._send({ cmd: 'unbridge' }, 'command').then(() => this._delayedStatus());
  }

  _delayedStatus() {
    setTimeout(() => this.refreshStatus(), 1200);
  }

  // -- incoming -----------------------------------------------------------
  _onStatusValue(view) {
    const obj = this._decode(view);
    if (obj && 'ready' in obj) this.onStatus(obj);
  }

  _onResponse(view) {
    const payload = this.reassembler.feed(view);
    if (!payload) return;
    let obj;
    try {
      obj = JSON.parse(new TextDecoder().decode(payload));
    } catch {
      this.onError('Bad reply from device');
      this.pending = 'none';
      return;
    }
    if (this.pending === 'scan') {
      this.onNetworks((obj && obj.networks) || []);
    } else if (obj && 'ready' in obj) {
      this.onStatus(obj);
    } else if (obj && obj.success === false) {
      this.onError(obj.message || obj.error || 'Command failed');
    }
    this.pending = 'none';
  }

  _decode(view) {
    try { return JSON.parse(new TextDecoder().decode(view)); }
    catch { return null; }
  }

  _describe(err) {
    const msg = (err && err.message) || String(err);
    if (/User denied|GATT operation not permitted|encryption/i.test(msg)) {
      return 'Pairing was declined or failed. Re-pair the device and retry.';
    }
    return msg;
  }
}

// --- UI ------------------------------------------------------------------
const client = new VasiliClient();
const $ = (id) => document.getElementById(id);

let lastStatus = null;

function setState(state) {
  $('connectBtn').hidden = state === 'connected';
  $('disconnectBtn').hidden = state !== 'connected';
  $('panels').hidden = state !== 'connected';
  $('connectBtn').textContent =
    state === 'connecting' ? 'Connecting…' : 'Connect to Vasili';
  $('connectBtn').disabled = state === 'connecting';
  if (state === 'disconnected') {
    lastStatus = null;
    $('networks').innerHTML = '';
  }
}

function renderStatus(s) {
  lastStatus = s;
  $('statusGrid').innerHTML = '';
  const rows = [
    ['Ready', s.ready ? 'Yes' : 'Starting up…'],
    ['Bridged network', s.current_bridge_ssid || '—'],
    ['Networks in range', s.networks_found ?? 0],
    ['Cards in use', s.cards_in_use ?? 0],
    ['Scanning', s.scanning ? 'Yes' : 'No'],
    ['Reconnects', s.reconnect_events ?? 0],
  ];
  if (s.hostap_last_error) rows.push(['HostAP error', s.hostap_last_error]);
  for (const [k, v] of rows) {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `<span class="k">${k}</span><span class="v"></span>`;
    row.querySelector('.v').textContent = String(v);
    $('statusGrid').appendChild(row);
  }
  const toggle = $('hostapToggle');
  toggle.checked = !!s.hostap_active;
  $('hostapLabel').textContent = s.hostap_active
    ? `HostAP on${s.hostap_ssid ? ' · ' + s.hostap_ssid : ''}` : 'HostAP off';
}

function renderNetworks(nets) {
  const list = $('networks');
  list.innerHTML = '';
  const bridged = lastStatus && lastStatus.current_bridge_ssid;
  nets.sort((a, b) => b.signal - a.signal);
  if (nets.length === 0) {
    list.innerHTML = '<li class="muted">No networks yet — refresh.</li>';
    return;
  }
  for (const net of nets) {
    const li = document.createElement('li');
    li.className = 'net';
    const lock = net.is_open ? '🔓' : '🔒';
    const here = bridged && bridged === net.ssid ? ' ✓' : '';
    li.innerHTML =
      `<div class="net-main"><span class="ssid"></span>${here}</div>` +
      `<div class="net-sub"></div>`;
    li.querySelector('.ssid').textContent =
      `${lock} ${net.ssid || '(hidden)'}`;
    li.querySelector('.net-sub').textContent =
      `${net.security} · ch ${net.channel} · ${net.signal} dBm`;
    li.onclick = () => chooseNetwork(net);
    list.appendChild(li);
  }
}

function chooseNetwork(net) {
  if (net.is_open) {
    client.selectNetwork(net.bssid, net.ssid, null);
  } else {
    const pw = window.prompt(`Password for "${net.ssid}"`);
    if (pw === null) return; // cancelled
    client.selectNetwork(net.bssid, net.ssid, pw);
  }
}

function showError(msg) {
  const el = $('error');
  el.textContent = msg;
  el.hidden = !msg;
  if (msg) setTimeout(() => { if (el.textContent === msg) el.hidden = true; }, 6000);
}

// Wire callbacks
client.onState = setState;
client.onStatus = renderStatus;
client.onNetworks = renderNetworks;
client.onError = showError;

// Wire DOM
window.addEventListener('DOMContentLoaded', () => {
  if (!client.supported) {
    $('unsupported').hidden = false;
    $('connectBtn').disabled = true;
  }
  $('connectBtn').onclick = () => { showError(''); client.connect(); };
  $('disconnectBtn').onclick = () => client.disconnect();
  $('refreshStatusBtn').onclick = () => client.refreshStatus();
  $('refreshScanBtn').onclick = () => client.refreshScan();
  $('unbridgeBtn').onclick = () => client.unbridge();
  $('hostapToggle').onchange = (e) => client.setHostAP(e.target.checked);
  setState('disconnected');
});

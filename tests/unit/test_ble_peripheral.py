"""Unit tests for the BLE control interface (ble_peripheral.py).

Covers the pure, hardware-independent pieces — response framing, command
dispatch, graceful degradation when the BlueZ/D-Bus stack is absent — plus the
WifiManager.select_network wrapper that the BLE "select" command drives. The
D-Bus / GATT plumbing is built lazily inside BLEPeripheral._run and is not
exercised here (it needs a real Bluetooth adapter); these tests confirm the
contract everything above it relies on.
"""

import builtins
import json
from unittest.mock import MagicMock

import pytest

import ble_peripheral as ble


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------
def _roundtrip(payload: bytes, frame_size: int) -> bytes:
    r = ble.Reassembler()
    out = None
    for frame in ble.frame_payload(payload, frame_size):
        assert len(frame) <= frame_size
        result = r.feed(frame)
        if result is not None:
            out = result
    return out


def test_frame_roundtrip_large_payload():
    payload = json.dumps(
        {'networks': [{'ssid': f'net{i}', 'bssid': f'aa:bb:cc:dd:ee:{i:02x}'}
                      for i in range(50)]}
    ).encode()
    assert len(payload) > 512  # must exceed the BLE ATT ceiling
    frames = ble.frame_payload(payload, 40)
    assert len(frames) > 1  # genuinely chunked
    assert _roundtrip(payload, 40) == payload


def test_frame_roundtrip_fits_single_frame():
    payload = b'{"ready":true}'
    frames = ble.frame_payload(payload, ble.DEFAULT_FRAME_SIZE)
    assert len(frames) == 1
    assert _roundtrip(payload, ble.DEFAULT_FRAME_SIZE) == payload


def test_frame_rejects_tiny_frame_size():
    with pytest.raises(ValueError):
        ble.frame_payload(b'x', 5)


def test_reassembler_resets_between_messages():
    r = ble.Reassembler()
    first = b'hello world payload one'
    second = b'second'
    for f in ble.frame_payload(first, 10):
        got = r.feed(f)
    assert got == first
    # A fresh START frame must reset state, not append to the previous buffer.
    for f in ble.frame_payload(second, 10):
        got2 = r.feed(f)
    assert got2 == second


# --------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------
def _fake_manager():
    mgr = MagicMock()
    mgr.status = {
        'scanning': True, 'networks_found': 2, 'cards_in_use': 1,
        'hostap_active': False, 'hostap_ssid': None,
        'current_bridge': {'ssid': 'Home'}, 'reconnect_events': 0,
    }
    net = MagicMock(ssid='CoffeeWiFi', bssid='aa:bb:cc:00:11:22',
                    signal_strength=-55, channel=11,
                    encryption_type='WPA2', is_open=False)
    mgr.nearby_networks = [net]
    mgr.get_hostap_status.return_value = {'is_active': False, 'last_error': None}
    mgr.select_network.return_value = {'success': True, 'ssid': 'CoffeeWiFi'}
    mgr.stop_bridge_override.return_value = {'success': True}
    mgr.confirm_hostap.return_value = {'success': True}
    mgr.disable_hostap_lazy.return_value = {'success': True}
    mgr._load_hostap_config.return_value = {}
    return mgr


def test_dispatch_bad_json_returns_bad_request():
    d = ble.CommandDispatcher(lambda: _fake_manager())
    assert json.loads(d.dispatch(b'not json')) == {'error': 'bad_request'}
    # A JSON value that isn't an object is also a bad request.
    assert json.loads(d.dispatch(b'[1,2,3]')) == {'error': 'bad_request'}


def test_dispatch_initializing_when_manager_none():
    d = ble.CommandDispatcher(lambda: None)
    assert json.loads(d.dispatch(b'{"cmd":"status"}')) == {'error': 'initializing'}
    assert json.loads(d.dispatch(b'{"cmd":"scan"}')) == {'error': 'initializing'}
    assert d.handle({'cmd': 'select', 'ssid': 'x'}) == {'error': 'initializing'}


def test_status_summary_shape():
    d = ble.CommandDispatcher(lambda: _fake_manager())
    out = json.loads(d.dispatch(b'{"cmd":"status"}'))
    assert out['ready'] is True
    assert out['scanning'] is True
    assert out['networks_found'] == 2
    assert out['current_bridge_ssid'] == 'Home'
    assert out['hostap_active'] is False


def test_scan_lists_networks():
    d = ble.CommandDispatcher(lambda: _fake_manager())
    out = json.loads(d.dispatch(b'{"cmd":"scan"}'))
    assert out['networks'][0] == {
        'ssid': 'CoffeeWiFi', 'bssid': 'aa:bb:cc:00:11:22', 'signal': -55,
        'channel': 11, 'security': 'WPA2', 'is_open': False,
    }


def test_select_passes_password_through():
    mgr = _fake_manager()
    d = ble.CommandDispatcher(lambda: mgr)
    d.handle({'cmd': 'select', 'bssid': 'b', 'ssid': 'CoffeeWiFi',
              'password': 'hunter2'})
    mgr.select_network.assert_called_once_with('b', 'CoffeeWiFi', 'hunter2')


def test_select_without_password_passes_none():
    mgr = _fake_manager()
    d = ble.CommandDispatcher(lambda: mgr)
    d.handle({'cmd': 'select', 'ssid': 'OpenNet'})
    mgr.select_network.assert_called_once_with('', 'OpenNet', None)


def test_hostap_start_confirms_and_stop_disables():
    mgr = _fake_manager()
    d = ble.CommandDispatcher(lambda: mgr)
    d.handle({'cmd': 'hostap_start'})
    mgr.confirm_hostap.assert_called_once()
    d.handle({'cmd': 'hostap_stop'})
    mgr.disable_hostap_lazy.assert_called_once()


def test_unbridge_calls_stop_bridge_override():
    mgr = _fake_manager()
    d = ble.CommandDispatcher(lambda: mgr)
    assert d.handle({'cmd': 'unbridge'}) == {'success': True}
    mgr.stop_bridge_override.assert_called_once()


def test_unknown_command():
    d = ble.CommandDispatcher(lambda: _fake_manager())
    assert d.handle({'cmd': 'frobnicate'}) == {
        'error': 'unknown_command', 'cmd': 'frobnicate'}


def test_handler_exception_is_caught():
    mgr = _fake_manager()
    mgr.get_hostap_status.side_effect = RuntimeError('boom')
    d = ble.CommandDispatcher(lambda: mgr)
    out = json.loads(d.dispatch(b'{"cmd":"hostap_status"}'))
    assert out['error'] == 'internal_error'


# --------------------------------------------------------------------------
# Graceful degradation (no BlueZ / D-Bus stack)
# --------------------------------------------------------------------------
def test_start_noops_without_dbus(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'dbus' or name.startswith('dbus.') or name == 'gi.repository':
            raise ImportError('no dbus here')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    p = ble.BLEPeripheral(lambda: None, config=None)
    assert p.start() is False
    status = p.get_status()
    assert status['running'] is False
    assert status['available'] is False
    assert 'D-Bus' in (status['last_error'] or '')
    # Push hooks must be safe no-ops when nothing is running.
    p.notify_status()
    p.notify_scan_changed()


def test_get_status_reflects_config():
    cfg = MagicMock(adapter='hci1', device_name='RecoveryPi',
                    require_pairing=False)
    p = ble.BLEPeripheral(lambda: None, config=cfg)
    st = p.get_status()
    assert st['adapter'] == 'hci1'
    assert st['device_name'] == 'RecoveryPi'
    assert st['require_pairing'] is False


# --------------------------------------------------------------------------
# WifiManager.select_network wrapper (the "select" command's target)
# --------------------------------------------------------------------------
def test_select_network_saves_password_then_overrides():
    from vasili import WifiManager
    mgr = WifiManager.__new__(WifiManager)
    mgr.known_networks_store = MagicMock()
    mgr.known_networks_store.add.return_value = True
    mgr.start_bridge_override = MagicMock(
        return_value={'success': True, 'ssid': 'Secure'})

    result = mgr.select_network('aa:bb', 'Secure', 'pw12345678')

    mgr.known_networks_store.add.assert_called_once_with(
        ssid='Secure', password='pw12345678')
    mgr.start_bridge_override.assert_called_once_with(bssid='aa:bb', ssid='Secure')
    assert result['success'] is True


def test_select_network_without_password_skips_store():
    from vasili import WifiManager
    mgr = WifiManager.__new__(WifiManager)
    mgr.known_networks_store = MagicMock()
    mgr.start_bridge_override = MagicMock(return_value={'success': True})

    mgr.select_network('', 'OpenNet', None)

    mgr.known_networks_store.add.assert_not_called()
    mgr.start_bridge_override.assert_called_once_with(bssid='', ssid='OpenNet')


def test_select_network_store_unavailable():
    from vasili import WifiManager
    mgr = WifiManager.__new__(WifiManager)
    mgr.known_networks_store = MagicMock()
    mgr.known_networks_store.add.return_value = False
    mgr.start_bridge_override = MagicMock()

    result = mgr.select_network('', 'Secure', 'pw12345678')

    assert result == {'success': False, 'error': 'store_unavailable'}
    mgr.start_bridge_override.assert_not_called()


def test_select_network_password_requires_ssid():
    from vasili import WifiManager
    mgr = WifiManager.__new__(WifiManager)
    mgr.known_networks_store = MagicMock()
    mgr.start_bridge_override = MagicMock()

    result = mgr.select_network('aa:bb', '', 'pw12345678')

    assert result == {'success': False, 'error': 'ssid_required_for_password'}
    mgr.known_networks_store.add.assert_not_called()

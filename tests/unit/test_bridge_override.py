"""Unit tests for the Bridge Override feature.

Bridge Override force-connects a card to an operator-chosen network and pins
it as the bridged upstream even with no internet, until manually unbridged.
These tests exercise WifiManager.start_bridge_override / stop_bridge_override
and the guards that keep the automatic machinery (auto-selector, reconcile)
from disturbing a pinned override. WifiManager/AutoSelector are built with
__new__ and hand-populated so we don't stand up the whole daemon.
"""

import threading
from unittest.mock import MagicMock

import vasili
from vasili import WifiManager, AutoSelector, ConnectionResult, WifiNetwork


def make_network(**kw):
    d = dict(ssid='Open1', bssid='aa:bb:cc:dd:ee:01', signal_strength=60,
             channel=6, encryption_type='Open', is_open=True)
    d.update(kw)
    return WifiNetwork(**d)


def make_result(net, iface='wlan1', pinned=False, connected=True):
    return ConnectionResult(
        network=net, download_speed=0, upload_speed=0, ping=0,
        connected=connected, connection_method='override',
        interface=iface, pinned=pinned,
    )


def bare_manager():
    mgr = WifiManager.__new__(WifiManager)
    mgr._connections_lock = threading.Lock()
    mgr.suitable_connections = []
    mgr.nearby_networks = []
    mgr._bridge_override_iface = None
    mgr.status = {}
    mgr.active_bridge = None
    mgr._auto_bridge_enabled = False
    mgr.card_manager = MagicMock()
    mgr.connection_monitor = MagicMock()
    mgr.known_networks_store = MagicMock()
    return mgr


def bare_selector(mgr):
    sel = AutoSelector.__new__(AutoSelector)
    sel.wifi_manager = mgr
    sel._evaluation_count = 0
    sel.min_score_improvement = 10.0
    return sel


# --------------------------------------------------------------------------
# ConnectionResult.pinned
# --------------------------------------------------------------------------

def test_connection_result_pinned_defaults_false():
    r = ConnectionResult(make_network(), 0, 0, 0, True, 'x', 'wlan0')
    assert r.pinned is False
    assert make_result(make_network(), pinned=True).pinned is True


# --------------------------------------------------------------------------
# start_bridge_override
# --------------------------------------------------------------------------

def test_start_override_open_network_bridges_and_pins():
    mgr = bare_manager()
    net = make_network(bssid='AA:BB:CC:DD:EE:01')
    mgr.nearby_networks = [net]
    card = MagicMock(interface='wlan1')
    card.connect.return_value = True
    mgr.card_manager.lease_card.return_value = card

    def fake_use(idx):
        mgr.active_bridge = MagicMock(is_active=True, wifi_interface='wlan1')
        return True
    mgr.use_connection = fake_use

    res = mgr.start_bridge_override('AA:BB:CC:DD:EE:01')
    assert res['success'] is True
    assert mgr._bridge_override_iface == 'wlan1'
    assert len(mgr.suitable_connections) == 1
    assert mgr.suitable_connections[0].pinned is True
    # Open network → no password passed to connect().
    assert card.connect.call_args.kwargs.get('password') is None
    mgr.connection_monitor.add_card.assert_called_once_with(card)


def test_start_override_network_not_found():
    mgr = bare_manager()
    assert mgr.start_bridge_override('ZZ:ZZ:ZZ:ZZ:ZZ:ZZ') == {
        'success': False, 'error': 'network_not_found',
    }


def test_start_override_encrypted_without_saved_credential():
    mgr = bare_manager()
    net = make_network(bssid='AA:BB:CC:DD:EE:02', ssid='Sec',
                       encryption_type='WPA2', is_open=False)
    mgr.nearby_networks = [net]
    mgr.known_networks_store.reveal.return_value = None
    res = mgr.start_bridge_override('AA:BB:CC:DD:EE:02')
    assert res['error'] == 'no_saved_credentials'
    mgr.card_manager.lease_card.assert_not_called()


def test_start_override_encrypted_uses_saved_password():
    mgr = bare_manager()
    net = make_network(bssid='AA:BB:CC:DD:EE:03', ssid='Sec',
                       encryption_type='WPA2', is_open=False)
    mgr.nearby_networks = [net]
    mgr.known_networks_store.reveal.return_value = 'hunter2'
    card = MagicMock(interface='wlan1')
    card.connect.return_value = True
    mgr.card_manager.lease_card.return_value = card
    mgr.use_connection = lambda idx: True

    res = mgr.start_bridge_override('AA:BB:CC:DD:EE:03')
    assert res['success'] is True
    assert card.connect.call_args.kwargs.get('password') == 'hunter2'


def test_start_override_no_free_card():
    mgr = bare_manager()
    mgr.nearby_networks = [make_network(bssid='AA:BB:CC:DD:EE:04')]
    mgr.card_manager.lease_card.return_value = None
    assert mgr.start_bridge_override('AA:BB:CC:DD:EE:04')['error'] == 'no_free_card'


def test_start_override_connect_failure_returns_card():
    mgr = bare_manager()
    mgr.nearby_networks = [make_network(bssid='AA:BB:CC:DD:EE:05')]
    card = MagicMock(interface='wlan1')
    card.connect.return_value = False
    mgr.card_manager.lease_card.return_value = card

    res = mgr.start_bridge_override('AA:BB:CC:DD:EE:05')
    assert res['error'] == 'connect_failed'
    mgr.card_manager.return_card.assert_called_once()
    assert mgr._bridge_override_iface is None
    assert mgr.suitable_connections == []


def test_start_override_pins_existing_connection():
    mgr = bare_manager()
    net = make_network(bssid='AA:BB:CC:DD:EE:06')
    mgr.nearby_networks = [net]
    existing = make_result(net, iface='wlan2', pinned=False)
    mgr.suitable_connections = [existing]
    mgr.use_connection = lambda idx: True

    res = mgr.start_bridge_override('AA:BB:CC:DD:EE:06')
    assert res['success'] is True and res.get('reused') is True
    assert existing.pinned is True
    assert mgr._bridge_override_iface == 'wlan2'
    mgr.card_manager.lease_card.assert_not_called()


# --------------------------------------------------------------------------
# stop_bridge_override
# --------------------------------------------------------------------------

def test_stop_bridge_override_returns_card_and_clears():
    mgr = bare_manager()
    net = make_network(bssid='AA:BB:CC:DD:EE:07')
    pinned = make_result(net, iface='wlan1', pinned=True)
    mgr.suitable_connections = [pinned]
    mgr._bridge_override_iface = 'wlan1'
    mgr.active_bridge = MagicMock(is_active=True, wifi_interface='wlan1')
    card = MagicMock(interface='wlan1')
    mgr._get_card_for_interface = lambda iface: card

    res = mgr.stop_bridge_override()
    assert res['success'] is True and res['changed'] is True
    assert mgr._bridge_override_iface is None
    assert mgr.suitable_connections == []
    mgr.card_manager.return_card.assert_called_once()
    card.disconnect.assert_called_once()
    mgr.connection_monitor.remove_card.assert_called_once_with(card)


def test_stop_bridge_override_noop_when_inactive():
    mgr = bare_manager()
    assert mgr.stop_bridge_override() == {'success': True, 'changed': False}


# --------------------------------------------------------------------------
# Auto-selector guards
# --------------------------------------------------------------------------

def test_autoselector_evaluate_skips_when_override_active():
    mgr = bare_manager()
    mgr._bridge_override_iface = 'wlan1'
    mgr.get_sorted_connections = MagicMock(
        side_effect=AssertionError('must not evaluate during override'))
    sel = bare_selector(mgr)
    sel._evaluate_and_switch()  # returns early, no switch
    mgr.get_sorted_connections.assert_not_called()


def test_autoselector_select_best_skips_when_override_active():
    mgr = bare_manager()
    mgr._bridge_override_iface = 'wlan1'
    mgr.get_sorted_connections = MagicMock(
        side_effect=AssertionError('must not select during override'))
    sel = bare_selector(mgr)
    sel._select_best_connection()
    mgr.get_sorted_connections.assert_not_called()


# --------------------------------------------------------------------------
# Reconcile guards
# --------------------------------------------------------------------------

def test_reconcile_block_runs_but_spares_override_bridge(monkeypatch):
    mgr = bare_manager()
    pinned = make_result(make_network(bssid='AA:BB:CC:DD:EE:09', ssid='Pinned'),
                         iface='wlan1', pinned=True)
    stale = make_result(make_network(bssid='AA:BB:CC:DD:EE:10', ssid='Stale'),
                        iface='wlan3', pinned=False)
    mgr.suitable_connections = [pinned, stale]
    mgr._bridge_override_iface = 'wlan1'
    bridge = MagicMock(is_active=True, wifi_interface='wlan1')
    mgr.active_bridge = bridge
    mgr.card_manager.get_all_cards.return_value = []
    # Nothing is associated at the OS level → stale would normally be dropped
    # and a non-override bridge torn down.
    monkeypatch.setattr(vasili, '_get_device_ssid_map', lambda: {})

    mgr._reconcile_suitable_connections()

    assert pinned in mgr.suitable_connections   # pinned survives
    assert stale not in mgr.suitable_connections  # stale dropped as usual
    bridge.stop.assert_not_called()             # override bridge preserved

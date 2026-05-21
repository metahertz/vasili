"""Unit tests for ConnectionMonitor — the drop-handler and D-Bus path."""

import pytest
from unittest.mock import MagicMock, patch
from vasili import ConnectionMonitor, WifiNetwork


def _make_card(iface='wlan0', ssid='Home', connected=True):
    """Build a stand-in WifiCard with only the attributes ConnectionMonitor reads."""
    card = MagicMock()
    card.interface = iface
    card._connected_network = (
        WifiNetwork(
            ssid=ssid, bssid='AA:BB:CC:DD:EE:01', signal_strength=80,
            channel=6, encryption_type='WPA2', is_open=False,
        ) if connected else None
    )
    card._connection_password = 'pw' if connected else None
    card.is_connected.return_value = True
    card.get_connected_ssid.return_value = ssid
    card.reconnect.return_value = True
    return card


@pytest.mark.unit
class TestHandleDrop:
    """The shared drop handler used by both the poll and D-Bus paths."""

    def test_successful_reconnect_resets_counter_and_fires_callback(self):
        mon = ConnectionMonitor(max_reconnect_attempts=3)
        card = _make_card()
        mon.add_card(card)
        cb = MagicMock()
        mon.on_reconnect(cb)

        # First-pass attempt counter starts at 0.
        mon._reconnect_attempts[card.interface] = 1
        card.reconnect.return_value = True
        mon._handle_drop(card)

        card.reconnect.assert_called_once_with(max_retries=2, base_delay=0.5)
        assert mon._reconnect_attempts[card.interface] == 0
        cb.assert_called_once_with(card, True)

    def test_failed_reconnect_increments_counter(self):
        mon = ConnectionMonitor(max_reconnect_attempts=3)
        card = _make_card()
        mon.add_card(card)
        card.reconnect.return_value = False

        mon._handle_drop(card)
        assert mon._reconnect_attempts[card.interface] == 1
        mon._handle_drop(card)
        assert mon._reconnect_attempts[card.interface] == 2

    def test_max_attempts_gives_up_and_clears_network(self):
        mon = ConnectionMonitor(max_reconnect_attempts=2)
        card = _make_card()
        mon.add_card(card)
        cb = MagicMock()
        mon.on_reconnect(cb)
        mon._reconnect_attempts[card.interface] = 2  # already at limit

        mon._handle_drop(card)

        card.reconnect.assert_not_called()
        assert card._connected_network is None
        assert card._connection_password is None
        assert mon._reconnect_attempts[card.interface] == 0
        cb.assert_called_once_with(card, False)

    def test_card_with_no_network_is_noop(self):
        mon = ConnectionMonitor()
        card = _make_card(connected=False)
        mon.add_card(card)
        mon._handle_drop(card)
        card.reconnect.assert_not_called()

    def test_concurrent_drops_for_same_card_serialize(self):
        """A second StateChanged firing while the first handler runs must skip."""
        mon = ConnectionMonitor()
        card = _make_card()
        mon.add_card(card)
        # Manually pre-grab the per-card lock so the next _handle_drop bails.
        lock = mon._handler_locks[card.interface]
        assert lock.acquire(blocking=False)
        try:
            mon._handle_drop(card)
            card.reconnect.assert_not_called()
        finally:
            lock.release()


@pytest.mark.unit
class TestDbusStateChangedDispatch:
    """The GLib-thread signal handler should dispatch on the right transitions only."""

    def test_drop_from_activated_to_disconnected_dispatches(self):
        mon = ConnectionMonitor()
        card = _make_card()
        mon.add_card(card)
        with patch.object(mon, '_resolve_iface_from_path', return_value='wlan0'), \
                patch('threading.Thread') as mock_thread:
            mon._on_device_state_changed(
                new_state=ConnectionMonitor._NM_STATE_DISCONNECTED,
                old_state=ConnectionMonitor._NM_STATE_ACTIVATED,
                reason=0,
                path='/org/freedesktop/NetworkManager/Devices/2',
            )
            mock_thread.assert_called_once()
            assert mock_thread.call_args.kwargs['target'] == mon._handle_drop

    def test_drop_from_activated_to_failed_dispatches(self):
        mon = ConnectionMonitor()
        card = _make_card()
        mon.add_card(card)
        with patch.object(mon, '_resolve_iface_from_path', return_value='wlan0'), \
                patch('threading.Thread') as mock_thread:
            mon._on_device_state_changed(
                new_state=ConnectionMonitor._NM_STATE_FAILED,
                old_state=ConnectionMonitor._NM_STATE_ACTIVATED,
                reason=0,
                path='/org/freedesktop/NetworkManager/Devices/2',
            )
            mock_thread.assert_called_once()

    def test_non_activated_origin_is_ignored(self):
        """E.g. PREPARE → DISCONNECTED isn't a drop — the card never reached ACTIVATED."""
        mon = ConnectionMonitor()
        card = _make_card()
        mon.add_card(card)
        with patch.object(mon, '_resolve_iface_from_path', return_value='wlan0'), \
                patch('threading.Thread') as mock_thread:
            mon._on_device_state_changed(
                new_state=ConnectionMonitor._NM_STATE_DISCONNECTED,
                old_state=40,  # PREPARE
                reason=0,
                path='/org/freedesktop/NetworkManager/Devices/2',
            )
            mock_thread.assert_not_called()

    def test_transition_to_non_dropped_state_is_ignored(self):
        """E.g. ACTIVATED → DEACTIVATING is the start of an orderly teardown."""
        mon = ConnectionMonitor()
        card = _make_card()
        mon.add_card(card)
        with patch.object(mon, '_resolve_iface_from_path', return_value='wlan0'), \
                patch('threading.Thread') as mock_thread:
            mon._on_device_state_changed(
                new_state=110,  # DEACTIVATING
                old_state=ConnectionMonitor._NM_STATE_ACTIVATED,
                reason=0,
                path='/org/freedesktop/NetworkManager/Devices/2',
            )
            mock_thread.assert_not_called()

    def test_unknown_interface_is_ignored(self):
        mon = ConnectionMonitor()
        with patch.object(mon, '_resolve_iface_from_path', return_value='wlan99'), \
                patch('threading.Thread') as mock_thread:
            mon._on_device_state_changed(
                new_state=ConnectionMonitor._NM_STATE_DISCONNECTED,
                old_state=ConnectionMonitor._NM_STATE_ACTIVATED,
                reason=0,
                path='/org/freedesktop/NetworkManager/Devices/9',
            )
            mock_thread.assert_not_called()


@pytest.mark.unit
class TestStartFallback:
    """If D-Bus init fails, start() must fall back to the poll worker."""

    def test_falls_back_to_polling_when_dbus_unavailable(self):
        mon = ConnectionMonitor(check_interval=0.01)
        with patch.object(mon, '_try_start_dbus', return_value=False), \
                patch('threading.Thread') as mock_thread:
            mon.start()
            assert mon._using_dbus is False
            mock_thread.assert_called_once()
            assert mock_thread.call_args.kwargs['target'] == mon._poll_worker
        mon._monitoring = False  # don't actually run the thread

    def test_uses_dbus_when_init_succeeds(self):
        mon = ConnectionMonitor()
        with patch.object(mon, '_try_start_dbus', return_value=True):
            mon.start()
            assert mon._using_dbus is True
            assert mon._monitor_thread is None  # poll worker not started
        mon._monitoring = False

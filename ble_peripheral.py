"""BLE GATT control interface for Vasili — out-of-band recovery over Bluetooth.

The Raspberry Pi 5's built-in HostAP WiFi network often fails to start at boot.
When it does, and no wired Ethernet is plugged in, the device is headless and
the Flask UI on ``0.0.0.0:5000`` is unreachable. This module exposes a small
BLE GATT peripheral so a phone (see the companion app in ``ios/``) can still:

* read basic device **status** (init state, current bridge, cards, HostAP),
* list the WiFi **networks in range** (live scan results),
* turn **HostAP on/off**,
* **directly choose a network** to bridge, provisioning a password if needed.

Design notes
------------
* **Starts early and survives a stalled init.** The peripheral is started in
  its own daemon thread from ``main()`` *before/alongside* the slow
  ``_init_app()``, so it keeps advertising even if WiFi-card enumeration or
  HostAP bring-up hangs — which is the exact failure this exists to recover
  from. Every call into the WiFi stack goes through a *getter* that may return
  ``None`` (not-yet-ready), in which case commands answer ``{"error":
  "initializing"}`` instead of throwing.

* **Graceful degradation.** All BlueZ / D-Bus imports are lazy and wrapped; on
  a dev box or in CI without ``dbus-python`` / a Bluetooth adapter, ``start()``
  logs and no-ops rather than crashing — matching the house rule used by
  ``ConnectionMonitor`` (Mongo/Playwright/D-Bus are all optional).

* **House D-Bus pattern.** BlueZ's GATT API *is* a system-bus D-Bus interface,
  so this mirrors ``ConnectionMonitor._try_start_dbus``: ``DBusGMainLoop`` +
  ``dbus.SystemBus()`` + a ``GLib.MainLoop()`` on a daemon thread.

GATT layout
-----------
One vendor service with three characteristics:

* **Status** (read, notify) — a small JSON summary, pushed on change.
* **Command** (write) — the phone writes a small JSON command.
* **Response** (notify) — the reply to the last command, streamed as
  length-prefixed frames so payloads larger than the BLE ATT MTU (scan lists,
  full status) transfer reliably. See :func:`frame_payload`.

The pure logic (framing + command dispatch) lives at module scope with no
D-Bus dependency so it is unit-testable without a Bluetooth stack; the D-Bus
object plumbing is built lazily inside :meth:`BLEPeripheral._run`.
"""

import json
import struct
import threading
from typing import Callable, List, Optional

from logging_config import get_logger

logger = get_logger('ble_peripheral')


# ---------------------------------------------------------------------------
# UUIDs — keep in sync with ios/VasiliBLE/BLEManager.swift (single source of
# truth for the wire contract). Vendor-random 128-bit base.
# ---------------------------------------------------------------------------
VASILI_SERVICE_UUID = '56415349-4c49-0000-0000-000000000001'
STATUS_CHAR_UUID = '56415349-4c49-0000-0000-000000000002'
COMMAND_CHAR_UUID = '56415349-4c49-0000-0000-000000000003'
RESPONSE_CHAR_UUID = '56415349-4c49-0000-0000-000000000004'

# Conservative frame size for notify payloads. iOS negotiates an ATT MTU of at
# least 185 in practice (usable notify length MTU-3); 160 leaves headroom so we
# never emit a value BlueZ would silently truncate. Larger MTUs just mean fewer
# frames, never corruption.
DEFAULT_FRAME_SIZE = 160

# Response framing flags (first byte of every frame).
_FRAME_START = 0x01  # [0x01][4-byte big-endian total len][data...]
_FRAME_CONT = 0x00   # [0x00][data...]


# ---------------------------------------------------------------------------
# Framing — pure, testable, mirrored by the iOS client.
# ---------------------------------------------------------------------------
def frame_payload(payload: bytes, frame_size: int = DEFAULT_FRAME_SIZE) -> List[bytes]:
    """Split ``payload`` into self-describing frames for the Response char.

    The first frame is ``[0x01][uint32 total length][data…]``; each subsequent
    frame is ``[0x00][data…]``. The receiver resets its buffer on a START
    frame, learns the total length, then appends CONT frames until it has the
    whole payload. This makes reassembly unambiguous regardless of how BlueZ /
    the central batches notifications.
    """
    if frame_size < 6:
        raise ValueError('frame_size must be at least 6 bytes')
    total = len(payload)
    header = bytes([_FRAME_START]) + struct.pack('>I', total)
    first_data = frame_size - len(header)
    frames = [header + payload[:first_data]]
    offset = first_data
    body = frame_size - 1
    while offset < total:
        chunk = payload[offset:offset + body]
        frames.append(bytes([_FRAME_CONT]) + chunk)
        offset += len(chunk)
    return frames


class Reassembler:
    """Reassemble :func:`frame_payload` output. Used by tests (the iOS client
    re-implements the same logic in Swift)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buf = b''
        self._total: Optional[int] = None

    def feed(self, frame: bytes) -> Optional[bytes]:
        """Feed one frame; return the full payload once complete, else None."""
        if not frame:
            return None
        flag = frame[0]
        if flag == _FRAME_START:
            self._total = struct.unpack('>I', frame[1:5])[0]
            self._buf = frame[5:]
        else:
            self._buf += frame[1:]
        if self._total is not None and len(self._buf) >= self._total:
            payload = self._buf[:self._total]
            self.reset()
            return payload
        return None


# ---------------------------------------------------------------------------
# Command dispatch — pure, testable, no D-Bus dependency.
# ---------------------------------------------------------------------------
class CommandDispatcher:
    """Map a JSON command to a ``WifiManager`` call and JSON-encode the reply.

    Holds a *getter* rather than the manager itself so the BLE service can be
    constructed and start advertising before ``wifi_manager`` exists; until it
    does, every command resolves to ``{"error": "initializing"}``.
    """

    def __init__(self, manager_getter: Callable[[], object]) -> None:
        self._get = manager_getter

    # -- public API ------------------------------------------------------
    def dispatch(self, raw: bytes) -> bytes:
        """Parse, handle, and JSON-encode — never raises."""
        try:
            cmd = json.loads(bytes(raw).decode('utf-8'))
            if not isinstance(cmd, dict):
                raise ValueError('command must be a JSON object')
        except Exception as e:
            logger.warning(f'BLE: bad command payload: {e}')
            return self._encode({'error': 'bad_request'})
        try:
            return self._encode(self.handle(cmd))
        except Exception as e:  # never let a handler kill the GATT thread
            logger.error(f'BLE: command {cmd.get("cmd")!r} failed: {e}')
            return self._encode({'error': 'internal_error', 'detail': str(e)})

    def handle(self, cmd: dict) -> dict:
        mgr = self._get()
        if mgr is None:
            return {'error': 'initializing'}
        name = cmd.get('cmd')
        if name == 'status':
            return self.status_summary(mgr)
        if name == 'scan':
            return {'networks': self._scan(mgr)}
        if name == 'hostap_status':
            return mgr.get_hostap_status()
        if name == 'hostap_start':
            # Confirm + start, persisting "enabled" so the AP also survives a
            # reboot — the recovery use case is "make HostAP come up and stay
            # up". Merge saved config over the static defaults.
            return mgr.confirm_hostap(self._hostap_conf(mgr))
        if name == 'hostap_stop':
            return mgr.disable_hostap_lazy()
        if name == 'select':
            return mgr.select_network(
                cmd.get('bssid', '') or '',
                cmd.get('ssid', '') or '',
                cmd.get('password') or None,
            )
        if name == 'unbridge':
            return mgr.stop_bridge_override()
        return {'error': 'unknown_command', 'cmd': name}

    # -- status summary (also used for the Status characteristic value) --
    def status_summary(self, mgr=None) -> dict:
        """Compact, push-friendly device-status summary."""
        mgr = mgr if mgr is not None else self._get()
        if mgr is None:
            return {'ready': False}
        st = dict(getattr(mgr, 'status', {}) or {})
        try:
            hostap = mgr.get_hostap_status()
        except Exception:
            hostap = {}
        bridge = st.get('current_bridge') or {}
        return {
            'ready': True,
            'scanning': st.get('scanning', False),
            'networks_found': st.get('networks_found', 0),
            'cards_in_use': st.get('cards_in_use', 0),
            'current_bridge_ssid': (bridge.get('ssid') if isinstance(bridge, dict)
                                    else None),
            'hostap_active': bool(st.get('hostap_active')),
            'hostap_ssid': st.get('hostap_ssid'),
            'hostap_last_error': hostap.get('last_error'),
            'reconnect_events': st.get('reconnect_events', 0),
        }

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _scan(mgr) -> list:
        out = []
        for net in getattr(mgr, 'nearby_networks', []) or []:
            out.append({
                'ssid': net.ssid,
                'bssid': net.bssid,
                'signal': net.signal_strength,
                'channel': net.channel,
                'security': net.encryption_type,
                'is_open': net.is_open,
            })
        return out

    @staticmethod
    def _hostap_conf(mgr) -> dict:
        """Saved HostAP config merged over config defaults."""
        conf = {}
        try:
            from config import get_config
            ha = get_config().hostap
            conf = {
                'ssid': ha.ssid, 'security': ha.security,
                'password': ha.password, 'channel': ha.channel,
            }
            if ha.interface:
                conf['interface'] = ha.interface
        except Exception:
            pass
        try:
            conf.update(mgr._load_hostap_config() or {})
        except Exception:
            pass
        return conf

    @staticmethod
    def _encode(obj: dict) -> bytes:
        return json.dumps(obj, separators=(',', ':')).encode('utf-8')


# ---------------------------------------------------------------------------
# BLE peripheral — D-Bus / BlueZ plumbing (lazy, optional).
# ---------------------------------------------------------------------------
class BLEPeripheral:
    """Background service that advertises the Vasili GATT control interface.

    Mirrors the other long-lived services (``ConnectionMonitor``,
    ``AutoSelector``): ``start()`` spawns a daemon thread running a GLib main
    loop; ``stop()`` quits it; ``get_status()`` reports liveness.
    """

    def __init__(self, manager_getter: Callable[[], object], config=None) -> None:
        self._dispatcher = CommandDispatcher(manager_getter)
        cfg = config
        self.adapter = getattr(cfg, 'adapter', 'hci0')
        self.device_name = getattr(cfg, 'device_name', 'Vasili')
        self.require_pairing = getattr(cfg, 'require_pairing', True)

        self._thread: Optional[threading.Thread] = None
        self._loop = None              # GLib.MainLoop, set in _run
        self._response_char = None     # ResponseCharacteristic, set in _run
        self._status_char = None       # StatusCharacteristic, set in _run
        self._running = False
        self._available = False        # True once BlueZ registration succeeds
        self._last_error: Optional[str] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        """Start advertising in a daemon thread. Returns False (and no-ops) if
        the BlueZ/D-Bus stack is unavailable — never raises."""
        if self._running:
            return self._available
        # Probe the optional stack up front so a missing dependency is a clean
        # log line, not a thread that dies on import.
        try:
            import dbus  # noqa: F401
            import dbus.mainloop.glib  # noqa: F401
            from gi.repository import GLib  # noqa: F401
        except Exception as e:
            self._last_error = f'BLE unavailable (D-Bus/GLib not importable): {e}'
            logger.warning(self._last_error + ' — BLE control interface disabled')
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run, name='ble-gatt', daemon=True,
        )
        self._thread.start()
        logger.info('BLE control interface starting on adapter %s', self.adapter)
        return True

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def get_status(self) -> dict:
        return {
            'running': self._running,
            'available': self._available,
            'adapter': self.adapter,
            'device_name': self.device_name,
            'require_pairing': self.require_pairing,
            'last_error': self._last_error,
        }

    # -- push notifications (called from vasili.py emit helpers) ---------
    def notify_status(self) -> None:
        """Push a fresh Status value to subscribed clients (best-effort)."""
        char = self._status_char
        loop = self._loop
        if char is None or loop is None or not self._available:
            return
        try:
            from gi.repository import GLib
            # Hop onto the GLib thread — D-Bus emits must happen there.
            GLib.idle_add(char.refresh_value)
        except Exception:
            pass

    def notify_scan_changed(self) -> None:
        # Scan results ride the Status summary (networks_found) for the cheap
        # push; the full list is fetched on demand via the "scan" command.
        self.notify_status()

    # -- GLib thread -----------------------------------------------------
    def _run(self) -> None:
        """Build the GATT app + advertisement and run the main loop.

        All BlueZ-dependent classes are defined here, after a confirmed import,
        so importing this module never requires dbus/gi to be installed.
        """
        try:
            import dbus
            import dbus.exceptions
            import dbus.mainloop.glib
            import dbus.service
            from gi.repository import GLib
        except Exception as e:  # already probed in start(), but be safe
            self._last_error = f'BLE D-Bus import failed: {e}'
            logger.error(self._last_error)
            self._running = False
            return

        BLUEZ = 'org.bluez'
        DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
        DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
        GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
        GATT_SERVICE_IFACE = 'org.bluez.GattService1'
        GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
        LE_ADV_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
        LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'
        AGENT_MANAGER_IFACE = 'org.bluez.AgentManager1'
        AGENT_IFACE = 'org.bluez.Agent1'

        dispatcher = self._dispatcher
        require_pairing = self.require_pairing
        frame_size = DEFAULT_FRAME_SIZE
        device_name = self.device_name

        read_flag = 'encrypt-read' if require_pairing else 'read'
        write_flag = 'encrypt-write' if require_pairing else 'write'

        # --- generic GATT base classes (canonical BlueZ example shape) ---
        class Application(dbus.service.Object):
            PATH = '/org/vasili/ble'

            def __init__(self, bus):
                self._services = []
                dbus.service.Object.__init__(self, bus, self.PATH)

            def add_service(self, service):
                self._services.append(service)

            @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
            def GetManagedObjects(self):
                resp = {}
                for service in self._services:
                    resp[service.get_path()] = service.get_properties()
                    for chrc in service.characteristics:
                        resp[chrc.get_path()] = chrc.get_properties()
                return resp

        class Service(dbus.service.Object):
            def __init__(self, bus, index, uuid, primary):
                self.path = f'{Application.PATH}/service{index}'
                self.uuid = uuid
                self.primary = primary
                self.characteristics = []
                dbus.service.Object.__init__(self, bus, self.path)

            def get_path(self):
                return dbus.ObjectPath(self.path)

            def add_characteristic(self, chrc):
                self.characteristics.append(chrc)

            def get_properties(self):
                return {GATT_SERVICE_IFACE: {
                    'UUID': self.uuid,
                    'Primary': self.primary,
                    'Characteristics': dbus.Array(
                        [c.get_path() for c in self.characteristics],
                        signature='o'),
                }}

        class Characteristic(dbus.service.Object):
            def __init__(self, bus, index, uuid, flags, service):
                self.path = f'{service.path}/char{index}'
                self.uuid = uuid
                self.flags = flags
                self.service = service
                self.notifying = False
                self.value = []
                dbus.service.Object.__init__(self, bus, self.path)

            def get_path(self):
                return dbus.ObjectPath(self.path)

            def get_properties(self):
                return {GATT_CHRC_IFACE: {
                    'Service': self.service.get_path(),
                    'UUID': self.uuid,
                    'Flags': dbus.Array(self.flags, signature='s'),
                }}

            @dbus.service.method(DBUS_PROP_IFACE, in_signature='s',
                                 out_signature='a{sv}')
            def GetAll(self, interface):
                if interface != GATT_CHRC_IFACE:
                    raise dbus.exceptions.DBusException(
                        'org.bluez.Error.InvalidArguments')
                return self.get_properties()[GATT_CHRC_IFACE]

            @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}',
                                 out_signature='ay')
            def ReadValue(self, options):
                return self.value

            @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
            def WriteValue(self, value, options):
                raise dbus.exceptions.DBusException(
                    'org.bluez.Error.NotSupported')

            @dbus.service.method(GATT_CHRC_IFACE)
            def StartNotify(self):
                self.notifying = True

            @dbus.service.method(GATT_CHRC_IFACE)
            def StopNotify(self):
                self.notifying = False

            @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
            def PropertiesChanged(self, interface, changed, invalidated):
                pass

            def _emit_value(self, data: bytes):
                arr = dbus.Array([dbus.Byte(b) for b in data], signature='y')
                self.value = arr
                if self.notifying:
                    self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': arr}, [])

        # --- the three Vasili characteristics ---------------------------
        class StatusCharacteristic(Characteristic):
            def __init__(self, bus, index, service):
                flags = [read_flag, 'notify']
                super().__init__(bus, index, STATUS_CHAR_UUID, flags, service)
                self.refresh_value()

            def refresh_value(self):
                payload = CommandDispatcher._encode(dispatcher.status_summary())
                # Status stays small by design; if it ever exceeds the MTU it
                # is simply truncated for the cheap-poll value (the full status
                # is always available via the "status" command).
                self._emit_value(payload[:frame_size])
                return False  # so GLib.idle_add does not reschedule

            def ReadValue(self, options):
                self.refresh_value()
                return self.value

        class ResponseCharacteristic(Characteristic):
            def __init__(self, bus, index, service):
                flags = [read_flag, 'notify']
                super().__init__(bus, index, RESPONSE_CHAR_UUID, flags, service)

            def send(self, payload: bytes):
                """Stream a payload as framed notifications."""
                for frame in frame_payload(payload, frame_size):
                    self._emit_value(bytes(frame))

        class CommandCharacteristic(Characteristic):
            def __init__(self, bus, index, service, response_char):
                flags = [write_flag]
                super().__init__(bus, index, COMMAND_CHAR_UUID, flags, service)
                self._response = response_char

            def WriteValue(self, value, options):
                raw = bytes(bytearray(value))
                reply = dispatcher.dispatch(raw)
                self._response.send(reply)

        class VasiliService(Service):
            def __init__(self, bus, index):
                super().__init__(bus, index, VASILI_SERVICE_UUID, True)
                status = StatusCharacteristic(bus, 0, self)
                response = ResponseCharacteristic(bus, 1, self)
                command = CommandCharacteristic(bus, 2, self, response)
                self.add_characteristic(status)
                self.add_characteristic(response)
                self.add_characteristic(command)
                self.status_char = status
                self.response_char = response

        # --- advertisement ----------------------------------------------
        class Advertisement(dbus.service.Object):
            PATH = '/org/vasili/ble/adv0'

            def __init__(self, bus):
                self.bus = bus
                dbus.service.Object.__init__(self, bus, self.PATH)

            def get_properties(self):
                return {LE_ADVERTISEMENT_IFACE: {
                    'Type': 'peripheral',
                    'ServiceUUIDs': dbus.Array([VASILI_SERVICE_UUID],
                                               signature='s'),
                    'LocalName': dbus.String(device_name),
                    'Includes': dbus.Array(['tx-power'], signature='s'),
                }}

            @dbus.service.method(DBUS_PROP_IFACE, in_signature='s',
                                 out_signature='a{sv}')
            def GetAll(self, interface):
                if interface != LE_ADVERTISEMENT_IFACE:
                    raise dbus.exceptions.DBusException(
                        'org.bluez.Error.InvalidArguments')
                return self.get_properties()[LE_ADVERTISEMENT_IFACE]

            @dbus.service.method(LE_ADVERTISEMENT_IFACE)
            def Release(self):
                logger.debug('BLE advertisement released')

        # --- Just Works pairing agent -----------------------------------
        class Agent(dbus.service.Object):
            PATH = '/org/vasili/ble/agent'

            @dbus.service.method(AGENT_IFACE)
            def Release(self):
                pass

            @dbus.service.method(AGENT_IFACE, in_signature='os')
            def AuthorizeService(self, device, uuid):
                return  # accept

            @dbus.service.method(AGENT_IFACE, in_signature='o',
                                 out_signature='s')
            def RequestPinCode(self, device):
                return '0000'

            @dbus.service.method(AGENT_IFACE, in_signature='o',
                                 out_signature='u')
            def RequestPasskey(self, device):
                return dbus.UInt32(0)

            @dbus.service.method(AGENT_IFACE, in_signature='ouq')
            def DisplayPasskey(self, device, passkey, entered):
                pass

            @dbus.service.method(AGENT_IFACE, in_signature='os')
            def DisplayPinCode(self, device, pincode):
                pass

            @dbus.service.method(AGENT_IFACE, in_signature='ou')
            def RequestConfirmation(self, device, passkey):
                return  # Just Works: auto-confirm

            @dbus.service.method(AGENT_IFACE, in_signature='o')
            def RequestAuthorization(self, device):
                return  # auto-accept

            @dbus.service.method(AGENT_IFACE)
            def Cancel(self):
                pass

        # --- bring it all up --------------------------------------------
        def find_adapter_path(bus):
            om = dbus.Interface(bus.get_object(BLUEZ, '/'), DBUS_OM_IFACE)
            for path, ifaces in om.GetManagedObjects().items():
                if GATT_MANAGER_IFACE in ifaces and LE_ADV_MANAGER_IFACE in ifaces:
                    if path.split('/')[-1] == self.adapter or self.adapter in path:
                        return path
            # Fall back to the first capable adapter.
            for path, ifaces in om.GetManagedObjects().items():
                if GATT_MANAGER_IFACE in ifaces and LE_ADV_MANAGER_IFACE in ifaces:
                    return path
            return None

        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            adapter_path = find_adapter_path(bus)
            if not adapter_path:
                raise RuntimeError('no BlueZ adapter with GATT + advertising support')

            adapter_props = dbus.Interface(
                bus.get_object(BLUEZ, adapter_path), DBUS_PROP_IFACE)
            adapter_props.Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(True))
            if require_pairing:
                adapter_props.Set('org.bluez.Adapter1', 'Pairable',
                                  dbus.Boolean(True))

            # Register the Just Works agent as default.
            agent = Agent(bus, Agent.PATH)
            try:
                agent_mgr = dbus.Interface(
                    bus.get_object(BLUEZ, '/org/bluez'), AGENT_MANAGER_IFACE)
                agent_mgr.RegisterAgent(Agent.PATH, 'NoInputNoOutput')
                agent_mgr.RequestDefaultAgent(Agent.PATH)
            except Exception as e:
                logger.warning(f'BLE: could not register pairing agent: {e}')

            # Build + register the GATT application.
            app = Application(bus)
            service = VasiliService(bus, 0)
            app.add_service(service)
            self._status_char = service.status_char
            self._response_char = service.response_char

            gatt_mgr = dbus.Interface(
                bus.get_object(BLUEZ, adapter_path), GATT_MANAGER_IFACE)
            adv_mgr = dbus.Interface(
                bus.get_object(BLUEZ, adapter_path), LE_ADV_MANAGER_IFACE)
            advert = Advertisement(bus)

            reg_state = {'gatt': False, 'adv': False}

            def _on_gatt_ok():
                reg_state['gatt'] = True
                logger.info('BLE GATT application registered')

            def _on_adv_ok():
                reg_state['adv'] = True
                logger.info('BLE advertisement registered as "%s"', device_name)

            def _on_err(label):
                def cb(error):
                    self._last_error = f'{label}: {error}'
                    logger.error('BLE %s failed: %s', label, error)
                return cb

            gatt_mgr.RegisterApplication(
                app.PATH, {},
                reply_handler=_on_gatt_ok,
                error_handler=_on_err('GATT registration'))
            adv_mgr.RegisterAdvertisement(
                advert.PATH, {},
                reply_handler=_on_adv_ok,
                error_handler=_on_err('advertisement registration'))

            self._available = True
            self._last_error = None
            self._loop = GLib.MainLoop()
            self._loop.run()

            # Loop exited (stop() called) — best-effort cleanup.
            try:
                adv_mgr.UnregisterAdvertisement(advert.PATH)
                gatt_mgr.UnregisterApplication(app.PATH)
            except Exception:
                pass
        except Exception as e:
            self._available = False
            self._last_error = f'BLE bring-up failed: {e}'
            logger.error(self._last_error)
        finally:
            self._running = False
            self._available = False

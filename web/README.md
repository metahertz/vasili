# VasiliBLE — Web Bluetooth client

A desktop browser client for the Vasili BLE control interface
(`../ble_peripheral.py`, see `../docs/BLE.md`). Same capabilities as the iOS
app — view status, list networks, toggle HostAP, choose a network (with
password) — but runs in a browser, so it needs no Xcode, no Apple account, and
no app install. Handy when the iOS build path is blocked.

## Browser support

Web Bluetooth works in **desktop Chrome, Edge, and Opera** (Chromium-based) on
macOS, Windows, Linux, and ChromeOS. It does **not** work in **Safari** or
**Firefox** (they don't implement the API).

## Run it

Web Bluetooth only runs in a *secure context* — `https://` or
`http://localhost`. `file://` does **not** work. The simplest path is to serve
the folder locally:

```bash
cd web
python3 -m http.server 8000
```

Then open **http://localhost:8000** in Chrome/Edge/Opera.

> Note: serving this from the Pi itself (e.g. `http://<pi>:5000`) would *not*
> work — plain `http://` to a non-localhost host isn't a secure context, and in
> the recovery scenario the Pi's web server is unreachable anyway. Serve it from
> your own machine.

## Use it

1. Make sure the Pi is running with BLE up
   (`journalctl -u vasili | grep -i ble` shows the advertisement registered).
2. Click **Connect to Vasili**. The browser shows a device chooser — pick
   **Vasili**.
3. The first time you connect, your OS performs Bluetooth **pairing**
   automatically (the page reads an encrypted characteristic, which triggers it
   — accept any system prompt). On Linux you may need to have paired once via
   `bluetoothctl`; on Windows/macOS Chrome handles it inline.
4. Use the **Status** panel (HostAP toggle) and **Networks** panel (tap to
   bridge; encrypted networks prompt for a password).

## How it maps to the device

Identical command set to the iOS app — see the table in `../ios/README.md`.
Replies on the `Response` characteristic are reassembled by the `Reassembler`
class in `app.js`, the JS mirror of `frame_payload` / `Reassembler` in
`../ble_peripheral.py`.

## Files

- `index.html` — markup.
- `app.js` — Web Bluetooth client (`VasiliClient`), framing, and UI glue.
  **Keep the UUIDs in sync with `../ble_peripheral.py`.**
- `style.css` — styling.

## Troubleshooting

- **"Web Bluetooth is not available"** — you're in Safari/Firefox, or not on a
  secure origin. Use Chrome/Edge/Opera over `localhost` or `https`.
- **Device chooser is empty** — the Pi isn't advertising (check the vasili
  service/logs), or Bluetooth is off on your computer.
- **Pairing / "GATT operation not permitted"** — pairing was declined or
  failed. Remove the device from your OS Bluetooth settings and reconnect. As a
  last resort on a trusted network you can set `ble.require_pairing: false` in
  the Pi's `config.yaml` to drop the encryption requirement.

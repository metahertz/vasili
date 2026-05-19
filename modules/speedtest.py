"""Speedtest action — runs *after* the connection modules.

This is deliberately NOT a ``ConnectionModule``. A standalone speedtest
module is unsafe as a connection module: its speedtest follows the
device's default route, so any other internet connection on the device
(e.g. a USB-C / ethernet NIC) makes it report success for a WiFi network
it never actually joined — a false positive against every network the
real modules can't handle.

Demoted to a post-connection action, it instead binds the speedtest to
the interface the connecting module actually used. The speed measured
then reflects that connection and nothing else.

The module loader only instantiates ``ConnectionModule`` subclasses, so
this class is ignored by module discovery — ``WifiManager`` wires it in
explicitly as an action.
"""

from logging_config import get_logger
from vasili import ConnectionResult, run_interface_speedtest

logger = get_logger(__name__)


class SpeedtestAction:
    """Post-connection action: measure speed on the connected interface.

    Invoked by ``WifiManager`` after a connection module returns a
    successful ``ConnectionResult``. The speedtest is bound to
    ``result.interface``; if that interface has no IP or no real
    internet, the speedtest fails loudly rather than reporting a false
    positive from another connection on the device.
    """

    name = 'speedtest'

    def __init__(self, card_manager):
        self.card_manager = card_manager

    def run(self, result: ConnectionResult) -> ConnectionResult:
        """Enrich a successful ConnectionResult with measured speed.

        Only runs when the connecting module did not already produce
        speed metrics. Returns the same result; on failure the result is
        left untouched and a warning is logged.
        """
        if not result.connected or not result.interface:
            return result

        # Modules such as the open-network and pipeline handlers already
        # run an interface-bound speedtest. Don't re-test in that case.
        if result.download_speed > 0 or result.upload_speed > 0:
            return result

        try:
            download, upload, ping = run_interface_speedtest(result.interface)
            result.download_speed = download
            result.upload_speed = upload
            result.ping = ping
            logger.info(
                f'Speedtest action on {result.interface}: '
                f'{download:.1f}/{upload:.1f} Mbps, {ping:.0f} ms'
            )
        except Exception as e:
            logger.warning(
                f'Speedtest action failed on {result.interface}: {e}'
            )

        return result

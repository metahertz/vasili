import logging

import speedtest

from vasili import ConnectionModule, WifiNetwork, ConnectionResult

logger = logging.getLogger(__name__)


class SpeedtestModule(ConnectionModule):
    def __init__(self, card_manager):
        super().__init__(card_manager)
        self.speedtest = speedtest.Speedtest()

    def can_connect(self, network: WifiNetwork) -> bool:
        # This module can test any connected network
        return True

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        try:
            # Get a wifi card
            card = self.card_manager.get_card()
            if not card:
                logger.error('No wifi cards available')
                return ConnectionResult(
                    network=network,
                    download_speed=0,
                    upload_speed=0,
                    ping=0,
                    connected=False,
                    connection_method='speedtest',
                    interface='',
                )

            # Run speedtest
            self.speedtest.get_best_server()
            download_speed = self.speedtest.download() / 1_000_000  # Convert to Mbps
            upload_speed = self.speedtest.upload() / 1_000_000  # Convert to Mbps
            ping = self.speedtest.results.ping

            return ConnectionResult(
                network=network,
                download_speed=download_speed,
                upload_speed=upload_speed,
                ping=ping,
                connected=True,
                connection_method='speedtest',
                interface=card.interface,
            )

        except Exception as e:
            logger.error(f'Speedtest failed: {e}')
            return ConnectionResult(
                network=network,
                download_speed=0,
                upload_speed=0,
                ping=0,
                connected=False,
                connection_method='speedtest',
                interface='',
            )

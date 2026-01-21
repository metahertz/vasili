from typing import Optional

import speedtest

from logging_config import get_logger
from vasili import ConnectionModule, WifiNetwork, ConnectionResult

logger = get_logger(__name__)


class OpenNetworkModule(ConnectionModule):
    def __init__(self, card_manager):
        super().__init__(card_manager)

    def can_connect(self, network: WifiNetwork) -> bool:
        # This module can only connect to open networks
        return network.is_open

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
                    connection_method='open',
                    interface='',
                )

            # Connect to the network
            card.connect(network)

            # Run speedtest to verify connection
            st = speedtest.Speedtest()
            st.get_best_server()
            download_speed = st.download() / 1_000_000  # Convert to Mbps
            upload_speed = st.upload() / 1_000_000  # Convert to Mbps
            ping = st.results.ping

            return ConnectionResult(
                network=network,
                download_speed=download_speed,
                upload_speed=upload_speed,
                ping=ping,
                connected=True,
                connection_method='open',
                interface=card.interface,
            )

        except Exception as e:
            logger.error(f'Failed to connect to open network: {e}')
            return ConnectionResult(
                network=network,
                download_speed=0,
                upload_speed=0,
                ping=0,
                connected=False,
                connection_method='open',
                interface='',
            )

import logging
from typing import Optional

import speedtest

from vasili import ConnectionModule, WifiNetwork, ConnectionResult

logger = logging.getLogger(__name__)

class WPA2NetworkModule(ConnectionModule):
    def __init__(self, card_manager):
        super().__init__(card_manager)

    def can_connect(self, network: WifiNetwork) -> bool:
        # This module can connect to WPA2 encrypted networks
        return network.encryption_type == "WPA2"

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        try:
            # Get a wifi card
            card = self.card_manager.get_card()
            if not card:
                logger.error("No wifi cards available")
                return ConnectionResult(
                    network=network,
                    download_speed=0,
                    upload_speed=0,
                    ping=0,
                    connected=False,
                    connection_method="wpa2",
                    interface=""
                )

            # Connect to the network
            card.connect(network)

            # Run speedtest to verify connection
            speedtest_client = speedtest.Speedtest()
            speedtest_client.get_best_server()
            download_speed = speedtest_client.download() / 1_000_000  # Convert to Mbps
            upload_speed = speedtest_client.upload() / 1_000_000  # Convert to Mbps
            ping = speedtest_client.results.ping

            return ConnectionResult(
                network=network,
                download_speed=download_speed,
                upload_speed=upload_speed,
                ping=ping,
                connected=True,
                connection_method="wpa2",
                interface=card.interface
            )

        except Exception as e:
            logger.error(f"Failed to connect to WPA2 network: {e}")
            return ConnectionResult(
                network=network,
                download_speed=0,
                upload_speed=0,
                ping=0,
                connected=False,
                connection_method="wpa2",
                interface=""
            )

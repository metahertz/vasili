from logging_config import get_logger
from vasili import ConnectionModule, WifiNetwork, ConnectionResult

logger = get_logger(__name__)


class WPA2NetworkModule(ConnectionModule):
    def __init__(self, card_manager):
        super().__init__(card_manager)

    def can_connect(self, network: WifiNetwork) -> bool:
        return network.encryption_type == 'WPA2'

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        try:
            card = self.card_manager.get_card()
            if not card:
                logger.error('No wifi cards available')
                return ConnectionResult(
                    network=network, download_speed=0, upload_speed=0,
                    ping=0, connected=False, connection_method='wpa2', interface='',
                )

            card.connect(network)
            download_speed, upload_speed, ping = self.run_speedtest(card)

            return ConnectionResult(
                network=network, download_speed=download_speed,
                upload_speed=upload_speed, ping=ping, connected=True,
                connection_method='wpa2', interface=card.interface,
            )

        except Exception as e:
            logger.error(f'Failed to connect to WPA2 network: {e}')
            return ConnectionResult(
                network=network, download_speed=0, upload_speed=0,
                ping=0, connected=False, connection_method='wpa2', interface='',
            )

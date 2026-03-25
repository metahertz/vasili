"""Unit tests for NotificationManager."""

import pytest

from notifications import NotificationManager, NotificationEvent


@pytest.mark.unit
class TestNotificationManager:
    """Test suite for NotificationManager."""

    def test_connection_established(self):
        """Test connection established notification."""
        nm = NotificationManager()
        nm.connection_established('TestNet', 'wlan0', 75.0)

        history = nm.get_history()
        assert len(history) == 1
        assert history[0]['event_type'] == NotificationEvent.CONNECTION_ESTABLISHED
        assert 'TestNet' in history[0]['message']

    def test_connection_lost(self):
        """Test connection lost notification."""
        nm = NotificationManager()
        nm.connection_lost('TestNet', 'wlan0')

        history = nm.get_history()
        assert len(history) == 1
        assert history[0]['event_type'] == NotificationEvent.CONNECTION_LOST

    def test_connection_degraded(self):
        """Test connection degraded notification."""
        nm = NotificationManager()
        nm.connection_degraded('TestNet', 'wlan0', 25.0)

        history = nm.get_history()
        assert len(history) == 1
        assert history[0]['event_type'] == NotificationEvent.CONNECTION_DEGRADED
        assert history[0]['data']['score'] == 25.0

    def test_better_network_found(self):
        """Test better network found notification."""
        nm = NotificationManager()
        nm.better_network_found('OldNet', 'BetterNet', 50.0, 80.0)

        history = nm.get_history()
        assert len(history) == 1
        assert history[0]['event_type'] == NotificationEvent.BETTER_NETWORK_FOUND
        assert history[0]['data']['new_ssid'] == 'BetterNet'

    def test_history_limit(self):
        """Test that history is capped at max_history."""
        nm = NotificationManager()
        nm._max_history = 5
        for i in range(10):
            nm.connection_established(f'Net{i}', 'wlan0', 50.0)

        history = nm.get_history()
        assert len(history) == 5

    def test_custom_listener(self):
        """Test adding a custom notification listener."""
        nm = NotificationManager()
        received = []
        nm.add_listener(lambda event: received.append(event.event_type))

        nm.connection_established('TestNet', 'wlan0', 75.0)
        assert len(received) == 1
        assert received[0] == NotificationEvent.CONNECTION_ESTABLISHED

    def test_notification_event_to_dict(self):
        """Test NotificationEvent serialization."""
        event = NotificationEvent(
            NotificationEvent.CONNECTION_LOST,
            'Lost connection',
            {'ssid': 'TestNet'},
        )
        d = event.to_dict()
        assert d['event_type'] == 'connection_lost'
        assert d['message'] == 'Lost connection'
        assert d['data']['ssid'] == 'TestNet'
        assert 'timestamp' in d

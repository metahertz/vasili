"""Notification system - alert when connection state changes."""

import threading
from datetime import datetime
from typing import Callable, Optional

import requests

from logging_config import get_logger

logger = get_logger('notifications')


class NotificationEvent:
    """Represents a notification event."""

    CONNECTION_ESTABLISHED = 'connection_established'
    CONNECTION_LOST = 'connection_lost'
    CONNECTION_DEGRADED = 'connection_degraded'
    BETTER_NETWORK_FOUND = 'better_network_found'
    SCAN_FAILED = 'scan_failed'

    def __init__(self, event_type: str, message: str, data: Optional[dict] = None):
        self.event_type = event_type
        self.message = message
        self.data = data or {}
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            'event_type': self.event_type,
            'message': self.message,
            'data': self.data,
            'timestamp': self.timestamp,
        }


class NotificationManager:
    """Manages notification delivery across multiple channels."""

    def __init__(
        self,
        webhook_url: str = '',
        degradation_threshold: float = 30.0,
        socketio_emit: Optional[Callable] = None,
    ):
        self.webhook_url = webhook_url
        self.degradation_threshold = degradation_threshold
        self._socketio_emit = socketio_emit
        self._listeners: list[Callable] = []
        self._history: list[dict] = []
        self._max_history = 100
        self._lock = threading.Lock()

    def add_listener(self, callback: Callable):
        """Add a callback that will be called for every notification."""
        self._listeners.append(callback)

    def notify(self, event: NotificationEvent):
        """Send a notification through all configured channels."""
        event_dict = event.to_dict()

        # Store in history
        with self._lock:
            self._history.append(event_dict)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        # Log the event
        log_level = 'warning' if event.event_type in (
            NotificationEvent.CONNECTION_LOST,
            NotificationEvent.CONNECTION_DEGRADED,
            NotificationEvent.SCAN_FAILED,
        ) else 'info'
        getattr(logger, log_level)(
            f'[{event.event_type}] {event.message}',
            extra={'notification': event_dict},
        )

        # WebSocket push
        if self._socketio_emit:
            try:
                self._socketio_emit('notification', event_dict)
            except Exception as e:
                logger.debug(f'WebSocket notification failed: {e}')

        # Webhook
        if self.webhook_url:
            self._send_webhook(event_dict)

        # Custom listeners
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                logger.debug(f'Listener notification failed: {e}')

    def _send_webhook(self, event_dict: dict):
        """Send notification via webhook POST."""
        try:
            response = requests.post(
                self.webhook_url,
                json=event_dict,
                timeout=5,
            )
            if response.status_code >= 400:
                logger.warning(
                    f'Webhook returned {response.status_code}: {response.text[:100]}'
                )
        except requests.RequestException as e:
            logger.debug(f'Webhook delivery failed: {e}')

    def connection_established(self, ssid: str, interface: str, score: float = 0.0):
        """Notify that a connection was established."""
        self.notify(NotificationEvent(
            NotificationEvent.CONNECTION_ESTABLISHED,
            f'Connected to {ssid} on {interface}',
            {'ssid': ssid, 'interface': interface, 'score': score},
        ))

    def connection_lost(self, ssid: str, interface: str):
        """Notify that a connection was lost."""
        self.notify(NotificationEvent(
            NotificationEvent.CONNECTION_LOST,
            f'Lost connection to {ssid} on {interface}',
            {'ssid': ssid, 'interface': interface},
        ))

    def connection_degraded(self, ssid: str, interface: str, score: float):
        """Notify that connection quality has degraded."""
        self.notify(NotificationEvent(
            NotificationEvent.CONNECTION_DEGRADED,
            f'Connection to {ssid} degraded (score: {score:.1f})',
            {'ssid': ssid, 'interface': interface, 'score': score},
        ))

    def better_network_found(
        self, current_ssid: str, new_ssid: str, current_score: float, new_score: float
    ):
        """Notify that a better network is available."""
        self.notify(NotificationEvent(
            NotificationEvent.BETTER_NETWORK_FOUND,
            f'Better network available: {new_ssid} (score: {new_score:.1f}) '
            f'vs current {current_ssid} (score: {current_score:.1f})',
            {
                'current_ssid': current_ssid,
                'new_ssid': new_ssid,
                'current_score': current_score,
                'new_score': new_score,
            },
        ))

    def get_history(self, limit: int = 50) -> list[dict]:
        """Get recent notification history."""
        with self._lock:
            return list(reversed(self._history[-limit:]))

"""
Captive Portal Detection and Authentication Module

This module detects when connected to a captive portal network and attempts
automatic authentication through common portal types.
"""

import re
import time
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import requests
import speedtest
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from logging_config import get_logger
from vasili import ConnectionModule, WifiNetwork, ConnectionResult

logger = get_logger(__name__)

# Known test URLs that should return 204 or specific content when not behind a captive portal
CAPTIVE_TEST_URLS = [
    'http://captive.apple.com/hotspot-detect.html',
    'http://connectivitycheck.gstatic.com/generate_204',
    'http://clients3.google.com/generate_204',
    'http://www.msftconnecttest.com/connecttest.txt',
]


class PortalDatabase:
    """Handles MongoDB operations for storing portal patterns."""

    def __init__(self, connection_string: str = 'mongodb://localhost:27017/'):
        self.connection_string = connection_string
        self.client: Optional[MongoClient] = None
        self.db = None
        self.patterns_collection = None
        self._connect()

    def _connect(self):
        """Establish MongoDB connection."""
        try:
            self.client = MongoClient(
                self.connection_string,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
            )
            # Test the connection
            self.client.admin.command('ping')
            self.db = self.client['vasili']
            self.patterns_collection = self.db['portal_patterns']
            logger.info('Connected to MongoDB successfully')
        except ConnectionFailure as e:
            logger.warning(f'Failed to connect to MongoDB: {e}. Portal pattern storage disabled.')
            self.client = None

    def store_portal_pattern(self, ssid: str, pattern_data: Dict[str, Any]):
        """Store a detected portal pattern for future reference."""
        if not self.client:
            return

        try:
            pattern_data.update({
                'ssid': ssid,
                'last_seen': time.time(),
                'success_count': 0,
                'failure_count': 0,
            })

            # Upsert pattern
            self.patterns_collection.update_one(
                {'ssid': ssid, 'redirect_domain': pattern_data.get('redirect_domain')},
                {'$set': pattern_data, '$inc': {'success_count': 1}},
                upsert=True,
            )
            logger.debug(f'Stored portal pattern for {ssid}')
        except Exception as e:
            logger.error(f'Failed to store portal pattern: {e}')

    def get_portal_pattern(self, ssid: str) -> Optional[Dict[str, Any]]:
        """Retrieve a known portal pattern for an SSID."""
        if not self.client:
            return None

        try:
            pattern = self.patterns_collection.find_one(
                {'ssid': ssid},
                sort=[('success_count', -1)],  # Get most successful pattern
            )
            if pattern:
                logger.debug(f'Found known portal pattern for {ssid}')
            return pattern
        except Exception as e:
            logger.error(f'Failed to retrieve portal pattern: {e}')
            return None

    def record_auth_result(self, ssid: str, redirect_domain: str, success: bool):
        """Record the result of an authentication attempt."""
        if not self.client:
            return

        try:
            field = 'success_count' if success else 'failure_count'
            self.patterns_collection.update_one(
                {'ssid': ssid, 'redirect_domain': redirect_domain},
                {'$inc': {field: 1}, '$set': {'last_seen': time.time()}},
            )
        except Exception as e:
            logger.error(f'Failed to record auth result: {e}')

    def close(self):
        """Close MongoDB connection."""
        if self.client:
            self.client.close()


class CaptivePortalDetector:
    """Detects captive portals using HTTP redirect detection."""

    def __init__(self):
        self.timeout = 10

    def detect(self) -> Optional[Dict[str, Any]]:
        """
        Detect if we're behind a captive portal.

        Returns:
            Dict with portal info if detected, None otherwise.
            Dict contains: redirect_url, redirect_domain, portal_type, etc.
        """
        for test_url in CAPTIVE_TEST_URLS:
            try:
                logger.debug(f'Testing connectivity with {test_url}')
                response = requests.get(
                    test_url,
                    timeout=self.timeout,
                    allow_redirects=False,
                    headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Vasili/1.0'},
                )

                # Check for redirects (302, 303, 307, 308)
                if response.status_code in [301, 302, 303, 307, 308]:
                    redirect_url = response.headers.get('Location', '')
                    logger.info(f'Captive portal detected: {response.status_code} -> {redirect_url}')

                    portal_info = self._analyze_portal(redirect_url, response)
                    return portal_info

                # Check if we got the expected response
                if test_url.endswith('generate_204'):
                    if response.status_code != 204:
                        logger.info(f'Unexpected response from {test_url}: {response.status_code}')
                        return self._analyze_portal('', response)
                elif test_url.endswith('hotspot-detect.html'):
                    if '<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>' not in response.text:
                        logger.info(f'Unexpected content from Apple hotspot test')
                        # Might be a captive portal with modified content
                        return self._analyze_portal('', response)

                # If we get here with a 200 and expected content, no portal detected
                logger.debug(f'No captive portal detected via {test_url}')

            except requests.exceptions.Timeout:
                logger.warning(f'Timeout testing {test_url}')
                continue
            except requests.exceptions.ConnectionError as e:
                logger.warning(f'Connection error testing {test_url}: {e}')
                continue
            except Exception as e:
                logger.error(f'Error testing {test_url}: {e}')
                continue

        # No portal detected
        return None

    def _analyze_portal(self, redirect_url: str, response: requests.Response) -> Dict[str, Any]:
        """Analyze portal characteristics from redirect URL and response."""
        portal_info = {
            'redirect_url': redirect_url,
            'redirect_domain': '',
            'portal_type': 'unknown',
            'auth_method': 'unknown',
        }

        if redirect_url:
            parsed = urlparse(redirect_url)
            portal_info['redirect_domain'] = parsed.netloc

            # Try to identify common portal types by domain patterns
            domain = parsed.netloc.lower()

            if 'captive.apple' in domain:
                portal_info['portal_type'] = 'apple'
            elif 'gstatic' in domain or 'google' in domain:
                portal_info['portal_type'] = 'google'
            elif 'msftconnecttest' in domain or 'microsoft' in domain:
                portal_info['portal_type'] = 'microsoft'
            elif 'wifi.id' in domain:
                portal_info['portal_type'] = 'wifi.id'
            elif 'fon.com' in domain:
                portal_info['portal_type'] = 'fon'
            elif 'hotspotsystem' in domain:
                portal_info['portal_type'] = 'hotspotsystem'
            else:
                # Try to extract vendor from domain
                match = re.search(r'([\w-]+)\.(com|net|org)', domain)
                if match:
                    portal_info['portal_type'] = match.group(1)

        # Analyze response for authentication hints
        if response.text:
            text_lower = response.text.lower()

            if 'accept' in text_lower and 'terms' in text_lower:
                portal_info['auth_method'] = 'terms_acceptance'
            elif 'login' in text_lower or 'username' in text_lower:
                portal_info['auth_method'] = 'login_required'
            elif 'click' in text_lower and ('continue' in text_lower or 'connect' in text_lower):
                portal_info['auth_method'] = 'click_through'
            elif 'payment' in text_lower or 'purchase' in text_lower:
                portal_info['auth_method'] = 'payment_required'

        return portal_info


class CaptivePortalAuthenticator:
    """Attempts automatic authentication through captive portals."""

    def __init__(self):
        self.timeout = 15

    def authenticate(self, portal_info: Dict[str, Any]) -> bool:
        """
        Attempt to authenticate through the captive portal.

        Args:
            portal_info: Portal information from detection

        Returns:
            True if authentication succeeded, False otherwise
        """
        auth_method = portal_info.get('auth_method')
        portal_type = portal_info.get('portal_type')

        logger.info(f'Attempting authentication: type={portal_type}, method={auth_method}')

        # Try authentication based on method
        if auth_method == 'terms_acceptance':
            return self._accept_terms(portal_info)
        elif auth_method == 'click_through':
            return self._click_through(portal_info)
        elif auth_method == 'login_required':
            logger.warning('Login required - automatic auth not possible without credentials')
            return False
        elif auth_method == 'payment_required':
            logger.warning('Payment required - automatic auth not possible')
            return False
        else:
            # Try generic click-through as fallback
            logger.info('Unknown auth method, trying generic click-through')
            return self._click_through(portal_info)

    def _accept_terms(self, portal_info: Dict[str, Any]) -> bool:
        """Attempt to accept terms and conditions automatically."""
        try:
            redirect_url = portal_info.get('redirect_url')
            if not redirect_url:
                return False

            # First, GET the portal page
            response = requests.get(redirect_url, timeout=self.timeout)

            # Look for form submission or acceptance button
            # This is a simplified version - real implementation would need more sophisticated parsing
            form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', response.text, re.IGNORECASE)

            if form_match:
                action_url = form_match.group(1)
                if not action_url.startswith('http'):
                    base_url = f"{urlparse(redirect_url).scheme}://{urlparse(redirect_url).netloc}"
                    action_url = base_url + action_url

                # Try POSTing acceptance
                logger.debug(f'Attempting to POST acceptance to {action_url}')
                post_response = requests.post(
                    action_url,
                    data={'accept': '1', 'terms': 'accepted', 'continue': '1'},
                    timeout=self.timeout,
                    allow_redirects=True,
                )

                # Check if we're now online
                if post_response.status_code == 200:
                    logger.info('Terms acceptance POST succeeded')
                    return True

        except Exception as e:
            logger.error(f'Error accepting terms: {e}')

        return False

    def _click_through(self, portal_info: Dict[str, Any]) -> bool:
        """Attempt simple click-through authentication."""
        try:
            redirect_url = portal_info.get('redirect_url')
            if not redirect_url:
                return False

            # Some portals just need you to visit the page
            response = requests.get(
                redirect_url,
                timeout=self.timeout,
                allow_redirects=True,
                headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Vasili/1.0'},
            )

            if response.status_code == 200:
                logger.info('Click-through succeeded')
                return True

        except Exception as e:
            logger.error(f'Error in click-through: {e}')

        return False


class CaptivePortalModule(ConnectionModule):
    """Connection module for handling captive portal networks."""

    def __init__(self, card_manager, mongodb_uri: str = 'mongodb://localhost:27017/'):
        super().__init__(card_manager)
        self.db = PortalDatabase(mongodb_uri)
        self.detector = CaptivePortalDetector()
        self.authenticator = CaptivePortalAuthenticator()

    def can_connect(self, network: WifiNetwork) -> bool:
        """
        This module can work with any network type, but primarily targets
        open networks that might have captive portals.
        """
        # Prioritize open networks as they're most likely to have captive portals
        return network.is_open

    def connect(self, network: WifiNetwork) -> ConnectionResult:
        """
        Connect to network and handle captive portal if present.

        Steps:
        1. Connect to the network
        2. Detect if there's a captive portal
        3. Attempt authentication if portal detected
        4. Verify connection with speedtest
        """
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
                    connection_method='captive_portal',
                    interface='',
                )

            # Connect to the network
            logger.info(f'Connecting to {network.ssid}')
            card.connect(network)

            # Give the connection a moment to stabilize
            time.sleep(2)

            # Check for known portal pattern first
            known_pattern = self.db.get_portal_pattern(network.ssid)
            if known_pattern:
                logger.info(f'Using known portal pattern for {network.ssid}')
                # Use known pattern to attempt direct authentication
                # This could be optimized in future versions

            # Detect captive portal
            logger.info('Checking for captive portal...')
            portal_info = self.detector.detect()

            if portal_info:
                logger.info(f'Captive portal detected: {portal_info}')

                # Store the pattern for future use
                self.db.store_portal_pattern(network.ssid, portal_info)

                # Attempt authentication
                auth_success = self.authenticator.authenticate(portal_info)

                # Record the result
                self.db.record_auth_result(
                    network.ssid,
                    portal_info.get('redirect_domain', ''),
                    auth_success,
                )

                if not auth_success:
                    logger.warning('Failed to authenticate through captive portal')
                    return ConnectionResult(
                        network=network,
                        download_speed=0,
                        upload_speed=0,
                        ping=0,
                        connected=False,
                        connection_method='captive_portal',
                        interface=card.interface,
                    )

                logger.info('Successfully authenticated through captive portal')
            else:
                logger.info('No captive portal detected')

            # Run speedtest to verify connection and measure performance
            logger.info('Running speedtest...')
            st = speedtest.Speedtest()
            st.get_best_server()
            download_speed = st.download() / 1_000_000  # Convert to Mbps
            upload_speed = st.upload() / 1_000_000  # Convert to Mbps
            ping = st.results.ping

            logger.info(
                f'Connection successful: {download_speed:.2f} Mbps down, '
                f'{upload_speed:.2f} Mbps up, {ping:.2f} ms ping'
            )

            return ConnectionResult(
                network=network,
                download_speed=download_speed,
                upload_speed=upload_speed,
                ping=ping,
                connected=True,
                connection_method='captive_portal',
                interface=card.interface,
            )

        except Exception as e:
            logger.error(f'Failed to connect via captive portal module: {e}')
            return ConnectionResult(
                network=network,
                download_speed=0,
                upload_speed=0,
                ping=0,
                connected=False,
                connection_method='captive_portal',
                interface='',
            )

    def __del__(self):
        """Cleanup database connection."""
        if hasattr(self, 'db'):
            self.db.close()

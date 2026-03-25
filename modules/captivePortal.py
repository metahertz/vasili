"""
Captive Portal Detection and Authentication Module

This module detects when connected to a captive portal network and attempts
automatic authentication through common portal types.
"""

import re
import subprocess
import time
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import requests
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

    def __init__(self, connection_string: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili'):
        self.connection_string = connection_string
        self.db_name = db_name
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
            self.db = self.client[self.db_name]
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
            pattern_data.update(
                {
                    'ssid': ssid,
                    'last_seen': time.time(),
                    'success_count': 0,
                    'failure_count': 0,
                }
            )

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

    def detect(self, interface: str = None) -> Optional[Dict[str, Any]]:
        """
        Detect if we're behind a captive portal.

        Args:
            interface: Network interface to bind requests to.
                      If provided, uses curl --interface for accurate detection.

        Returns:
            Dict with portal info if detected, None otherwise.
            Dict contains: redirect_url, redirect_domain, portal_type, etc.
        """
        for test_url in CAPTIVE_TEST_URLS:
            try:
                logger.debug(f'Testing connectivity with {test_url}')
                if interface:
                    response = self._curl_request(test_url, interface)
                    if response is None:
                        continue
                else:
                    response = requests.get(
                        test_url,
                        timeout=self.timeout,
                        allow_redirects=False,
                        headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Vasili/1.0'},
                    )

                # Check for redirects (302, 303, 307, 308)
                if response.status_code in [301, 302, 303, 307, 308]:
                    redirect_url = response.headers.get('Location', '')
                    logger.info(
                        f'Captive portal detected: {response.status_code} -> {redirect_url}'
                    )

                    portal_info = self._analyze_portal(redirect_url, response)
                    return portal_info

                # Check if we got the expected response
                if test_url.endswith('generate_204'):
                    if response.status_code != 204:
                        logger.info(f'Unexpected response from {test_url}: {response.status_code}')
                        return self._analyze_portal('', response)
                elif test_url.endswith('hotspot-detect.html'):
                    if (
                        '<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>'
                        not in response.text
                    ):
                        logger.info('Unexpected content from Apple hotspot test')
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

    def _curl_request(self, url: str, interface: str):
        """Make an HTTP request bound to a specific interface via curl.

        Returns a requests.Response-like object with status_code and headers,
        or None on failure.
        """
        try:
            result = subprocess.run(
                [
                    'curl', '--interface', interface,
                    '--connect-timeout', str(self.timeout),
                    '-s', '-o', '/dev/null',
                    '-w', '%{http_code} %{redirect_url}',
                    '-D', '-',  # dump headers to stdout
                    '--max-redirs', '0',
                    url,
                ],
                capture_output=True, text=True, timeout=self.timeout + 5,
            )
            parts = result.stdout.strip().rsplit('\n', 1)
            if len(parts) < 1:
                return None
            status_line = parts[-1].strip()
            status_parts = status_line.split(' ', 1)
            status_code = int(status_parts[0])
            redirect_url = status_parts[1] if len(status_parts) > 1 else ''

            # Create a minimal response-like object
            resp = type('CurlResponse', (), {
                'status_code': status_code,
                'headers': {'Location': redirect_url} if redirect_url else {},
                'text': '',
            })()
            return resp
        except Exception as e:
            logger.debug(f'curl request to {url} via {interface} failed: {e}')
            return None

    def _analyze_portal(self, redirect_url: str, response) -> Dict[str, Any]:
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
    """Authenticate through captive portals using intelligent form parsing.

    Handles marketing portals, terms acceptance, click-through, and
    multi-step flows using:
    - HTML form parsing (stdlib html.parser)
    - Heuristic field classification (email, name, terms, etc.)
    - Auto-fill with configurable identity data
    - Session/cookie persistence across requests
    - CSRF token extraction from hidden fields
    """

    USER_AGENT = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/125.0 Mobile Safari/537.36'

    def __init__(self, identity: dict = None):
        self.timeout = 15
        self.max_steps = 5
        self.identity = identity or {}
        self.auth_log: list[dict] = []  # Detailed log for UI

    def authenticate(self, portal_info: Dict[str, Any],
                     interface: str = None) -> bool:
        """Attempt to authenticate through the captive portal.

        Tries multiple strategies in order:
        1. Smart form parsing + auto-fill (handles marketing portals)
        2. Simple click-through (handles redirect-only portals)

        Args:
            portal_info: Portal information from detection
            interface: WiFi interface to bind requests to

        Returns:
            True if authentication succeeded
        """
        auth_method = portal_info.get('auth_method')
        portal_type = portal_info.get('portal_type')
        self.auth_log = []

        logger.info(f'Attempting auth: type={portal_type}, method={auth_method}')

        if auth_method == 'payment_required':
            self._log('skip', 'Payment required — cannot auto-authenticate')
            return False

        redirect_url = portal_info.get('redirect_url')
        if not redirect_url:
            self._log('skip', 'No redirect URL in portal info')
            return False

        # Strategy 1: Smart form parsing (handles most portal types)
        result = self._smart_form_auth(redirect_url, interface)
        if result:
            return True

        # Strategy 2: Simple click-through fallback
        result = self._click_through(redirect_url, interface)
        if result:
            return True

        self._log('failed', 'All authentication strategies exhausted')
        return False

    def _smart_form_auth(self, url: str, interface: str = None) -> bool:
        """Parse and submit portal forms with intelligent auto-fill."""
        from portal_forms import parse_and_fill

        session = requests.Session()
        session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,*/*',
            'Accept-Language': 'en-US,en;q=0.9',
        })

        # Bind session to WiFi interface if specified
        if interface:
            try:
                import network_isolation
                source_ip = network_isolation.get_interface_ip(interface)
                if source_ip:
                    from requests.adapters import HTTPAdapter
                    adapter = _SourceAddressAdapter(source_ip)
                    session.mount('http://', adapter)
                    session.mount('https://', adapter)
            except Exception as e:
                logger.debug(f'Could not bind session to {interface}: {e}')

        try:
            current_url = url
            for step in range(self.max_steps):
                self._log('step', f'Step {step+1}: GET {current_url}')

                response = session.get(
                    current_url, timeout=self.timeout, allow_redirects=True
                )

                if response.status_code != 200:
                    self._log('error', f'HTTP {response.status_code} from {current_url}')
                    return False

                # Parse forms from the page
                base_url = f'{urlparse(response.url).scheme}://{urlparse(response.url).netloc}'
                form_fills = parse_and_fill(response.text, base_url, self.identity)

                if not form_fills:
                    self._log('info', f'No forms found on page (step {step+1})')
                    # No forms — might be a success page already
                    if step > 0:
                        return True  # We submitted a form and landed on a no-form page
                    return False

                # Try each form (best candidate first)
                submitted = False
                for form, filled_data in form_fills:
                    if not form.action and not current_url:
                        continue

                    action_url = form.action or current_url
                    method = form.method

                    field_summary = ', '.join(
                        f'{k}={"***" if len(v) > 0 else "(empty)"}'
                        for k, v in filled_data.items()
                        if k != '__submit__'
                    )
                    self._log(
                        'submit',
                        f'{method} {action_url} [{len(filled_data)} fields: {field_summary}]'
                    )

                    if method == 'GET':
                        response = session.get(
                            action_url, params=filled_data,
                            timeout=self.timeout, allow_redirects=True,
                        )
                    else:
                        response = session.post(
                            action_url, data=filled_data,
                            timeout=self.timeout, allow_redirects=True,
                            headers={'Referer': current_url},
                        )

                    submitted = True
                    current_url = response.url

                    # Check if response has more forms (multi-step)
                    if '<form' in response.text.lower():
                        self._log('info', f'Response contains form — continuing to step {step+2}')
                        break  # Continue outer loop with new page

                    # No more forms — assume we're done
                    self._log('success', f'Form submitted, no more forms on response page')
                    return True

                if not submitted:
                    break

        except requests.exceptions.Timeout:
            self._log('error', 'Request timed out')
        except Exception as e:
            self._log('error', f'Smart form auth error: {str(e)[:200]}')

        return False

    def _click_through(self, url: str, interface: str = None) -> bool:
        """Simple click-through: just visit the URL with redirects."""
        try:
            self._log('step', f'Click-through: GET {url}')
            kwargs = {
                'timeout': self.timeout,
                'allow_redirects': True,
                'headers': {'User-Agent': self.USER_AGENT},
            }
            response = requests.get(url, **kwargs)
            if response.status_code == 200:
                self._log('success', 'Click-through returned 200')
                return True
        except Exception as e:
            self._log('error', f'Click-through error: {e}')
        return False

    def _log(self, level: str, message: str):
        """Add to auth log for detailed UI output."""
        self.auth_log.append({
            'level': level,
            'message': message,
            'timestamp': time.time(),
        })
        if level == 'error':
            logger.warning(f'Portal auth: {message}')
        else:
            logger.info(f'Portal auth: {message}')


class _SourceAddressAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that binds to a specific source IP."""

    def __init__(self, source_address, **kwargs):
        self._source_address = source_address
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['source_address'] = (self._source_address, 0)
        super().init_poolmanager(*args, **kwargs)


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

            # Detect captive portal (bound to the WiFi interface)
            logger.info('Checking for captive portal...')
            portal_info = self.detector.detect(interface=card.interface)

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

            # Run speedtest bound to WiFi interface to verify connection
            logger.info('Running speedtest...')
            download_speed, upload_speed, ping = self.run_speedtest(card)

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

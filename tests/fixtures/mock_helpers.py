"""Helper functions and factories for creating mock objects."""

from subprocess import CompletedProcess
from unittest.mock import MagicMock
from tests.fixtures.mock_data import (
    IWLIST_SCAN_OUTPUT,
    IWLIST_SCAN_EMPTY,
    IWCONFIG_OUTPUT_VALID,
    NMCLI_CONNECT_SUCCESS,
    NMCLI_CONNECT_FAILURE,
    INTERFACE_UP,
    INTERFACE_DOWN,
)


class SubprocessMockFactory:
    """Factory for creating context-aware subprocess mocks."""

    @staticmethod
    def create_mock(command_handlers=None, scan_output=None, connect_success=True):
        """
        Create a mock subprocess.run that returns appropriate responses based on command.

        Args:
            command_handlers: Optional dict mapping command patterns to CompletedProcess objects
            scan_output: Optional custom scan output (defaults to IWLIST_SCAN_OUTPUT)
            connect_success: Whether nmcli connect commands should succeed (default True)

        Returns:
            A side_effect function suitable for use with unittest.mock.patch
        """
        if scan_output is None:
            scan_output = IWLIST_SCAN_OUTPUT

        def side_effect(cmd, **kwargs):
            # Convert command to string for easier matching
            if isinstance(cmd, list):
                cmd_str = ' '.join(cmd)
            else:
                cmd_str = cmd

            # Check custom handlers first
            if command_handlers:
                for pattern, response in command_handlers.items():
                    if pattern in cmd_str:
                        return response

            # iwconfig - check if interface is wireless
            if 'iwconfig' in cmd_str:
                # Assume wlan* interfaces are valid
                if 'wlan' in cmd_str:
                    return CompletedProcess(
                        args=cmd, returncode=0, stdout=IWCONFIG_OUTPUT_VALID, stderr=''
                    )
                else:
                    return CompletedProcess(
                        args=cmd, returncode=1, stdout='', stderr='no wireless extensions'
                    )

            # iwlist scan
            if 'iwlist' in cmd_str and 'scan' in cmd_str:
                return CompletedProcess(args=cmd, returncode=0, stdout=scan_output, stderr='')

            # ip link set up
            if 'ip' in cmd_str and 'link' in cmd_str and 'up' in cmd_str:
                return CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

            # nmcli device disconnect
            if 'nmcli' in cmd_str and 'disconnect' in cmd_str:
                return CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

            # nmcli device wifi connect
            if 'nmcli' in cmd_str and 'connect' in cmd_str:
                if connect_success:
                    return CompletedProcess(
                        args=cmd, returncode=0, stdout=NMCLI_CONNECT_SUCCESS, stderr=''
                    )
                else:
                    return CompletedProcess(
                        args=cmd, returncode=1, stdout='', stderr=NMCLI_CONNECT_FAILURE
                    )

            # iptables commands (for NetworkBridge)
            if 'iptables' in cmd_str:
                return CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

            # ip route commands
            if 'ip' in cmd_str and 'route' in cmd_str:
                return CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

            # Default: command succeeds with no output
            return CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

        return side_effect


class FileIOMockFactory:
    """Factory for creating file I/O mocks."""

    @staticmethod
    def create_mock_open(file_handlers=None, interface_state='up'):
        """
        Create a mock open() that handles /sys and /proc file reads.

        Args:
            file_handlers: Optional dict mapping file paths to content
            interface_state: Default interface state ('up' or 'down')

        Returns:
            A side_effect function for mocking builtins.open
        """

        def side_effect(file, mode='r', **kwargs):
            # Check custom handlers first
            if file_handlers and file in file_handlers:
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = file_handlers[file]
                return mock_file

            # Handle /sys/class/net/*/operstate
            if '/sys/class/net/' in file and 'operstate' in file:
                mock_file = MagicMock()
                content = INTERFACE_UP if interface_state == 'up' else INTERFACE_DOWN
                mock_file.__enter__.return_value.read.return_value = content
                return mock_file

            # Handle /proc/sys/net/ipv4/ip_forward
            if '/proc/sys/net/ipv4/ip_forward' in file:
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = '0\n'
                return mock_file

            # For other files, raise FileNotFoundError
            raise FileNotFoundError(f"No such file: {file}")

        return side_effect


class SpeedtestMockFactory:
    """Factory for creating speedtest mocks."""

    @staticmethod
    def create_mock(download_speed=50_000_000, upload_speed=10_000_000, ping=25.0):
        """
        Create a mock Speedtest instance.

        Args:
            download_speed: Download speed in bps (default 50 Mbps)
            upload_speed: Upload speed in bps (default 10 Mbps)
            ping: Ping latency in ms (default 25.0)

        Returns:
            A mock Speedtest instance
        """
        instance = MagicMock()
        instance.download.return_value = download_speed
        instance.upload.return_value = upload_speed
        instance.results.ping = ping
        return instance

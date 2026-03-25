"""Unit tests for network_isolation module."""

import pytest
from unittest.mock import patch, MagicMock, call

import network_isolation


@pytest.fixture(autouse=True)
def reset_table_map():
    """Reset the table map between tests."""
    network_isolation._table_map.clear()
    network_isolation._next_table = network_isolation._TABLE_BASE


@pytest.fixture
def mock_subprocess():
    with patch('network_isolation.subprocess') as mock:
        # Default: all commands succeed
        mock.run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        yield mock


@pytest.fixture
def mock_netifaces():
    with patch('network_isolation.netifaces') as mock:
        mock.AF_INET = 2
        mock.ifaddresses.return_value = {
            2: [{'addr': '192.168.1.100', 'netmask': '255.255.255.0'}]
        }
        yield mock


@pytest.mark.unit
class TestGetInterfaceIp:

    def test_returns_ip_when_available(self, mock_netifaces):
        ip = network_isolation.get_interface_ip('wlan0')
        assert ip == '192.168.1.100'
        mock_netifaces.ifaddresses.assert_called_with('wlan0')

    def test_returns_none_when_no_ipv4(self, mock_netifaces):
        mock_netifaces.ifaddresses.return_value = {}
        assert network_isolation.get_interface_ip('wlan0') is None

    def test_returns_none_on_error(self, mock_netifaces):
        mock_netifaces.ifaddresses.side_effect = ValueError('no such interface')
        assert network_isolation.get_interface_ip('wlan99') is None


@pytest.mark.unit
class TestGetInterfaceGateway:

    def test_returns_gateway(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0,
            stdout='IP4.GATEWAY:192.168.1.1\n',
        )
        gw = network_isolation.get_interface_gateway('wlan0')
        assert gw == '192.168.1.1'

    def test_returns_none_when_no_gateway(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0,
            stdout='IP4.GATEWAY:--\n',
        )
        assert network_isolation.get_interface_gateway('wlan0') is None

    def test_returns_none_on_error(self, mock_subprocess):
        mock_subprocess.run.side_effect = Exception('nmcli failed')
        assert network_isolation.get_interface_gateway('wlan0') is None


@pytest.mark.unit
class TestSetupInterfaceRouting:

    def test_sets_up_routing(self, mock_subprocess, mock_netifaces):
        mock_subprocess.run.side_effect = [
            # nmcli gateway query
            MagicMock(returncode=0, stdout='IP4.GATEWAY:192.168.1.1\n'),
            # ip route flush
            MagicMock(returncode=0),
            # ip rule del (cleanup)
            MagicMock(returncode=0),
            # ip route add
            MagicMock(returncode=0, stderr=''),
            # ip rule add
            MagicMock(returncode=0, stderr=''),
        ]

        result = network_isolation.setup_interface_routing('wlan0')

        assert result is not None
        assert result['ip'] == '192.168.1.100'
        assert result['gateway'] == '192.168.1.1'
        assert result['table'] == 100
        assert result['interface'] == 'wlan0'

    def test_returns_none_when_no_ip(self, mock_netifaces):
        mock_netifaces.ifaddresses.return_value = {}
        result = network_isolation.setup_interface_routing('wlan0')
        assert result is None

    def test_returns_none_when_no_gateway(self, mock_subprocess, mock_netifaces):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0, stdout='IP4.GATEWAY:--\n'
        )
        result = network_isolation.setup_interface_routing('wlan0')
        assert result is None

    def test_assigns_unique_tables(self, mock_subprocess, mock_netifaces):
        mock_subprocess.run.side_effect = [
            MagicMock(returncode=0, stdout='IP4.GATEWAY:192.168.1.1\n'),
            MagicMock(returncode=0), MagicMock(returncode=0),
            MagicMock(returncode=0, stderr=''), MagicMock(returncode=0, stderr=''),
        ] * 2

        r1 = network_isolation.setup_interface_routing('wlan0')
        r2 = network_isolation.setup_interface_routing('wlan1')

        assert r1['table'] != r2['table']
        assert r1['table'] == 100
        assert r2['table'] == 101

    def test_same_interface_reuses_table(self, mock_subprocess, mock_netifaces):
        mock_subprocess.run.side_effect = [
            MagicMock(returncode=0, stdout='IP4.GATEWAY:192.168.1.1\n'),
            MagicMock(returncode=0), MagicMock(returncode=0),
            MagicMock(returncode=0, stderr=''), MagicMock(returncode=0, stderr=''),
        ] * 2

        r1 = network_isolation.setup_interface_routing('wlan0')
        r2 = network_isolation.setup_interface_routing('wlan0')

        assert r1['table'] == r2['table']


@pytest.mark.unit
class TestTeardownInterfaceRouting:

    def test_removes_rule_and_flushes_table(self, mock_subprocess):
        routing_info = {
            'ip': '192.168.1.100',
            'gateway': '192.168.1.1',
            'table': 100,
            'interface': 'wlan0',
        }

        network_isolation.teardown_interface_routing('wlan0', routing_info)

        calls = mock_subprocess.run.call_args_list
        # Should have called ip rule del and ip route flush
        assert len(calls) == 2
        assert 'rule' in str(calls[0])
        assert 'flush' in str(calls[1])

    def test_handles_none_routing_info(self, mock_subprocess):
        # Should not raise
        network_isolation.teardown_interface_routing('wlan0', None)
        mock_subprocess.run.assert_not_called()

    def test_handles_empty_routing_info(self, mock_subprocess):
        network_isolation.teardown_interface_routing('wlan0', {})
        # Should still attempt flush if table present... but empty dict has no table
        # So no calls should be made for rule del, but flush might be attempted
        # This is a graceful no-op


@pytest.mark.unit
class TestVerifyConnectivity:

    def test_returns_true_on_204(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0, stdout='204'
        )
        assert network_isolation.verify_connectivity('wlan0') is True

    def test_returns_false_on_non_204(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0, stdout='302'
        )
        assert network_isolation.verify_connectivity('wlan0') is False

    def test_returns_false_on_timeout(self, mock_subprocess):
        from subprocess import TimeoutExpired
        mock_subprocess.run.side_effect = TimeoutExpired('curl', 10)
        assert network_isolation.verify_connectivity('wlan0') is False

    def test_returns_false_on_error(self, mock_subprocess):
        mock_subprocess.run.side_effect = Exception('curl not found')
        assert network_isolation.verify_connectivity('wlan0') is False

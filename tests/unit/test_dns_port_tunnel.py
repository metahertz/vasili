"""Unit tests for the DNS-port tunnel stage (SSH/53 + WireGuard/53)."""

import pytest
from unittest.mock import patch, MagicMock

from vasili import WifiNetwork


def make_network(**kw):
    defaults = dict(
        ssid='TestNet', bssid='aa:bb:cc:dd:ee:ff', signal_strength=70,
        channel=6, encryption_type='Open', is_open=True,
    )
    defaults.update(kw)
    return WifiNetwork(**defaults)


def make_card(interface='wlan0', ip='10.0.0.5'):
    card = MagicMock()
    card.interface = interface
    card.get_ip_address.return_value = ip
    return card


def make_stage(**cfg_overrides):
    from modules.stages.dns_port_tunnel import DnsPortTunnelStage
    stage = DnsPortTunnelStage()
    cfg = {
        'ssh_server': '',
        'ssh_user': 'root',
        'ssh_key_path': '',
        'wg_config_path': '',
        'timeout': 5,
    }
    cfg.update(cfg_overrides)
    stage._stage_config = cfg
    return stage


# ---------------------------------------------------------------------------
# can_run tests
# ---------------------------------------------------------------------------

class TestDnsPortTunnelCanRun:

    def test_skip_when_internet_available(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'has_internet': True, 'dns_reachable_tcp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_skip_when_no_dns(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': False, 'dns_reachable_udp': False}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_skip_when_nothing_configured(self):
        stage = make_stage()  # no ssh_server, no wg_config_path
        ctx = {'dns_reachable_tcp': True, 'dns_reachable_udp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_runs_with_ssh_and_tcp(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': True, 'dns_reachable_udp': False}
        assert stage.can_run(make_network(), make_card(), ctx) is True

    def test_runs_with_wg_and_udp(self):
        stage = make_stage(wg_config_path='/etc/wireguard/wg0.conf')
        ctx = {'dns_reachable_tcp': False, 'dns_reachable_udp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is True

    def test_ssh_needs_tcp(self):
        """SSH configured but only UDP available — should not run."""
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': False, 'dns_reachable_udp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_wg_needs_udp(self):
        """WG configured but only TCP available — should not run."""
        stage = make_stage(wg_config_path='/etc/wireguard/wg0.conf')
        ctx = {'dns_reachable_tcp': True, 'dns_reachable_udp': False}
        assert stage.can_run(make_network(), make_card(), ctx) is False


# ---------------------------------------------------------------------------
# run() tests — SSH path
# ---------------------------------------------------------------------------

class TestDnsPortTunnelSsh:

    def test_ssh_success(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = {'interface': 'tun53', 'ip': '10.53.0.2'}
        mock_helper.verify.return_value = True
        mock_helper.tunnel_interface = 'tun53'

        with patch('modules.helpers.ssh_tunnel.SshTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is True
        assert result.has_internet is True
        assert result.context_updates['tunnel_type'] == 'ssh'
        assert result.context_updates['tunnel_interface'] == 'tun53'

    def test_ssh_not_installed(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = False

        with patch('modules.helpers.ssh_tunnel.SshTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        # Falls through to WG (not configured) then returns failure
        assert result.success is False

    def test_ssh_establish_fails_returns_failure(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = None

        with patch('modules.helpers.ssh_tunnel.SshTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is False

    def test_ssh_verify_fails_tears_down(self):
        stage = make_stage(ssh_server='example.com')
        ctx = {'dns_reachable_tcp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = {'interface': 'tun53', 'ip': '10.53.0.2'}
        mock_helper.verify.return_value = False

        with patch('modules.helpers.ssh_tunnel.SshTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is False
        mock_helper.teardown.assert_called_once()


# ---------------------------------------------------------------------------
# run() tests — WireGuard path
# ---------------------------------------------------------------------------

class TestDnsPortTunnelWg:

    def test_wg_success(self):
        stage = make_stage(wg_config_path='/etc/wireguard/wg0.conf')
        ctx = {'dns_reachable_udp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = {'interface': 'wg0', 'ip': '10.0.0.2'}
        mock_helper.verify.return_value = True
        mock_helper.tunnel_interface = 'wg0'

        with patch('modules.helpers.wg_tunnel.WgTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is True
        assert result.has_internet is True
        assert result.context_updates['tunnel_type'] == 'wireguard'

    def test_wg_not_installed(self):
        stage = make_stage(wg_config_path='/etc/wireguard/wg0.conf')
        ctx = {'dns_reachable_udp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = False

        with patch('modules.helpers.wg_tunnel.WgTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is False

    def test_wg_verify_fails_tears_down(self):
        stage = make_stage(wg_config_path='/etc/wireguard/wg0.conf')
        ctx = {'dns_reachable_udp': True}

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = {'interface': 'wg0', 'ip': '10.0.0.2'}
        mock_helper.verify.return_value = False

        with patch('modules.helpers.wg_tunnel.WgTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is False
        mock_helper.teardown.assert_called_once()


# ---------------------------------------------------------------------------
# SSH tried first, falls back to WG
# ---------------------------------------------------------------------------

class TestDnsPortTunnelFallback:

    def test_ssh_fails_then_wg_succeeds(self):
        """When SSH is configured but fails, WG is tried next."""
        stage = make_stage(
            ssh_server='example.com',
            wg_config_path='/etc/wireguard/wg0.conf',
        )
        ctx = {'dns_reachable_tcp': True, 'dns_reachable_udp': True}

        ssh_helper = MagicMock()
        ssh_helper.is_available.return_value = True
        ssh_helper.establish.return_value = None  # SSH fails

        wg_helper = MagicMock()
        wg_helper.is_available.return_value = True
        wg_helper.establish.return_value = {'interface': 'wg0', 'ip': '10.0.0.2'}
        wg_helper.verify.return_value = True
        wg_helper.tunnel_interface = 'wg0'

        with patch('modules.helpers.ssh_tunnel.SshTunnelHelper',
                   return_value=ssh_helper), \
             patch('modules.helpers.wg_tunnel.WgTunnelHelper',
                   return_value=wg_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is True
        assert result.context_updates['tunnel_type'] == 'wireguard'


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

class TestSshTunnelHelper:

    @patch('modules.helpers.ssh_tunnel.shutil.which', return_value=None)
    def test_not_available(self, _):
        from modules.helpers.ssh_tunnel import SshTunnelHelper
        h = SshTunnelHelper(server='example.com')
        assert h.is_available() is False

    @patch('modules.helpers.ssh_tunnel.shutil.which', return_value='/usr/bin/ssh')
    def test_available(self, _):
        from modules.helpers.ssh_tunnel import SshTunnelHelper
        h = SshTunnelHelper(server='example.com')
        assert h.is_available() is True

    def test_teardown_kills_process(self):
        from modules.helpers.ssh_tunnel import SshTunnelHelper
        h = SshTunnelHelper(server='example.com')
        mock_proc = MagicMock()
        h.process = mock_proc
        h.tunnel_interface = 'tun53'
        with patch('modules.helpers.ssh_tunnel.subprocess.run'):
            h.teardown()
        mock_proc.terminate.assert_called_once()
        assert h.process is None
        assert h.tunnel_interface is None


class TestWgTunnelHelper:

    @patch('modules.helpers.wg_tunnel.shutil.which', return_value=None)
    def test_not_available_no_binary(self, _):
        from modules.helpers.wg_tunnel import WgTunnelHelper
        h = WgTunnelHelper(config_path='/etc/wireguard/wg0.conf')
        assert h.is_available() is False

    @patch('modules.helpers.wg_tunnel.os.path.isfile', return_value=False)
    @patch('modules.helpers.wg_tunnel.shutil.which', return_value='/usr/bin/wg-quick')
    def test_not_available_no_config(self, _, __):
        from modules.helpers.wg_tunnel import WgTunnelHelper
        h = WgTunnelHelper(config_path='/nonexistent.conf')
        assert h.is_available() is False

    @patch('modules.helpers.wg_tunnel.os.path.isfile', return_value=True)
    @patch('modules.helpers.wg_tunnel.shutil.which', return_value='/usr/bin/wg-quick')
    def test_available(self, _, __):
        from modules.helpers.wg_tunnel import WgTunnelHelper
        h = WgTunnelHelper(config_path='/etc/wireguard/wg0.conf')
        assert h.is_available() is True

    def test_teardown_calls_wg_down(self):
        from modules.helpers.wg_tunnel import WgTunnelHelper
        h = WgTunnelHelper(config_path='/etc/wireguard/wg0.conf')
        h.tunnel_interface = 'wg0'
        with patch('modules.helpers.wg_tunnel.subprocess.run') as mock_run:
            h.teardown()
        # wg-quick down should have been called
        mock_run.assert_called_once()
        assert 'down' in mock_run.call_args[0][0]
        assert h.tunnel_interface is None

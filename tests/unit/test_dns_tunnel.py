"""Unit tests for the DNS tunnel helper and stage."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from vasili import PipelineStage, StageResult, WifiNetwork


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# DnsTunnelStage tests
# ---------------------------------------------------------------------------

class TestDnsTunnelStageCanRun:
    """Test can_run gating logic."""

    def _make_stage(self, server_domain='t.example.com'):
        from modules.stages.dns_tunnel import DnsTunnelStage
        stage = DnsTunnelStage()
        # Inject config so can_run can check server_domain
        stage._stage_config = {
            'server_domain': server_domain,
            'tunnel_password': '',
            'tunnel_type': 'iodine',
            'timeout': 30,
        }
        return stage

    def test_skip_when_internet_already_available(self):
        stage = self._make_stage()
        ctx = {'has_internet': True, 'dns_reachable_tcp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_skip_when_no_dns_reachability(self):
        stage = self._make_stage()
        ctx = {'has_internet': False, 'dns_reachable_tcp': False,
               'dns_reachable_udp': False}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_skip_when_no_server_configured(self):
        stage = self._make_stage(server_domain='')
        ctx = {'has_internet': False, 'dns_reachable_tcp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is False

    def test_runs_when_dns_tcp_reachable(self):
        stage = self._make_stage()
        ctx = {'has_internet': False, 'dns_reachable_tcp': True,
               'dns_reachable_udp': False}
        assert stage.can_run(make_network(), make_card(), ctx) is True

    def test_runs_when_dns_udp_reachable(self):
        stage = self._make_stage()
        ctx = {'has_internet': False, 'dns_reachable_tcp': False,
               'dns_reachable_udp': True}
        assert stage.can_run(make_network(), make_card(), ctx) is True


class TestDnsTunnelStageRun:
    """Test run() with mocked helper."""

    def _make_stage(self):
        from modules.stages.dns_tunnel import DnsTunnelStage
        stage = DnsTunnelStage()
        stage._stage_config = {
            'server_domain': 't.example.com',
            'tunnel_password': 'secret',
            'tunnel_type': 'iodine',
            'timeout': 10,
        }
        return stage

    @patch('modules.stages.dns_tunnel.network_isolation')
    @patch('modules.helpers.dns_tunnel.DnsTunnelHelper')
    def test_unavailable_tool_returns_failure(self, MockHelper, mock_ni):
        """If iodine is not installed, stage fails gracefully."""
        from modules.stages.dns_tunnel import DnsTunnelStage
        stage = self._make_stage()

        mock_instance = MagicMock()
        mock_instance.is_available.return_value = False
        MockHelper.return_value = mock_instance
        mock_ni.get_interface_ip.return_value = '10.0.0.5'

        ctx = {'dns_reachable_tcp': True, 'reachable_dns_servers': ['8.8.8.8:53']}

        with patch('modules.stages.dns_tunnel.DnsTunnelStage.run') as real_run:
            # Test the actual stage logic by calling directly
            pass

        # Call the real run method
        result = stage.run(make_network(), make_card(), ctx)
        assert result.success is False
        assert result.has_internet is False
        assert 'not installed' in result.message

    def test_establish_failure(self):
        """If tunnel fails to establish, stage returns failure."""
        stage = self._make_stage()

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = None

        ctx = {'dns_reachable_tcp': True, 'reachable_dns_servers': ['8.8.8.8:53']}

        with patch('modules.helpers.dns_tunnel.DnsTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is False
        assert result.has_internet is False

    def test_verify_failure_tears_down(self):
        """If tunnel is up but no internet, it's torn down."""
        stage = self._make_stage()

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = {'interface': 'dns0', 'ip': '10.0.0.2'}
        mock_helper.verify.return_value = False

        ctx = {'dns_reachable_tcp': True, 'reachable_dns_servers': ['8.8.8.8:53']}

        with patch('modules.helpers.dns_tunnel.DnsTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is False
        mock_helper.teardown.assert_called_once()

    def test_success_sets_context(self):
        """Successful tunnel sets tunnel_active, tunnel_interface, etc."""
        stage = self._make_stage()

        mock_helper = MagicMock()
        mock_helper.is_available.return_value = True
        mock_helper.establish.return_value = {'interface': 'dns0', 'ip': '10.0.0.2'}
        mock_helper.verify.return_value = True
        mock_helper.tunnel_interface = 'dns0'
        mock_helper.tunnel_type = 'iodine'

        ctx = {'dns_reachable_udp': True, 'reachable_dns_servers': ['1.1.1.1:53']}

        with patch('modules.helpers.dns_tunnel.DnsTunnelHelper', return_value=mock_helper):
            result = stage.run(make_network(), make_card(), ctx)

        assert result.success is True
        assert result.has_internet is True
        assert result.context_updates['tunnel_active'] is True
        assert result.context_updates['tunnel_interface'] == 'dns0'
        assert result.context_updates['tunnel_type'] == 'iodine'
        assert result.context_updates['_tunnel_helper'] is mock_helper


# ---------------------------------------------------------------------------
# DnsTunnelHelper tests
# ---------------------------------------------------------------------------

class TestDnsTunnelHelper:

    @patch('modules.helpers.dns_tunnel.shutil.which', return_value=None)
    def test_not_available_when_binary_missing(self, mock_which):
        from modules.helpers.dns_tunnel import DnsTunnelHelper
        h = DnsTunnelHelper(server_domain='t.example.com')
        assert h.is_available() is False
        mock_which.assert_called_with('iodine')

    @patch('modules.helpers.dns_tunnel.shutil.which', return_value='/usr/bin/iodine')
    def test_available_when_binary_exists(self, mock_which):
        from modules.helpers.dns_tunnel import DnsTunnelHelper
        h = DnsTunnelHelper(server_domain='t.example.com')
        assert h.is_available() is True

    @patch('modules.helpers.dns_tunnel.subprocess.Popen')
    def test_establish_success(self, mock_popen, mock_network_isolation):
        from modules.helpers.dns_tunnel import DnsTunnelHelper

        # Override the autouse mock_network_isolation for the helper's import
        with patch('modules.helpers.dns_tunnel.network_isolation') as mock_ni:
            # Simulate dns0 appearing with IP after a few polls.
            # Use a function so it doesn't exhaust like a list.
            call_count = 0
            def fake_get_ip(iface):
                nonlocal call_count
                call_count += 1
                return '10.0.0.2' if call_count >= 3 else None

            mock_ni.get_interface_ip.side_effect = fake_get_ip
            mock_proc = MagicMock()
            mock_popen.return_value = mock_proc

            h = DnsTunnelHelper(server_domain='t.example.com', password='pw',
                                timeout=5)
            result = h.establish(source_ip='192.168.1.5', nameserver='8.8.8.8')

        assert result is not None
        assert result['interface'] == 'dns0'
        assert result['ip'] == '10.0.0.2'
        assert h.process is mock_proc

    @patch('modules.helpers.dns_tunnel.subprocess.Popen')
    @patch('modules.helpers.dns_tunnel.network_isolation')
    def test_establish_timeout_tears_down(self, mock_ni, mock_popen):
        from modules.helpers.dns_tunnel import DnsTunnelHelper

        # Interface never appears
        mock_ni.get_interface_ip.return_value = None
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b'error output', None)
        mock_popen.return_value = mock_proc

        h = DnsTunnelHelper(server_domain='t.example.com', timeout=1)
        result = h.establish()

        assert result is None
        assert h.process is None  # Teardown was called

    def test_teardown_kills_process(self):
        from modules.helpers.dns_tunnel import DnsTunnelHelper
        h = DnsTunnelHelper(server_domain='t.example.com')

        mock_proc = MagicMock()
        h.process = mock_proc
        h.tunnel_interface = 'dns0'

        with patch('modules.helpers.dns_tunnel.subprocess.run'):
            h.teardown()

        mock_proc.terminate.assert_called_once()
        assert h.process is None
        assert h.tunnel_interface is None


# ---------------------------------------------------------------------------
# PipelineModule tunnel integration
# ---------------------------------------------------------------------------

class TestPipelineTunnelTeardown:
    """Test that _teardown_tunnel is called on disconnect path."""

    def test_teardown_called_with_helper(self):
        from vasili import PipelineModule
        mock_helper = MagicMock()
        ctx = {'_tunnel_helper': mock_helper}
        PipelineModule._teardown_tunnel(ctx)
        mock_helper.teardown.assert_called_once()

    def test_teardown_noop_without_helper(self):
        from vasili import PipelineModule
        # Should not raise
        PipelineModule._teardown_tunnel({})
        PipelineModule._teardown_tunnel({'_tunnel_helper': None})

    def test_teardown_handles_exception(self):
        from vasili import PipelineModule
        mock_helper = MagicMock()
        mock_helper.teardown.side_effect = RuntimeError('boom')
        ctx = {'_tunnel_helper': mock_helper}
        # Should not raise
        PipelineModule._teardown_tunnel(ctx)

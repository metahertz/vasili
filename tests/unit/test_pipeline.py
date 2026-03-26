"""Unit tests for the pipeline architecture (Phase 0a + Phase 1a)."""

import pytest
import time
from unittest.mock import patch, MagicMock

from vasili import (
    PipelineStage, PipelineModule, StageResult, StrategyResult,
    ReconModule, ConnectionModule, WifiNetwork,
)


class FakePassStage(PipelineStage):
    name = 'fake_pass'
    requires_consent = False

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        return StageResult(
            success=True, has_internet=True,
            context_updates={'fake_ran': True},
            message='Fake internet',
        )


class FakeFailStage(PipelineStage):
    name = 'fake_fail'
    requires_consent = False

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        return StageResult(
            success=False, has_internet=False,
            context_updates={'fake_fail_ran': True},
            message='No internet',
        )


class FakeConsentStage(PipelineStage):
    name = 'needs_consent'
    requires_consent = True

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        return StageResult(
            success=True, has_internet=True,
            context_updates={}, message='Consent stage ran',
        )


class FakeConditionalStage(PipelineStage):
    name = 'conditional'
    requires_consent = False

    def can_run(self, network, card, context):
        return context.get('fake_fail_ran', False)

    def run(self, network, card, context):
        return StageResult(
            success=True, has_internet=True,
            context_updates={'conditional_ran': True},
            message='Conditional triggered',
        )


def _make_network(ssid='TestNet', is_open=True):
    return WifiNetwork(
        ssid=ssid, bssid='AA:BB:CC:DD:EE:FF',
        signal_strength=80, channel=6,
        encryption_type='' if is_open else 'WPA2',
        is_open=is_open,
    )


def _make_card_manager():
    card = MagicMock()
    card.interface = 'wlan0'
    card.connect.return_value = True
    card.get_ip_address.return_value = '192.168.1.100'
    card._routing_info = None

    mgr = MagicMock()
    mgr.get_card.return_value = card
    return mgr, card


@pytest.mark.unit
class TestStageResult:
    def test_basic_fields(self):
        r = StageResult(success=True, has_internet=True, context_updates={'k': 'v'})
        assert r.success is True
        assert r.has_internet is True
        assert r.context_updates == {'k': 'v'}
        assert r.message == ''


@pytest.mark.unit
class TestPipelineStage:
    def test_abstract_methods(self):
        stage = PipelineStage()
        with pytest.raises(NotImplementedError):
            stage.can_run(None, None, {})
        with pytest.raises(NotImplementedError):
            stage.run(None, None, {})

    def test_default_config_schema(self):
        assert PipelineStage().get_config_schema() == {}


@pytest.mark.unit
class TestPipelineModule:
    def test_stops_on_internet(self):
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, stages=[FakePassStage()])

        with patch.object(pipeline, 'run_speedtest', return_value=(50.0, 25.0, 10.0)):
            result = pipeline.connect(_make_network())

        assert result.connected is True
        assert result.download_speed == 50.0
        assert 'pipeline:fake_pass' in result.connection_method

    def test_tries_multiple_stages(self):
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, stages=[FakeFailStage(), FakePassStage()])

        with patch.object(pipeline, 'run_speedtest', return_value=(50.0, 25.0, 10.0)):
            result = pipeline.connect(_make_network())

        assert result.connected is True

    def test_all_fail_disconnects(self):
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, stages=[FakeFailStage()])

        result = pipeline.connect(_make_network())

        assert result.connected is False
        card.disconnect.assert_called_once()

    def test_context_passes_between_stages(self):
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(
            mgr, stages=[FakeFailStage(), FakeConditionalStage()]
        )

        with patch.object(pipeline, 'run_speedtest', return_value=(50.0, 25.0, 10.0)):
            result = pipeline.connect(_make_network())

        # FakeFailStage sets fake_fail_ran=True, ConditionalStage sees it
        assert result.connected is True

    def test_consent_stage_skipped_without_consent(self):
        mgr, card = _make_card_manager()
        consent = MagicMock()
        consent.has_consent.return_value = False

        pipeline = PipelineModule(
            mgr, stages=[FakeConsentStage()], consent_manager=consent
        )

        result = pipeline.connect(_make_network())

        # Stage skipped due to no consent → no internet → disconnects
        assert result.connected is False
        consent.has_consent.assert_called_once()
        assert consent.has_consent.call_args[0][0] == 'needs_consent'

    def test_consent_stage_runs_with_consent(self):
        mgr, card = _make_card_manager()
        consent = MagicMock()
        consent.has_consent.return_value = True

        pipeline = PipelineModule(
            mgr, stages=[FakeConsentStage()], consent_manager=consent
        )

        with patch.object(pipeline, 'run_speedtest', return_value=(50.0, 25.0, 10.0)):
            result = pipeline.connect(_make_network())

        assert result.connected is True

    def test_no_card_available(self):
        mgr = MagicMock()
        mgr.get_card.return_value = None
        pipeline = PipelineModule(mgr, stages=[FakePassStage()])

        result = pipeline.connect(_make_network())
        assert result.connected is False

    def test_priority_attribute(self):
        assert PipelineModule.priority == 10
        assert ConnectionModule.priority == 50


class FakeSlowPassStage(PipelineStage):
    """A pass stage that sleeps briefly — verifies parallel execution."""
    name = 'slow_pass'
    requires_consent = False

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        time.sleep(0.05)
        return StageResult(
            success=True, has_internet=True,
            context_updates={'slow_pass_ran': True},
            message='Slow internet',
        )


class FakeTunnelStage(PipelineStage):
    """Simulates a tunnel that provides internet via a virtual interface."""
    name = 'fake_tunnel'
    requires_consent = False

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        helper = MagicMock()
        return StageResult(
            success=True, has_internet=True,
            context_updates={
                'tunnel_active': True,
                'tunnel_interface': 'dns0',
                '_tunnel_helper': helper,
            },
            message='Tunnel up',
        )


class FakeDiscoveryStage(PipelineStage):
    """Sets context but does not achieve internet."""
    name = 'discovery'
    requires_consent = False

    def can_run(self, network, card, context):
        return True

    def run(self, network, card, context):
        return StageResult(
            success=True, has_internet=False,
            context_updates={'discovered': True},
            message='Found something',
        )


@pytest.mark.unit
class TestPipelineParallelPhase:
    """Tests for parallel phase execution."""

    def test_both_parallel_stages_succeed_best_speed_wins(self):
        """When both strategies get internet, speedtest picks the fastest."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, phases=[
            [FakePassStage(), FakeSlowPassStage()],
        ])

        speeds = iter([(50.0, 25.0, 10.0), (5.0, 2.0, 100.0)])
        with patch.object(pipeline, 'run_speedtest', side_effect=speeds):
            result = pipeline.connect(_make_network())

        assert result.connected is True
        assert result.download_speed == 50.0  # best speed used directly

    def test_single_parallel_winner_no_extra_speedtest(self):
        """Single winner skips the comparison speedtest — connect() does it."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, phases=[
            [FakePassStage(), FakeFailStage()],
        ])

        with patch.object(pipeline, 'run_speedtest', return_value=(30.0, 15.0, 20.0)) as mock_st:
            result = pipeline.connect(_make_network())

        assert result.connected is True
        # run_speedtest called once (in connect's success block), not in parallel phase
        assert mock_st.call_count == 1

    def test_no_parallel_winners_merges_context(self):
        """When no parallel stage gets internet, their context merges and pipeline continues."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, phases=[
            [FakeFailStage(), FakeDiscoveryStage()],
            FakeConditionalStage(),  # requires fake_fail_ran in context
        ])

        with patch.object(pipeline, 'run_speedtest', return_value=(10.0, 5.0, 50.0)):
            result = pipeline.connect(_make_network())

        # FakeConditionalStage should have run because fake_fail_ran was merged
        assert result.connected is True

    def test_tunnel_loser_gets_torn_down(self):
        """When tunnel wins but direct also wins, loser's tunnel is torn down."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, phases=[
            [FakePassStage(), FakeTunnelStage()],
        ])

        # First speedtest (for one winner), second for the other
        speeds = iter([(50.0, 25.0, 10.0), (0.5, 0.2, 500.0)])
        with patch.object(pipeline, 'run_speedtest', side_effect=speeds):
            result = pipeline.connect(_make_network())

        assert result.connected is True
        # The slower tunnel should have been torn down
        # Find the tunnel stage's helper from last_stage_log
        tunnel_entries = [e for e in pipeline.last_stage_log
                          if e['stage'] == 'fake_tunnel']
        assert len(tunnel_entries) == 1

    def test_backward_compat_stages_kwarg(self):
        """Passing stages=[...] still works identically."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, stages=[FakeFailStage(), FakePassStage()])

        with patch.object(pipeline, 'run_speedtest', return_value=(50.0, 25.0, 10.0)):
            result = pipeline.connect(_make_network())

        assert result.connected is True
        assert 'pipeline:fake_pass' in result.connection_method

    def test_flat_stages_list_for_api_introspection(self):
        """self.stages contains a flat list of all stages from all phases."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, phases=[
            FakeFailStage(),
            [FakePassStage(), FakeSlowPassStage()],
            FakeConditionalStage(),
        ])

        names = [s.name for s in pipeline.stages]
        assert names == ['fake_fail', 'fake_pass', 'slow_pass', 'conditional']

    def test_sequential_fallback_after_parallel_phase_fails(self):
        """Sequential stages after a failing parallel phase still run."""
        mgr, card = _make_card_manager()
        pipeline = PipelineModule(mgr, phases=[
            [FakeFailStage(), FakeDiscoveryStage()],  # parallel, no internet
            FakePassStage(),                           # sequential fallback
        ])

        with patch.object(pipeline, 'run_speedtest', return_value=(40.0, 20.0, 15.0)):
            result = pipeline.connect(_make_network())

        assert result.connected is True
        assert 'pipeline:fake_pass' in result.connection_method


@pytest.mark.unit
class TestReconModule:
    def test_abstract_methods(self):
        mod = ReconModule()
        with pytest.raises(NotImplementedError):
            mod.start()
        with pytest.raises(NotImplementedError):
            mod.stop()

    def test_defaults(self):
        mod = ReconModule()
        assert mod.get_data() == {}
        assert mod.get_config_schema() == {}

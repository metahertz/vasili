"""Reusable pipeline stages for vasili connection modules.

Stages are building blocks that can be composed into pipelines.
Each stage runs against an already-connected card and communicates
results via a shared context dict.

Usage:
    from modules.stages import ConnectivityCheckStage, DnsProbeStage

    class MyPipeline(PipelineModule):
        def __init__(self, card_manager, **kwargs):
            stages = [
                ConnectivityCheckStage(),
                DnsProbeStage(),
            ]
            super().__init__(card_manager, stages=stages, **kwargs)
"""

from modules.stages.connectivity import ConnectivityCheckStage
from modules.stages.dns_probe import DnsProbeStage
from modules.stages.captive_portal import CaptivePortalStage
from modules.stages.credentials import SavedCredentialsStage, ConfiguredKeysStage
from modules.stages.pmkid import PmkidCaptureStage
from modules.stages.connection_gate import ConnectionGateStage

__all__ = [
    'ConnectivityCheckStage',
    'DnsProbeStage',
    'CaptivePortalStage',
    'SavedCredentialsStage',
    'ConfiguredKeysStage',
    'PmkidCaptureStage',
    'ConnectionGateStage',
]

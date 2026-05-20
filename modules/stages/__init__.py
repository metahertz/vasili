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
from modules.stages.known_networks import KnownCredentialsStage
from modules.stages.pmkid import PmkidCaptureStage
from modules.stages.connection_gate import ConnectionGateStage
from modules.stages.wep_crack import WepCrackStage, WepCommonKeysStage
from modules.stages.dns_tunnel import DnsTunnelStage
from modules.stages.dns_port_tunnel import DnsPortTunnelStage
from modules.stages.dns_offload_crack import DnsOffloadCrackStage

__all__ = [
    'ConnectivityCheckStage',
    'DnsProbeStage',
    'CaptivePortalStage',
    'SavedCredentialsStage',
    'ConfiguredKeysStage',
    'KnownCredentialsStage',
    'PmkidCaptureStage',
    'ConnectionGateStage',
    'WepCrackStage',
    'WepCommonKeysStage',
    'DnsTunnelStage',
    'DnsPortTunnelStage',
    'DnsOffloadCrackStage',
    'STAGE_REGISTRY',
    'get_stage_registry',
]


# Registry keyed by stage.name (the same identifier persisted in the
# pipeline-builder config) so the API and UI can list every stage we
# know how to instantiate. ``mac_clone`` lives outside this package but
# is added by ``get_stage_registry()`` to keep the import graph flat.
STAGE_REGISTRY: dict = {
    ConnectivityCheckStage.name: ConnectivityCheckStage,
    ConnectionGateStage.name: ConnectionGateStage,
    SavedCredentialsStage.name: SavedCredentialsStage,
    ConfiguredKeysStage.name: ConfiguredKeysStage,
    KnownCredentialsStage.name: KnownCredentialsStage,
    DnsProbeStage.name: DnsProbeStage,
    CaptivePortalStage.name: CaptivePortalStage,
    DnsTunnelStage.name: DnsTunnelStage,
    DnsPortTunnelStage.name: DnsPortTunnelStage,
    DnsOffloadCrackStage.name: DnsOffloadCrackStage,
    PmkidCaptureStage.name: PmkidCaptureStage,
    WepCommonKeysStage.name: WepCommonKeysStage,
    WepCrackStage.name: WepCrackStage,
}


def get_stage_registry() -> dict:
    """Return ``{stage_name: stage_class}`` for every known stage.

    Lazy-imports ``MacCloneStage`` so this module stays independent of
    ``modules.macClone`` (which itself imports from ``vasili``).
    """
    registry = dict(STAGE_REGISTRY)
    try:
        from modules.macClone import MacCloneStage
        registry[MacCloneStage.name] = MacCloneStage
    except Exception:
        pass
    return registry

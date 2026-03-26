"""Helper subsystems that pipeline stages can invoke.

Helpers are heavier subsystems (tunnels, VPNs, etc.) that a thin
PipelineStage wrapper lazy-imports when specific context conditions
are met.  They are NOT ConnectionModules and do not participate in
the scan loop.
"""

"""
    Command runner implementations for the Windows sysman package.

    Public surface:

    - :class:`WindowsLocalCommandRunner` -- subprocess-backed local
      runner that satisfies
      :class:`automojo.interfaces.icommandrunner.ICommandRunner`.
      Performs an Administrator-token pre-check at construction time
      and refuses ``elevated=True`` calls when the host process is
      not elevated, surfacing the refusal as a typed
      :class:`WindowsRunResult` with ``elevation_blocked=True``.
    - :class:`WindowsSshCommandRunner` -- skeleton SSH runner that
      preserves the public surface of the local runner so that a
      future patch can plug in the transport (``paramiko`` against
      OpenSSH Server) without affecting callers.
    - :class:`WindowsRunResult` -- the runner result dataclass.
    - The aspect classes for retry, timeout, and persistence.
"""

from automojo.sysman.windows.runners.aspects import (
    IWindowsExecutionAspect,
    WindowsPersistenceAspect,
    WindowsPersistenceAspectConfig,
    WindowsRetryAspect,
    WindowsRetryAspectConfig,
    WindowsRetryDecision,
    WindowsTimeoutAspect,
    WindowsTimeoutAspectConfig,
)
from automojo.sysman.windows.runners.local import (
    WindowsLocalCommandRunner,
    WindowsLocalRunnerConfig,
)
from automojo.sysman.windows.runners.models import (
    RunResult,
    WindowsExecutionContext,
    WindowsRunRecord,
    WindowsRunResult,
    WindowsSshErrorType,
    WindowsSshReliabilityMetrics,
)
from automojo.sysman.windows.runners.ssh import (
    WindowsSshCommandRunner,
    WindowsSshRunnerConfig,
)


__all__ = [
    "IWindowsExecutionAspect",
    "RunResult",
    "WindowsExecutionContext",
    "WindowsLocalCommandRunner",
    "WindowsLocalRunnerConfig",
    "WindowsPersistenceAspect",
    "WindowsPersistenceAspectConfig",
    "WindowsRetryAspect",
    "WindowsRetryAspectConfig",
    "WindowsRetryDecision",
    "WindowsRunRecord",
    "WindowsRunResult",
    "WindowsSshCommandRunner",
    "WindowsSshErrorType",
    "WindowsSshReliabilityMetrics",
    "WindowsSshRunnerConfig",
    "WindowsTimeoutAspect",
    "WindowsTimeoutAspectConfig",
]

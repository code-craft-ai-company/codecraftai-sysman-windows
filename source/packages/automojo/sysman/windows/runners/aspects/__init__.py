"""
    Execution aspects that compose cross-cutting behaviour onto the
    Windows command runners.
"""

from automojo.sysman.windows.runners.aspects.base import IWindowsExecutionAspect
from automojo.sysman.windows.runners.aspects.persistence import (
    WindowsPersistenceAspect,
    WindowsPersistenceAspectConfig,
)
from automojo.sysman.windows.runners.aspects.retry import (
    WindowsRetryAspect,
    WindowsRetryAspectConfig,
    WindowsRetryDecision,
)
from automojo.sysman.windows.runners.aspects.timeout import (
    WindowsTimeoutAspect,
    WindowsTimeoutAspectConfig,
)


__all__ = [
    "IWindowsExecutionAspect",
    "WindowsPersistenceAspect",
    "WindowsPersistenceAspectConfig",
    "WindowsRetryAspect",
    "WindowsRetryAspectConfig",
    "WindowsRetryDecision",
    "WindowsTimeoutAspect",
    "WindowsTimeoutAspectConfig",
]

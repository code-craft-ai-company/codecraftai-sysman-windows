"""
    Execution aspects that compose cross-cutting behaviour onto the
    Windows command runners.
"""

from ccai.sysman.windows.runners.aspects.base import IWindowsExecutionAspect
from ccai.sysman.windows.runners.aspects.persistence import (
    WindowsPersistenceAspect,
    WindowsPersistenceAspectConfig,
)
from ccai.sysman.windows.runners.aspects.retry import (
    WindowsRetryAspect,
    WindowsRetryAspectConfig,
    WindowsRetryDecision,
)
from ccai.sysman.windows.runners.aspects.timeout import (
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

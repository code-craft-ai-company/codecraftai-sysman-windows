"""
    Base protocol shared by every Windows execution aspect.

    Aspects compose cross-cutting behaviour (persistence, retry,
    timeout) onto a :class:`WindowsLocalCommandRunner` /
    :class:`WindowsSshCommandRunner` without modifying the runner.
    The runner invokes ``before_execute`` in registration order before
    dispatching the command and ``after_execute`` in reverse order
    afterwards, threading the :class:`WindowsRunResult` through each
    aspect.
"""

from typing import Protocol, runtime_checkable

from ccai.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
)


__all__ = [
    "IWindowsExecutionAspect",
]


@runtime_checkable
class IWindowsExecutionAspect(Protocol):
    """
        Protocol that every Windows execution aspect satisfies.
    """

    def before_execute(self, context: WindowsExecutionContext) -> None:
        """
            Invoked before the command is dispatched.  Aspects may
            inspect or mutate ``context``; raising aborts execution.

            :param context: The execution context for the pending
                            command.
        """
        ...

    def after_execute(
        self,
        context: WindowsExecutionContext,
        result: WindowsRunResult,
    ) -> WindowsRunResult:
        """
            Invoked after the command has completed.  Aspects may
            inspect or transform ``result`` before returning it.

            :param context: The execution context for the completed
                            command.
            :param result: The result returned by the runner.

            :returns: The (possibly modified) :class:`WindowsRunResult`.
        """
        ...

    @property
    def enabled(self) -> bool:
        """
            Whether the aspect is currently enabled.  Disabled aspects
            are skipped by the runner.
        """
        ...

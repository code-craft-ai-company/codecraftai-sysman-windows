"""
    Timeout aspect that injects a default timeout into every
    execution that does not already carry one (or, optionally,
    overrides any caller-supplied timeout with the configured
    default).
"""

from dataclasses import dataclass

from ccai.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
)


@dataclass
class WindowsTimeoutAspectConfig:
    """
        Configuration for :class:`WindowsTimeoutAspect`.

        :param default_timeout: Default timeout in seconds applied to
                                commands that do not set their own.
        :param allow_override: When True, caller-supplied timeouts are
                               kept as-is.  When False, the default
                               always wins.
    """

    default_timeout: float
    allow_override: bool = True


class WindowsTimeoutAspect:
    """
        Execution aspect that ensures every command has a timeout.
    """

    def __init__(self, config: WindowsTimeoutAspectConfig) -> None:
        """
            :param config: Timeout configuration.
        """
        self._config: WindowsTimeoutAspectConfig = config
        self._enabled: bool = True
        return

    @property
    def enabled(self) -> bool:
        """
            Whether the aspect is active.
        """
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        return

    @property
    def config(self) -> WindowsTimeoutAspectConfig:
        """
            The active :class:`WindowsTimeoutAspectConfig`.
        """
        return self._config

    def before_execute(self, context: WindowsExecutionContext) -> None:
        """
            Apply the default timeout, honouring or overriding the
            caller-supplied value per the configured policy.
        """
        if self._config.allow_override is True:
            if context.timeout is None:
                context.timeout = self._config.default_timeout
        else:
            context.timeout = self._config.default_timeout
        return

    def after_execute(
        self,
        context: WindowsExecutionContext,
        result: WindowsRunResult,
    ) -> WindowsRunResult:
        """
            No-op after execution.  Timeout enforcement happens inside
            the runner.
        """
        return result

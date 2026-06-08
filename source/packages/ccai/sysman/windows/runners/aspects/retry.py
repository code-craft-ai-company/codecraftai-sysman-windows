"""
    Retry aspect that flags whether a failed execution warrants a
    retry, based on exit code and stderr regex matches.

    The aspect itself does not re-enter the runner -- it records a
    :class:`WindowsRetryDecision` on the execution metadata so that
    the caller (or the convenience :meth:`run_with_retries` helper)
    can drive the retry loop.  This keeps the aspect's role
    observational inside ``after_execute`` and avoids surprising
    re-entrancy from inside the runner's aspect chain.
"""

import re
import time

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
)


_METADATA_RETRY_DECISION_KEY = "windows_retry.decision"
_METADATA_RETRY_ATTEMPT_KEY = "windows_retry.attempt"


@dataclass
class WindowsRetryDecision:
    """
        Signal returned by :meth:`WindowsRetryAspect.should_retry`.

        :param should_retry: True when the command should be retried.
        :param backoff_seconds: Delay before the retry attempt.
        :param reason: Human-readable rationale.
    """

    should_retry: bool
    backoff_seconds: float = 0.0
    reason: str = ""


@dataclass
class WindowsRetryAspectConfig:
    """
        Configuration for :class:`WindowsRetryAspect`.

        :param max_retries: Maximum retry attempts after the initial
                            failure.
        :param backoff_base: Base of the exponential backoff.
        :param retryable_exit_codes: Non-zero exit codes that trigger
                                     a retry.
        :param retryable_error_patterns: Regex patterns matched
                                         against ``stderr`` to trigger
                                         a retry.
    """

    max_retries: int = 3
    backoff_base: float = 2.0
    retryable_exit_codes: list[int] = field(default_factory=list)
    retryable_error_patterns: list[str] = field(default_factory=list)


class WindowsRetryAspect:
    """
        Execution aspect that records a retry recommendation on the
        execution context after each attempt.
    """

    def __init__(self, config: WindowsRetryAspectConfig) -> None:
        """
            :param config: Retry configuration.
        """
        self._config: WindowsRetryAspectConfig = config
        self._enabled: bool = True
        compiled: list[re.Pattern[str]] = []
        for pattern in config.retryable_error_patterns:
            compiled.append(re.compile(pattern))
        self._compiled_patterns: list[re.Pattern[str]] = compiled
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
    def config(self) -> WindowsRetryAspectConfig:
        """
            The active :class:`WindowsRetryAspectConfig`.
        """
        return self._config

    def before_execute(self, context: WindowsExecutionContext) -> None:
        """
            Initialise the attempt counter on the context if not
            already set.
        """
        if _METADATA_RETRY_ATTEMPT_KEY not in context.metadata:
            context.metadata[_METADATA_RETRY_ATTEMPT_KEY] = 0
        return

    def after_execute(
        self,
        context: WindowsExecutionContext,
        result: WindowsRunResult,
    ) -> WindowsRunResult:
        """
            Compute and record a :class:`WindowsRetryDecision` on the
            context's metadata.  The result itself is returned
            unchanged.
        """
        attempt_value = context.metadata.get(_METADATA_RETRY_ATTEMPT_KEY, 0)
        decision = self.should_retry(result=result, attempt=attempt_value)
        context.metadata[_METADATA_RETRY_DECISION_KEY] = decision
        return result

    def should_retry(
        self,
        *,
        result: WindowsRunResult,
        attempt: int,
    ) -> WindowsRetryDecision:
        """
            Decide whether a command should be retried given its
            current outcome.

            :param result: Result of the most recent attempt.
            :param attempt: Zero-based attempt number that just
                            completed.

            :returns: A :class:`WindowsRetryDecision`.
        """
        if attempt >= self._config.max_retries:
            terminal_decision = WindowsRetryDecision(
                should_retry=False,
                reason="max retries reached",
            )
            return terminal_decision

        if result.success is True:
            success_decision = WindowsRetryDecision(
                should_retry=False,
                reason="command succeeded",
            )
            return success_decision

        if result.elevation_blocked is True:
            elevation_decision = WindowsRetryDecision(
                should_retry=False,
                reason="elevation blocked; not retryable",
            )
            return elevation_decision

        matched_exit = False
        if result.exit_code is not None:
            matched_exit = result.exit_code in self._config.retryable_exit_codes

        matched_pattern = False
        if matched_exit is False:
            for pattern in self._compiled_patterns:
                if pattern.search(result.stderr) is not None:
                    matched_pattern = True
                    break

        if matched_exit is False and matched_pattern is False:
            non_retryable_decision = WindowsRetryDecision(
                should_retry=False,
                reason="error not retryable",
            )
            return non_retryable_decision

        backoff = self._config.backoff_base ** attempt
        if matched_exit is True:
            reason = "exit code {} is retryable".format(result.exit_code)
        else:
            reason = "stderr matched retryable pattern"

        retry_decision = WindowsRetryDecision(
            should_retry=True,
            backoff_seconds=backoff,
            reason=reason,
        )
        return retry_decision

    def run_with_retries(
        self,
        runner: object,
        args: list[str],
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> WindowsRunResult:
        """
            Execute a command through ``runner``, retrying according
            to the configured policy.

            :param runner: Object exposing a callable ``run`` matching
                           the :class:`ICommandRunner` shape.
            :param args: Argument vector.
            :param elevated: Whether to run elevated.
            :param cwd: Working directory.
            :param env: Environment overrides.
            :param timeout: Per-attempt timeout.

            :returns: The :class:`WindowsRunResult` from the final
                      attempt.
        """
        current_attempt = 0
        last_result: Optional[WindowsRunResult] = None
        while True:
            attempt_result = runner.run(  # type: ignore[attr-defined]
                args,
                elevated=elevated,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            attempt_result.retry_count = current_attempt
            last_result = attempt_result

            decision = self.should_retry(result=attempt_result, attempt=current_attempt)
            if decision.should_retry is False:
                return attempt_result

            if decision.backoff_seconds > 0.0:
                time.sleep(decision.backoff_seconds)
            current_attempt = current_attempt + 1

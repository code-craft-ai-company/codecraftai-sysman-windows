"""
    Local command runner for Windows.

    :class:`WindowsLocalCommandRunner` implements
    :class:`automojo.interfaces.icommandrunner.ICommandRunner` using
    Python's :mod:`subprocess` module.

    Windows has no shell-mediated privilege elevation analogous to
    ``sudo``.  The calling process must already be running with an
    Administrator (UAC-elevated) token.  The runner detects elevation
    at construction time and caches the result; ``run`` calls that
    pass ``elevated=True`` while the host process is NOT elevated
    return a typed FAILED :class:`WindowsRunResult` with
    ``elevation_blocked=True`` and ``exit_code=-1`` rather than
    raising or silently falling back.

    The runner supports the standard aspect-composition hooks
    (``add_aspect`` / ``remove_aspect``) and tracks per-instance
    reliability metrics for parity with
    :class:`WindowsSshCommandRunner`.
"""

import ctypes
import logging
import os
import platform
import subprocess
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from automojo.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
    WindowsSshReliabilityMetrics,
)


if TYPE_CHECKING:
    from automojo.sysman.windows.runners.aspects.base import IWindowsExecutionAspect


__all__ = [
    "WindowsLocalCommandRunner",
    "WindowsLocalRunnerConfig",
]


LOGGER = logging.getLogger(__name__)


_ELEVATION_BLOCKED_MESSAGE = (
    "Process is not elevated; cannot satisfy elevated=True. "
    "Run as Administrator."
)


@dataclass
class WindowsLocalRunnerConfig:
    """
        Configuration for :class:`WindowsLocalCommandRunner`.

        :param dry_run: When True, no subprocess is spawned and a
                        synthetic success result is returned.
        :param merge_env_with_process: When True (the default), an
                                       explicit ``env`` is merged with
                                       :data:`os.environ`.  When False,
                                       ``env`` replaces the process
                                       env entirely (the standard
                                       :func:`subprocess.run` semantic).
        :param suppress_console_window: When True (the default), the
                                        runner passes
                                        ``CREATE_NO_WINDOW`` to
                                        :func:`subprocess.run` on
                                        Windows hosts to suppress the
                                        console flash that GUI-mode
                                        Python interpreters would
                                        otherwise produce when
                                        invoking console commands.
                                        Has no effect on non-Windows
                                        hosts.
        :param skip_elevation_check: When True, the runner does not
                                     enforce the elevation pre-check
                                     and forwards ``elevated=True``
                                     through to the subprocess
                                     unchanged.  Intended for
                                     constrained-runtime hosts that
                                     manage elevation by another
                                     mechanism (e.g. JEA endpoints,
                                     scheduled tasks).
    """

    dry_run: bool = False
    merge_env_with_process: bool = True
    suppress_console_window: bool = True
    skip_elevation_check: bool = False


class WindowsLocalCommandRunner:
    """
        ``ICommandRunner`` implementation that runs commands locally
        via :func:`subprocess.run`.
    """

    def __init__(
        self,
        config: Optional[WindowsLocalRunnerConfig] = None,
    ) -> None:
        """
            :param config: Runner configuration.  Defaults are used
                           when ``None`` is supplied.
        """
        if config is None:
            config = WindowsLocalRunnerConfig()

        self._config: WindowsLocalRunnerConfig = config
        self._aspects: list["IWindowsExecutionAspect"] = []
        self._metrics: WindowsSshReliabilityMetrics = WindowsSshReliabilityMetrics()
        self._is_elevated: bool = self._detect_elevation()
        return

    @property
    def config(self) -> WindowsLocalRunnerConfig:
        """
            The active :class:`WindowsLocalRunnerConfig`.
        """
        return self._config

    @property
    def is_elevated(self) -> bool:
        """
            True when the host process is running with an
            Administrator (UAC-elevated) token, False otherwise.

            Cached at construction time.
        """
        return self._is_elevated

    @property
    def aspects(self) -> list["IWindowsExecutionAspect"]:
        """
            Snapshot of registered aspects in registration order.
        """
        rtnval = list(self._aspects)
        return rtnval

    def add_aspect(self, aspect: "IWindowsExecutionAspect") -> None:
        """
            Register an aspect to participate in subsequent
            executions.
        """
        self._aspects.append(aspect)
        return

    def remove_aspect(self, aspect: "IWindowsExecutionAspect") -> None:
        """
            Unregister a previously registered aspect.  Silent when
            the aspect was not registered.
        """
        if aspect in self._aspects:
            self._aspects.remove(aspect)
        return

    def get_metrics(self) -> Optional[WindowsSshReliabilityMetrics]:
        """
            Return the accumulated execution metrics for this runner.
        """
        return self._metrics

    def reset_metrics(self) -> None:
        """
            Reset all execution counters.
        """
        self._metrics = WindowsSshReliabilityMetrics()
        return

    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> WindowsRunResult:
        """
            Execute a single command locally.

            :param args: Argument vector for the command.  Pass
                         through verbatim; the runner does NOT
                         auto-wrap shell-script strings or prepend
                         any privilege command.
            :param elevated: When True, the runner verifies that the
                             host process holds an Administrator
                             token.  If not, the call returns a typed
                             failure :class:`WindowsRunResult`
                             without spawning a subprocess.
            :param cwd: Working directory for the command.
            :param env: Environment overrides.  Behaviour controlled
                        by
                        :attr:`WindowsLocalRunnerConfig.merge_env_with_process`.
            :param stdin: Optional stdin payload passed via
                          :func:`subprocess.run`'s ``input`` parameter.
            :param timeout: Timeout in seconds; ``None`` means no
                            timeout.
            :param on_output: Callback invoked with the captured
                              stdout.
            :param on_error: Callback invoked with the captured
                             stderr.
            :param capture_output: When False, stdout/stderr are
                                   inherited from the parent process
                                   and the callbacks receive nothing.

            :returns: A :class:`WindowsRunResult`.
        """
        context = WindowsExecutionContext(
            args=list(args),
            elevated=elevated,
            cwd=cwd,
            env=env,
            timeout=timeout,
            transport="local",
            dry_run=self._config.dry_run,
        )

        self._apply_before_aspects(context=context)

        if context.dry_run is True:
            dry_result = WindowsRunResult(
                exit_code=0,
                stdout="",
                stderr="",
                command=list(context.args),
                dry_run=True,
                transport="local",
            )
            final_dry = self._apply_after_aspects(context=context, result=dry_result)
            return final_dry

        if context.elevated is True:
            elevation_required = True
        else:
            elevation_required = False

        if elevation_required is True and self._config.skip_elevation_check is False:
            if self._is_elevated is False:
                blocked_result = WindowsRunResult(
                    exit_code=-1,
                    stdout="",
                    stderr=_ELEVATION_BLOCKED_MESSAGE,
                    command=list(context.args),
                    transport="local",
                    elevation_blocked=True,
                )
                self._update_metrics(result=blocked_result)
                final_blocked = self._apply_after_aspects(
                    context=context,
                    result=blocked_result,
                )
                return final_blocked

        invocation_args = list(context.args)
        invocation_env = self._compose_env(env=context.env)

        start_monotonic = time.monotonic()
        invocation_result = self._invoke_subprocess(
            args=invocation_args,
            cwd=context.cwd,
            env=invocation_env,
            timeout=context.timeout,
            capture_output=capture_output,
            on_output=on_output,
            on_error=on_error,
            stdin=stdin,
        )
        end_monotonic = time.monotonic()

        invocation_result.duration_ms = (end_monotonic - start_monotonic) * 1000.0
        self._update_metrics(result=invocation_result)
        final_result = self._apply_after_aspects(
            context=context,
            result=invocation_result,
        )
        return final_result

    def _detect_elevation(self) -> bool:
        """
            Detect whether the host process holds an Administrator
            token.

            On Windows, calls
            ``ctypes.windll.shell32.IsUserAnAdmin()``.  On any other
            platform (or when the underlying call fails for any
            reason), returns False so that tests can run on
            non-Windows hosts and an unprivileged process never
            mistakenly believes it is elevated.

            :returns: True when the host is Windows AND the process
                      holds an Administrator token; False otherwise.
        """
        if platform.system() != "Windows":
            return False

        try:
            shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
            is_admin_int = shell32.IsUserAnAdmin()
        except Exception as ex:
            LOGGER.warning(
                "WindowsLocalCommandRunner: elevation probe failed: %s", ex,
            )
            return False

        rtnval = bool(is_admin_int)
        return rtnval

    def _compose_env(
        self,
        *,
        env: Optional[dict[str, str]],
    ) -> Optional[dict[str, str]]:
        """
            Compose the environment dict that will be handed to
            :func:`subprocess.run`.

            :param env: Caller-supplied env overrides.

            :returns: ``None`` when ``env`` is ``None`` (subprocess
                      inherits the parent env), or a dict assembled
                      per the runner's merge policy.
        """
        if env is None:
            return None

        if self._config.merge_env_with_process is True:
            merged_env = dict(os.environ)
            merged_env.update(env)
            return merged_env

        replacement_env = dict(env)
        return replacement_env

    def _update_metrics(self, *, result: WindowsRunResult) -> None:
        """
            Update the cumulative metrics based on a single subprocess
            invocation outcome.
        """
        if result.exit_code == 0 and result.timed_out is False:
            self._metrics.success_count = self._metrics.success_count + 1
        else:
            self._metrics.failure_count = self._metrics.failure_count + 1

        if result.timed_out is True:
            self._metrics.timeout_count = self._metrics.timeout_count + 1

        if result.elevation_blocked is True:
            self._metrics.elevation_block_count = (
                self._metrics.elevation_block_count + 1
            )

        if len(result.stderr) > 0 and result.exit_code != 0:
            self._metrics.last_error = result.stderr

        if result.duration_ms is not None:
            self._metrics.total_duration_ms = (
                self._metrics.total_duration_ms + result.duration_ms
            )
            if self._metrics.min_duration_ms is None:
                self._metrics.min_duration_ms = result.duration_ms
            elif result.duration_ms < self._metrics.min_duration_ms:
                self._metrics.min_duration_ms = result.duration_ms
            if self._metrics.max_duration_ms is None:
                self._metrics.max_duration_ms = result.duration_ms
            elif result.duration_ms > self._metrics.max_duration_ms:
                self._metrics.max_duration_ms = result.duration_ms
            total_count = self._metrics.success_count + self._metrics.failure_count
            if total_count > 0:
                self._metrics.avg_duration_ms = (
                    self._metrics.total_duration_ms / total_count
                )
        return

    def _apply_before_aspects(
        self,
        *,
        context: WindowsExecutionContext,
    ) -> None:
        """
            Invoke ``before_execute`` on every enabled aspect in
            registration order.
        """
        for aspect in self._aspects:
            if aspect.enabled is True:
                aspect.before_execute(context)
        return

    def _apply_after_aspects(
        self,
        *,
        context: WindowsExecutionContext,
        result: WindowsRunResult,
    ) -> WindowsRunResult:
        """
            Invoke ``after_execute`` on every enabled aspect in
            reverse registration order, threading the result through
            each.
        """
        current = result
        for aspect in reversed(self._aspects):
            if aspect.enabled is True:
                current = aspect.after_execute(context, current)
        return current

    def _resolve_creation_flags(self) -> int:
        """
            Compute the ``creationflags`` value for
            :func:`subprocess.run` based on the runner's configuration
            and host platform.

            :returns: The combined creationflags integer.  Always
                      zero on non-Windows hosts.
        """
        if platform.system() != "Windows":
            return 0
        if self._config.suppress_console_window is False:
            return 0
        no_window_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        rtnval = no_window_flag
        return rtnval

    def _invoke_subprocess(
        self,
        *,
        args: list[str],
        cwd: Optional[str],
        env: Optional[dict[str, str]],
        timeout: Optional[float],
        capture_output: bool,
        on_output: Optional[Callable[[str], None]],
        on_error: Optional[Callable[[str], None]],
        stdin: Optional[str],
    ) -> WindowsRunResult:
        """
            Invoke :func:`subprocess.run` and map the outcome to a
            :class:`WindowsRunResult`.
        """
        creation_flags = self._resolve_creation_flags()
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                env=env,
                timeout=timeout,
                capture_output=capture_output,
                text=True,
                input=stdin,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as te:
            partial_stdout: str = ""
            partial_stderr: str = "Command timed out"
            if te.stdout is not None:
                if isinstance(te.stdout, bytes):
                    partial_stdout = te.stdout.decode(errors="replace")
                else:
                    partial_stdout = te.stdout
            if te.stderr is not None:
                if isinstance(te.stderr, bytes):
                    partial_stderr = te.stderr.decode(errors="replace")
                else:
                    partial_stderr = te.stderr

            timeout_result = WindowsRunResult(
                exit_code=-1,
                stdout=partial_stdout,
                stderr=partial_stderr,
                command=list(args),
                timed_out=True,
                transport="local",
            )
            return timeout_result
        except FileNotFoundError as fnf:
            missing_result = WindowsRunResult(
                exit_code=127,
                stdout="",
                stderr=str(fnf),
                command=list(args),
                transport="local",
            )
            return missing_result

        stdout_text = ""
        stderr_text = ""
        if proc.stdout is not None:
            stdout_text = proc.stdout
        if proc.stderr is not None:
            stderr_text = proc.stderr

        if on_output is not None and len(stdout_text) > 0:
            on_output(stdout_text)
        if on_error is not None and len(stderr_text) > 0:
            on_error(stderr_text)

        subprocess_result = WindowsRunResult(
            exit_code=proc.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            command=list(args),
            transport="local",
        )
        return subprocess_result

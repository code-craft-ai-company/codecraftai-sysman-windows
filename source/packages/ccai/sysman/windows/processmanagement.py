"""
    Windows process-management feature.

    Implements the cross-OS process-management contract for Windows
    using the PowerShell ``Get-Process``, ``Stop-Process``,
    ``Start-Process``, ``Wait-Process``, and ``taskkill`` cmdlets.
    Every verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    Verb / tool mapping:

    +------------------------+-----------------------------------------+
    | Verb                   | PowerShell tool                         |
    +========================+=========================================+
    | ``list_processes``     | ``Get-Process | Select-Object Id,Name,``|
    |                        | ``Path,UserName,CPU,WorkingSet64,``     |
    |                        | ``VirtualMemorySize64,StartTime,``      |
    |                        | ``PriorityClass | ConvertTo-Json``      |
    +------------------------+-----------------------------------------+
    | ``get_process``        | ``Get-Process -Id <pid> | Select ... |``|
    |                        | ``ConvertTo-Json``                      |
    +------------------------+-----------------------------------------+
    | ``kill_process``       | ``Stop-Process -Id <pid> -Force``       |
    +------------------------+-----------------------------------------+
    | ``kill_process_tree``  | ``taskkill /F /T /PID <pid>``           |
    +------------------------+-----------------------------------------+
    | ``start_process``      | ``Start-Process -FilePath <exe>``       |
    |                        | ``-ArgumentList <args> -PassThru |``    |
    |                        | ``ConvertTo-Json``                      |
    +------------------------+-----------------------------------------+
    | ``set_process_priority`` | ``(Get-Process -Id <pid>).PriorityClass`` |
    |                        | ``= '<class>'`` (priority maps to       |
    |                        | one of Idle / BelowNormal / Normal /    |
    |                        | AboveNormal / High / RealTime)          |
    +------------------------+-----------------------------------------+
    | ``wait_for_process``   | ``Wait-Process -Id <pid> [-Timeout <t>]`` |
    +------------------------+-----------------------------------------+
    | ``find_processes_by_name`` | ``Get-Process -Name <name> |``      |
    |                        | ``ConvertTo-Json``                      |
    +------------------------+-----------------------------------------+

    Idempotency / dry-run / elevation:

    - ``kill_process`` / ``kill_process_tree`` SKIP when the target PID
      no longer exists (verified through ``get_process``).
    - ``set_process_priority`` SKIPs when the current ``PriorityClass``
      already matches the requested class (after the integer-to-class
      mapping).
    - ``start_process`` is non-idempotent and never skips.
    - All writes honour ``config.dry_run`` and short-circuit to
      :attr:`WindowsProcessActionStatus.DRY_RUN` without invoking the
      runner.
    - Windows has no ``sudo``.  ``config.elevated=True`` is advisory:
      the manager forwards ``elevated=True`` to the runner for write
      verbs and ``elevated=False`` for reads.  The runner is
      responsible for being launched with an Administrator token, and
      for surfacing a typed FAILED result when the host process lacks
      elevation.

    Signal mapping:

    Windows has no POSIX signals.  ``Stop-Process -Force`` is the
    closest equivalent to ``SIGTERM`` / ``SIGKILL`` (with `-Force`
    behaving more like ``SIGKILL``).  ``HUP`` / ``INT`` / ``QUIT`` /
    ``USR1`` / ``USR2`` have no clean mapping; this pilot treats them
    as FAILED at the verb level rather than silently coercing them to
    ``Stop-Process``.  See :data:`_SIGNAL_TO_ACTION`.

    Priority mapping:

    Windows priority is a discrete six-level enum
    (``Idle, BelowNormal, Normal, AboveNormal, High, RealTime``).
    The cross-OS contract uses an integer in the
    POSIX nice range (-20..19); the manager maps integers to classes
    via :func:`_priority_int_to_class`.  Callers can override the
    mapping by supplying ``priority_map`` on the config.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_CONVERT_TO_JSON = "| ConvertTo-Json -Compress -Depth 5"

_LIST_SELECT_FIELDS = (
    "Id,Name,Path,UserName,CPU,WorkingSet64,VirtualMemorySize64,"
    "StartTime,PriorityClass"
)


# Six PowerShell priority classes mapped to their wire-name strings.
class WindowsProcessPriorityClass(Enum):
    """
        The six discrete Windows priority classes.

        Maps to ``System.Diagnostics.ProcessPriorityClass`` enum
        members and is round-trip-safe with PowerShell's
        ``-PriorityClass`` argument.
    """
    IDLE = "Idle"
    BELOW_NORMAL = "BelowNormal"
    NORMAL = "Normal"
    ABOVE_NORMAL = "AboveNormal"
    HIGH = "High"
    REAL_TIME = "RealTime"


# Default integer-to-class mapping for the cross-OS POSIX-style nice
# value.  Inspired by the contract's typical use of negative values for
# "more important" and positive values for "less important".
_DEFAULT_PRIORITY_MAP: dict = {
    -20: WindowsProcessPriorityClass.REAL_TIME,
    -10: WindowsProcessPriorityClass.HIGH,
    -5: WindowsProcessPriorityClass.ABOVE_NORMAL,
    0: WindowsProcessPriorityClass.NORMAL,
    10: WindowsProcessPriorityClass.BELOW_NORMAL,
    19: WindowsProcessPriorityClass.IDLE,
}


# Cross-OS signal vocabulary mapped to a Windows action.
# ``"stop"``     -> issue ``Stop-Process -Id <pid> -Force``
# ``"unmapped"`` -> return FAILED with a "no signal mapping" detail
_SIGNAL_TO_ACTION: dict = {
    "TERM": "stop",
    "KILL": "stop",
    "HUP":  "unmapped",
    "INT":  "unmapped",
    "QUIT": "unmapped",
    "USR1": "unmapped",
    "USR2": "unmapped",
}


# Backwards-compatible alias matching the contract's naming.
WindowsProcessActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class WindowsProcessManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsProcessManager`.

        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable used to host every
                                      cmdlet.  Defaults to
                                      ``"powershell.exe"`` (Windows
                                      PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param priority_map: Optional override for the integer-to-
                             class mapping consulted by
                             :meth:`WindowsProcessManager.set_process_priority`.
                             ``None`` selects the default map (-20 ->
                             RealTime, -10 -> High, ..., 19 -> Idle).
        :param wait_poll_interval_seconds: Poll cadence reserved for
                                           a future Python-side
                                           ``Wait-Process`` shim.  The
                                           current implementation
                                           delegates the wait directly
                                           to PowerShell's
                                           ``-Timeout`` parameter and
                                           does not consult this field.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    priority_map: Optional[dict] = None
    wait_poll_interval_seconds: float = 0.2


@dataclass
class WindowsProcessInfo:
    """
        Per-process record returned by ``list_processes`` /
        ``get_process`` / ``find_processes_by_name``.

        ``state`` is populated from the ``PriorityClass`` field on the
        Get-Process record, mapped to one of the
        :class:`WindowsProcessPriorityClass` values.
    """
    pid: int
    ppid: Optional[int] = None
    user: Optional[str] = None
    state: Optional[str] = None
    priority: Optional[int] = None
    nice: Optional[int] = None
    vsz: Optional[int] = None
    rss: Optional[int] = None
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None
    elapsed: Optional[str] = None
    command: Optional[str] = None
    argv: list = field(default_factory=list)


@dataclass
class WindowsProcessSpec:
    """
        Spec for :meth:`WindowsProcessManager.start_process`.
    """
    executable: str
    args: list = field(default_factory=list)
    working_dir: Optional[str] = None
    env: Optional[dict] = None
    user: Optional[str] = None


@dataclass
class WindowsProcessActionResult(FeatureActionResult):
    """
        Outcome of a process-management write.

        :param pid: Target PID.  ``None`` when no PID is known (e.g.,
                    dry-run on ``start_process``, or unmapped signal).
        :param signal: Signal name dispatched.  ``None`` when not
                       applicable.
    """
    pid: Optional[int] = None
    signal: Optional[str] = None


@dataclass
class WindowsProcessWaitResult:
    """
        Outcome of :meth:`WindowsProcessManager.wait_for_process`.
    """
    pid: int
    exit_code: Optional[int] = None
    timed_out: bool = False


# ----------------------------------------------------------------------
# Exception
# ----------------------------------------------------------------------


class WindowsProcessQueryError(Exception):
    """
        Raised when a process read fails for a reason other than the
        target PID being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional override for the exception
                            message.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Process query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsProcessManager(BaseFeatureManager):
    """
        Windows process manager.  Wraps PowerShell's ``Get-Process``,
        ``Stop-Process``, ``Start-Process``, ``Wait-Process``, and
        ``taskkill`` through the injected :class:`ICommandRunner`.
    """

    result_class = WindowsProcessActionResult
    status_class = WindowsProcessActionStatus
    feature_name = "process-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_processes(
        self,
        *,
        owner: Optional[str] = None,
    ) -> list:
        """
            List running processes via
            ``Get-Process | Select-Object ... | ConvertTo-Json``.

            :param owner: Optional username filter.  When supplied the
                          manager appends a Where-Object clause
                          filtering on ``UserName``.

            :returns: A list of :class:`WindowsProcessInfo`.
            :raises WindowsProcessQueryError: When PowerShell exits
                                              non-zero or emits
                                              malformed JSON.
        """
        if owner is None:
            script = (
                "Get-Process | Select-Object {} {}"
                .format(_LIST_SELECT_FIELDS, _CONVERT_TO_JSON)
            )
        else:
            script = (
                "Get-Process -IncludeUserName | "
                "Where-Object {{ $_.UserName -eq '{}' }} | "
                "Select-Object {} {}"
                .format(owner, _LIST_SELECT_FIELDS, _CONVERT_TO_JSON)
            )

        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        records = _parse_get_process_records(
            stdout=run_result.stdout, command=command,
        )
        return records

    def get_process(self, *, pid: int) -> Optional[WindowsProcessInfo]:
        """
            Look up a single process by PID.

            Runs ``Get-Process -Id <pid> | Select-Object ... |
            ConvertTo-Json``.  ``Get-Process -Id <unknown>`` exits
            non-zero with stderr containing
            ``"Cannot find a process with the process identifier"``.
            That shape maps to ``None``; any other non-zero exit code
            raises :class:`WindowsProcessQueryError`.

            :param pid: Target process ID.
        """
        script = (
            "Get-Process -Id {} | Select-Object {} {}"
            .format(pid, _LIST_SELECT_FIELDS, _CONVERT_TO_JSON)
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if "Cannot find" in stderr_text or "process identifier" in stderr_text:
                return None
            if len(stderr_text.strip()) == 0:
                return None
            raise WindowsProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_process_records(
            stdout=run_result.stdout, command=command,
        )
        if len(records) == 0:
            return None

        first = records[0]
        return first

    def find_processes_by_name(self, *, name: str) -> list:
        """
            Find running processes by name via
            ``Get-Process -Name <name> | Select-Object ... |
            ConvertTo-Json``.

            ``Get-Process -Name`` accepts a wildcard pattern; the name
            is forwarded verbatim.  When no process matches PowerShell
            emits a non-terminating error AND empty stdout; the
            manager treats that case as the empty-list path.

            :param name: Process name pattern.

            :returns: A list of :class:`WindowsProcessInfo`.  Empty
                      when no processes match.
        """
        script = (
            "Get-Process -Name '{}' -ErrorAction SilentlyContinue | "
            "Select-Object {} {}"
            .format(name, _LIST_SELECT_FIELDS, _CONVERT_TO_JSON)
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if "Cannot find" in stderr_text:
                return []
            if len(stderr_text.strip()) == 0:
                return []
            raise WindowsProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_process_records(
            stdout=run_result.stdout, command=command,
        )
        return records

    def wait_for_process(
        self,
        *,
        pid: int,
        timeout_seconds: Optional[float] = None,
    ) -> WindowsProcessWaitResult:
        """
            Wait for ``pid`` to exit, optionally bounded by
            ``timeout_seconds``.

            Runs ``Wait-Process -Id <pid>`` (with an optional
            ``-Timeout <seconds>`` argument when ``timeout_seconds`` is
            non-``None``).  ``Wait-Process`` exits 0 when the PID
            disappeared and non-zero with a ``TimeoutException`` in
            stderr when the timeout elapsed.

            :param pid: Target process ID.
            :param timeout_seconds: Maximum wait in wall-clock seconds.

            :returns: A :class:`WindowsProcessWaitResult`.
        """
        if timeout_seconds is None:
            script = "Wait-Process -Id {}".format(pid)
        else:
            timeout_int = int(timeout_seconds)
            script = "Wait-Process -Id {} -Timeout {}".format(
                pid, timeout_int,
            )

        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            rtnval = WindowsProcessWaitResult(
                pid=pid, exit_code=None, timed_out=False,
            )
            return rtnval

        stderr_text = run_result.stderr or ""
        if "TimeoutException" in stderr_text or "Timeout" in stderr_text:
            rtnval = WindowsProcessWaitResult(
                pid=pid, exit_code=None, timed_out=True,
            )
            return rtnval

        # ``Wait-Process`` against an unknown PID exits non-zero with
        # ``Cannot find a process with the process identifier``;
        # treat that as "process is not running" -> not timed out.
        if "Cannot find" in stderr_text:
            rtnval = WindowsProcessWaitResult(
                pid=pid, exit_code=None, timed_out=False,
            )
            return rtnval

        # Any other non-zero exit is unexpected; surface it as a wait
        # result with timed_out=False rather than raise so we satisfy
        # the contract's "MUST NOT raise on timeout" rule.
        rtnval = WindowsProcessWaitResult(
            pid=pid, exit_code=run_result.exit_code, timed_out=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def kill_process(
        self,
        *,
        pid: int,
        signal: Optional[str] = None,
    ) -> WindowsProcessActionResult:
        """
            Terminate ``pid`` with ``Stop-Process``.

            Windows has no POSIX signal facility.  ``signal=TERM`` and
            ``signal=KILL`` map to ``Stop-Process -Id <pid> -Force``.
            All other ``ProcessSignal`` values return FAILED with a
            "no Windows mapping" detail.

            :param pid: Target process ID.
            :param signal: Cross-OS signal name (``"TERM"``, ``"KILL"``,
                           ``"HUP"``, ``"INT"``, ``"QUIT"``, ``"USR1"``,
                           ``"USR2"``).  Defaults to ``"TERM"``.

            :returns: The :class:`WindowsProcessActionResult`.
        """
        effective_signal = signal if signal is not None else "TERM"
        normalised = effective_signal.upper()
        if normalised.startswith("SIG"):
            normalised = normalised[3:]

        action = _SIGNAL_TO_ACTION.get(normalised, "unmapped")
        script = "Stop-Process -Id {} -Force".format(pid)
        command = self._build_powershell_argv(script=script)

        if action == "unmapped":
            unmapped_result = self._build_result(
                status=WindowsProcessActionStatus.FAILED,
                command=command,
                pid=pid,
                signal=normalised,
                detail="signal {} has no Windows mapping".format(normalised),
                stderr=(
                    "Windows does not support POSIX signal {}".format(normalised)
                ),
            )
            return unmapped_result

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=normalised,
                detail=str(pid),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is None:
            skip_result = self._build_result(
                status=WindowsProcessActionStatus.SKIPPED,
                command=command,
                pid=pid,
                signal=normalised,
                detail="pid {} not present".format(pid),
            )
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            detail=str(pid),
            operation="kill_process",
            pid=pid,
            signal=normalised,
        )
        return rtnval

    def kill_process_tree(
        self,
        *,
        pid: int,
        signal: Optional[str] = None,
    ) -> WindowsProcessActionResult:
        """
            Terminate ``pid`` and every descendant via
            ``taskkill /F /T /PID <pid>``.

            ``taskkill`` performs the recursive walk natively, so this
            verb dispatches a single command rather than the
            children-then-root pattern used on Linux / macOS.

            :param pid: Root PID.
            :param signal: Cross-OS signal name; same mapping as
                           :meth:`kill_process`.

            :returns: The :class:`WindowsProcessActionResult`.
        """
        effective_signal = signal if signal is not None else "TERM"
        normalised = effective_signal.upper()
        if normalised.startswith("SIG"):
            normalised = normalised[3:]

        action = _SIGNAL_TO_ACTION.get(normalised, "unmapped")
        # ``taskkill`` is invoked through PowerShell so the runner shape
        # is uniform with the rest of this module.
        script = "taskkill /F /T /PID {}".format(pid)
        command = self._build_powershell_argv(script=script)

        if action == "unmapped":
            unmapped_result = self._build_result(
                status=WindowsProcessActionStatus.FAILED,
                command=command,
                pid=pid,
                signal=normalised,
                detail="signal {} has no Windows mapping".format(normalised),
                stderr=(
                    "Windows does not support POSIX signal {}".format(normalised)
                ),
            )
            return unmapped_result

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=normalised,
                detail=str(pid),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is None:
            skip_result = self._build_result(
                status=WindowsProcessActionStatus.SKIPPED,
                command=command,
                pid=pid,
                signal=normalised,
                detail="pid {} not present".format(pid),
            )
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            detail=str(pid),
            operation="kill_process_tree",
            pid=pid,
            signal=normalised,
        )
        return rtnval

    def start_process(
        self,
        *,
        spec: WindowsProcessSpec,
    ) -> WindowsProcessActionResult:
        """
            Spawn a new process via ``Start-Process -FilePath ...
            -PassThru | Select-Object Id | ConvertTo-Json``.

            ``-PassThru`` returns the spawned process object so we can
            harvest its PID without a follow-up `Get-Process`.

            :param spec: The :class:`WindowsProcessSpec`.

            :returns: The :class:`WindowsProcessActionResult` with
                      ``pid`` populated on SUCCESS.
        """
        script_parts: list = ["Start-Process"]
        script_parts.append("-FilePath '{}'".format(spec.executable))
        if len(spec.args) > 0:
            joined_args = ",".join(
                "'{}'".format(_escape_single_quote(text=str(arg)))
                for arg in spec.args
            )
            script_parts.append("-ArgumentList @({})".format(joined_args))
        if spec.working_dir is not None:
            script_parts.append("-WorkingDirectory '{}'".format(spec.working_dir))
        script_parts.append("-PassThru")
        script_parts.append("| Select-Object Id")
        script_parts.append(_CONVERT_TO_JSON)

        script = " ".join(script_parts)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsProcessActionStatus.DRY_RUN,
                command=command,
                pid=None,
                signal=None,
                detail=spec.executable,
            )
            return dry_result

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            cwd=spec.working_dir,
            env=spec.env,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsProcessActionStatus.SUCCESS
            pid_value = _parse_start_process_pid(stdout=run_result.stdout)
        else:
            mapped_status = WindowsProcessActionStatus.FAILED
            pid_value = None

        rtnval = self._build_result(
            status=mapped_status,
            command=command,
            pid=pid_value,
            signal=None,
            detail=spec.executable,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
        )
        return rtnval

    def set_process_priority(
        self,
        *,
        pid: int,
        priority: int,
    ) -> WindowsProcessActionResult:
        """
            Adjust the priority class of ``pid``.

            Maps the integer ``priority`` to one of the six
            :class:`WindowsProcessPriorityClass` values via the
            configured ``priority_map`` (or the default map).  Then
            issues
            ``(Get-Process -Id <pid>).PriorityClass = '<class>'``.

            :param pid: Target process ID.
            :param priority: Cross-OS POSIX-style priority integer
                             (e.g., -20, 0, 19).
        """
        target_class = _priority_int_to_class(
            priority=priority,
            override_map=self._typed_config().priority_map,
        )
        script = (
            "(Get-Process -Id {}).PriorityClass = '{}'"
            .format(pid, target_class.value)
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=None,
                detail=target_class.value,
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is not None and existing.state is not None:
            if existing.state == target_class.value:
                skip_result = self._build_result(
                    status=WindowsProcessActionStatus.SKIPPED,
                    command=command,
                    pid=pid,
                    signal=None,
                    detail="priority class already {}".format(target_class.value),
                )
                return skip_result

        rtnval = self._dispatch_write(
            command=command,
            detail=target_class.value,
            operation="set_process_priority",
            pid=pid,
            signal=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self, *, command: list) -> "RunResult":
        """
            Invoke the runner with ``elevated=False`` and raise
            :class:`WindowsProcessQueryError` on a non-zero exit.
        """
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        return run_result

    def _dispatch_write(
        self,
        *,
        command: list,
        detail: Optional[str],
        operation: str,
        pid: int,
        signal: Optional[str],
    ) -> WindowsProcessActionResult:
        """
            Dispatch a write command, applying ``elevated=config.elevated``.
        """
        rtnval = self._dispatch(
            command=command,
            detail=detail,
            operation=operation,
            pid=pid,
            signal=signal,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsProcessActionStatus,
        command: list,
        pid: Optional[int],
        signal: Optional[str],
        detail: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsProcessActionResult:
        """
            Construct a :class:`WindowsProcessActionResult` without
            dispatching.
        """
        rtnval = WindowsProcessActionResult(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(command),
            pid=pid,
            signal=signal,
        )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the PowerShell argv for a given scriptblock.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _typed_config(self) -> WindowsProcessManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsProcessManagerConfig`.
        """
        if isinstance(self._config, WindowsProcessManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsProcessManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers / helpers
# ----------------------------------------------------------------------


def _parse_get_process_records(
    *,
    stdout: str,
    command: list,
) -> list:
    """
        Decode JSON emitted by ``Get-Process | Select-Object ... |
        ConvertTo-Json`` into a list of :class:`WindowsProcessInfo`.

        ``ConvertTo-Json`` returns an object when the upstream cmdlet
        emits one record and an array when it emits many; the helper
        coerces the result to a list.
    """
    text = stdout.strip()
    if len(text) == 0:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise WindowsProcessQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="Get-Process emitted malformed JSON: {}".format(ex),
        ) from None

    if isinstance(data, list):
        records = data
    else:
        records = [data]

    rtnval: list = []
    for entry in records:
        if not isinstance(entry, dict):
            continue
        info = _build_process_info(entry=entry)
        if info is not None:
            rtnval.append(info)

    return rtnval


def _build_process_info(*, entry: dict) -> Optional[WindowsProcessInfo]:
    """
        Build one :class:`WindowsProcessInfo` from a Get-Process JSON
        record.
    """
    pid_value = entry.get("Id")
    if pid_value is None:
        return None
    try:
        pid_int = int(pid_value)
    except (TypeError, ValueError):
        return None

    name_value = entry.get("Name")
    path_value = entry.get("Path")
    user_value = entry.get("UserName")
    cpu_value = entry.get("CPU")
    rss_value = entry.get("WorkingSet64")
    vsz_value = entry.get("VirtualMemorySize64")
    start_value = entry.get("StartTime")
    priority_class_value = entry.get("PriorityClass")

    cpu_float = _safe_float_from_any(value=cpu_value)
    rss_int = _safe_int_from_any(value=rss_value)
    vsz_int = _safe_int_from_any(value=vsz_value)

    state_value = _coerce_priority_class_string(value=priority_class_value)

    if path_value is not None:
        argv_list = [path_value]
    elif name_value is not None:
        argv_list = [name_value]
    else:
        argv_list = []

    if isinstance(start_value, str):
        elapsed_value = start_value
    else:
        elapsed_value = None

    rtnval = WindowsProcessInfo(
        pid=pid_int,
        ppid=None,
        user=user_value if isinstance(user_value, str) else None,
        state=state_value,
        priority=None,
        nice=None,
        vsz=vsz_int,
        rss=rss_int,
        cpu_percent=cpu_float,
        mem_percent=None,
        elapsed=elapsed_value,
        command=name_value if isinstance(name_value, str) else None,
        argv=argv_list,
    )
    return rtnval


def _coerce_priority_class_string(*, value) -> Optional[str]:
    """
        Coerce the ``PriorityClass`` field to its string label.

        ``ConvertTo-Json`` serialises the underlying .NET enum as
        either an integer or a string depending on PowerShell version;
        we accept both shapes.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        # Map the integer back to a class label.  These integers come
        # from ``System.Diagnostics.ProcessPriorityClass``.
        int_to_label: dict = {
            64: WindowsProcessPriorityClass.IDLE.value,
            16384: WindowsProcessPriorityClass.BELOW_NORMAL.value,
            32: WindowsProcessPriorityClass.NORMAL.value,
            32768: WindowsProcessPriorityClass.ABOVE_NORMAL.value,
            128: WindowsProcessPriorityClass.HIGH.value,
            256: WindowsProcessPriorityClass.REAL_TIME.value,
        }
        return int_to_label.get(value)
    return None


def _parse_start_process_pid(*, stdout: str) -> Optional[int]:
    """
        Decode the PID JSON emitted by ``Start-Process -PassThru |
        Select-Object Id | ConvertTo-Json``.
    """
    text = stdout.strip()
    if len(text) == 0:
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        pid_value = data.get("Id")
    elif isinstance(data, int):
        pid_value = data
    else:
        return None

    try:
        rtnval = int(pid_value)
    except (TypeError, ValueError):
        return None

    return rtnval


def _priority_int_to_class(
    *,
    priority: int,
    override_map: Optional[dict],
) -> WindowsProcessPriorityClass:
    """
        Map a POSIX-style nice value to a Windows priority class.

        :param priority: Integer in roughly the -20..19 range.
        :param override_map: Optional caller-supplied mapping that
                             overrides the default.  Keys are integers,
                             values are :class:`WindowsProcessPriorityClass`
                             members.

        :returns: The selected :class:`WindowsProcessPriorityClass`.
    """
    if override_map is not None and len(override_map) > 0:
        active_map = override_map
    else:
        active_map = _DEFAULT_PRIORITY_MAP

    if priority in active_map:
        rtnval = active_map[priority]
        return rtnval

    # Find the closest key by absolute difference.  This keeps the
    # mapping defined for arbitrary integers without requiring callers
    # to memorise the canonical six values.
    keys = list(active_map.keys())
    best_key = keys[0]
    best_distance = abs(priority - best_key)
    for key in keys[1:]:
        distance = abs(priority - key)
        if distance < best_distance:
            best_distance = distance
            best_key = key

    rtnval = active_map[best_key]
    return rtnval


def _escape_single_quote(*, text: str) -> str:
    """
        Escape embedded single quotes for inclusion inside a
        single-quoted PowerShell string.

        PowerShell's single-quoted strings only need ``''`` to escape
        a literal quote (no backslashing); this helper applies that
        rule.
    """
    rtnval = text.replace("'", "''")
    return rtnval


def _safe_int_from_any(*, value) -> Optional[int]:
    """
        Coerce a JSON-decoded value to ``int`` when possible.
    """
    if value is None:
        return None
    try:
        rtnval = int(value)
    except (TypeError, ValueError):
        return None
    return rtnval


def _safe_float_from_any(*, value) -> Optional[float]:
    """
        Coerce a JSON-decoded value to ``float`` when possible.
    """
    if value is None:
        return None
    try:
        rtnval = float(value)
    except (TypeError, ValueError):
        return None
    return rtnval

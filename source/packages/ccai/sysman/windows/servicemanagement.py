"""
    Windows service-management feature.

    Implements the cross-OS service-management contract for Windows using
    the PowerShell ``ServiceController`` cmdlets (``Start-Service``,
    ``Stop-Service``, ``Restart-Service``, ``Set-Service``, ``Get-Service``).
    Every verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    Scope mapping:
        ``WindowsServiceScope.SYSTEM`` -> standard Service Control Manager
            scope (LocalSystem / LocalService / NetworkService).
        ``WindowsServiceScope.USER``   -> per-user services (suffixed with
            ``_<sessionid>``) are out of scope for the base contract.
            USER-scope calls return a ``FAILED`` result with a descriptive
            ``message`` and do NOT invoke the runner.

    Elevation policy:
        Windows has no ``sudo``.  ``config.elevated=True`` is advisory: the
        manager forwards ``elevated=True`` to the runner for every write
        verb and ``elevated=False`` for reads.  The runner is responsible
        for being launched with an Administrator token, and for surfacing
        a typed FAILED result when the host process lacks elevation.

    Notes on command surface:
        - Reads use ``Get-Service ... | ConvertTo-Json -Compress -Depth 5``
          for a single, robust serialization that round-trips across
          PowerShell 5.1 and PowerShell 7+.
        - ``Status`` and ``StartType`` come back as integers because
          ``ConvertTo-Json`` serializes the underlying .NET enum values as
          their numeric encoding.  See :func:`_decode_status_int` and
          :func:`_decode_start_type_int` for the mapping.
        - ``mask_service`` / ``unmask_service`` are aliased to
          ``disable`` / ``enable`` because the Windows Service Control
          Manager has no separate masking concept.
        - ``reload_service`` always returns ``FAILED`` because Windows
          services have no native reload signal; ``Restart-Service`` is
          the closest equivalent if a reconfigure-with-restart is
          acceptable.
        - ``reload_daemon`` is a SUCCESS no-op: the Windows Service
          Control Manager applies service definitions immediately.
"""

import json
import os

from dataclasses import dataclass
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

# Active-state strings surfaced on :class:`WindowsServiceStatus`.
_ACTIVE_STATE_RUNNING = "Running"
_ACTIVE_STATE_STOPPED = "Stopped"
_ACTIVE_STATE_NOT_FOUND = "NotFound"

# StartType strings surfaced on :class:`WindowsServiceStatus`.
_START_TYPE_AUTOMATIC = "Automatic"
_START_TYPE_AUTOMATIC_DELAYED = "AutomaticDelayedStart"
_START_TYPE_MANUAL = "Manual"
_START_TYPE_DISABLED = "Disabled"
_START_TYPE_BOOT = "Boot"
_START_TYPE_SYSTEM = "System"
_START_TYPE_NOT_FOUND = "NotFound"

# ``System.ServiceProcess.ServiceControllerStatus`` enum values, as emitted
# by ``ConvertTo-Json`` on a ``Get-Service`` object.
_STATUS_INT_TO_STRING: dict = {
    1: "Stopped",
    2: "StartPending",
    3: "StopPending",
    4: "Running",
    5: "ContinuePending",
    6: "PausePending",
    7: "Paused",
}

# ``System.ServiceProcess.ServiceStartMode`` enum values, as emitted by
# ``ConvertTo-Json`` on a ``Get-Service`` object.  The
# ``AutomaticDelayedStart`` (5) value is a PowerShell 7+ extension; we
# accept it for forwards compatibility.
_START_TYPE_INT_TO_STRING: dict = {
    0: _START_TYPE_BOOT,
    1: _START_TYPE_SYSTEM,
    2: _START_TYPE_AUTOMATIC,
    3: _START_TYPE_MANUAL,
    4: _START_TYPE_DISABLED,
    5: _START_TYPE_AUTOMATIC_DELAYED,
}


WindowsServiceActionStatus = ActionStatus


class WindowsServiceScope(Enum):
    """
        Scope selector for the Windows service manager.

        - ``SYSTEM`` -> standard Service Control Manager scope
          (LocalSystem / LocalService / NetworkService accounts).
        - ``USER``   -> per-user services suffixed with ``_<sessionid>``;
          out of scope for this pilot.  USER-scope calls return a
          ``FAILED`` result with a descriptive ``message`` and do NOT
          invoke the runner.
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class WindowsServiceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsServiceManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable
                                      used to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-resolved
                                      Windows PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param target_machine: Optional remote computer name forwarded to
                               cmdlets that support ``-ComputerName``.
                               Reserved for the remote-service-control
                               variants the runner may adopt; not consumed
                               by the current verbs.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    target_machine: Optional[str] = None


@dataclass
class WindowsServiceActionResult(FeatureActionResult):
    """
        Result of a service-management write operation.

        :param service_name: The service targeted.  ``None`` for
                             :meth:`WindowsServiceManager.reload_daemon`.
        :param scope: The scope targeted.
        :param message: Optional human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes (e.g. "service is already
                        running", "Windows has no reload signal").
    """
    service_name: Optional[str] = None
    scope: Optional[WindowsServiceScope] = None
    message: Optional[str] = None


@dataclass
class WindowsServiceStatus:
    """
        Current state of a single Windows service.

        :param service_name: The service queried.
        :param scope: The scope queried.
        :param state: One of the ``ServiceControllerStatus`` strings
                      (``Running``, ``Stopped``, ``Paused``, etc.) or
                      ``"NotFound"`` when the service is unknown.
        :param start_type: One of the ``ServiceStartMode`` strings
                           (``Automatic``, ``Manual``, ``Disabled``,
                           ``Boot``, ``System``,
                           ``AutomaticDelayedStart``) or ``"NotFound"``
                           when the service is unknown.
        :param display_name: The service's display name when reported by
                             ``Get-Service``; otherwise None.
        :param pid: Reserved for future ``Get-WmiObject Win32_Service``
                    integration; the current parser does not populate it.
    """
    service_name: str
    scope: WindowsServiceScope
    state: str
    start_type: Optional[str] = None
    display_name: Optional[str] = None
    pid: Optional[int] = None


@dataclass
class WindowsServiceInfo:
    """
        One row from ``Get-Service | ConvertTo-Json``.

        :param name: The service name.
        :param scope: The scope the listing was taken from.
        :param status: Decoded ``ServiceControllerStatus`` string.
        :param start_type: Decoded ``ServiceStartMode`` string when
                           reported in the listing; otherwise None.
        :param display_name: Display name column when reported; otherwise
                             None.
        :param description: Reserved for future extensions; ``Get-Service``
                            does not include the Description by default.
                            Will be populated when the manager opts into
                            ``Get-CimInstance Win32_Service``.
    """
    name: str
    scope: WindowsServiceScope
    status: str
    start_type: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None


class WindowsServiceQueryError(Exception):
    """
        Raised when a service read fails for an unrecoverable reason
        (non-zero exit + unrecognised stderr from PowerShell, or a JSON
        payload that fails to parse).
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when relevant).
            :param message: Optional pre-built message used for cases
                            where there is no failed argv to report
                            (e.g. JSON parse errors).
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Service query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _decode_status_int(value: int) -> str:
    """
        Decode a ``ServiceControllerStatus`` integer (as emitted by
        ``ConvertTo-Json``) into the matching status string.

        :param value: The integer value reported by ``ConvertTo-Json``.

        :returns: The decoded status string.  Falls back to
                  ``"Unknown"`` for unrecognised integers so callers can
                  surface the raw payload without raising on forwards-
                  compatible enum extensions.
    """
    if value in _STATUS_INT_TO_STRING:
        rtnval = _STATUS_INT_TO_STRING[value]
    else:
        rtnval = "Unknown"
    return rtnval


def _decode_start_type_int(value: int) -> str:
    """
        Decode a ``ServiceStartMode`` integer (as emitted by
        ``ConvertTo-Json``) into the matching start-type string.

        :param value: The integer value reported by ``ConvertTo-Json``.

        :returns: The decoded start-type string.  Falls back to
                  ``"Unknown"`` for unrecognised integers.
    """
    if value in _START_TYPE_INT_TO_STRING:
        rtnval = _START_TYPE_INT_TO_STRING[value]
    else:
        rtnval = "Unknown"
    return rtnval


def _normalize_status_field(raw_value) -> str:
    """
        Normalize a ``Status`` field that may be either an int (Windows
        PowerShell 5.1) or a string (PowerShell 7+, certain serializers).

        :param raw_value: The raw field value pulled out of the parsed
                          JSON object.

        :returns: A canonical ``ServiceControllerStatus`` string, or
                  ``"Unknown"`` when the field is missing or
                  unrecognised.
    """
    if isinstance(raw_value, int):
        rtnval = _decode_status_int(raw_value)
    elif isinstance(raw_value, str):
        if len(raw_value) == 0:
            rtnval = "Unknown"
        else:
            rtnval = raw_value
    else:
        rtnval = "Unknown"
    return rtnval


def _normalize_start_type_field(raw_value) -> Optional[str]:
    """
        Normalize a ``StartType`` field that may be an int, a string, or
        absent.

        :param raw_value: The raw field value pulled out of the parsed
                          JSON object, or None when the field is absent.

        :returns: A canonical ``ServiceStartMode`` string, or None when
                  the field is absent / empty.
    """
    if raw_value is None:
        rtnval: Optional[str] = None
    elif isinstance(raw_value, int):
        rtnval = _decode_start_type_int(raw_value)
    elif isinstance(raw_value, str):
        if len(raw_value) == 0:
            rtnval = None
        else:
            rtnval = raw_value
    else:
        rtnval = None
    return rtnval


class WindowsServiceManager(BaseFeatureManager):
    """
        Windows Service Control Manager (SCM) service manager.  Every
        verb is scoped by a :class:`WindowsServiceScope` and dispatched
        through the injected :class:`ICommandRunner`.
    """

    result_class = WindowsServiceActionResult
    status_class = WindowsServiceActionStatus
    feature_name = "service-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def start_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Start a service.  Skipped when the service is already
            running.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=self._build_powershell_argv(
                script="Start-Service -Name '{}'".format(name),
            ),
        )
        if user_reject is not None:
            return user_reject

        script = "Start-Service -Name '{}'".format(name)
        command = self._build_powershell_argv(script=script)

        skip_result = self._maybe_skip_on_state(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_ACTIVE_STATE_RUNNING},
            skip_message="service is already running",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="start_service",
        )
        return rtnval

    def stop_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Stop a service.  Skipped when the service is already
            stopped.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=self._build_powershell_argv(
                script="Stop-Service -Name '{}'".format(name),
            ),
        )
        if user_reject is not None:
            return user_reject

        script = "Stop-Service -Name '{}'".format(name)
        command = self._build_powershell_argv(script=script)

        skip_result = self._maybe_skip_on_state(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_ACTIVE_STATE_STOPPED},
            skip_message="service is already stopped",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="stop_service",
        )
        return rtnval

    def restart_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Restart a service.  Never skips.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        script = "Restart-Service -Name '{}'".format(name)
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="restart_service",
        )
        return rtnval

    def reload_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            "Reload" a service.

            Windows services have no native reload signal: the SCM has no
            equivalent to systemd's ``reload`` or POSIX ``SIGHUP`` for
            services.  This method always returns ``FAILED`` with a
            descriptive ``message`` documenting the limitation.
            ``Restart-Service`` is the closest equivalent if a
            reconfigure-with-restart is acceptable to the caller.  Honours
            dry-run.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        # Synthesise a placeholder argv so the result still records what
        # the caller's intent was; the runner is never invoked.
        script = "# reload_service is not supported on Windows"
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        if self._config.dry_run:
            rtnval = self._build_result(
                status=WindowsServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return rtnval

        message = (
            "Windows services have no native reload signal; "
            "use restart_service for a reconfigure-with-restart."
        )
        rtnval = self._build_result(
            status=WindowsServiceActionStatus.FAILED,
            name=name,
            scope=scope,
            command=command,
            message=message,
            stderr=message,
        )
        return rtnval

    def enable_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Enable a service so the SCM starts it automatically at boot.
            Skipped when the service is already configured for
            ``Automatic`` start.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        script = "Set-Service -Name '{}' -StartupType Automatic".format(name)
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        skip_result = self._maybe_skip_on_start_type(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_START_TYPE_AUTOMATIC},
            skip_message="service is already enabled (Automatic)",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="enable_service",
        )
        return rtnval

    def disable_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Disable a service so the SCM refuses to start it.  Skipped
            when the service is already configured for ``Disabled``
            start.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        script = "Set-Service -Name '{}' -StartupType Disabled".format(name)
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        skip_result = self._maybe_skip_on_start_type(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_START_TYPE_DISABLED},
            skip_message="service is already disabled",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="disable_service",
        )
        return rtnval

    def mask_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Mask a service.

            Windows has no separate masking concept: the closest analogue
            is ``Set-Service -StartupType Disabled``.  This method aliases
            ``mask_service`` to ``disable_service`` for cross-OS
            consistency with the contract; mask state is therefore
            equivalent to disable state on Windows.  Skipped when the
            service is already disabled.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        script = "Set-Service -Name '{}' -StartupType Disabled".format(name)
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        skip_result = self._maybe_skip_on_start_type(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_START_TYPE_DISABLED},
            skip_message="service is already masked (Disabled)",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="mask_service",
        )
        return rtnval

    def unmask_service(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Unmask a service.

            Inverse of :meth:`mask_service`.  Aliases to
            ``Set-Service -StartupType Automatic`` because mask is
            implemented as disable on Windows.  Skipped when the service
            is already enabled (``Automatic``).

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        script = "Set-Service -Name '{}' -StartupType Automatic".format(name)
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=name,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        skip_result = self._maybe_skip_on_start_type(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_START_TYPE_AUTOMATIC},
            skip_message="service is not masked",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="unmask_service",
        )
        return rtnval

    def reload_daemon(
        self,
        scope: WindowsServiceScope,
    ) -> WindowsServiceActionResult:
        """
            Reload the service-manager configuration.

            Windows has no equivalent of systemd's ``daemon-reload``: the
            Service Control Manager applies service definitions
            immediately whenever ``Set-Service`` (or the underlying
            registry edit) commits.  This method returns SUCCESS with a
            descriptive ``message`` and does NOT invoke the runner, so
            cross-OS callers can call ``reload_daemon`` blindly without
            paying for an SCM no-op or branching on OS in the
            coordinator.  Honours dry-run.

            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        script = "# reload_daemon is a no-op on Windows"
        command = self._build_powershell_argv(script=script)

        user_reject = self._maybe_reject_user_scope(
            name=None,
            scope=scope,
            command=command,
        )
        if user_reject is not None:
            return user_reject

        if self._config.dry_run:
            rtnval = self._build_result(
                status=WindowsServiceActionStatus.DRY_RUN,
                name=None,
                scope=scope,
                command=command,
            )
            return rtnval

        message = (
            "Windows Service Control Manager applies service definitions "
            "immediately; no daemon-reload concept."
        )
        rtnval = self._build_result(
            status=WindowsServiceActionStatus.SUCCESS,
            name=None,
            scope=scope,
            command=command,
            message=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_service_status(
        self,
        name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceStatus:
        """
            Query a service's current state.

            Runs ``Get-Service -Name <name> | ConvertTo-Json -Compress
            -Depth 5`` (no elevation) and parses the resulting JSON
            object for ``Status``, ``StartType``, ``Name``,
            ``DisplayName``, and ``MachineName``.  When ``Get-Service``
            reports the service missing (``NoServiceFoundForGivenName``
            or non-zero exit with empty stdout), returns a synthetic
            ``NotFound`` :class:`WindowsServiceStatus` rather than
            raising.

            :param name: The Windows service name.
            :param scope: The scope selector.

            :returns: The :class:`WindowsServiceStatus`.
            :raises WindowsServiceQueryError: When ``Get-Service`` exits
                                              non-zero with an
                                              unrecognised stderr, or
                                              when the JSON payload fails
                                              to parse.
        """
        if scope == WindowsServiceScope.USER:
            err_msg_lines = [
                "USER-scope reads are not supported in this pilot.",
            ]
            err_msg_lines.append("    SERVICE: {}".format(name))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsServiceQueryError(message=err_msg)

        script = (
            "Get-Service -Name '{}' "
            "| Select-Object Name, DisplayName, Status, StartType, "
            "MachineName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(name)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)

        looks_not_found = self._stderr_looks_not_found(
            stderr=run_result.stderr,
        )

        if run_result.exit_code != 0:
            if looks_not_found:
                rtnval = WindowsServiceStatus(
                    service_name=name,
                    scope=scope,
                    state=_ACTIVE_STATE_NOT_FOUND,
                    start_type=_START_TYPE_NOT_FOUND,
                    display_name=None,
                    pid=None,
                )
                return rtnval

            raise WindowsServiceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        stdout_stripped = run_result.stdout.strip()
        if len(stdout_stripped) == 0:
            rtnval = WindowsServiceStatus(
                service_name=name,
                scope=scope,
                state=_ACTIVE_STATE_NOT_FOUND,
                start_type=_START_TYPE_NOT_FOUND,
                display_name=None,
                pid=None,
            )
            return rtnval

        try:
            parsed = json.loads(stdout_stripped)
        except ValueError as parse_error:
            err_msg_lines = ["Failed to parse Get-Service JSON payload."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    STDOUT: {}".format(stdout_stripped))
            err_msg_lines.append("    PARSE_ERROR: {}".format(parse_error))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsServiceQueryError(message=err_msg) from None

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = WindowsServiceStatus(
                    service_name=name,
                    scope=scope,
                    state=_ACTIVE_STATE_NOT_FOUND,
                    start_type=_START_TYPE_NOT_FOUND,
                    display_name=None,
                    pid=None,
                )
                return rtnval
            record = parsed[0]
        else:
            record = parsed

        rtnval = self._record_to_status(
            record=record,
            requested_name=name,
            scope=scope,
        )
        return rtnval

    def list_services(
        self,
        scope: WindowsServiceScope,
    ) -> list[WindowsServiceInfo]:
        """
            List all services for the given scope.

            Runs ``Get-Service | ConvertTo-Json -Compress -Depth 5``.
            ``Get-Service`` (no ``-Name``) emits a JSON array; for a
            machine with a single service installed it can emit a single
            JSON object instead.  This method tolerates both shapes.
            Never elevates.

            :param scope: The scope selector.

            :returns: One :class:`WindowsServiceInfo` per row.
            :raises WindowsServiceQueryError: When the command fails or
                                              the JSON payload fails to
                                              parse.
        """
        if scope == WindowsServiceScope.USER:
            err_msg_lines = [
                "USER-scope reads are not supported in this pilot.",
            ]
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsServiceQueryError(message=err_msg)

        script = (
            "Get-Service "
            "| Select-Object Name, DisplayName, Status, StartType "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsServiceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        stdout_stripped = run_result.stdout.strip()
        if len(stdout_stripped) == 0:
            rtnval: list[WindowsServiceInfo] = []
            return rtnval

        try:
            parsed = json.loads(stdout_stripped)
        except ValueError as parse_error:
            err_msg_lines = ["Failed to parse Get-Service list JSON payload."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    STDOUT: {}".format(stdout_stripped))
            err_msg_lines.append("    PARSE_ERROR: {}".format(parse_error))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsServiceQueryError(message=err_msg) from None

        if isinstance(parsed, list):
            records = parsed
        else:
            # Single-service machine: ConvertTo-Json emitted a bare object.
            records = [parsed]

        rtnval = self._records_to_infos(records=records, scope=scope)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / dispatch / result construction
    # ------------------------------------------------------------------

    def _build_powershell_argv(self, script: str) -> list[str]:
        """
            Build the PowerShell argv for a given scriptblock.

            The argv shape is fixed at
            ``["powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
            cmdlet behaviour and PowerShell never blocks waiting for
            interactive input on a runner that has no controlling
            terminal.

            :param script: The PowerShell scriptblock text.  Constructed
                           by callers using cmdlet invocations and, where
                           applicable, ``| ConvertTo-Json -Compress
                           -Depth 5``.

            :returns: The full argv as a list of strings.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _typed_config(self) -> WindowsServiceManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsServiceManagerConfig`.

            The base class stores it as :class:`FeatureConfig` for
            type-checker friendliness.  This helper centralises the
            ``isinstance`` cast so callers can read
            ``powershell_executable`` / ``target_machine`` without losing
            static-type clarity.

            :returns: The active config typed as
                      :class:`WindowsServiceManagerConfig`.
        """
        if isinstance(self._config, WindowsServiceManagerConfig):
            rtnval = self._config
        else:
            # Fallback when the caller passed a plain FeatureConfig:
            # synthesise a default-bearing config so the rest of the
            # manager code does not need to special-case.
            rtnval = WindowsServiceManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: Optional[str],
        scope: WindowsServiceScope,
        operation: str,
    ) -> WindowsServiceActionResult:
        """
            Dispatch a write command, applying the elevation policy
            (writes elevate when ``config.elevated`` is True) and
            short-circuiting dry-run via the base :meth:`_dispatch`.

            :param command: The full argv.
            :param name: The service name to echo onto the result.
            :param scope: The scope targeted.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        # Windows has no per-scope elevation override: both system writes
        # and user writes ride on the runner's elevation flag (and
        # USER-scope writes are short-circuited before reaching this
        # helper anyway).  Pass None so the base honours
        # ``config.elevated`` directly.
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation=operation,
            elevated_override=None,
            service_name=name,
            scope=scope,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsServiceActionStatus,
        name: Optional[str],
        scope: WindowsServiceScope,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsServiceActionResult:
        """
            Construct a :class:`WindowsServiceActionResult` without
            dispatching.

            :param status: The outcome status.
            :param name: The service name.  ``None`` for
                         :meth:`reload_daemon`.
            :param scope: The scope targeted.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`WindowsServiceActionResult`.
        """
        rtnval = WindowsServiceActionResult(
            status=status,
            detail=name,
            command=list(command),
            service_name=name,
            scope=scope,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _maybe_reject_user_scope(
        self,
        *,
        name: Optional[str],
        scope: WindowsServiceScope,
        command: list[str],
    ) -> Optional[WindowsServiceActionResult]:
        """
            Short-circuit USER-scope writes with a typed FAILED result.

            Windows per-user services (suffixed with ``_<sessionid>``)
            are out of scope for the base contract.  This helper centralises
            the FAILED-with-message return so every write verb can call
            it as the first thing it does.

            :param name: The service name (or None for reload_daemon).
            :param scope: The scope to test.
            :param command: The would-have-been argv, captured on the
                            result.

            :returns: A FAILED result when ``scope`` is USER; otherwise
                      None.
        """
        if scope != WindowsServiceScope.USER:
            return None

        message = (
            "USER-scope service operations (per-user services suffixed with "
            "_<sessionid>) are not supported in this pilot."
        )
        rtnval = self._build_result(
            status=WindowsServiceActionStatus.FAILED,
            name=name,
            scope=scope,
            command=command,
            message=message,
            stderr=message,
        )
        return rtnval

    def _maybe_skip_on_state(
        self,
        *,
        name: str,
        scope: WindowsServiceScope,
        command: list[str],
        skip_when_state_in,
        skip_message: str,
    ) -> Optional[WindowsServiceActionResult]:
        """
            Evaluate the dry-run short-circuit and the ``state``
            idempotency guard.

            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv, for dry-run capture.
            :param skip_when_state_in: States that trigger ``SKIPPED``.
            :param skip_message: Human-readable reason for the skip.

            :returns: A result when the dispatch should short-circuit;
                      None when the caller should proceed with the write.
        """
        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return dry_result

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.state in skip_when_state_in:
            skip_result = self._build_result(
                status=WindowsServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=skip_message,
            )
            return skip_result

        return None

    def _maybe_skip_on_start_type(
        self,
        *,
        name: str,
        scope: WindowsServiceScope,
        command: list[str],
        skip_when_state_in,
        skip_message: str,
    ) -> Optional[WindowsServiceActionResult]:
        """
            Evaluate the dry-run short-circuit and the ``start_type``
            idempotency guard.

            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv, for dry-run capture.
            :param skip_when_state_in: Start-type values that trigger
                                       ``SKIPPED``.
            :param skip_message: Human-readable reason for the skip.

            :returns: A result when the dispatch should short-circuit;
                      None when the caller should proceed with the write.
        """
        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return dry_result

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.start_type in skip_when_state_in:
            skip_result = self._build_result(
                status=WindowsServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=skip_message,
            )
            return skip_result

        return None

    # ------------------------------------------------------------------
    # Internal helpers — output parsing
    # ------------------------------------------------------------------

    def _stderr_looks_not_found(self, *, stderr: str) -> bool:
        """
            Heuristically detect a "service not found" stderr from
            ``Get-Service``.

            PowerShell emits a localized ``Cannot find any service ...``
            error and surfaces an internal ``NoServiceFoundForGivenName``
            FullyQualifiedErrorId.  We match on either marker (case
            folded) so neither localization nor PS version variability
            breaks the read path.

            :param stderr: The captured stderr text.

            :returns: True when the stderr clearly indicates an unknown
                      service; False otherwise.
        """
        stderr_lower = stderr.lower()
        looks_not_found = (
            "noservicefoundforgivenname" in stderr_lower
            or "cannot find any service" in stderr_lower
        )
        return looks_not_found

    def _record_to_status(
        self,
        *,
        record: dict,
        requested_name: str,
        scope: WindowsServiceScope,
    ) -> WindowsServiceStatus:
        """
            Convert a single parsed ``Get-Service`` record into a
            :class:`WindowsServiceStatus`.

            :param record: Parsed JSON object for one service.
            :param requested_name: The name the caller asked for (used
                                   when the JSON payload omits ``Name``,
                                   which can happen for ``Select-Object``
                                   shapes that drop empty fields).
            :param scope: The scope the read was taken at.

            :returns: The :class:`WindowsServiceStatus`.
        """
        raw_status = record.get("Status")
        state_value = _normalize_status_field(raw_value=raw_status)

        raw_start_type = record.get("StartType")
        start_type_value = _normalize_start_type_field(
            raw_value=raw_start_type,
        )

        name_value = record.get("Name")
        if name_value is None or len(str(name_value)) == 0:
            service_name = requested_name
        else:
            service_name = str(name_value)

        display_raw = record.get("DisplayName")
        if display_raw is None or len(str(display_raw)) == 0:
            display_name = None
        else:
            display_name = str(display_raw)

        rtnval = WindowsServiceStatus(
            service_name=service_name,
            scope=scope,
            state=state_value,
            start_type=start_type_value,
            display_name=display_name,
            pid=None,
        )
        return rtnval

    def _records_to_infos(
        self,
        *,
        records: list,
        scope: WindowsServiceScope,
    ) -> list[WindowsServiceInfo]:
        """
            Convert a list of parsed ``Get-Service`` records into
            :class:`WindowsServiceInfo` rows.

            :param records: Parsed JSON list of service objects.
            :param scope: The scope the listing came from.

            :returns: One :class:`WindowsServiceInfo` per record.
        """
        rtnval: list[WindowsServiceInfo] = []
        for record in records:
            if not isinstance(record, dict):
                continue

            name_raw = record.get("Name")
            if name_raw is None:
                continue
            name_value = str(name_raw)
            if len(name_value) == 0:
                continue

            raw_status = record.get("Status")
            status_value = _normalize_status_field(raw_value=raw_status)

            raw_start_type = record.get("StartType")
            start_type_value = _normalize_start_type_field(
                raw_value=raw_start_type,
            )

            display_raw = record.get("DisplayName")
            if display_raw is None or len(str(display_raw)) == 0:
                display_name = None
            else:
                display_name = str(display_raw)

            description_raw = record.get("Description")
            if description_raw is None or len(str(description_raw)) == 0:
                description = None
            else:
                description = str(description_raw)

            info = WindowsServiceInfo(
                name=name_value,
                scope=scope,
                status=status_value,
                start_type=start_type_value,
                display_name=display_name,
                description=description,
            )
            rtnval.append(info)

        return rtnval

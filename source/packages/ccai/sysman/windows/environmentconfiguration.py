"""
    Windows environment-configuration feature.

    Implements the cross-OS environment-configuration contract for
    Windows by routing every read / write through PowerShell.  System
    scope uses ``[Environment]::SetEnvironmentVariable(..., 'Machine')``
    so the .NET runtime broadcasts ``WM_SETTINGCHANGE`` to existing
    processes.  User scope uses ``[Environment]::SetEnvironmentVariable
    (..., 'User')`` for the current user and direct registry edits via
    ``Registry::HKEY_USERS\\<SID>\\Environment`` for other users (which
    requires the user's hive to be loaded — typically true while the
    user is logged on, and false otherwise; this manager does not
    attempt to load the hive).

    Persistence surfaces:
        - System scope: ``HKLM:\\SYSTEM\\CurrentControlSet\\Control\\
          Session Manager\\Environment`` (managed by the .NET API).
        - User scope, current user: ``HKCU:\\Environment``.
        - User scope, other user: ``Registry::HKEY_USERS\\<SID>\\
          Environment``.

    Elevation policy:
        - System writes / unsets: elevate (the .NET API requires
          Administrator to write Machine scope).
        - User writes / unsets for the current user: do NOT elevate.
        - User writes / unsets for other users (via SID): elevate.
        - Reads: never elevate.

    Path syntax fragility:
        PowerShell registry cmdlets accept the colon-prefixed PSDrive
        form (``HKCU:\\Environment``) for the current user, but
        per-SID writes require the explicit ``Registry::`` provider
        path (``Registry::HKEY_USERS\\<SID>\\Environment``).  Mixing
        the two forms produces ``ItemNotFound`` errors.  This module
        uses the colon-prefixed PSDrive form for the current user and
        the ``Registry::`` form for cross-user writes.

    File I/O:
        No file I/O.  All operations are registry-backed and dispatched
        through the injected :class:`ICommandRunner`.
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

_SYSTEM_REGISTRY_PATH = (
    r"HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
)
_USER_REGISTRY_PATH_CURRENT = r"HKCU:\Environment"


WindowsEnvActionStatus = ActionStatus


class WindowsEnvScope(Enum):
    """
        Scope selector for Windows environment-configuration verbs.

        - ``SYSTEM`` -> Machine-scope, persisted under HKLM Session
          Manager Environment.
        - ``USER``   -> Per-user scope, persisted under HKCU
          Environment (current user) or HKEY_USERS\\<SID>\\Environment
          (other users).
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class WindowsEnvironmentConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`WindowsEnvironmentConfigurationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: PowerShell executable path used
                                      to run cmdlets.  Defaults to
                                      ``powershell.exe`` (PATH-resolved).
        :param current_user: When known, the username that the runner
                             is executing as.  Used by the user-scope
                             verbs to decide whether to take the
                             ``HKCU`` path or the ``HKEY_USERS\\<SID>``
                             path.  ``None`` (default) means "treat
                             every user-scope target as 'other user'"
                             which forces the SID path and elevation.
                             Setting this lets callers opt into the
                             cheaper, no-elevation HKCU path when they
                             know the runner identity.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    current_user: Optional[str] = None


@dataclass
class WindowsEnvActionResult(FeatureActionResult):
    """
        Result of an environment-configuration write operation.

        :param name: The variable name targeted.
        :param value: The value written, or ``None`` for unset
                      operations.
        :param scope: The scope targeted.
        :param user: The username (USER scope only); ``None`` for
                     system scope.
        :param registry_path: The registry path the action targeted,
                              when applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    value: Optional[str] = None
    scope: Optional[WindowsEnvScope] = None
    user: Optional[str] = None
    registry_path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsEnvValue:
    """
        A single environment variable value.

        :param name: The variable name.
        :param value: The variable value.
        :param scope: The scope the variable was read from.
        :param user: The username for USER scope; ``None`` otherwise.
    """
    name: str
    value: str
    scope: WindowsEnvScope
    user: Optional[str] = None


class WindowsEnvQueryError(Exception):
    """
        Raised when an environment-read fails for a reason other than
        the target value simply being absent.
    """

    def __init__(
        self,
        *,
        command: Optional[list] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional pre-built message.
        """
        if command is None:
            self.command: list = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Environment query command failed."]
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


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        PowerShell single-quoted strings escape an embedded single
        quote by doubling it.

        :param value: The raw string.

        :returns: The escaped value (without the surrounding quotes).
    """
    rtnval = value.replace("'", "''")
    return rtnval


# ----------------------------------------------------------------------
# WindowsEnvironmentConfigurationManager
# ----------------------------------------------------------------------


class WindowsEnvironmentConfigurationManager(BaseFeatureManager):
    """
        Windows registry-backed environment-configuration manager.
        System-scope writes go through the .NET
        ``[Environment]::SetEnvironmentVariable`` API so processes that
        respond to ``WM_SETTINGCHANGE`` (notably Explorer) refresh
        their environment block automatically.

        User-scope writes go through the same .NET API for the current
        user (no elevation needed) and through direct
        ``Set-ItemProperty`` against ``Registry::HKEY_USERS\\<SID>``
        for other users (elevation required).
    """

    result_class = WindowsEnvActionResult
    status_class = WindowsEnvActionStatus
    feature_name = "environment-configuration"

    # ------------------------------------------------------------------
    # System-scope verbs
    # ------------------------------------------------------------------

    def set_system_env(
        self,
        name: str,
        value: str,
    ) -> WindowsEnvActionResult:
        """
            Set a Machine-scope environment variable.

            Idempotent: skips when the current Machine-scope value
            already matches.

            :param name: The variable name.
            :param value: The variable value.

            :returns: The :class:`WindowsEnvActionResult`.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        escaped_value = _ps_escape_single_quoted(value=value)
        script = (
            "[Environment]::SetEnvironmentVariable('{}', '{}', 'Machine')"
        ).format(escaped_name, escaped_value)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=WindowsEnvScope.SYSTEM,
                user=None,
                registry_path=_SYSTEM_REGISTRY_PATH,
                command=command,
            )
            return dry

        current_value = self._read_machine_env(name=name)
        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=WindowsEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=WindowsEnvScope.SYSTEM,
                user=None,
                registry_path=_SYSTEM_REGISTRY_PATH,
                command=command,
                message="value already set in Machine scope",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsEnvActionStatus.SUCCESS
        else:
            mapped_status = WindowsEnvActionStatus.FAILED

        rtnval = WindowsEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            value=value,
            scope=WindowsEnvScope.SYSTEM,
            user=None,
            registry_path=_SYSTEM_REGISTRY_PATH,
        )
        return rtnval

    def unset_system_env(self, name: str) -> WindowsEnvActionResult:
        """
            Remove a Machine-scope environment variable.

            Idempotent: skips when the variable is not currently set.

            :param name: The variable name.

            :returns: The :class:`WindowsEnvActionResult`.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        script = (
            "[Environment]::SetEnvironmentVariable('{}', $null, 'Machine')"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=WindowsEnvScope.SYSTEM,
                user=None,
                registry_path=_SYSTEM_REGISTRY_PATH,
                command=command,
            )
            return dry

        current_value = self._read_machine_env(name=name)
        if current_value is None:
            skipped = self._build_result(
                status=WindowsEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=WindowsEnvScope.SYSTEM,
                user=None,
                registry_path=_SYSTEM_REGISTRY_PATH,
                command=command,
                message="variable not present in Machine scope",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsEnvActionStatus.SUCCESS
        else:
            mapped_status = WindowsEnvActionStatus.FAILED

        rtnval = WindowsEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            value=None,
            scope=WindowsEnvScope.SYSTEM,
            user=None,
            registry_path=_SYSTEM_REGISTRY_PATH,
        )
        return rtnval

    def get_system_env(self, name: str) -> Optional[WindowsEnvValue]:
        """
            Read a Machine-scope environment variable.

            :param name: The variable name.

            :returns: The :class:`WindowsEnvValue`, or ``None`` when
                      the variable is not set in Machine scope.
        """
        current_value = self._read_machine_env(name=name)
        if current_value is None:
            rtnval: Optional[WindowsEnvValue] = None
            return rtnval

        rtnval = WindowsEnvValue(
            name=name,
            value=current_value,
            scope=WindowsEnvScope.SYSTEM,
            user=None,
        )
        return rtnval

    def list_system_env(self) -> dict:
        """
            Enumerate every Machine-scope environment variable.

            :returns: A ``dict[str, str]`` of name to value.
            :raises WindowsEnvQueryError: When the runner reports an
                                          unexpected failure or the
                                          JSON payload fails to parse.
        """
        script = (
            "[Environment]::GetEnvironmentVariables('Machine') "
            "| ConvertTo-Json -Compress"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise WindowsEnvQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = self._parse_json_dict(
            command=command,
            stdout=run_result.stdout,
        )
        return rtnval

    # ------------------------------------------------------------------
    # User-scope verbs
    # ------------------------------------------------------------------

    def set_user_env(
        self,
        user: str,
        name: str,
        value: str,
    ) -> WindowsEnvActionResult:
        """
            Set a per-user environment variable.

            For the current user (when ``current_user`` on the config
            matches), uses
            ``[Environment]::SetEnvironmentVariable(..., 'User')``
            and does not elevate.  For any other user, resolves the
            target's SID via PowerShell and writes via
            ``Set-ItemProperty -Path 'Registry::HKEY_USERS\\<SID>\\
            Environment'`` (elevation honoured).

            Idempotent: skips when the existing value matches.

            :param user: The target username.
            :param name: The variable name.
            :param value: The variable value.

            :returns: The :class:`WindowsEnvActionResult`.
        """
        if self._is_current_user(user=user):
            rtnval = self._set_current_user_env(
                user=user, name=name, value=value,
            )
            return rtnval

        rtnval = self._set_other_user_env(
            user=user, name=name, value=value,
        )
        return rtnval

    def unset_user_env(
        self,
        user: str,
        name: str,
    ) -> WindowsEnvActionResult:
        """
            Remove a per-user environment variable.

            For the current user, uses
            ``[Environment]::SetEnvironmentVariable(..., $null, 'User')``
            without elevation.  For other users, uses
            ``Remove-ItemProperty`` against the per-SID registry path
            with elevation honoured.

            Idempotent: skips when the variable is not present.

            :param user: The target username.
            :param name: The variable name.

            :returns: The :class:`WindowsEnvActionResult`.
        """
        if self._is_current_user(user=user):
            rtnval = self._unset_current_user_env(
                user=user, name=name,
            )
            return rtnval

        rtnval = self._unset_other_user_env(
            user=user, name=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — current-user user-scope verbs
    # ------------------------------------------------------------------

    def _set_current_user_env(
        self,
        *,
        user: str,
        name: str,
        value: str,
    ) -> WindowsEnvActionResult:
        """
            Implement ``set_user_env`` for the current user via the
            .NET ``User`` scope API.  Does not elevate.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        escaped_value = _ps_escape_single_quoted(value=value)
        script = (
            "[Environment]::SetEnvironmentVariable('{}', '{}', 'User')"
        ).format(escaped_name, escaped_value)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=_USER_REGISTRY_PATH_CURRENT,
                command=command,
            )
            return dry

        current_value = self._read_user_env(name=name)
        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=WindowsEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=_USER_REGISTRY_PATH_CURRENT,
                command=command,
                message="value already set in User scope",
            )
            return skipped

        # Same-user writes do not elevate, even when config.elevated.
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code == 0:
            mapped_status = WindowsEnvActionStatus.SUCCESS
        else:
            mapped_status = WindowsEnvActionStatus.FAILED

        rtnval = WindowsEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            value=value,
            scope=WindowsEnvScope.USER,
            user=user,
            registry_path=_USER_REGISTRY_PATH_CURRENT,
        )
        return rtnval

    def _unset_current_user_env(
        self,
        *,
        user: str,
        name: str,
    ) -> WindowsEnvActionResult:
        """
            Implement ``unset_user_env`` for the current user.  Does
            not elevate.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        script = (
            "[Environment]::SetEnvironmentVariable('{}', $null, 'User')"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=_USER_REGISTRY_PATH_CURRENT,
                command=command,
            )
            return dry

        current_value = self._read_user_env(name=name)
        if current_value is None:
            skipped = self._build_result(
                status=WindowsEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=_USER_REGISTRY_PATH_CURRENT,
                command=command,
                message="variable not present in User scope",
            )
            return skipped

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code == 0:
            mapped_status = WindowsEnvActionStatus.SUCCESS
        else:
            mapped_status = WindowsEnvActionStatus.FAILED

        rtnval = WindowsEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            value=None,
            scope=WindowsEnvScope.USER,
            user=user,
            registry_path=_USER_REGISTRY_PATH_CURRENT,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — other-user user-scope verbs
    # ------------------------------------------------------------------

    def _set_other_user_env(
        self,
        *,
        user: str,
        name: str,
        value: str,
    ) -> WindowsEnvActionResult:
        """
            Implement ``set_user_env`` for a user other than the
            running identity.  Resolves the user's SID, then writes
            via ``Set-ItemProperty`` against the per-SID registry
            path.  Honours ``config.elevated``.
        """
        try:
            sid = self._resolve_user_sid(user=user)
        except WindowsEnvQueryError as exc:
            failed = self._build_result(
                status=WindowsEnvActionStatus.FAILED,
                name=name,
                value=value,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=None,
                command=self._sid_resolution_argv(user=user),
                message="failed to resolve SID for user '{}'".format(user),
                stderr=exc.stderr,
            )
            return failed

        registry_path = self._sid_registry_path(sid=sid)
        escaped_name = _ps_escape_single_quoted(value=name)
        escaped_value = _ps_escape_single_quoted(value=value)
        escaped_path = _ps_escape_single_quoted(value=registry_path)
        script = (
            "Set-ItemProperty -Path '{}' -Name '{}' -Value '{}'"
        ).format(escaped_path, escaped_name, escaped_value)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=registry_path,
                command=command,
            )
            return dry

        current_value = self._read_other_user_env(
            registry_path=registry_path,
            name=name,
        )
        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=WindowsEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=registry_path,
                command=command,
                message="value already set in target user's hive",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsEnvActionStatus.SUCCESS
        else:
            mapped_status = WindowsEnvActionStatus.FAILED

        rtnval = WindowsEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            value=value,
            scope=WindowsEnvScope.USER,
            user=user,
            registry_path=registry_path,
        )
        return rtnval

    def _unset_other_user_env(
        self,
        *,
        user: str,
        name: str,
    ) -> WindowsEnvActionResult:
        """
            Implement ``unset_user_env`` for a user other than the
            running identity.  Honours ``config.elevated``.
        """
        try:
            sid = self._resolve_user_sid(user=user)
        except WindowsEnvQueryError as exc:
            failed = self._build_result(
                status=WindowsEnvActionStatus.FAILED,
                name=name,
                value=None,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=None,
                command=self._sid_resolution_argv(user=user),
                message="failed to resolve SID for user '{}'".format(user),
                stderr=exc.stderr,
            )
            return failed

        registry_path = self._sid_registry_path(sid=sid)
        escaped_name = _ps_escape_single_quoted(value=name)
        escaped_path = _ps_escape_single_quoted(value=registry_path)
        script = (
            "Remove-ItemProperty -Path '{}' -Name '{}'"
        ).format(escaped_path, escaped_name)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=registry_path,
                command=command,
            )
            return dry

        current_value = self._read_other_user_env(
            registry_path=registry_path,
            name=name,
        )
        if current_value is None:
            skipped = self._build_result(
                status=WindowsEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=WindowsEnvScope.USER,
                user=user,
                registry_path=registry_path,
                command=command,
                message="variable not present in target user's hive",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsEnvActionStatus.SUCCESS
        else:
            mapped_status = WindowsEnvActionStatus.FAILED

        rtnval = WindowsEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            value=None,
            scope=WindowsEnvScope.USER,
            user=user,
            registry_path=registry_path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — read paths
    # ------------------------------------------------------------------

    def _read_machine_env(self, *, name: str) -> Optional[str]:
        """
            Read a Machine-scope variable via
            ``[Environment]::GetEnvironmentVariable(..., 'Machine')``.

            :param name: The variable name.

            :returns: The current value, or ``None`` when absent.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        script = (
            "[Environment]::GetEnvironmentVariable('{}', 'Machine')"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval
        rtnval = stripped
        return rtnval

    def _read_user_env(self, *, name: str) -> Optional[str]:
        """
            Read a User-scope variable for the current user via
            ``[Environment]::GetEnvironmentVariable(..., 'User')``.

            :param name: The variable name.

            :returns: The current value, or ``None`` when absent.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        script = (
            "[Environment]::GetEnvironmentVariable('{}', 'User')"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval
        rtnval = stripped
        return rtnval

    def _read_other_user_env(
        self,
        *,
        registry_path: str,
        name: str,
    ) -> Optional[str]:
        """
            Read a User-scope variable from another user's hive via
            ``Get-ItemProperty -Path 'Registry::HKEY_USERS\\<SID>\\
            Environment'``.

            :param registry_path: The per-SID registry path.
            :param name: The variable name.

            :returns: The current value, or ``None`` when absent.
        """
        escaped_path = _ps_escape_single_quoted(value=registry_path)
        escaped_name = _ps_escape_single_quoted(value=name)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "(Get-ItemProperty -Path '{path}' -Name '{key}' "
            "-ErrorAction Stop).'{key}'"
        ).format(path=escaped_path, key=escaped_name)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval
        rtnval = stripped
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — SID resolution / argv / current-user check
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsEnvironmentConfigurationManagerConfig:
        """
            Return the manager's config typed.
        """
        if isinstance(
            self._config, WindowsEnvironmentConfigurationManagerConfig,
        ):
            rtnval = self._config
        else:
            rtnval = WindowsEnvironmentConfigurationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _is_current_user(self, *, user: str) -> bool:
        """
            Return True when ``user`` matches the configured
            ``current_user`` on the config (case-insensitive bare
            comparison).  False when ``current_user`` is ``None`` or
            differs.

            Domain-qualified names (``DOMAIN\\user``) are matched on
            the bare account portion.

            :param user: The target username.

            :returns: True for same-user; False otherwise.
        """
        configured = self._typed_config().current_user
        if configured is None:
            return False
        bare_target = self._strip_domain(user=user)
        bare_configured = self._strip_domain(user=configured)
        if bare_target.lower() == bare_configured.lower():
            return True
        return False

    def _strip_domain(self, *, user: str) -> str:
        """
            Strip a domain prefix (``DOMAIN\\user``) and return the
            bare account name.

            :param user: The (possibly qualified) username.

            :returns: The bare account name.
        """
        if "\\" in user:
            _, _, bare = user.partition("\\")
            return bare
        if "@" in user:
            bare, _, _ = user.partition("@")
            return bare
        return user

    def _resolve_user_sid(self, *, user: str) -> str:
        """
            Resolve a username to its SID via the .NET
            ``System.Security.Principal.NTAccount.Translate`` path.

            :param user: The username (bare or domain-qualified).

            :returns: The SID string (e.g. ``S-1-5-21-...``).
            :raises WindowsEnvQueryError: When the runner reports a
                                          non-zero exit code or the
                                          stdout is empty.
        """
        command = self._sid_resolution_argv(user=user)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsEnvQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            raise WindowsEnvQueryError(
                command=command,
                exit_code=0,
                stderr="empty stdout from SID translation",
            )

        if not stripped.startswith("S-"):
            raise WindowsEnvQueryError(
                command=command,
                exit_code=0,
                stderr="unexpected SID translation output: {}".format(
                    stripped,
                ),
            )

        rtnval = stripped
        return rtnval

    def _sid_resolution_argv(self, *, user: str) -> list:
        """
            Build the argv for the SID-resolution scriptblock.

            :param user: The username.

            :returns: The argv list.
        """
        escaped_user = _ps_escape_single_quoted(value=user)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "(New-Object System.Security.Principal.NTAccount('{}')).Translate("
            "[System.Security.Principal.SecurityIdentifier]).Value"
        ).format(escaped_user)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _sid_registry_path(self, *, sid: str) -> str:
        """
            Build the per-SID environment registry path.

            Note: this uses the ``Registry::`` provider prefix because
            ``HKEY_USERS`` is not a default PowerShell PSDrive.  The
            colon-prefixed PSDrive form (``HKU:``) is not always
            available — we use the explicit provider path for
            portability.

            :param sid: The user SID.

            :returns: ``Registry::HKEY_USERS\\<SID>\\Environment``.
        """
        rtnval = "Registry::HKEY_USERS\\{}\\Environment".format(sid)
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.

            :param script: The scriptblock text.

            :returns: The argv list.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _parse_json_dict(
        self,
        *,
        command: list,
        stdout: str,
    ) -> dict:
        """
            Parse a ``ConvertTo-Json`` payload that should decode to a
            dict-like object.  PowerShell's hashtable-to-JSON
            serialization includes synthetic keys (e.g. ``IsReadOnly``
            on certain shapes); those are filtered out.

            :param command: The argv used (for error reporting).
            :param stdout: Raw stdout text.

            :returns: A ``dict[str, str]``.
            :raises WindowsEnvQueryError: When the JSON payload fails
                                          to parse or is not a dict.
        """
        stripped = stdout.strip()
        if len(stripped) == 0:
            rtnval: dict = {}
            return rtnval

        try:
            parsed = json.loads(stripped)
        except ValueError as parse_error:
            err_msg_lines = ["Failed to parse environment JSON payload."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    STDOUT: {}".format(stripped))
            err_msg_lines.append("    PARSE_ERROR: {}".format(parse_error))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsEnvQueryError(message=err_msg) from None

        if not isinstance(parsed, dict):
            err_msg_lines = ["Environment JSON payload is not a dict."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    STDOUT: {}".format(stripped))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsEnvQueryError(message=err_msg)

        rtnval = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # Filter PowerShell pseudo-keys that surface on certain
            # serializer shapes (e.g. when ConvertTo-Json wraps the
            # hashtable).
            if key.startswith("PS"):
                continue
            if key in ("IsReadOnly", "IsFixedSize", "IsSynchronized",
                       "Keys", "Values", "SyncRoot", "Count"):
                continue
            if value is None:
                rtnval[key] = ""
                continue
            rtnval[key] = str(value)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsEnvActionStatus,
        name: Optional[str],
        value: Optional[str],
        scope: Optional[WindowsEnvScope],
        user: Optional[str],
        registry_path: Optional[str],
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsEnvActionResult:
        """
            Construct a :class:`WindowsEnvActionResult` without
            invoking the runner.
        """
        rtnval = WindowsEnvActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            value=value,
            scope=scope,
            user=user,
            registry_path=registry_path,
            message=message,
            stderr=stderr,
        )
        return rtnval

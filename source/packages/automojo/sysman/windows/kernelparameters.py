"""
    Windows kernel-parameters feature.

    Implements the cross-OS kernel-parameters contract for Windows by
    translating a bundled subset of Linux-style dotted parameter names
    (``net.ipv4.tcp_keepalive_time`` etc.) into Windows registry
    locations and dispatching PowerShell ``Get-ItemProperty`` /
    ``Set-ItemProperty`` / ``Remove-ItemProperty`` cmdlets through an
    :class:`ICommandRunner`.  Every verb is dispatched via the runner so
    the manager works identically under local execution and remote
    PowerShell.

    Design summary:
        Windows has no ``sysctl(8)`` equivalent.  Kernel-tunable behaviour
        on Windows is configured via registry values that the kernel,
        TCP/IP stack, AFD, and other drivers consult either at boot or
        on next-driver-bind.  This manager ships a small bundled mapping
        table from cross-OS dotted names to ``(registry_path, value_name,
        registry_type, requires_reboot)`` tuples.  Parameters not in the
        table return ``FAILED`` with a descriptive
        ``parameter_not_supported`` message — the contract explicitly
        permits non-silent rejection of out-of-scope tunables on
        platforms that have no equivalent.

    File / registry path syntax:
        PowerShell registry cmdlets use the colon-prefixed PSDrive form
        (``HKLM:\\SYSTEM\\...``); ``reg.exe`` and ``Registry::`` provider
        paths use the colon-less form (``HKLM\\SYSTEM\\...``).  This
        module exclusively uses the PowerShell colon-prefixed form for
        all cmdlet invocations.  See :mod:`configurationmanagement` for
        the colon-less variant required by ``reg export`` / ``reg
        import``.

    Persistence:
        Windows registry writes ARE persistent — there is no separate
        runtime-only surface analogous to ``sysctl -w``.  ``persistent``
        is therefore advisory: every ``set_parameter`` write touches the
        registry whether the caller asked for persistent or runtime-only.
        ``persistent=False`` callers receive a result whose ``message``
        documents that Windows registry writes are inherently persistent.

    Reload:
        ``reload_parameters`` is a no-op SUCCESS that returns a
        descriptive ``message``: Windows applies registry-backed kernel
        parameters either immediately (per-driver, on next bind) or on
        next boot.  No documented user-mode primitive forces a uniform
        reload.

    Elevation policy:
        Windows has no ``sudo``.  ``config.elevated=True`` is advisory:
        the manager forwards ``elevated=True`` to the runner for every
        write verb (``set_parameter``, ``remove_persisted_parameter``)
        and ``elevated=False`` for every read verb and idempotency probe.
        HKLM reads typically require Administrator on hardened systems;
        the runner is responsible for surfacing an access-denied error
        when the host process lacks elevation.
"""

import os

from dataclasses import dataclass
from typing import Optional

from automojo.sysman.common.featurebase import (
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

# Registry-type strings accepted by ``Set-ItemProperty -Type``.
_REG_TYPE_DWORD = "DWord"
_REG_TYPE_QWORD = "QWord"
_REG_TYPE_STRING = "String"
_REG_TYPE_EXPAND_STRING = "ExpandString"
_REG_TYPE_MULTI_STRING = "MultiString"
_REG_TYPE_BINARY = "Binary"

# Stderr / FullyQualifiedErrorId fragments emitted by the registry cmdlets
# when the property or path does not exist.  PowerShell localises the
# free-text "Property does not exist" message but the FullyQualifiedErrorId
# tokens are stable across locales.
_NOT_FOUND_FRAGMENTS = (
    "propertynotfound",
    "pathnotfound",
    "itemnotfound",
    "does not exist",
    "cannot find path",
    "cannot find any property",
)


WindowsKernelParameterActionStatus = ActionStatus


@dataclass(frozen=True)
class WindowsKernelParameterMappingEntry:
    """
        One row of the bundled cross-OS-name -> registry mapping table.

        :param registry_path: PowerShell-style registry path
                              (``HKLM:\\SYSTEM\\...``).  ``None`` means
                              "no Windows equivalent"; the manager
                              rejects such requests with a typed FAILED
                              result.
        :param value_name: Name of the registry value beneath
                           ``registry_path``.
        :param registry_type: ``Set-ItemProperty -Type`` value, e.g.
                              ``DWord``.
        :param requires_reboot: True when changes to the value do not
                                take effect until the next reboot.
                                Surfaced verbatim onto the result so
                                callers can prompt the operator.
    """
    registry_path: Optional[str]
    value_name: Optional[str]
    registry_type: Optional[str]
    requires_reboot: Optional[bool]


# Bundled mapping table.  Conservative selection of common Linux-style
# tunables that have a documented Windows registry equivalent.  Entries
# whose Windows equivalent does not exist (or has substantially different
# semantics) are recorded with a sentinel ``None`` registry path so the
# manager can reject them with a specific message rather than silently
# pretending to apply.
_DEFAULT_PARAMETER_MAP: dict = {
    "net.ipv4.tcp_keepalive_time": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
        value_name="KeepAliveTime",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "net.ipv4.tcp_keepalive_interval": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
        value_name="KeepAliveInterval",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "net.ipv4.tcp_max_syn_backlog": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
        value_name="TcpMaxSynBacklog",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "net.ipv4.ip_forward": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
        value_name="IPEnableRouter",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "net.core.somaxconn": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\AFD\Parameters",
        value_name="ListenBackLog",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "net.ipv4.tcp_syncookies": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
        value_name="SynAttackProtect",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "net.ipv6.conf.all.disable_ipv6": WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters",
        value_name="DisabledComponents",
        registry_type=_REG_TYPE_DWORD,
        requires_reboot=True,
    ),
    "vm.swappiness": WindowsKernelParameterMappingEntry(
        # No direct equivalent — Windows uses the pagefile registry
        # (``Memory Management\\PagingFiles``) but its semantics differ
        # so much from Linux's swappiness that conflating them would be
        # actively misleading.  Caller receives a specific FAILED.
        registry_path=None,
        value_name=None,
        registry_type=None,
        requires_reboot=None,
    ),
}


@dataclass
class WindowsKernelParameterManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsKernelParameterManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: PowerShell executable path used to
                                      run cmdlets.  Defaults to
                                      ``powershell.exe`` (PATH-resolved).
        :param parameter_map: Optional override for the bundled
                              cross-OS-name -> registry mapping.  When
                              ``None`` (default) the bundled
                              ``_DEFAULT_PARAMETER_MAP`` is used.  Pass
                              a dict to extend or shadow individual
                              entries; the manager always takes a
                              defensive copy at construction time.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    parameter_map: Optional[dict] = None


@dataclass
class WindowsKernelParameterActionResult(FeatureActionResult):
    """
        Result of a kernel-parameter write operation.

        :param name: The dotted parameter name targeted, or ``None``
                     for ``reload_parameters``.
        :param value: The value written (or would-be in ``DRY_RUN``);
                      ``None`` for ``reload_parameters`` and
                      ``remove_persisted_parameter``.
        :param persistent: Echoed-back value of the caller's
                           ``persistent`` flag; ``None`` for verbs
                           that do not split runtime vs persistent.
                           Note: Windows has no separate runtime
                           surface — all writes ARE persistent.
        :param current_value: Pre-action registry value, when known.
        :param applied_value: Post-action registry value (or would-be
                              on ``DRY_RUN`` / ``SKIPPED``).
        :param requires_reboot: True when the underlying registry key
                                requires a reboot for the value to take
                                effect.  Copied directly from the
                                bundled mapping table.
        :param registry_path: The PowerShell-style registry path the
                              action targeted, when applicable.
        :param registry_key: The registry value name (e.g.
                             ``KeepAliveTime``) targeted, when
                             applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    value: Optional[str] = None
    persistent: Optional[bool] = None
    current_value: Optional[str] = None
    applied_value: Optional[str] = None
    requires_reboot: Optional[bool] = None
    registry_path: Optional[str] = None
    registry_key: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsParameterValue:
    """
        A single Windows registry-backed kernel parameter and its
        current value.

        :param name: The dotted cross-OS parameter name.
        :param value: The current string value as returned by
                      ``Get-ItemProperty``.  Whitespace stripped.
        :param is_default: Reserved tri-state describing whether the
                           value equals the documented Windows default.
                           ``None`` when not computed (the default for
                           this implementation).
        :param registry_path: The PowerShell-style registry path the
                              value lives at.
        :param registry_key: The registry value name beneath
                             ``registry_path``.
    """
    name: str
    value: str
    is_default: Optional[bool] = None
    registry_path: Optional[str] = None
    registry_key: Optional[str] = None


class WindowsKernelParameterQueryError(Exception):
    """
        Raised when a parameter read fails for a reason other than the
        target value simply being absent.
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
            err_msg_lines = ["Kernel parameter query command failed."]
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


def _looks_not_found(*, stderr: str, stdout: str) -> bool:
    """
        Detect "property does not exist" / "path not found" stderr or
        stdout fragments emitted by the registry cmdlets.

        :param stderr: Captured stderr text.
        :param stdout: Captured stdout text.

        :returns: True when the runner output indicates the requested
                  registry value is absent.
    """
    stderr_lower = stderr.lower()
    stdout_lower = stdout.lower()
    for fragment in _NOT_FOUND_FRAGMENTS:
        if fragment in stderr_lower:
            rtnval = True
            return rtnval
        if fragment in stdout_lower:
            rtnval = True
            return rtnval
    rtnval = False
    return rtnval


def _format_value_for_powershell(*, value: str, registry_type: str) -> str:
    """
        Render a value into the literal expected by
        ``Set-ItemProperty -Value <literal> -Type <type>``.

        ``DWord`` / ``QWord`` values are emitted as bare integer
        literals; everything else is single-quoted with embedded single
        quotes doubled per PowerShell's quoting rules.

        :param value: The desired value as a string.
        :param registry_type: The bundled ``Set-ItemProperty -Type``.

        :returns: The PowerShell literal text.
    """
    if registry_type in (_REG_TYPE_DWORD, _REG_TYPE_QWORD):
        rtnval = value
        return rtnval

    escaped = value.replace("'", "''")
    rtnval = "'{}'".format(escaped)
    return rtnval


# ----------------------------------------------------------------------
# WindowsKernelParameterManager
# ----------------------------------------------------------------------


class WindowsKernelParameterManager(BaseFeatureManager):
    """
        Windows registry-backed kernel-parameter manager.  Translates a
        bundled subset of cross-OS dotted parameter names into registry
        operations dispatched through the injected
        :class:`ICommandRunner`.

        Writes (``set_parameter``, ``remove_persisted_parameter``)
        honour ``config.elevated`` and pass it through to the runner.
        Reads (``get_parameter``, ``list_parameters``) and idempotency
        probes always pass ``elevated=False``.
    """

    result_class = WindowsKernelParameterActionResult
    status_class = WindowsKernelParameterActionStatus
    feature_name = "kernel-parameters"

    def __init__(
        self,
        runner,
        config: Optional[WindowsKernelParameterManagerConfig] = None,
    ) -> None:
        """
            :param runner: The :class:`ICommandRunner` used by every
                           operation.
            :param config: Manager configuration.  Defaults to a fresh
                           :class:`WindowsKernelParameterManagerConfig`
                           when omitted.
        """
        if config is None:
            config = WindowsKernelParameterManagerConfig()

        super().__init__(runner=runner, config=config)

        # Take a defensive copy of the mapping table so callers cannot
        # mutate the manager's view by editing the dict they passed in.
        if config.parameter_map is None:
            self._parameter_map: dict = dict(_DEFAULT_PARAMETER_MAP)
        else:
            self._parameter_map = dict(config.parameter_map)
        return

    # ------------------------------------------------------------------
    # Mapping-table accessors
    # ------------------------------------------------------------------

    def get_mapping_table(self) -> dict:
        """
            Return a shallow copy of the bundled parameter mapping.

            :returns: A fresh ``dict`` of cross-OS-name to
                      :class:`WindowsKernelParameterMappingEntry`.
        """
        rtnval = dict(self._parameter_map)
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_parameter(self, name: str) -> Optional[WindowsParameterValue]:
        """
            Read the current value of a Windows registry-backed kernel
            parameter.

            :param name: The dotted cross-OS parameter name.

            :returns: The :class:`WindowsParameterValue`, or ``None``
                      when the parameter is unmapped, mapped-but-no-
                      Windows-equivalent, or simply absent from the
                      registry.
            :raises WindowsKernelParameterQueryError: When the runner
                                                      reports an
                                                      unexpected
                                                      failure.
        """
        entry = self._parameter_map.get(name)
        if entry is None:
            rtnval: Optional[WindowsParameterValue] = None
            return rtnval
        if entry.registry_path is None or entry.value_name is None:
            rtnval = None
            return rtnval

        script = (
            "$ErrorActionPreference = 'Stop'; "
            "(Get-ItemProperty -Path '{}' -Name '{}' -ErrorAction Stop)"
            ".'{}'"
        ).format(entry.registry_path, entry.value_name, entry.value_name)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            stdout_value = run_result.stdout.strip()
            rtnval = WindowsParameterValue(
                name=name,
                value=stdout_value,
                is_default=None,
                registry_path=entry.registry_path,
                registry_key=entry.value_name,
            )
            return rtnval

        if _looks_not_found(
            stderr=run_result.stderr or "",
            stdout=run_result.stdout or "",
        ):
            rtnval = None
            return rtnval

        raise WindowsKernelParameterQueryError(
            command=command,
            exit_code=run_result.exit_code,
            stderr=run_result.stderr or "",
        )

    def list_parameters(self) -> dict:
        """
            Enumerate every mapped parameter that currently has a value
            in the registry.

            Walks the bundled mapping table and queries each entry via
            :meth:`get_parameter`.  Parameters that are unmapped, marked
            no-Windows-equivalent, or simply absent are silently
            omitted.  An unexpected query failure for any single entry
            is escalated to :class:`WindowsKernelParameterQueryError`.

            :returns: A ``dict[str, str]`` of present parameters and
                      their current string values.
            :raises WindowsKernelParameterQueryError: When any single
                                                      entry's read fails
                                                      unexpectedly.
        """
        rtnval: dict = {}
        for name, entry in self._parameter_map.items():
            if entry.registry_path is None or entry.value_name is None:
                continue
            current_info = self.get_parameter(name=name)
            if current_info is None:
                continue
            rtnval[name] = current_info.value
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_parameter(
        self,
        name: str,
        value: str,
        persistent: bool = True,
    ) -> WindowsKernelParameterActionResult:
        """
            Write a Windows registry-backed kernel parameter.

            Windows registry writes are inherently persistent — there
            is no separate runtime-only surface.  ``persistent=False``
            callers receive a result whose ``message`` documents that
            the write was still persistent.

            Idempotency: skips when the current registry value already
            matches the desired value.

            :param name: The dotted cross-OS parameter name.
            :param value: The desired value as a string.
            :param persistent: Echoed onto the result; advisory only.

            :returns: The :class:`WindowsKernelParameterActionResult`.
        """
        entry = self._parameter_map.get(name)
        if entry is None:
            rejected = self._build_result(
                status=WindowsKernelParameterActionStatus.FAILED,
                name=name,
                value=value,
                persistent=persistent,
                command=self._reject_argv(name=name),
                message=(
                    "parameter {} is not in the supported Windows "
                    "kernel-parameter mapping."
                ).format(name),
                stderr="parameter_not_supported",
            )
            return rejected

        if entry.registry_path is None or entry.value_name is None:
            rejected = self._build_result(
                status=WindowsKernelParameterActionStatus.FAILED,
                name=name,
                value=value,
                persistent=persistent,
                command=self._reject_argv(name=name),
                message=(
                    "parameter {} has no Windows equivalent."
                ).format(name),
                stderr="parameter_no_windows_equivalent",
            )
            return rejected

        ps_literal = _format_value_for_powershell(
            value=value, registry_type=entry.registry_type,
        )
        write_script = (
            "$ErrorActionPreference = 'Stop'; "
            "if (-not (Test-Path -Path '{path}')) "
            "{{ New-Item -Path '{path}' -Force | Out-Null }}; "
            "Set-ItemProperty -Path '{path}' -Name '{key}' "
            "-Value {value_literal} -Type {reg_type}"
        ).format(
            path=entry.registry_path,
            key=entry.value_name,
            value_literal=ps_literal,
            reg_type=entry.registry_type,
        )
        write_command = self._build_powershell_argv(script=write_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=value,
                persistent=persistent,
                command=write_command,
                applied_value=value,
                registry_path=entry.registry_path,
                registry_key=entry.value_name,
                requires_reboot=entry.requires_reboot,
            )
            return dry

        current_info = self.get_parameter(name=name)
        if current_info is not None:
            current_value = current_info.value
        else:
            current_value = None

        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=WindowsKernelParameterActionStatus.SKIPPED,
                name=name,
                value=value,
                persistent=persistent,
                command=write_command,
                current_value=current_value,
                applied_value=value,
                registry_path=entry.registry_path,
                registry_key=entry.value_name,
                requires_reboot=entry.requires_reboot,
                message="registry value already matches",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsKernelParameterActionStatus.SUCCESS
            applied = value
            if persistent is False:
                detail_message = (
                    "Windows registry writes are inherently persistent; "
                    "persistent=False is advisory only."
                )
            else:
                detail_message = None
        else:
            mapped_status = WindowsKernelParameterActionStatus.FAILED
            applied = None
            detail_message = "registry_write_failed"

        rtnval = WindowsKernelParameterActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            name=name,
            value=value,
            persistent=persistent,
            current_value=current_value,
            applied_value=applied,
            registry_path=entry.registry_path,
            registry_key=entry.value_name,
            requires_reboot=entry.requires_reboot,
            message=detail_message,
        )
        return rtnval

    def reload_parameters(self) -> WindowsKernelParameterActionResult:
        """
            "Reload" registry-backed kernel parameters.

            Windows has no documented user-mode primitive equivalent to
            ``sysctl --system``.  Some keys take effect immediately on
            registry write; others require a reboot or driver restart.
            This method is a documented SUCCESS no-op that returns a
            descriptive ``message`` so cross-OS callers can call it
            blindly without branching on OS.  Honours dry-run.

            :returns: The :class:`WindowsKernelParameterActionResult`.
        """
        # Synthetic argv — the runner is never invoked.
        script = "# reload_parameters is a documented no-op on Windows"
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsKernelParameterActionStatus.DRY_RUN,
                name=None,
                value=None,
                persistent=None,
                command=command,
            )
            return dry

        message = (
            "Windows applies registry-backed kernel parameters either "
            "immediately (per-driver, on next bind) or on next boot. "
            "No reload primitive is available."
        )
        rtnval = self._build_result(
            status=WindowsKernelParameterActionStatus.SUCCESS,
            name=None,
            value=None,
            persistent=None,
            command=command,
            message=message,
        )
        return rtnval

    def remove_persisted_parameter(
        self,
        name: str,
    ) -> WindowsKernelParameterActionResult:
        """
            Remove a registry-backed kernel parameter via
            ``Remove-ItemProperty``.

            Skips when the parameter is unmapped (FAILED), marked
            no-Windows-equivalent (FAILED), or absent from the
            registry (SKIPPED).

            :param name: The dotted cross-OS parameter name.

            :returns: The :class:`WindowsKernelParameterActionResult`.
        """
        entry = self._parameter_map.get(name)
        if entry is None:
            rejected = self._build_result(
                status=WindowsKernelParameterActionStatus.FAILED,
                name=name,
                value=None,
                persistent=True,
                command=self._reject_argv(name=name),
                message=(
                    "parameter {} is not in the supported Windows "
                    "kernel-parameter mapping."
                ).format(name),
                stderr="parameter_not_supported",
            )
            return rejected

        if entry.registry_path is None or entry.value_name is None:
            rejected = self._build_result(
                status=WindowsKernelParameterActionStatus.FAILED,
                name=name,
                value=None,
                persistent=True,
                command=self._reject_argv(name=name),
                message=(
                    "parameter {} has no Windows equivalent."
                ).format(name),
                stderr="parameter_no_windows_equivalent",
            )
            return rejected

        remove_script = (
            "$ErrorActionPreference = 'Stop'; "
            "Remove-ItemProperty -Path '{}' -Name '{}'"
        ).format(entry.registry_path, entry.value_name)
        remove_command = self._build_powershell_argv(script=remove_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=None,
                persistent=True,
                command=remove_command,
                registry_path=entry.registry_path,
                registry_key=entry.value_name,
                requires_reboot=entry.requires_reboot,
            )
            return dry

        current_info = self.get_parameter(name=name)
        if current_info is None:
            skipped = self._build_result(
                status=WindowsKernelParameterActionStatus.SKIPPED,
                name=name,
                value=None,
                persistent=True,
                command=remove_command,
                registry_path=entry.registry_path,
                registry_key=entry.value_name,
                requires_reboot=entry.requires_reboot,
                message="registry value is not present",
            )
            return skipped

        run_result = self._runner.run(
            list(remove_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = WindowsKernelParameterActionStatus.FAILED

        rtnval = WindowsKernelParameterActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(remove_command),
            name=name,
            value=None,
            persistent=True,
            current_value=current_info.value,
            applied_value=None,
            registry_path=entry.registry_path,
            registry_key=entry.value_name,
            requires_reboot=entry.requires_reboot,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsKernelParameterManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsKernelParameterManagerConfig`.

            :returns: The active config typed for static-type clarity.
        """
        if isinstance(self._config, WindowsKernelParameterManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsKernelParameterManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
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

    def _reject_argv(self, *, name: str) -> list:
        """
            Build a synthetic argv recorded on rejection results so the
            caller can still see what the manager would have done.

            :param name: The parameter name.

            :returns: A documentation-only argv.
        """
        script = "# parameter '{}' rejected (not in mapping or no equivalent)".format(
            name,
        )
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsKernelParameterActionStatus,
        name: Optional[str],
        value: Optional[str],
        persistent: Optional[bool],
        command: list,
        current_value: Optional[str] = None,
        applied_value: Optional[str] = None,
        registry_path: Optional[str] = None,
        registry_key: Optional[str] = None,
        requires_reboot: Optional[bool] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsKernelParameterActionResult:
        """
            Construct a :class:`WindowsKernelParameterActionResult`
            without invoking the runner.

            :returns: The :class:`WindowsKernelParameterActionResult`.
        """
        rtnval = WindowsKernelParameterActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            value=value,
            persistent=persistent,
            current_value=current_value,
            applied_value=applied_value,
            registry_path=registry_path,
            registry_key=registry_key,
            requires_reboot=requires_reboot,
            message=message,
            stderr=stderr,
        )
        return rtnval

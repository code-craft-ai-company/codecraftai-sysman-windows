"""
    Windows configuration-management feature.

    Implements the cross-OS configuration-management contract for
    Windows by routing every operation through the injected
    :class:`ICommandRunner`.  The manager dispatches reads / writes /
    removes / lists by classifying the supplied "file" string as either
    a registry path (``HKLM:`` / ``HKCU:`` / ``HKEY_*`` prefix) or a
    plain-text configuration file path (anything else, e.g.
    ``C:\\ProgramData\\app\\config.ini``).

    Routing rules:
        - Source starts with ``HKLM:``, ``HKCU:``, ``HKEY_LOCAL_MACHINE``,
          ``HKEY_CURRENT_USER``, ``HKEY_USERS``, ``HKEY_CLASSES_ROOT``, or
          ``HKEY_CURRENT_CONFIG`` -> registry routing.
        - Otherwise -> plain-text routing.

    Registry surface:
        Reads use ``Get-ItemProperty``, writes use ``Set-ItemProperty``,
        removes use ``Remove-ItemProperty``, and listing uses
        ``Get-Item``'s ``Property`` collection plus a ``Get-ItemProperty``
        per name.  Backup is via ``reg export`` and restore is via
        ``reg import`` (both ``reg.exe`` invocations); these require the
        non-PowerShell ``HKLM\\foo`` syntax (NO colon between hive and
        path) so the manager strips a leading ``HKLM:`` / ``HKCU:`` and
        replaces the colon-prefixed PSDrive form with ``reg.exe``'s
        provider form before invoking ``reg``.

    Plain-text surface:
        Reads via ``Get-Content``, writes via ``Set-Content``, parses
        shell-style ``KEY=VALUE`` lines (the same dialect used by the
        Ubuntu sibling).  Backups via ``Copy-Item``.

    Path-syntax fragility:
        PowerShell registry cmdlets accept ``HKLM:\\foo`` (colon
        between hive and path).  ``reg.exe`` requires ``HKLM\\foo`` (no
        colon).  This module documents and centralises the conversion
        in :func:`_to_reg_exe_path`.

    Type inference (registry writes):
        - bool / int -> ``DWord``
        - str -> ``String`` (auto-promoted to ``ExpandString`` when the
          value contains a ``%VAR%``-shaped expansion token)
        - bytes / bytearray -> ``Binary``
        - list / tuple -> ``MultiString``

    File I/O:
        All file I/O is via PowerShell cmdlets through the runner.  No
        :class:`IFileOperator` is consumed.

    Elevation policy:
        - HKLM writes: elevate (require Administrator).
        - HKCU writes: do not elevate.
        - Plain-file writes: honour ``config.elevated``.
        - Reads: never elevate.
        - ``backup_configuration`` / ``restore_configuration``: honour
          ``config.elevated`` (the source file may be admin-only).
"""

import json
import os
import re

from dataclasses import dataclass
from datetime import datetime
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
_REG_EXE = "reg.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_REG_TYPE_DWORD = "DWord"
_REG_TYPE_QWORD = "QWord"
_REG_TYPE_STRING = "String"
_REG_TYPE_EXPAND_STRING = "ExpandString"
_REG_TYPE_MULTI_STRING = "MultiString"
_REG_TYPE_BINARY = "Binary"

# Routing prefixes (case-insensitive) that select the registry path.
_REGISTRY_PREFIXES = (
    "hklm:",
    "hkcu:",
    "hku:",
    "hkcr:",
    "hkcc:",
    "hkey_local_machine",
    "hkey_current_user",
    "hkey_users",
    "hkey_classes_root",
    "hkey_current_config",
    "registry::",
)

_HKLM_PREFIX = "hklm:"
_HKCU_PREFIX = "hkcu:"

# Stderr / FullyQualifiedErrorId fragments emitted by registry cmdlets
# when the property or path does not exist.
_NOT_FOUND_FRAGMENTS = (
    "propertynotfound",
    "pathnotfound",
    "itemnotfound",
    "does not exist",
    "cannot find path",
    "cannot find any property",
)

# Shell-style KEY=VALUE pattern shared with the Ubuntu sibling.
_SHELL_KV_PATTERN = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)


WindowsConfigActionStatus = ActionStatus


@dataclass
class WindowsConfigManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: PowerShell executable used to
                                      run cmdlets.
        :param backup_suffix_format: ``strftime`` format used to render
                                     the timestamp portion of backup
                                     filenames.  Default produces
                                     compact local-time stamps.
        :param backup_extension: Trailing extension appended to
                                 backups.  Defaults to ``.bak``.
        :param registry_backup_extension: Extension used by registry
                                          backups (``reg export``
                                          conventionally writes
                                          ``.reg``).
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    backup_suffix_format: str = "%Y%m%dT%H%M%S"
    backup_extension: str = ".bak"
    registry_backup_extension: str = ".reg"


@dataclass
class WindowsConfigActionResult(FeatureActionResult):
    """
        Result of a configuration-management write operation.

        :param file: The configuration source the action targeted, or
                     ``None`` for ``reload_configuration``.
        :param key: The setting key targeted, or ``None`` for whole-file
                    operations.
        :param backup_path: Populated by :meth:`backup_configuration`
                            and :meth:`restore_configuration`.
        :param routing: ``"registry"`` or ``"file"`` indicating which
                        backend handled the request.  Useful for tests
                        and observability.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    file: Optional[str] = None
    key: Optional[str] = None
    backup_path: Optional[str] = None
    routing: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsSettingValue:
    """
        A single configuration setting.

        :param key: The setting name.
        :param value: The setting value (stringified for both registry
                      and plain-file shapes).
        :param file: The source the value was parsed from.
        :param routing: ``"registry"`` or ``"file"``.
    """
    key: str
    value: str
    file: str
    routing: Optional[str] = None


class WindowsConfigQueryError(Exception):
    """
        Raised when a configuration read fails for a reason other than
        the target source simply being absent.
    """

    def __init__(
        self,
        *,
        file: Optional[str] = None,
        command: Optional[list] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param file: The source path that failed to read.
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional pre-built message.
        """
        if command is None:
            self.command: list = []
        else:
            self.command = list(command)
        self.file = file
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Configuration query command failed."]
            err_msg_lines.append("    FILE: {}".format(file))
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers — routing / path / type inference
# ----------------------------------------------------------------------


def _classify_source(*, source: str) -> str:
    """
        Return ``"registry"`` for a registry hive prefix; ``"file"``
        otherwise.

        :param source: The configuration source string.

        :returns: Routing label.
    """
    lower = source.lower()
    for prefix in _REGISTRY_PREFIXES:
        if lower.startswith(prefix):
            return "registry"
    return "file"


def _to_reg_exe_path(*, source: str) -> str:
    """
        Convert a PowerShell-style registry path
        (``HKLM:\\SYSTEM\\foo``) into the colon-less form required by
        ``reg.exe`` (``HKLM\\SYSTEM\\foo``).

        :param source: The registry source path.

        :returns: The ``reg.exe``-compatible path.
    """
    lower = source.lower()
    if lower.startswith("hklm:"):
        return "HKLM" + source[len("HKLM:"):]
    if lower.startswith("hkcu:"):
        return "HKCU" + source[len("HKCU:"):]
    if lower.startswith("hku:"):
        return "HKU" + source[len("HKU:"):]
    if lower.startswith("hkcr:"):
        return "HKCR" + source[len("HKCR:"):]
    if lower.startswith("hkcc:"):
        return "HKCC" + source[len("HKCC:"):]
    if lower.startswith("registry::"):
        return source[len("Registry::"):]
    return source


def _is_hklm_source(*, source: str) -> bool:
    """
        Return True when the source targets the HKLM hive (case-
        insensitive on either prefix style).
    """
    lower = source.lower()
    if lower.startswith("hklm:"):
        return True
    if lower.startswith("hkey_local_machine"):
        return True
    return False


def _infer_reg_type(*, value) -> str:
    """
        Infer the registry type for a Python value.

        :param value: The Python value being written.

        :returns: A ``Set-ItemProperty -Type`` string.
    """
    if isinstance(value, bool):
        return _REG_TYPE_DWORD
    if isinstance(value, int):
        return _REG_TYPE_DWORD
    if isinstance(value, (bytes, bytearray)):
        return _REG_TYPE_BINARY
    if isinstance(value, (list, tuple)):
        return _REG_TYPE_MULTI_STRING
    if isinstance(value, str):
        if "%" in value and value.count("%") >= 2:
            return _REG_TYPE_EXPAND_STRING
        return _REG_TYPE_STRING
    return _REG_TYPE_STRING


def _format_value_for_set_item(*, value, registry_type: str) -> str:
    """
        Format a Python value into the literal accepted by
        ``Set-ItemProperty -Value <literal>``.

        :param value: The Python value.
        :param registry_type: The inferred registry type.

        :returns: The PowerShell literal text.
    """
    if registry_type == _REG_TYPE_DWORD:
        if isinstance(value, bool):
            if value is True:
                return "1"
            return "0"
        return str(value)
    if registry_type == _REG_TYPE_QWORD:
        return str(value)
    if registry_type == _REG_TYPE_BINARY:
        # Render as @(byte, byte, ...) of decimal byte values.
        if isinstance(value, (bytes, bytearray)):
            byte_list = list(value)
        else:
            byte_list = []
        joined = ", ".join(str(b) for b in byte_list)
        return "@({})".format(joined)
    if registry_type == _REG_TYPE_MULTI_STRING:
        items = list(value)
        escaped_items = ["'{}'".format(str(i).replace("'", "''")) for i in items]
        return "@({})".format(", ".join(escaped_items))
    # String / ExpandString
    text = str(value)
    escaped = text.replace("'", "''")
    return "'{}'".format(escaped)


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _looks_not_found(*, stderr: str, stdout: str) -> bool:
    """
        Detect "property does not exist" / "path not found" fragments
        emitted by the registry cmdlets.
    """
    stderr_lower = stderr.lower()
    stdout_lower = stdout.lower()
    for fragment in _NOT_FOUND_FRAGMENTS:
        if fragment in stderr_lower:
            return True
        if fragment in stdout_lower:
            return True
    return False


# ----------------------------------------------------------------------
# Module-level helpers — plain-text shell-style parsing
# ----------------------------------------------------------------------


def _parse_shell_kv(*, text: str) -> dict:
    """
        Parse a shell-style ``KEY=VALUE`` file into a dict.
    """
    rtnval: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        match = _SHELL_KV_PATTERN.match(line)
        if match is None:
            continue
        key = match.group(1)
        raw_value = match.group(2)
        rtnval[key] = _strip_quotes(value=raw_value)
    return rtnval


def _set_shell_kv(*, content: str, key: str, value: str) -> str:
    """
        Return ``content`` updated to contain ``KEY="value"``.
    """
    escaped = _escape_value(value=value)
    new_line = '{}="{}"'.format(key, escaped)

    if len(content) == 0:
        body = new_line + "\n"
        return body

    lines = content.splitlines()
    updated: list = []
    replaced = False
    for line in lines:
        match = _SHELL_KV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == key:
            updated.append(new_line)
            replaced = True
            continue
        updated.append(line)

    if not replaced:
        updated.append(new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_shell_kv(*, content: str, key: str) -> str:
    """
        Return ``content`` with any matching ``KEY=`` line removed.
    """
    if len(content) == 0:
        return content

    lines = content.splitlines()
    kept: list = []
    for line in lines:
        match = _SHELL_KV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == key:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _strip_quotes(*, value: str) -> str:
    """
        Strip a single pair of surrounding double quotes from ``value``.
    """
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        unescaped = inner.replace('\\"', '"').replace("\\\\", "\\")
        return unescaped
    return value


def _escape_value(*, value: str) -> str:
    """
        Escape a value for double-quoted shell rendering.
    """
    rtnval = value.replace("\\", "\\\\").replace('"', '\\"')
    return rtnval


# ----------------------------------------------------------------------
# WindowsConfigurationManager
# ----------------------------------------------------------------------


class WindowsConfigurationManager(BaseFeatureManager):
    """
        Windows configuration-management manager.

        Routes by source path: registry hive prefixes go to PowerShell
        registry cmdlets; plain paths go to ``Get-Content`` /
        ``Set-Content`` / shell-style KV parsing.  Every operation is
        dispatched through the injected :class:`ICommandRunner`.
    """

    result_class = WindowsConfigActionResult
    status_class = WindowsConfigActionStatus
    feature_name = "configuration-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def read_setting(self, file: str, key: str) -> WindowsSettingValue:
        """
            Read a single setting from a registry path or plain-text
            configuration file.

            :param file: The source path (registry or plain file).
            :param key: The setting name.

            :returns: The :class:`WindowsSettingValue`.
            :raises WindowsConfigQueryError: When the source is absent
                                             or the key is not present.
        """
        routing = _classify_source(source=file)
        if routing == "registry":
            rtnval = self._read_registry_setting(source=file, key=key)
            return rtnval
        rtnval = self._read_file_setting(source=file, key=key)
        return rtnval

    def list_settings(self, file: str) -> dict:
        """
            Enumerate every setting in a registry path or plain-text
            configuration file.

            :param file: The source path.

            :returns: A ``dict[str, str]`` of every parsed entry.
                      Empty when the source is absent.
        """
        routing = _classify_source(source=file)
        if routing == "registry":
            rtnval = self._list_registry_settings(source=file)
            return rtnval
        rtnval = self._list_file_settings(source=file)
        return rtnval

    # ------------------------------------------------------------------
    # Per-setting write verbs
    # ------------------------------------------------------------------

    def write_setting(
        self,
        file: str,
        key: str,
        value,
    ) -> WindowsConfigActionResult:
        """
            Set ``key=value`` inside ``file``.

            :param file: The source path.
            :param key: The setting name.
            :param value: The desired value (any type — registry type
                          is inferred for registry sources).

            :returns: The :class:`WindowsConfigActionResult`.
        """
        routing = _classify_source(source=file)
        if routing == "registry":
            rtnval = self._write_registry_setting(
                source=file, key=key, value=value,
            )
            return rtnval
        # Plain-text writes coerce to string.
        rtnval = self._write_file_setting(
            source=file, key=key, value=str(value),
        )
        return rtnval

    def remove_setting(
        self,
        file: str,
        key: str,
    ) -> WindowsConfigActionResult:
        """
            Remove ``key`` from ``file``.

            :param file: The source path.
            :param key: The setting name.

            :returns: The :class:`WindowsConfigActionResult`.
        """
        routing = _classify_source(source=file)
        if routing == "registry":
            rtnval = self._remove_registry_setting(source=file, key=key)
            return rtnval
        rtnval = self._remove_file_setting(source=file, key=key)
        return rtnval

    # ------------------------------------------------------------------
    # Whole-source verbs
    # ------------------------------------------------------------------

    def reload_configuration(
        self,
        service: str,
    ) -> WindowsConfigActionResult:
        """
            Reload a backing service via ``Restart-Service -Name
            <service>``.

            Windows registry-backed configuration is consumed by
            services on demand; the only "reload" primitive is a
            service restart.  Honours dry-run.

            When ``service`` is the literal sentinel ``"none"``, the
            method returns SKIPPED without invoking the runner — useful
            for pure-registry edits where no service consumes the
            value.

            :param service: The Windows service name, or ``"none"``.

            :returns: The :class:`WindowsConfigActionResult`.
        """
        if service == "none":
            skipped = self._build_result(
                status=WindowsConfigActionStatus.SKIPPED,
                file=None,
                key=None,
                command=self._build_powershell_argv(
                    script="# reload skipped (service=none)",
                ),
                routing=None,
                message="reload skipped: service sentinel 'none' supplied",
            )
            return skipped

        escaped_service = _ps_escape_single_quoted(value=service)
        script = "Restart-Service -Name '{}'".format(escaped_service)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=None,
                key=None,
                command=command,
                routing=None,
                message=service,
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=service,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=None,
            key=None,
            routing=None,
        )
        return rtnval

    def backup_configuration(
        self,
        file: str,
    ) -> WindowsConfigActionResult:
        """
            Create a sibling timestamped backup of ``file``.

            For registry sources, runs ``reg export <hivepath> <out>
            /y`` (note: ``reg.exe`` requires the colon-less hive
            syntax — see :func:`_to_reg_exe_path`).  For plain files,
            runs ``Copy-Item -Path <src> -Destination <dst>``.  No
            idempotency: every successful invocation creates a fresh
            backup with a new timestamp suffix.  Honours dry-run.

            :param file: The source path.

            :returns: The :class:`WindowsConfigActionResult` with
                      ``backup_path`` populated on success.
        """
        routing = _classify_source(source=file)
        if routing == "registry":
            rtnval = self._backup_registry(source=file)
            return rtnval
        rtnval = self._backup_file(source=file)
        return rtnval

    def restore_configuration(
        self,
        backup_path: str,
        target_path: str,
    ) -> WindowsConfigActionResult:
        """
            Restore ``backup_path`` over ``target_path``.

            Routing is decided by ``target_path``: registry targets use
            ``reg import`` (which writes the .reg file's hive entries);
            file targets use ``Copy-Item``.

            :param backup_path: The backup path.
            :param target_path: The destination path.

            :returns: The :class:`WindowsConfigActionResult`.
        """
        routing = _classify_source(source=target_path)
        if routing == "registry":
            rtnval = self._restore_registry(
                backup_path=backup_path, target_path=target_path,
            )
            return rtnval
        rtnval = self._restore_file(
            backup_path=backup_path, target_path=target_path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal — registry read/list/write/remove
    # ------------------------------------------------------------------

    def _read_registry_setting(
        self,
        *,
        source: str,
        key: str,
    ) -> WindowsSettingValue:
        """
            Read a registry value via ``Get-ItemProperty`` and return a
            :class:`WindowsSettingValue`.
        """
        escaped_path = _ps_escape_single_quoted(value=source)
        escaped_key = _ps_escape_single_quoted(value=key)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "(Get-ItemProperty -Path '{path}' -Name '{key}' "
            "-ErrorAction Stop).'{key}'"
        ).format(path=escaped_path, key=escaped_key)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if _looks_not_found(
                stderr=run_result.stderr or "",
                stdout=run_result.stdout or "",
            ):
                raise WindowsConfigQueryError(
                    file=source,
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr or "",
                    message=(
                        "registry value not found: {}\\{}"
                    ).format(source, key),
                )
            raise WindowsConfigQueryError(
                file=source,
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        stripped = run_result.stdout.strip()
        rtnval = WindowsSettingValue(
            key=key,
            value=stripped,
            file=source,
            routing="registry",
        )
        return rtnval

    def _list_registry_settings(self, *, source: str) -> dict:
        """
            Enumerate every registry value under ``source`` via
            ``Get-Item`` followed by individual ``Get-ItemProperty``
            queries.  Returns an empty dict when the path does not
            exist.
        """
        escaped_path = _ps_escape_single_quoted(value=source)
        # Compact JSON dictionary of the path's values.
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$item = Get-Item -Path '{path}' -ErrorAction Stop; "
            "$rtn = @{{}}; "
            "foreach ($n in $item.Property) "
            "{{ $v = (Get-ItemProperty -Path '{path}' -Name $n).$n; "
            "$rtn[$n] = $v }}; "
            "$rtn | ConvertTo-Json -Compress"
        ).format(path=escaped_path)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if _looks_not_found(
                stderr=run_result.stderr or "",
                stdout=run_result.stdout or "",
            ):
                rtnval: dict = {}
                return rtnval
            rtnval = {}
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = {}
            return rtnval

        try:
            parsed = json.loads(stripped)
        except ValueError:
            rtnval = {}
            return rtnval

        if not isinstance(parsed, dict):
            rtnval = {}
            return rtnval

        rtnval = {}
        for k, v in parsed.items():
            if not isinstance(k, str):
                continue
            if v is None:
                rtnval[k] = ""
                continue
            rtnval[k] = str(v)
        return rtnval

    def _write_registry_setting(
        self,
        *,
        source: str,
        key: str,
        value,
    ) -> WindowsConfigActionResult:
        """
            Write a registry value via ``Set-ItemProperty`` after
            inferring the registry type from the Python value's type.
        """
        registry_type = _infer_reg_type(value=value)
        ps_literal = _format_value_for_set_item(
            value=value, registry_type=registry_type,
        )
        escaped_path = _ps_escape_single_quoted(value=source)
        escaped_key = _ps_escape_single_quoted(value=key)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "if (-not (Test-Path -Path '{path}')) "
            "{{ New-Item -Path '{path}' -Force | Out-Null }}; "
            "Set-ItemProperty -Path '{path}' -Name '{key}' "
            "-Value {value_literal} -Type {reg_type}"
        ).format(
            path=escaped_path,
            key=escaped_key,
            value_literal=ps_literal,
            reg_type=registry_type,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=source,
                key=key,
                command=command,
                routing="registry",
            )
            return dry

        # Idempotency: skip when the current value already matches.
        try:
            current_setting = self._read_registry_setting(
                source=source, key=key,
            )
            current_value = current_setting.value
        except WindowsConfigQueryError:
            current_value = None

        desired_str = self._registry_idempotency_string(
            value=value, registry_type=registry_type,
        )
        if current_value is not None and current_value == desired_str:
            skipped = self._build_result(
                status=WindowsConfigActionStatus.SKIPPED,
                file=source,
                key=key,
                command=command,
                routing="registry",
                message="registry value already matches",
            )
            return skipped

        elevate = self._registry_elevation(source=source)
        run_result = self._runner.run(
            list(command), elevated=elevate,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=source,
            key=key,
            routing="registry",
        )
        return rtnval

    def _remove_registry_setting(
        self,
        *,
        source: str,
        key: str,
    ) -> WindowsConfigActionResult:
        """
            Remove a registry value via ``Remove-ItemProperty``.
            Skips when the value is not currently present.
        """
        escaped_path = _ps_escape_single_quoted(value=source)
        escaped_key = _ps_escape_single_quoted(value=key)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Remove-ItemProperty -Path '{}' -Name '{}'"
        ).format(escaped_path, escaped_key)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=source,
                key=key,
                command=command,
                routing="registry",
            )
            return dry

        try:
            self._read_registry_setting(source=source, key=key)
        except WindowsConfigQueryError:
            skipped = self._build_result(
                status=WindowsConfigActionStatus.SKIPPED,
                file=source,
                key=key,
                command=command,
                routing="registry",
                message="registry value is not present",
            )
            return skipped

        elevate = self._registry_elevation(source=source)
        run_result = self._runner.run(
            list(command), elevated=elevate,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=source,
            key=key,
            routing="registry",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal — plain-text read/list/write/remove
    # ------------------------------------------------------------------

    def _read_file_setting(
        self,
        *,
        source: str,
        key: str,
    ) -> WindowsSettingValue:
        """
            Read a single setting from a plain-text shell-style file.
        """
        content = self._read_file(path=source)
        if content is None:
            raise WindowsConfigQueryError(
                file=source,
                message="file does not exist or is unreadable",
            )

        parsed = _parse_shell_kv(text=content)
        if key not in parsed:
            raise WindowsConfigQueryError(
                file=source,
                message="key not present: {}".format(key),
            )

        rtnval = WindowsSettingValue(
            key=key,
            value=parsed[key],
            file=source,
            routing="file",
        )
        return rtnval

    def _list_file_settings(self, *, source: str) -> dict:
        """
            Enumerate settings in a plain-text shell-style file.
        """
        content = self._read_file(path=source)
        if content is None:
            rtnval: dict = {}
            return rtnval

        rtnval = _parse_shell_kv(text=content)
        return rtnval

    def _write_file_setting(
        self,
        *,
        source: str,
        key: str,
        value: str,
    ) -> WindowsConfigActionResult:
        """
            Write a single setting into a plain-text shell-style file.
        """
        write_command = self._build_set_content_argv(path=source)

        existing = self._read_file(path=source)
        existing_content = "" if existing is None else existing
        desired_content = _set_shell_kv(
            content=existing_content, key=key, value=value,
        )

        if existing is not None and existing == desired_content:
            skipped = self._build_result(
                status=WindowsConfigActionStatus.SKIPPED,
                file=source,
                key=key,
                command=write_command,
                routing="file",
                message="setting already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=source,
                key=key,
                command=write_command,
                routing="file",
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired_content,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=source,
            key=key,
            routing="file",
        )
        return rtnval

    def _remove_file_setting(
        self,
        *,
        source: str,
        key: str,
    ) -> WindowsConfigActionResult:
        """
            Remove a setting from a plain-text shell-style file.
        """
        write_command = self._build_set_content_argv(path=source)

        existing = self._read_file(path=source)
        if existing is None:
            skipped = self._build_result(
                status=WindowsConfigActionStatus.SKIPPED,
                file=source,
                key=key,
                command=write_command,
                routing="file",
                message="file does not exist",
            )
            return skipped

        new_content = _remove_shell_kv(content=existing, key=key)
        if new_content == existing:
            skipped = self._build_result(
                status=WindowsConfigActionStatus.SKIPPED,
                file=source,
                key=key,
                command=write_command,
                routing="file",
                message="key not present in file",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=source,
                key=key,
                command=write_command,
                routing="file",
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=source,
            key=key,
            routing="file",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal — backup / restore
    # ------------------------------------------------------------------

    def _backup_registry(
        self,
        *,
        source: str,
    ) -> WindowsConfigActionResult:
        """
            Backup a registry hive subtree via ``reg export``.
        """
        reg_path = _to_reg_exe_path(source=source)
        backup_path = self._build_backup_path(
            file=source,
            extension=self._typed_config().registry_backup_extension,
        )
        command = [_REG_EXE, "export", reg_path, backup_path, "/y"]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=source,
                key=None,
                command=command,
                backup_path=backup_path,
                routing="registry",
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
            populated_backup = backup_path
        else:
            mapped_status = WindowsConfigActionStatus.FAILED
            populated_backup = None

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=source,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=source,
            key=None,
            backup_path=populated_backup,
            routing="registry",
        )
        return rtnval

    def _backup_file(
        self,
        *,
        source: str,
    ) -> WindowsConfigActionResult:
        """
            Backup a plain-text file via ``Copy-Item``.
        """
        backup_path = self._build_backup_path(
            file=source,
            extension=self._typed_config().backup_extension,
        )
        escaped_source = _ps_escape_single_quoted(value=source)
        escaped_dest = _ps_escape_single_quoted(value=backup_path)
        script = (
            "Copy-Item -Path '{}' -Destination '{}' -Force"
        ).format(escaped_source, escaped_dest)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=source,
                key=None,
                command=command,
                backup_path=backup_path,
                routing="file",
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
            populated_backup = backup_path
        else:
            mapped_status = WindowsConfigActionStatus.FAILED
            populated_backup = None

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=source,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=source,
            key=None,
            backup_path=populated_backup,
            routing="file",
        )
        return rtnval

    def _restore_registry(
        self,
        *,
        backup_path: str,
        target_path: str,
    ) -> WindowsConfigActionResult:
        """
            Restore a registry subtree via ``reg import``.

            ``target_path`` is recorded for traceability but ``reg
            import`` derives the target from the .reg file's contents.
        """
        command = [_REG_EXE, "import", backup_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=target_path,
                key=None,
                command=command,
                backup_path=backup_path,
                routing="registry",
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=target_path,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=target_path,
            key=None,
            backup_path=backup_path,
            routing="registry",
        )
        return rtnval

    def _restore_file(
        self,
        *,
        backup_path: str,
        target_path: str,
    ) -> WindowsConfigActionResult:
        """
            Restore a plain-text file via ``Copy-Item``.

            Idempotency: skips when the target's contents already
            byte-match the backup.
        """
        target_existing = self._read_file(path=target_path)
        backup_content = self._read_file(path=backup_path)

        skip_command = self._build_set_content_argv(path=target_path)
        if backup_content is not None and target_existing is not None:
            if backup_content == target_existing:
                skipped = self._build_result(
                    status=WindowsConfigActionStatus.SKIPPED,
                    file=target_path,
                    key=None,
                    command=skip_command,
                    backup_path=backup_path,
                    routing="file",
                    message="target byte-matches backup",
                )
                return skipped

        escaped_backup = _ps_escape_single_quoted(value=backup_path)
        escaped_target = _ps_escape_single_quoted(value=target_path)
        script = (
            "Copy-Item -Path '{}' -Destination '{}' -Force"
        ).format(escaped_backup, escaped_target)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsConfigActionStatus.DRY_RUN,
                file=target_path,
                key=None,
                command=command,
                backup_path=backup_path,
                routing="file",
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsConfigActionStatus.SUCCESS
        else:
            mapped_status = WindowsConfigActionStatus.FAILED

        rtnval = WindowsConfigActionResult(
            status=mapped_status,
            detail=target_path,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=target_path,
            key=None,
            backup_path=backup_path,
            routing="file",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal — argv / file I/O / config / result helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsConfigManagerConfig:
        """
            Return the manager's config typed.
        """
        if isinstance(self._config, WindowsConfigManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsConfigManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _registry_elevation(self, *, source: str) -> bool:
        """
            Decide whether to elevate a registry write.

            HKLM-rooted writes elevate; HKCU writes do not.  Other
            hives (HKEY_USERS via SID) honour ``config.elevated``
            because the right answer depends on which user's hive is
            being touched.

            :param source: The registry source.

            :returns: True when the write should pass elevated=True.
        """
        if _is_hklm_source(source=source):
            return True
        lower = source.lower()
        if lower.startswith("hkcu:"):
            return False
        if lower.startswith("hkey_current_user"):
            return False
        # HKEY_USERS / Registry::HKEY_USERS / HKCR / HKCC -> caller
        # controls.
        return self._config.elevated

    def _registry_idempotency_string(
        self,
        *,
        value,
        registry_type: str,
    ) -> str:
        """
            Render a Python value into the string a subsequent
            ``Get-ItemProperty`` call would return.  Used for the
            write-skip idempotency comparison.
        """
        if registry_type == _REG_TYPE_DWORD:
            if isinstance(value, bool):
                if value is True:
                    return "1"
                return "0"
            return str(value)
        if registry_type == _REG_TYPE_QWORD:
            return str(value)
        if registry_type == _REG_TYPE_BINARY:
            if isinstance(value, (bytes, bytearray)):
                # PowerShell stringifies a byte[] as space-separated
                # decimal integers when retrieved via the property
                # accessor in a non-array context.  We compare on the
                # space-separated form because that is what stripped
                # stdout ends up looking like.
                return " ".join(str(b) for b in list(value))
            return str(value)
        if registry_type == _REG_TYPE_MULTI_STRING:
            items = list(value)
            return " ".join(str(i) for i in items)
        # String / ExpandString
        return str(value)

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _build_set_content_argv(self, *, path: str) -> list:
        """
            Build the argv used to write a plain-text file via
            ``Set-Content``.  Content is supplied on stdin.

            :param path: The destination file path.

            :returns: The argv list.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        # Use -Value (Get-Content -Raw on stdin) to write everything
        # from stdin atomically; PowerShell's pipeline `$Input` is
        # consumed before `-Value` is bound, so we route via
        # ``[Console]::In.ReadToEnd()`` which matches our runner's
        # stdin payload.
        script = (
            "$content = [Console]::In.ReadToEnd(); "
            "Set-Content -Path '{}' -Value $content -NoNewline -Encoding utf8"
        ).format(escaped_path)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a plain-text file via ``Get-Content -Raw``.

            :param path: The absolute path.

            :returns: The contents, or None when missing.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-Content -Path '{}' -Raw -ErrorAction Stop"
        ).format(escaped_path)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_backup_path(self, *, file: str, extension: str) -> str:
        """
            Render a sibling timestamped backup path.

            For registry sources, the path is sanitised by replacing
            the colon and backslashes with hyphens so the result is a
            valid filesystem name.

            :param file: The source path or registry path.
            :param extension: The backup extension.

            :returns: The new backup path.
        """
        timestamp = datetime.now().strftime(
            self._typed_config().backup_suffix_format,
        )
        if _classify_source(source=file) == "registry":
            sanitised = file.replace(":", "_").replace("\\", "_")
            rtnval = "{}.{}{}".format(sanitised, timestamp, extension)
            return rtnval
        rtnval = "{}.{}{}".format(file, timestamp, extension)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsConfigActionStatus,
        file: Optional[str],
        key: Optional[str],
        command: list,
        backup_path: Optional[str] = None,
        routing: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsConfigActionResult:
        """
            Construct a :class:`WindowsConfigActionResult` without
            invoking the runner.
        """
        if key is not None:
            detail = key
        else:
            detail = file
        rtnval = WindowsConfigActionResult(
            status=status,
            detail=detail,
            command=list(command),
            file=file,
            key=key,
            backup_path=backup_path,
            routing=routing,
            message=message,
            stderr=stderr,
        )
        return rtnval

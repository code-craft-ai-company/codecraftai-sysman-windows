"""
    Windows kdump-management feature.

    Windows kernel crash dumps are configured via the
    ``HKLM:\\SYSTEM\\CurrentControlSet\\Control\\CrashControl`` registry
    key.  The verbs of the cross-OS kdump-management contract translate
    onto registry-value writes and PowerShell-driven file enumeration:

        * ``enable_kdump``  -> ``Set-ItemProperty`` of
          ``CrashDumpEnabled`` to a non-zero kdump-mode integer
          (1=Complete, 2=Kernel, 3=Small/Mini, 7=Automatic Memory
          Dump).  Marks ``requires_reboot=True``.
        * ``disable_kdump`` -> ``Set-ItemProperty`` of
          ``CrashDumpEnabled`` to ``0``.  Marks
          ``requires_reboot=True``.
        * ``configure_kdump_target`` -> ``Set-ItemProperty`` of
          ``DumpFile`` (default ``%SystemRoot%\\MEMORY.DMP``) for
          ``LOCAL_PATH`` targets only.  Marks
          ``requires_reboot=True``.  ``SSH`` and ``NFS`` target kinds
          return ``FAILED`` with an ``unsupported_target_kind`` stderr
          token because Windows kernel dumps cannot be redirected to
          remote endpoints from this layer.
        * ``get_kdump_status`` -> ``Get-ItemProperty`` against the
          CrashControl key + JSON parse.
        * ``list_recent_dumps`` -> ``Get-ChildItem`` of
          ``%SystemRoot%\\Minidump\\*.dmp`` plus
          ``%SystemRoot%\\MEMORY.DMP``.
        * ``clear_old_dumps`` -> ``Get-ChildItem ... | Where-Object {
          $_.LastWriteTime -lt ... } | Remove-Item``.
        * ``get_dump_info`` -> ``Get-Item`` for filename / mtime /
          size; no .dmp parsing in pure Python.

    File / registry path syntax:
        PowerShell registry cmdlets use the colon-prefixed PSDrive form
        (``HKLM:\\SYSTEM\\...``).  This module exclusively uses that
        form for every cmdlet invocation.

    Persistence:
        Windows registry writes ARE persistent.  However, the kernel
        consults ``CrashControl`` only at boot, so changing
        ``CrashDumpEnabled`` or ``DumpFile`` requires a reboot for the
        new value to take effect.  Every successful write
        therefore returns ``requires_reboot=True``.

    Elevation policy:
        Writes (``enable_kdump``, ``disable_kdump``,
        ``configure_kdump_target``, ``clear_old_dumps``) honour
        ``config.elevated``.  Reads (``get_kdump_status``,
        ``list_recent_dumps``, ``get_dump_info``) and idempotency
        probes always pass ``elevated=False``.  HKLM reads typically
        require Administrator on hardened systems; the runner is
        responsible for surfacing the access-denied error when the
        host process lacks elevation.
"""

import json
import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_DEFAULT_CRASHCONTROL_PATH = (
    r"HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl"
)
_DEFAULT_MINIDUMP_DIR = r"%SystemRoot%\Minidump"
_DEFAULT_MEMORY_DMP = r"%SystemRoot%\MEMORY.DMP"

_CRASH_DUMP_ENABLED_NAME = "CrashDumpEnabled"
_DUMP_FILE_NAME = "DumpFile"

_KDUMP_MODE_DISABLED = 0
_KDUMP_MODE_COMPLETE = 1
_KDUMP_MODE_KERNEL = 2
_KDUMP_MODE_SMALL = 3
_KDUMP_MODE_AUTOMATIC = 7

_KNOWN_KDUMP_MODES = frozenset({
    _KDUMP_MODE_DISABLED,
    _KDUMP_MODE_COMPLETE,
    _KDUMP_MODE_KERNEL,
    _KDUMP_MODE_SMALL,
    _KDUMP_MODE_AUTOMATIC,
})

_DEFAULT_ENABLE_MODE = _KDUMP_MODE_AUTOMATIC


WindowsKdumpActionStatus = ActionStatus


class WindowsKdumpTargetKind(Enum):
    """
        Windows kdump target kinds.

        Only ``LOCAL_PATH`` is supported on Windows -- the kernel dump
        engine writes a single file at the path stored in the
        ``DumpFile`` registry value.  ``SSH`` and ``NFS`` are listed
        for cross-OS contract conformance and the manager rejects
        attempts to use them with a typed ``FAILED`` result.
    """
    LOCAL_PATH = "local_path"
    SSH        = "ssh"
    NFS        = "nfs"


@dataclass
class WindowsKdumpManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsKdumpManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: PowerShell executable path used
                                      to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-
                                      resolved).
        :param crashcontrol_path: Registry path for the CrashControl
                                  key.  Default
                                  ``HKLM:\\SYSTEM\\CurrentControlSet\\Control\\CrashControl``.
        :param minidump_dir: Directory where minidumps land.  Default
                             ``%SystemRoot%\\Minidump``.
        :param memory_dmp: Path to the complete-mode memory dump
                           file.  Default ``%SystemRoot%\\MEMORY.DMP``.
        :param default_enable_mode: Integer value to write to
                                    ``CrashDumpEnabled`` when the
                                    coordinator calls
                                    :meth:`WindowsKdumpManager.enable_kdump`
                                    without a more specific request.
                                    Default ``7`` (Automatic Memory
                                    Dump, the modern Windows default).
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    crashcontrol_path: str = _DEFAULT_CRASHCONTROL_PATH
    minidump_dir: str = _DEFAULT_MINIDUMP_DIR
    memory_dmp: str = _DEFAULT_MEMORY_DMP
    default_enable_mode: int = _DEFAULT_ENABLE_MODE


@dataclass
class WindowsKdumpActionResult(FeatureActionResult):
    """
        Result of a kdump-management write operation.

        :param target: The dump target affected, when applicable
                       (e.g. the ``DumpFile`` path for
                       ``configure_kdump_target``).  ``None`` for
                       lifecycle verbs that do not touch the target.
        :param requires_reboot: True for any successful write to the
                                CrashControl registry key.  Windows
                                consults these values only at boot.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    target: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class WindowsKdumpTargetSpec:
    """
        Desired Windows kdump target.

        :param target_kind: The :class:`WindowsKdumpTargetKind`
                            selector.  Only ``LOCAL_PATH`` results in
                            a write; ``SSH`` and ``NFS`` produce a
                            typed ``FAILED``.
        :param path: Local filesystem path for ``LOCAL_PATH``.
        :param ssh_user: Reserved -- Windows rejects SSH targets.
        :param ssh_host: Reserved -- Windows rejects SSH targets.
        :param nfs_export: Reserved -- Windows rejects NFS targets.
        :param compression: Reserved -- Windows uses its own
                            compression knobs (e.g. NoLazyWrite) that
                            are out of scope here.
    """
    target_kind: WindowsKdumpTargetKind
    path: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_host: Optional[str] = None
    nfs_export: Optional[str] = None
    compression: Optional[str] = None


@dataclass
class WindowsKdumpStatus:
    """
        Snapshot of the current Windows crash-dump configuration.

        :param enabled: True when ``CrashDumpEnabled`` is non-zero.
        :param running: True when ``enabled`` is True (Windows kernel
                        crash dumping is kernel-resident; there is no
                        separate "running" state).
        :param target_kind: Always ``LOCAL_PATH`` when a ``DumpFile``
                            value is present; otherwise ``None``.
        :param target_path: The ``DumpFile`` registry value, when
                            present.
        :param kernel: Reserved -- Windows does not surface a separate
                       capture-kernel image.  Always ``None``.
        :param reserved_memory_mb: Reserved -- Windows kdump uses the
                                   pagefile rather than pre-reserved
                                   memory.  Always ``None``.
        :param message: Optional human-readable advisory.
    """
    enabled: bool
    running: bool
    target_kind: Optional[WindowsKdumpTargetKind] = None
    target_path: Optional[str] = None
    kernel: Optional[str] = None
    reserved_memory_mb: Optional[int] = None
    message: Optional[str] = None


@dataclass
class WindowsKdumpInfo:
    """
        Metadata for a single Windows kernel-dump artifact.

        :param id: Filename of the dump (e.g.
                   ``MEMORY.DMP`` or ``Mini042426-01.dmp``).
        :param path: Absolute path to the dump file.
        :param created: ``LastWriteTime`` as reported by
                        ``Get-ChildItem`` / ``Get-Item``, free-form
                        text.
        :param size_bytes: ``Length`` reported by Get-ChildItem.
        :param kernel: Reserved -- Windows does not expose a kernel
                       version through the .dmp file metadata.  Always
                       ``None``.
        :param panic_message: Reserved -- there is no in-Python parser
                              for .dmp files.  Always ``None``; use a
                              dedicated debugger (e.g.
                              ``windbg -z``) for triage.
    """
    id: str
    path: str
    created: str
    size_bytes: Optional[int] = None
    kernel: Optional[str] = None
    panic_message: Optional[str] = None


class WindowsKdumpQueryError(Exception):
    """
        Raised when a kdump-management read fails for a reason other
        than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Windows kdump query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
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

        :param value: Raw text.

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _parse_crashcontrol_json(*, text: str) -> dict:
    """
        Parse the JSON payload emitted by ``Get-ItemProperty ... |
        ConvertTo-Json``.

        ``Get-ItemProperty`` against a registry key produces a
        property-bag object that ``ConvertTo-Json`` renders as a JSON
        object whose keys include the registry value names plus
        PowerShell metadata fields (``PSPath``, ``PSChildName``, ...).

        :param text: Raw JSON text.

        :returns: A flat dict of the parsed JSON, or an empty dict
                  when the payload is empty / unparseable.
    """
    rtnval: dict = {}
    if text is None:
        return rtnval
    body = text.strip()
    if len(body) == 0:
        return rtnval
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):
        return rtnval
    if isinstance(decoded, dict):
        for key, value in decoded.items():
            rtnval[key] = value
    return rtnval


def _parse_listing_json(*, text: str) -> list[dict]:
    """
        Parse the JSON payload emitted by ``Get-ChildItem | Select |
        ConvertTo-Json``.

        ``ConvertTo-Json`` collapses a single-element list into a
        single object instead of a 1-list; this helper normalises both
        shapes into a list.

        :param text: Raw JSON text.

        :returns: A list of dicts; empty when the payload is empty.
    """
    rtnval: list[dict] = []
    if text is None:
        return rtnval
    body = text.strip()
    if len(body) == 0:
        return rtnval
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):
        return rtnval

    if isinstance(decoded, dict):
        rtnval.append(decoded)
        return rtnval
    if isinstance(decoded, list):
        for entry in decoded:
            if isinstance(entry, dict):
                rtnval.append(entry)
        return rtnval

    return rtnval


# ----------------------------------------------------------------------
# WindowsKdumpManager
# ----------------------------------------------------------------------


class WindowsKdumpManager(BaseFeatureManager):
    """
        Windows kdump (Memory.dmp / minidump) manager.  Translates
        cross-OS verbs into PowerShell ``Get-ItemProperty`` /
        ``Set-ItemProperty`` / ``Get-ChildItem`` / ``Remove-Item``
        invocations dispatched through the injected
        :class:`ICommandRunner`.

        Writes (``enable_kdump``, ``disable_kdump``,
        ``configure_kdump_target``, ``clear_old_dumps``) honour
        ``config.elevated``.  Successful CrashControl writes always
        carry ``requires_reboot=True``.  Reads (``get_kdump_status``,
        ``list_recent_dumps``, ``get_dump_info``) and idempotency
        probes always pass ``elevated=False``.
    """

    result_class = WindowsKdumpActionResult
    status_class = WindowsKdumpActionStatus
    feature_name = "kdump-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_kdump(self) -> WindowsKdumpActionResult:
        """
            Enable Windows kernel crash dump capture.

            Writes the configured ``default_enable_mode`` (default
            ``7`` -- Automatic Memory Dump) to
            ``CrashControl\\CrashDumpEnabled``.  Idempotent: skips
            when the registry already carries the desired mode.

            :returns: The :class:`WindowsKdumpActionResult` with
                      ``requires_reboot=True`` on success.
        """
        action = "enable"
        desired_mode = self._config.default_enable_mode
        write_command = self._build_set_dword_argv(
            value_name=_CRASH_DUMP_ENABLED_NAME,
            value=desired_mode,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsKdumpActionStatus.DRY_RUN,
                detail=action,
                command=list(write_command),
                target=self._config.crashcontrol_path,
                requires_reboot=True,
            )
            return dry

        current = self._read_crashcontrol()
        current_mode = _coerce_int(value=current.get(_CRASH_DUMP_ENABLED_NAME))
        if current_mode == desired_mode:
            skipped = self._build_result(
                status=WindowsKdumpActionStatus.SKIPPED,
                detail=action,
                command=list(write_command),
                target=self._config.crashcontrol_path,
                requires_reboot=False,
                message="CrashDumpEnabled already matches the requested mode",
            )
            return skipped

        rtnval = self._dispatch(
            command=write_command,
            detail=action,
            operation="enable_kdump",
            target=self._config.crashcontrol_path,
            requires_reboot=True,
        )
        return rtnval

    def disable_kdump(self) -> WindowsKdumpActionResult:
        """
            Disable Windows kernel crash dump capture.

            Writes ``0`` to ``CrashControl\\CrashDumpEnabled``.
            Idempotent: skips when the registry already carries ``0``.

            :returns: The :class:`WindowsKdumpActionResult` with
                      ``requires_reboot=True`` on success.
        """
        action = "disable"
        desired_mode = _KDUMP_MODE_DISABLED
        write_command = self._build_set_dword_argv(
            value_name=_CRASH_DUMP_ENABLED_NAME,
            value=desired_mode,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsKdumpActionStatus.DRY_RUN,
                detail=action,
                command=list(write_command),
                target=self._config.crashcontrol_path,
                requires_reboot=True,
            )
            return dry

        current = self._read_crashcontrol()
        current_mode = _coerce_int(value=current.get(_CRASH_DUMP_ENABLED_NAME))
        if current_mode == desired_mode:
            skipped = self._build_result(
                status=WindowsKdumpActionStatus.SKIPPED,
                detail=action,
                command=list(write_command),
                target=self._config.crashcontrol_path,
                requires_reboot=False,
                message="CrashDumpEnabled is already disabled",
            )
            return skipped

        rtnval = self._dispatch(
            command=write_command,
            detail=action,
            operation="disable_kdump",
            target=self._config.crashcontrol_path,
            requires_reboot=True,
        )
        return rtnval

    def configure_kdump_target(
        self,
        spec: WindowsKdumpTargetSpec,
    ) -> WindowsKdumpActionResult:
        """
            Apply ``spec`` to the CrashControl registry key.

            Only ``LOCAL_PATH`` is supported.  ``SSH`` and ``NFS``
            return a typed ``FAILED`` with stderr token
            ``unsupported_target_kind``.

            :param spec: The :class:`WindowsKdumpTargetSpec` to apply.

            :returns: The :class:`WindowsKdumpActionResult` with
                      ``requires_reboot=True`` on success.
        """
        action = "configure_target"
        if spec.target_kind != WindowsKdumpTargetKind.LOCAL_PATH:
            failed = self._build_result(
                status=WindowsKdumpActionStatus.FAILED,
                detail=action,
                command=None,
                target=None,
                requires_reboot=False,
                message=(
                    "Windows kdump only supports LOCAL_PATH targets; "
                    "received {}".format(spec.target_kind.value)
                ),
                stderr="unsupported_target_kind",
            )
            return failed

        if spec.path is None or len(spec.path) == 0:
            failed = self._build_result(
                status=WindowsKdumpActionStatus.FAILED,
                detail=action,
                command=None,
                target=None,
                requires_reboot=False,
                message="LOCAL_PATH target requires a non-empty path",
                stderr="invalid_target_spec",
            )
            return failed

        write_command = self._build_set_string_argv(
            value_name=_DUMP_FILE_NAME,
            value=spec.path,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsKdumpActionStatus.DRY_RUN,
                detail=action,
                command=list(write_command),
                target=spec.path,
                requires_reboot=True,
            )
            return dry

        current = self._read_crashcontrol()
        current_dumpfile = current.get(_DUMP_FILE_NAME)
        if isinstance(current_dumpfile, str) and current_dumpfile == spec.path:
            skipped = self._build_result(
                status=WindowsKdumpActionStatus.SKIPPED,
                detail=action,
                command=list(write_command),
                target=spec.path,
                requires_reboot=False,
                message="DumpFile already matches the requested path",
            )
            return skipped

        rtnval = self._dispatch(
            command=write_command,
            detail=action,
            operation="configure_kdump_target",
            target=spec.path,
            requires_reboot=True,
        )
        return rtnval

    def clear_old_dumps(
        self,
        age_days: int,
    ) -> WindowsKdumpActionResult:
        """
            Remove minidumps and ``MEMORY.DMP`` older than
            ``age_days``.

            :param age_days: Minimum age in days.  Must be ``>= 0``.

            :returns: The :class:`WindowsKdumpActionResult`.
        """
        action = "clear_old_dumps"
        if age_days < 0:
            failed = self._build_result(
                status=WindowsKdumpActionStatus.FAILED,
                detail=action,
                command=None,
                target=self._config.minidump_dir,
                requires_reboot=False,
                message="age_days must be >= 0",
                stderr="invalid_age_days",
            )
            return failed

        minidump_pattern = "{}\\*.dmp".format(self._config.minidump_dir)
        memory_dmp = self._config.memory_dmp

        script = (
            "$cutoff = (Get-Date).AddDays(-{age}); "
            "Get-ChildItem -Path '{mini}','{memory}' "
            "-File -ErrorAction SilentlyContinue | "
            "Where-Object {{ $_.LastWriteTime -lt $cutoff }} | "
            "Remove-Item -Force -ErrorAction Stop"
        ).format(
            age=age_days,
            mini=_ps_escape_single_quoted(value=minidump_pattern),
            memory=_ps_escape_single_quoted(value=memory_dmp),
        )
        command = self._build_powershell_argv(script=script)

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="clear_old_dumps",
            target=self._config.minidump_dir,
            requires_reboot=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_kdump_status(self) -> WindowsKdumpStatus:
        """
            Snapshot of the current Windows kdump configuration.

            Runs ``Get-ItemProperty`` against the CrashControl key
            and converts the result to JSON.  Reads never elevate.

            :returns: The :class:`WindowsKdumpStatus`.
        """
        current = self._read_crashcontrol()

        crash_mode = _coerce_int(value=current.get(_CRASH_DUMP_ENABLED_NAME))
        if crash_mode is None:
            enabled = False
        elif crash_mode == _KDUMP_MODE_DISABLED:
            enabled = False
        else:
            enabled = True

        dumpfile = current.get(_DUMP_FILE_NAME)
        if isinstance(dumpfile, str) and len(dumpfile) > 0:
            target_kind: Optional[WindowsKdumpTargetKind] = (
                WindowsKdumpTargetKind.LOCAL_PATH
            )
            target_path: Optional[str] = dumpfile
        else:
            target_kind = None
            target_path = None

        message: Optional[str]
        if crash_mode is not None and crash_mode not in _KNOWN_KDUMP_MODES:
            message = "unknown CrashDumpEnabled mode: {}".format(crash_mode)
        else:
            message = None

        rtnval = WindowsKdumpStatus(
            enabled=enabled,
            running=enabled,
            target_kind=target_kind,
            target_path=target_path,
            kernel=None,
            reserved_memory_mb=None,
            message=message,
        )
        return rtnval

    def list_recent_dumps(self) -> list[WindowsKdumpInfo]:
        """
            Enumerate kernel-dump artifacts.

            Runs ``Get-ChildItem -File`` against
            ``%SystemRoot%\\Minidump\\*.dmp`` and
            ``%SystemRoot%\\MEMORY.DMP``, projects ``Name``,
            ``FullName``, ``LastWriteTime``, and ``Length``, and
            converts to JSON for parsing.

            :returns: A list of :class:`WindowsKdumpInfo`, freshest
                      first.
            :raises WindowsKdumpQueryError: When the cmdlet fails for
                                            a reason other than the
                                            paths being absent.
        """
        minidump_pattern = "{}\\*.dmp".format(self._config.minidump_dir)
        memory_dmp = self._config.memory_dmp

        script = (
            "$results = Get-ChildItem -Path '{mini}','{memory}' "
            "-File -ErrorAction SilentlyContinue | "
            "Sort-Object LastWriteTime -Descending | "
            "Select-Object Name,FullName,LastWriteTime,Length; "
            "if ($null -eq $results) {{ '[]' }} "
            "else {{ $results | ConvertTo-Json -Depth 5 -Compress }}"
        ).format(
            mini=_ps_escape_single_quoted(value=minidump_pattern),
            memory=_ps_escape_single_quoted(value=memory_dmp),
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsKdumpQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rows = _parse_listing_json(text=run_result.stdout)
        rtnval: list[WindowsKdumpInfo] = []
        for row in rows:
            name_value = row.get("Name")
            if not isinstance(name_value, str):
                continue
            full_name = row.get("FullName")
            if isinstance(full_name, str) and len(full_name) > 0:
                path_value = full_name
            else:
                path_value = name_value
            last_write = row.get("LastWriteTime")
            if isinstance(last_write, str):
                created = last_write
            else:
                created = ""
            length_value = row.get("Length")
            if isinstance(length_value, int):
                size_bytes: Optional[int] = length_value
            elif isinstance(length_value, str) and length_value.isdigit():
                size_bytes = int(length_value)
            else:
                size_bytes = None

            info = WindowsKdumpInfo(
                id=name_value,
                path=path_value,
                created=created,
                size_bytes=size_bytes,
                kernel=None,
                panic_message=None,
            )
            rtnval.append(info)

        return rtnval

    def get_dump_info(self, dump_id: str) -> WindowsKdumpInfo:
        """
            Return metadata for a single dump file.

            Resolves ``<minidump_dir>\\<dump_id>`` first; falls back
            to the configured ``MEMORY.DMP`` path when ``dump_id``
            matches its filename.  Surfaces only filesystem metadata;
            this manager does NOT parse .dmp internals.

            :param dump_id: The dump filename.

            :returns: The :class:`WindowsKdumpInfo`.
            :raises WindowsKdumpQueryError: When neither candidate
                                            path resolves.
        """
        candidate = self._resolve_dump_path(dump_id=dump_id)

        script = (
            "Get-Item -LiteralPath '{path}' -ErrorAction Stop | "
            "Select-Object Name,FullName,LastWriteTime,Length | "
            "ConvertTo-Json -Depth 5 -Compress"
        ).format(path=_ps_escape_single_quoted(value=candidate))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsKdumpQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rows = _parse_listing_json(text=run_result.stdout)
        if len(rows) == 0:
            raise WindowsKdumpQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr="empty Get-Item payload",
            )

        row = rows[0]
        name_value = row.get("Name", dump_id)
        if not isinstance(name_value, str):
            name_value = dump_id
        full_name = row.get("FullName")
        if isinstance(full_name, str) and len(full_name) > 0:
            path_value = full_name
        else:
            path_value = candidate
        last_write = row.get("LastWriteTime")
        if isinstance(last_write, str):
            created = last_write
        else:
            created = ""
        length_value = row.get("Length")
        if isinstance(length_value, int):
            size_bytes: Optional[int] = length_value
        elif isinstance(length_value, str) and length_value.isdigit():
            size_bytes = int(length_value)
        else:
            size_bytes = None

        rtnval = WindowsKdumpInfo(
            id=name_value,
            path=path_value,
            created=created,
            size_bytes=size_bytes,
            kernel=None,
            panic_message=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_dump_path(self, *, dump_id: str) -> str:
        """
            Resolve ``dump_id`` against the configured paths.

            :param dump_id: The dump filename or basename.

            :returns: An absolute path candidate.  The manager does
                      NOT verify existence here; the subsequent
                      ``Get-Item`` invocation handles the missing-file
                      case.
        """
        memory_basename = self._config.memory_dmp.rsplit("\\", 1)[-1]
        if dump_id.upper() == memory_basename.upper():
            rtnval = self._config.memory_dmp
            return rtnval

        rtnval = "{}\\{}".format(self._config.minidump_dir, dump_id)
        return rtnval

    def _read_crashcontrol(self) -> dict:
        """
            Read the CrashControl registry key via
            ``Get-ItemProperty`` and parse the JSON payload.

            :returns: A dict of registry value name to value.  Empty
                      when the key is missing or the read fails.
        """
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-ItemProperty -Path '{path}' | "
            "ConvertTo-Json -Depth 5 -Compress"
        ).format(
            path=_ps_escape_single_quoted(value=self._config.crashcontrol_path),
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: dict = {}
            return rtnval
        rtnval = _parse_crashcontrol_json(text=run_result.stdout)
        return rtnval

    def _build_set_dword_argv(
        self,
        *,
        value_name: str,
        value: int,
    ) -> list[str]:
        """
            Build the PowerShell argv for ``Set-ItemProperty -Type
            DWord``.

            :param value_name: The registry value name.
            :param value: The integer value.

            :returns: The full argv.
        """
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Set-ItemProperty -Path '{path}' -Name '{name}' "
            "-Value {value} -Type DWord"
        ).format(
            path=_ps_escape_single_quoted(value=self._config.crashcontrol_path),
            name=_ps_escape_single_quoted(value=value_name),
            value=value,
        )
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_set_string_argv(
        self,
        *,
        value_name: str,
        value: str,
    ) -> list[str]:
        """
            Build the PowerShell argv for ``Set-ItemProperty -Type
            ExpandString``.  ``DumpFile`` traditionally embeds
            ``%SystemRoot%`` so ExpandString is the right type.

            :param value_name: The registry value name.
            :param value: The string value.

            :returns: The full argv.
        """
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Set-ItemProperty -Path '{path}' -Name '{name}' "
            "-Value '{value}' -Type ExpandString"
        ).format(
            path=_ps_escape_single_quoted(value=self._config.crashcontrol_path),
            name=_ps_escape_single_quoted(value=value_name),
            value=_ps_escape_single_quoted(value=value),
        )
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list[str]:
        """
            Wrap ``script`` in the canonical PowerShell argv prefix.

            :param script: The PowerShell script body.

            :returns: The full argv.
        """
        rtnval = [
            self._config.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsKdumpActionStatus,
        detail: Optional[str],
        command: Optional[list[str]],
        target: Optional[str] = None,
        requires_reboot: bool = False,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> WindowsKdumpActionResult:
        """
            Construct a :class:`WindowsKdumpActionResult` without
            dispatching.

            :param status: The outcome status.
            :param detail: Short identifier of the action.
            :param command: Would-have-been argv (or executed argv).
            :param target: The dump target affected.
            :param requires_reboot: Reboot advisory.
            :param message: Optional human-readable explanation.
            :param stderr: Optional synthetic stderr payload.
            :param exit_code: Optional exit code.

            :returns: The :class:`WindowsKdumpActionResult`.
        """
        if command is None:
            command_copy: Optional[list[str]] = None
        else:
            command_copy = list(command)
        rtnval = WindowsKdumpActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            target=target,
            requires_reboot=requires_reboot,
            message=message,
            stderr=stderr,
            exit_code=exit_code,
        )
        return rtnval


def _coerce_int(*, value) -> Optional[int]:
    """
        Coerce a JSON-decoded value into an int when possible.

        :param value: Raw value from the parsed JSON.

        :returns: The int, or ``None`` when the value is missing /
                  not a numeric form.
    """
    if value is None:
        rtnval: Optional[int] = None
        return rtnval
    if isinstance(value, bool):
        # ``bool`` is a subclass of int; reject it explicitly to avoid
        # treating True/False as 1/0 modes.
        rtnval = None
        return rtnval
    if isinstance(value, int):
        rtnval = value
        return rtnval
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval
        if stripped.lstrip("-").isdigit():
            rtnval = int(stripped)
            return rtnval
    rtnval = None
    return rtnval

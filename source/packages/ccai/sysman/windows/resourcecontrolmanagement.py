"""
    Windows resource-control-management feature.

    Implements the cross-OS resource-control-management contract
    for Windows using **logical group records** (one JSON spec
    file per group at ``<group_dir>\\<name>.json``) plus
    **per-PID PowerShell adjustments** (``PriorityClass``,
    ``ProcessorAffinity``).

    Windows Job Objects are the kernel-level mechanism for
    arbitrary process-group resource ceilings, but PowerShell
    does NOT expose clean cmdlets to create or attach to Job
    Objects.  The pilot therefore takes the "logical group"
    path: the JSON spec is the source of truth, attached PIDs
    are listed in the spec, and the spec's recorded
    ``PriorityClass`` is applied to each PID via
    ``Get-Process``.

    Windows LIMITATIONS — PROMINENT:

    * ``set_memory_limit`` returns ``FAILED`` with
      ``detail="windows_no_powershell_job_object_memory_limit"``.
      PowerShell exposes no clean Job Object cmdlet.
    * ``set_io_limits`` returns ``FAILED`` with
      ``detail="windows_no_powershell_io_limit"``.  PowerShell
      exposes no per-process IO bandwidth limit.
    * ``set_cpu_limit`` is a coarse mapping from percent to
      ``ProcessPriorityClass`` (Idle / BelowNormal / Normal /
      AboveNormal / High); it is NOT a percent throttle.
    * ``attach_process`` records the PID in the spec and
      applies the recorded ``PriorityClass``; the PID is NOT a
      member of any kernel-side container.
    * ``get_group_usage.io_read_bytes`` /
      ``get_group_usage.io_write_bytes`` are always ``None``;
      ``Get-Process`` does not surface per-PID I/O bytes in a
      portable shape.

    File I/O policy:
        Every read / write / probe is dispatched through the
        injected :class:`ICommandRunner` as PowerShell scripts
        (``Get-Content``, ``Set-Content``, ``Test-Path``,
        ``Get-ChildItem``, ``Remove-Item``,
        ``New-Item -ItemType Directory``).  This module does NOT
        consume an :class:`IFileOperator`.

    Elevation:
        Writes honour ``config.elevated``.  Internal probes
        (``Get-Content``, ``Test-Path``, ``Get-ChildItem``) and
        the dedicated read verbs (``list_groups``,
        ``get_group_usage``) NEVER elevate.
"""

import json
import os
import re

from dataclasses import dataclass, field
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

_DEFAULT_GROUP_DIR = r"C:\ProgramData\CodeCraftAI\rcgroups"

_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# Priority-class values exposed by ``System.Diagnostics.ProcessPriorityClass``.
_PRIORITY_IDLE = "Idle"
_PRIORITY_BELOW_NORMAL = "BelowNormal"
_PRIORITY_NORMAL = "Normal"
_PRIORITY_ABOVE_NORMAL = "AboveNormal"
_PRIORITY_HIGH = "High"

_DETAIL_NO_MEMORY_LIMIT = "windows_no_powershell_job_object_memory_limit"
_DETAIL_NO_IO_LIMIT = "windows_no_powershell_io_limit"


WindowsResourceControlActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsResourceControlManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`WindowsResourceControlManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.
        :param group_dir: Directory holding the manager's group
                          JSON specs.  Defaults to
                          ``"C:\\ProgramData\\CodeCraftAI\\rcgroups"``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    group_dir: str = _DEFAULT_GROUP_DIR


@dataclass
class WindowsResourceControlActionResult(FeatureActionResult):
    """
        Result of a resource-control write operation.

        :param group_name: Group name acted upon.
        :param pid: Process ID acted upon (only populated by
                    :meth:`WindowsResourceControlManager.attach_process`
                    and :meth:`detach_process`).
        :param message: Optional short reason token for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    group_name: Optional[str] = None
    pid: Optional[int] = None
    message: Optional[str] = None


@dataclass
class WindowsIoLimitSpec:
    """
        IO-limit specification carried by
        :meth:`WindowsResourceControlManager.set_io_limits`.

        Every field is recorded for cross-OS API parity;
        Windows returns ``FAILED`` with
        ``detail="windows_no_powershell_io_limit"``.

        :param weight: Cross-OS IO weight; ignored.
        :param read_bps: Cross-OS read-bps cap; ignored.
        :param write_bps: Cross-OS write-bps cap; ignored.
    """
    weight: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None


def _empty_pid_list() -> list[int]:
    """
        Default-factory for
        :class:`WindowsResourceGroupSpec.attached_pids`.

        :returns: A fresh empty list per call.
    """
    rtnval: list[int] = []
    return rtnval


@dataclass
class WindowsResourceGroupSpec:
    """
        Specification used by
        :meth:`WindowsResourceControlManager.create_group`.

        :param name: Group identifier.  Must match
                     ``^[A-Za-z0-9][A-Za-z0-9._\\-]*$``.
        :param cpu_percent: Coarse percent value mapped to
                            ``ProcessPriorityClass``.  See the
                            design note for the mapping.
                            ``None`` leaves the priority class
                            at ``Normal`` for newly attached
                            PIDs.
        :param memory_bytes: Recorded for cross-OS API parity;
                             ignored on Windows (PowerShell has
                             no Job Object memory cmdlet).
        :param io_weight: Recorded for parity; ignored.
        :param io_read_bps: Recorded for parity; ignored.
        :param io_write_bps: Recorded for parity; ignored.
        :param attached_pids: PIDs whose ``PriorityClass`` has
                              been (or will be) adjusted.
    """
    name: str
    cpu_percent: Optional[int] = None
    memory_bytes: Optional[int] = None
    io_weight: Optional[int] = None
    io_read_bps: Optional[int] = None
    io_write_bps: Optional[int] = None
    attached_pids: list[int] = field(default_factory=_empty_pid_list)


@dataclass
class WindowsResourceGroupInfo:
    """
        Current state of a single resource group.

        :param name: Group name.
        :param cpu_percent: Recorded percent from the spec.
        :param memory_bytes: Recorded bytes from the spec.
        :param io_weight: Recorded weight from the spec.
        :param io_read_bps: Recorded read-bps from the spec.
        :param io_write_bps: Recorded write-bps from the spec.
        :param attached_pids: PIDs from the spec.
        :param is_active: ``True`` when the spec file is present.
        :param effective_cpu_percent: Same as ``cpu_percent``.
        :param effective_memory_bytes: Same as ``memory_bytes``.
    """
    name: str
    cpu_percent: Optional[int]
    memory_bytes: Optional[int]
    io_weight: Optional[int]
    io_read_bps: Optional[int]
    io_write_bps: Optional[int]
    attached_pids: list[int]
    is_active: bool
    effective_cpu_percent: Optional[int]
    effective_memory_bytes: Optional[int]


@dataclass
class WindowsResourceGroupUsage:
    """
        Live usage snapshot for a single resource group.

        :param name: Group name.
        :param cpu_usage_seconds: Sum of per-PID ``CPU``
                                  cumulative seconds, or
                                  ``None`` when no PID was
                                  attached.
        :param memory_used_bytes: Sum of per-PID
                                  ``WorkingSet64`` values, or
                                  ``None`` when no PID was
                                  attached.
        :param io_read_bytes: Always ``None`` on Windows.
        :param io_write_bytes: Always ``None`` on Windows.
        :param process_count: Number of attached PIDs.
    """
    name: str
    cpu_usage_seconds: Optional[float]
    memory_used_bytes: Optional[int]
    io_read_bytes: Optional[int]
    io_write_bytes: Optional[int]
    process_count: int


class WindowsResourceControlQueryError(Exception):
    """
        Raised when a resource-control read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
        detail: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param detail: Optional short reason token.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr
        self.detail = detail

        err_msg_lines = ["Resource-control query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        if detail is not None:
            err_msg_lines.append("    DETAIL: {}".format(detail))

        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsResourceControlManager(BaseFeatureManager):
    """
        Windows logical-group resource-control manager.  Every
        verb is dispatched through the injected
        :class:`ICommandRunner` as a PowerShell scriptblock.
        See module docstring for the documented Windows
        limitations.
    """

    result_class = WindowsResourceControlActionResult
    status_class = WindowsResourceControlActionStatus
    feature_name = "resource-control-management"

    def __init__(
        self,
        runner,
        config: Optional[WindowsResourceControlManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           mutating operation.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`WindowsResourceControlManagerConfig`
                           when omitted.  This override (vs the
                           base ``BaseFeatureManager.__init__``)
                           is required so the verb implementations
                           can rely on
                           ``self._config.powershell_executable``
                           and ``self._config.group_dir`` always
                           being present.
        """
        if config is None:
            config = WindowsResourceControlManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_group(
        self,
        spec: WindowsResourceGroupSpec,
    ) -> WindowsResourceControlActionResult:
        """
            Create or update a resource group.

            Renders the JSON spec document and writes it to
            ``<group_dir>\\<name>.json`` via PowerShell
            ``Set-Content``.

            Idempotent: when the on-disk JSON spec matches the
            rendered document byte-for-byte, returns
            ``SKIPPED``.

            :param spec: The :class:`WindowsResourceGroupSpec`.

            :returns: The
                      :class:`WindowsResourceControlActionResult`.
        """
        try:
            _validate_name(name=spec.name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=spec.name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        spec_path = self._spec_path(name=spec.name)
        rendered_json = _render_spec_json(spec=spec)

        write_argv = self._set_content_argv(
            path=spec_path, body=rendered_json,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=spec.name,
                command=write_argv,
            )
            return dry

        existing = self._read_spec_text(name=spec.name)
        if existing is not None and existing == rendered_json:
            skipped = self._build_result(
                status=WindowsResourceControlActionStatus.SKIPPED,
                group_name=spec.name,
                command=write_argv,
                message="group spec already matches",
            )
            return skipped

        # Ensure the group directory exists.
        ensure_run = self._ensure_group_dir()
        if ensure_run.exit_code != 0:
            failed = WindowsResourceControlActionResult(
                status=WindowsResourceControlActionStatus.FAILED,
                detail=spec.name,
                exit_code=ensure_run.exit_code,
                stdout=ensure_run.stdout,
                stderr=ensure_run.stderr,
                command=list(self._ensure_group_dir_argv()),
                group_name=spec.name,
                message="group_dir_create_failed",
            )
            return failed

        # Write the spec file.
        write_run = self._runner.run(
            list(write_argv), elevated=self._config.elevated,
        )
        if write_run.exit_code == 0:
            mapped_status = WindowsResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = WindowsResourceControlActionStatus.FAILED
            message = "spec_write_failed"

        rtnval = WindowsResourceControlActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=write_run.exit_code,
            stdout=write_run.stdout,
            stderr=write_run.stderr,
            command=list(write_argv),
            group_name=spec.name,
            message=message,
        )
        return rtnval

    def delete_group(
        self,
        name: str,
    ) -> WindowsResourceControlActionResult:
        """
            Delete a resource group.

            Dispatches PowerShell ``Remove-Item -LiteralPath
            <spec_path> -Force``.

            Idempotent: when the spec file does not exist,
            returns ``SKIPPED`` without invoking the runner.

            :param name: Group name.

            :returns: The
                      :class:`WindowsResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        spec_path = self._spec_path(name=name)
        escaped = _ps_escape_single_quoted(value=spec_path)
        scriptblock = (
            "Remove-Item -LiteralPath '{}' -Force"
        ).format(escaped)
        delete_argv = self._ps_command_argv(scriptblock=scriptblock)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=delete_argv,
            )
            return dry

        if not self._spec_exists(name=name):
            skipped = self._build_result(
                status=WindowsResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=delete_argv,
                message="group spec not found",
            )
            return skipped

        delete_run = self._runner.run(
            list(delete_argv), elevated=self._config.elevated,
        )

        if delete_run.exit_code == 0:
            mapped_status = WindowsResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = WindowsResourceControlActionStatus.FAILED
            message = "spec_delete_failed"

        rtnval = WindowsResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=delete_run.exit_code,
            stdout=delete_run.stdout,
            stderr=delete_run.stderr,
            command=list(delete_argv),
            group_name=name,
            message=message,
        )
        return rtnval

    def set_cpu_limit(
        self,
        name: str,
        percent: int,
    ) -> WindowsResourceControlActionResult:
        """
            Set the group's CPU "limit" by mapping ``percent``
            to a ``ProcessPriorityClass`` and applying it to
            every attached PID.

            macOS / Ubuntu callers expecting a percent ceiling
            should be aware that Windows has NO native CPU
            percent throttle for arbitrary process groups; the
            mapping is a coarse approximation.

            Idempotent: when the existing
            ``cpu_percent`` field on the spec maps to the same
            priority class, returns ``SKIPPED``.

            :param name: Group name.
            :param percent: Coarse percent value.

            :returns: The
                      :class:`WindowsResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        new_class = _percent_to_priority_class(percent=percent)
        scriptblock = (
            "(Get-Process -Id $pid).PriorityClass = '{}'"
        ).format(new_class)
        documented_argv = self._ps_command_argv(scriptblock=scriptblock)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=documented_argv,
            )
            return dry

        existing_spec = self._read_spec(name=name)
        if existing_spec is None:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                command=documented_argv,
                message="group_not_found",
                stderr="group spec not found: {}".format(self._spec_path(name=name)),
            )
            return failed

        existing_class = _percent_to_priority_class(
            percent=existing_spec.cpu_percent,
        )
        if existing_class == new_class:
            skipped = self._build_result(
                status=WindowsResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=documented_argv,
                message="priority class already at requested mapping",
            )
            return skipped

        # Persist the new percent into the spec.
        new_spec = WindowsResourceGroupSpec(
            name=name,
            cpu_percent=percent,
            memory_bytes=existing_spec.memory_bytes,
            io_weight=existing_spec.io_weight,
            io_read_bps=existing_spec.io_read_bps,
            io_write_bps=existing_spec.io_write_bps,
            attached_pids=list(existing_spec.attached_pids),
        )
        spec_write_result = self._write_spec(spec=new_spec)
        if spec_write_result.status != WindowsResourceControlActionStatus.SUCCESS:
            return spec_write_result

        # Apply to each attached PID.
        last_run_result = None
        last_argv = documented_argv
        for pid in new_spec.attached_pids:
            apply_argv = self._ps_command_argv(
                scriptblock=(
                    "(Get-Process -Id {pid}).PriorityClass = '{cls}'"
                ).format(pid=pid, cls=new_class),
            )
            apply_run = self._runner.run(
                list(apply_argv), elevated=self._config.elevated,
            )
            last_run_result = apply_run
            last_argv = apply_argv
            if apply_run.exit_code != 0:
                failed = WindowsResourceControlActionResult(
                    status=WindowsResourceControlActionStatus.FAILED,
                    detail=name,
                    exit_code=apply_run.exit_code,
                    stdout=apply_run.stdout,
                    stderr=apply_run.stderr,
                    command=list(apply_argv),
                    group_name=name,
                    pid=pid,
                    message="priority_class_apply_failed",
                )
                return failed

        if last_run_result is None:
            # No attached PIDs — the spec write was the only
            # mutation.  Echo the spec-write result.
            rtnval = WindowsResourceControlActionResult(
                status=WindowsResourceControlActionStatus.SUCCESS,
                detail=name,
                exit_code=spec_write_result.exit_code,
                stdout=spec_write_result.stdout,
                stderr=spec_write_result.stderr,
                command=spec_write_result.command,
                group_name=name,
                message=None,
            )
            return rtnval

        rtnval = WindowsResourceControlActionResult(
            status=WindowsResourceControlActionStatus.SUCCESS,
            detail=name,
            exit_code=last_run_result.exit_code,
            stdout=last_run_result.stdout,
            stderr=last_run_result.stderr,
            command=list(last_argv),
            group_name=name,
        )
        return rtnval

    def set_memory_limit(
        self,
        name: str,
        bytes_: int,
    ) -> WindowsResourceControlActionResult:
        """
            Set the group's memory ceiling.

            Windows LIMITATION: PowerShell exposes no clean Job
            Object memory limit cmdlet.  This method always
            returns ``FAILED`` with
            ``detail="windows_no_powershell_job_object_memory_limit"``
            and does NOT invoke the runner.

            :param name: Group name.
            :param bytes_: Ignored (recorded on the result for
                           audit).

            :returns: The
                      :class:`WindowsResourceControlActionResult`
                      with ``status=FAILED``.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        documented_argv = self._ps_command_argv(
            scriptblock="(Get-Process -Id $pid).WorkingSet64",
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=documented_argv,
            )
            return dry

        failed = WindowsResourceControlActionResult(
            status=WindowsResourceControlActionStatus.FAILED,
            detail=_DETAIL_NO_MEMORY_LIMIT,
            exit_code=None,
            stdout=None,
            stderr=(
                "PowerShell exposes no clean Job Object memory "
                "limit cmdlet; a future P/Invoke companion is "
                "the documented follow-up."
            ),
            command=list(documented_argv),
            group_name=name,
            message="PowerShell exposes no clean Job Object memory limit cmdlet",
        )
        return failed

    def set_io_limits(
        self,
        name: str,
        spec: WindowsIoLimitSpec,
    ) -> WindowsResourceControlActionResult:
        """
            Set per-group IO ceilings.

            Windows LIMITATION: PowerShell exposes no
            per-process IO bandwidth limit.  This method always
            returns ``FAILED`` with
            ``detail="windows_no_powershell_io_limit"`` and
            does NOT invoke the runner.

            :param name: Group name.
            :param spec: Ignored.

            :returns: The
                      :class:`WindowsResourceControlActionResult`
                      with ``status=FAILED``.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        documented_argv = self._ps_command_argv(
            scriptblock="Set-IoPriority",
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=documented_argv,
            )
            return dry

        failed = WindowsResourceControlActionResult(
            status=WindowsResourceControlActionStatus.FAILED,
            detail=_DETAIL_NO_IO_LIMIT,
            exit_code=None,
            stdout=None,
            stderr=(
                "PowerShell exposes no per-process IO "
                "bandwidth limit; a future P/Invoke companion "
                "is the documented follow-up."
            ),
            command=list(documented_argv),
            group_name=name,
            message="PowerShell exposes no per-process IO bandwidth limit",
        )
        return failed

    def attach_process(
        self,
        name: str,
        pid: int,
    ) -> WindowsResourceControlActionResult:
        """
            Attach an existing PID to the resource group.

            Records the PID in the spec's ``attached_pids``
            array, re-writes the spec via ``Set-Content``, then
            applies the spec's recorded ``PriorityClass`` to
            the running process via
            ``(Get-Process -Id <pid>).PriorityClass = '<class>'``.

            Idempotent: when the PID is already in the spec's
            ``attached_pids``, returns ``SKIPPED``.

            :param name: Group name.
            :param pid: Positive process ID.

            :returns: The
                      :class:`WindowsResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
            _validate_pid(pid=pid)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=[],
                message="invalid_attach_arguments",
                stderr=str(exc),
            )
            return failed

        existing_class = _PRIORITY_NORMAL
        existing_spec = self._read_spec(name=name)
        if existing_spec is not None:
            existing_class = _percent_to_priority_class(
                percent=existing_spec.cpu_percent,
            )

        apply_argv = self._ps_command_argv(
            scriptblock=(
                "(Get-Process -Id {pid}).PriorityClass = '{cls}'"
            ).format(pid=pid, cls=existing_class),
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=name,
                pid=pid,
                command=apply_argv,
            )
            return dry

        if existing_spec is None:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=apply_argv,
                message="group_not_found",
                stderr="group spec not found: {}".format(self._spec_path(name=name)),
            )
            return failed

        if pid in existing_spec.attached_pids:
            skipped = self._build_result(
                status=WindowsResourceControlActionStatus.SKIPPED,
                group_name=name,
                pid=pid,
                command=apply_argv,
                message="pid already attached",
            )
            return skipped

        new_pids = list(existing_spec.attached_pids) + [pid]
        new_spec = WindowsResourceGroupSpec(
            name=name,
            cpu_percent=existing_spec.cpu_percent,
            memory_bytes=existing_spec.memory_bytes,
            io_weight=existing_spec.io_weight,
            io_read_bps=existing_spec.io_read_bps,
            io_write_bps=existing_spec.io_write_bps,
            attached_pids=new_pids,
        )
        spec_write_result = self._write_spec(spec=new_spec)
        if spec_write_result.status != WindowsResourceControlActionStatus.SUCCESS:
            return spec_write_result

        apply_run = self._runner.run(
            list(apply_argv), elevated=self._config.elevated,
        )
        if apply_run.exit_code == 0:
            mapped_status = WindowsResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = WindowsResourceControlActionStatus.FAILED
            message = "priority_class_apply_failed"

        rtnval = WindowsResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=apply_run.exit_code,
            stdout=apply_run.stdout,
            stderr=apply_run.stderr,
            command=list(apply_argv),
            group_name=name,
            pid=pid,
            message=message,
        )
        return rtnval

    def detach_process(
        self,
        name: str,
        pid: int,
    ) -> WindowsResourceControlActionResult:
        """
            Detach a PID from the resource group.

            Removes the PID from the spec's ``attached_pids``,
            re-writes the spec, then resets the PID's
            ``PriorityClass`` to ``Normal``.

            Idempotent: when the PID is NOT in the spec's
            ``attached_pids``, returns ``SKIPPED``.

            :param name: Group name.
            :param pid: Positive process ID.

            :returns: The
                      :class:`WindowsResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
            _validate_pid(pid=pid)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=[],
                message="invalid_detach_arguments",
                stderr=str(exc),
            )
            return failed

        reset_argv = self._ps_command_argv(
            scriptblock=(
                "(Get-Process -Id {pid}).PriorityClass = 'Normal'"
            ).format(pid=pid),
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsResourceControlActionStatus.DRY_RUN,
                group_name=name,
                pid=pid,
                command=reset_argv,
            )
            return dry

        existing_spec = self._read_spec(name=name)
        if existing_spec is None:
            failed = self._build_result(
                status=WindowsResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=reset_argv,
                message="group_not_found",
                stderr="group spec not found: {}".format(self._spec_path(name=name)),
            )
            return failed

        if pid not in existing_spec.attached_pids:
            skipped = self._build_result(
                status=WindowsResourceControlActionStatus.SKIPPED,
                group_name=name,
                pid=pid,
                command=reset_argv,
                message="pid not currently attached",
            )
            return skipped

        new_pids = [p for p in existing_spec.attached_pids if p != pid]
        new_spec = WindowsResourceGroupSpec(
            name=name,
            cpu_percent=existing_spec.cpu_percent,
            memory_bytes=existing_spec.memory_bytes,
            io_weight=existing_spec.io_weight,
            io_read_bps=existing_spec.io_read_bps,
            io_write_bps=existing_spec.io_write_bps,
            attached_pids=new_pids,
        )
        spec_write_result = self._write_spec(spec=new_spec)
        if spec_write_result.status != WindowsResourceControlActionStatus.SUCCESS:
            return spec_write_result

        reset_run = self._runner.run(
            list(reset_argv), elevated=self._config.elevated,
        )
        if reset_run.exit_code == 0:
            mapped_status = WindowsResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = WindowsResourceControlActionStatus.FAILED
            message = "priority_class_apply_failed"

        rtnval = WindowsResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=reset_run.exit_code,
            stdout=reset_run.stdout,
            stderr=reset_run.stderr,
            command=list(reset_argv),
            group_name=name,
            pid=pid,
            message=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_groups(self) -> list[WindowsResourceGroupInfo]:
        """
            Enumerate every resource group present under the
            group directory.

            Invokes PowerShell ``Get-ChildItem -LiteralPath
            <group_dir> -Filter '*.json' | Select-Object
            -ExpandProperty Name | ConvertTo-Json -Compress``
            and reads each spec file.  Reads do not elevate.

            :returns: One
                      :class:`WindowsResourceGroupInfo` per
                      detected spec.  Empty list when no spec
                      file is present.
            :raises WindowsResourceControlQueryError: When the
                                                      enumeration
                                                      command
                                                      fails with
                                                      a non-empty
                                                      stderr.
        """
        escaped_dir = _ps_escape_single_quoted(value=self._config.group_dir)
        scriptblock = (
            "if (Test-Path -LiteralPath '{dir}') {{ "
            "Get-ChildItem -LiteralPath '{dir}' -Filter '*.json' | "
            "Select-Object -ExpandProperty Name | "
            "ConvertTo-Json -Compress }} else {{ '[]' }}"
        ).format(dir=escaped_dir)
        list_argv = self._ps_command_argv(scriptblock=scriptblock)

        list_run = self._runner.run(list(list_argv), elevated=False)
        if list_run.exit_code != 0 and len(list_run.stderr.strip()) > 0:
            raise WindowsResourceControlQueryError(
                command=list_argv,
                exit_code=list_run.exit_code,
                stderr=list_run.stderr,
            )

        names = _parse_filename_list(text=list_run.stdout)

        rtnval: list[WindowsResourceGroupInfo] = []
        for filename in names:
            if not filename.endswith(".json"):
                continue
            stem = filename[: -len(".json")]
            spec = self._read_spec(name=stem)
            if spec is None:
                continue

            info = WindowsResourceGroupInfo(
                name=spec.name,
                cpu_percent=spec.cpu_percent,
                memory_bytes=spec.memory_bytes,
                io_weight=spec.io_weight,
                io_read_bps=spec.io_read_bps,
                io_write_bps=spec.io_write_bps,
                attached_pids=list(spec.attached_pids),
                is_active=True,
                effective_cpu_percent=spec.cpu_percent,
                effective_memory_bytes=spec.memory_bytes,
            )
            rtnval.append(info)

        return rtnval

    def get_group_usage(
        self,
        name: str,
    ) -> WindowsResourceGroupUsage:
        """
            Read live usage counters for a single resource
            group.

            Aggregates ``Get-Process -Id <pid> | Select-Object
            WorkingSet64,CPU | ConvertTo-Json -Compress`` across
            every attached PID.  Reads do not elevate.

            :param name: Group name.

            :returns: The
                      :class:`WindowsResourceGroupUsage`.
            :raises WindowsResourceControlQueryError: When the
                                                      spec file
                                                      does not
                                                      exist
                                                      (raised
                                                      with
                                                      ``detail="unknown_group"``).
        """
        _validate_name(name=name)

        spec = self._read_spec(name=name)
        if spec is None:
            raise WindowsResourceControlQueryError(
                command=self._read_spec_argv(name=name),
                exit_code=1,
                stderr="group spec not found: {}".format(self._spec_path(name=name)),
                detail="unknown_group",
            )

        if len(spec.attached_pids) == 0:
            rtnval = WindowsResourceGroupUsage(
                name=name,
                cpu_usage_seconds=None,
                memory_used_bytes=None,
                io_read_bytes=None,
                io_write_bytes=None,
                process_count=0,
            )
            return rtnval

        total_memory = 0
        memory_seen = False
        total_cpu = 0.0
        cpu_seen = False

        for pid in spec.attached_pids:
            scriptblock = (
                "Get-Process -Id {pid} | Select-Object "
                "WorkingSet64,CPU | ConvertTo-Json -Compress"
            ).format(pid=pid)
            argv = self._ps_command_argv(scriptblock=scriptblock)
            run_result = self._runner.run(list(argv), elevated=False)
            if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
                raise WindowsResourceControlQueryError(
                    command=argv,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            mem, cpu = _parse_get_process_output(text=run_result.stdout)
            if mem is not None:
                total_memory = total_memory + mem
                memory_seen = True
            if cpu is not None:
                total_cpu = total_cpu + cpu
                cpu_seen = True

        if memory_seen:
            memory_used: Optional[int] = total_memory
        else:
            memory_used = None
        if cpu_seen:
            cpu_used: Optional[float] = total_cpu
        else:
            cpu_used = None

        rtnval = WindowsResourceGroupUsage(
            name=name,
            cpu_usage_seconds=cpu_used,
            memory_used_bytes=memory_used,
            io_read_bytes=None,
            io_write_bytes=None,
            process_count=len(spec.attached_pids),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spec_path(self, *, name: str) -> str:
        """
            :returns: ``<group_dir>\\<name>.json``.
        """
        rtnval = "{}\\{}.json".format(self._config.group_dir, name)
        return rtnval

    def _ps_command_argv(self, *, scriptblock: str) -> list[str]:
        """
            Wrap ``scriptblock`` in the standard PowerShell
            invocation argv.

            :param scriptblock: The PowerShell scriptblock body.

            :returns: A fresh list ``[powershell, -NoProfile,
                      -NonInteractive, -Command, scriptblock]``.
        """
        rtnval = [
            self._config.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            scriptblock,
        ]
        return rtnval

    def _set_content_argv(self, *, path: str, body: str) -> list[str]:
        """
            Build the PowerShell argv that writes ``body`` to
            ``path`` via ``Set-Content``.

            :param path: Absolute spec file path.
            :param body: JSON spec body.

            :returns: A fresh PowerShell argv.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        escaped_body = _ps_escape_single_quoted(value=body)
        scriptblock = (
            "Set-Content -LiteralPath '{path}' -Value '{body}' "
            "-Encoding UTF8"
        ).format(path=escaped_path, body=escaped_body)
        rtnval = self._ps_command_argv(scriptblock=scriptblock)
        return rtnval

    def _ensure_group_dir_argv(self) -> list[str]:
        """
            Build the PowerShell argv that ensures the group
            directory exists.

            :returns: A fresh PowerShell argv.
        """
        escaped_dir = _ps_escape_single_quoted(value=self._config.group_dir)
        scriptblock = (
            "New-Item -ItemType Directory -Path '{dir}' -Force | "
            "Out-Null"
        ).format(dir=escaped_dir)
        rtnval = self._ps_command_argv(scriptblock=scriptblock)
        return rtnval

    def _ensure_group_dir(self):
        """
            Dispatch the directory-create scriptblock.

            :returns: The runner :class:`RunResult`.
        """
        argv = self._ensure_group_dir_argv()
        rtnval = self._runner.run(
            list(argv), elevated=self._config.elevated,
        )
        return rtnval

    def _read_spec_argv(self, *, name: str) -> list[str]:
        """
            Build the PowerShell argv that reads the spec text.

            :param name: Group name.

            :returns: A fresh PowerShell argv.
        """
        spec_path = self._spec_path(name=name)
        escaped_path = _ps_escape_single_quoted(value=spec_path)
        scriptblock = (
            "if (Test-Path -LiteralPath '{path}') {{ "
            "Get-Content -LiteralPath '{path}' -Raw }}"
        ).format(path=escaped_path)
        rtnval = self._ps_command_argv(scriptblock=scriptblock)
        return rtnval

    def _read_spec_text(self, *, name: str) -> Optional[str]:
        """
            Read the raw spec text via PowerShell ``Get-Content
            -Raw``.  Reads do not elevate.

            :param name: Group name.

            :returns: Raw JSON text on success; ``None`` when
                      the file is missing or returns empty.
        """
        argv = self._read_spec_argv(name=name)
        run_result = self._runner.run(list(argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        text = run_result.stdout
        if text is None or len(text.strip()) == 0:
            rtnval = None
            return rtnval
        rtnval = text
        return rtnval

    def _read_spec(self, *, name: str) -> Optional[WindowsResourceGroupSpec]:
        """
            Read and parse the spec file.

            :param name: Group name.

            :returns: The parsed spec, or ``None`` when the
                      file is missing or the body is
                      unparseable.
        """
        text = self._read_spec_text(name=name)
        if text is None:
            rtnval: Optional[WindowsResourceGroupSpec] = None
            return rtnval
        rtnval = _parse_spec_json(text=text)
        return rtnval

    def _spec_exists(self, *, name: str) -> bool:
        """
            Probe spec-file existence via PowerShell ``Test-Path``.

            :param name: Group name.

            :returns: True when ``Test-Path`` reports ``True``.
        """
        spec_path = self._spec_path(name=name)
        escaped = _ps_escape_single_quoted(value=spec_path)
        scriptblock = (
            "if (Test-Path -LiteralPath '{path}') {{ "
            "'TRUE' }} else {{ 'FALSE' }}"
        ).format(path=escaped)
        argv = self._ps_command_argv(scriptblock=scriptblock)
        run_result = self._runner.run(list(argv), elevated=False)
        rtnval = run_result.stdout.strip() == "TRUE"
        return rtnval

    def _write_spec(
        self,
        *,
        spec: WindowsResourceGroupSpec,
    ) -> WindowsResourceControlActionResult:
        """
            Render and write the spec via PowerShell
            ``Set-Content``.

            :param spec: The spec to persist.

            :returns: The
                      :class:`WindowsResourceControlActionResult`.
        """
        spec_path = self._spec_path(name=spec.name)
        body = _render_spec_json(spec=spec)
        argv = self._set_content_argv(path=spec_path, body=body)
        run_result = self._runner.run(
            list(argv), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = WindowsResourceControlActionStatus.FAILED
            message = "spec_write_failed"

        rtnval = WindowsResourceControlActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            group_name=spec.name,
            message=message,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsResourceControlActionStatus,
        group_name: Optional[str],
        command: list[str],
        pid: Optional[int] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsResourceControlActionResult:
        """
            Construct a
            :class:`WindowsResourceControlActionResult` without
            invoking the runner.
        """
        rtnval = WindowsResourceControlActionResult(
            status=status,
            detail=group_name,
            command=list(command),
            group_name=group_name,
            pid=pid,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _validate_name(*, name: str) -> None:
    """
        Raise ``ValueError`` when ``name`` does not match the
        allowed character set.

        :param name: Candidate group name.
        :raises ValueError: When ``name`` does not match
                            ``[A-Za-z0-9][A-Za-z0-9._\\-]*``.
    """
    if name is None:
        err_msg = "group name must not be None"
        raise ValueError(err_msg)
    if _VALID_NAME.match(name) is None:
        err_msg = "invalid group name: {!r}".format(name)
        raise ValueError(err_msg)
    return


def _validate_pid(*, pid: int) -> None:
    """
        Raise ``ValueError`` when ``pid`` is not a positive
        integer.

        :param pid: Candidate PID.
        :raises ValueError: When ``pid`` is None or
                            non-positive.
    """
    if pid is None:
        err_msg = "pid must not be None"
        raise ValueError(err_msg)
    if pid <= 0:
        err_msg = "pid must be a positive integer: {}".format(pid)
        raise ValueError(err_msg)
    return


def _percent_to_priority_class(*, percent: Optional[int]) -> str:
    """
        Map a coarse percent value to a
        ``ProcessPriorityClass`` value.

        :param percent: Coarse percent value, or ``None`` (mapped
                        to ``Normal``).

        :returns: One of ``"Idle"``, ``"BelowNormal"``,
                  ``"Normal"``, ``"AboveNormal"``, ``"High"``.
    """
    if percent is None:
        rtnval = _PRIORITY_NORMAL
        return rtnval
    if percent <= 25:
        rtnval = _PRIORITY_IDLE
        return rtnval
    if percent <= 50:
        rtnval = _PRIORITY_BELOW_NORMAL
        return rtnval
    if percent <= 75:
        rtnval = _PRIORITY_NORMAL
        return rtnval
    if percent < 100:
        rtnval = _PRIORITY_ABOVE_NORMAL
        return rtnval
    rtnval = _PRIORITY_HIGH
    return rtnval


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single
        quotes.

        :param value: Raw text.

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _render_spec_json(*, spec: WindowsResourceGroupSpec) -> str:
    """
        Render the JSON document for a spec.

        Uses ``json.dumps`` with ``sort_keys=True`` and
        ``indent=2`` so the output is byte-stable for
        idempotency comparisons.

        :param spec: The spec to render.

        :returns: A JSON document with a trailing newline.
    """
    payload = {
        "name": spec.name,
        "cpu_percent": spec.cpu_percent,
        "memory_bytes": spec.memory_bytes,
        "io_weight": spec.io_weight,
        "io_read_bps": spec.io_read_bps,
        "io_write_bps": spec.io_write_bps,
        "attached_pids": list(spec.attached_pids),
    }
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return body


def _parse_spec_json(*, text: str) -> Optional[WindowsResourceGroupSpec]:
    """
        Parse a spec JSON document into a
        :class:`WindowsResourceGroupSpec`.

        :param text: Raw JSON document body.

        :returns: The parsed spec, or ``None`` when ``text`` is
                  not parseable JSON or lacks ``name``.
    """
    if text is None:
        rtnval: Optional[WindowsResourceGroupSpec] = None
        return rtnval
    candidate = text.strip()
    if len(candidate) == 0:
        rtnval = None
        return rtnval

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        rtnval = None
        return rtnval

    if not isinstance(parsed, dict):
        rtnval = None
        return rtnval

    name = parsed.get("name")
    if name is None or not isinstance(name, str):
        rtnval = None
        return rtnval

    attached_raw = parsed.get("attached_pids", [])
    if not isinstance(attached_raw, list):
        attached_raw = []
    attached_pids: list[int] = []
    for entry in attached_raw:
        try:
            attached_pids.append(int(entry))
        except (TypeError, ValueError):
            continue

    rtnval = WindowsResourceGroupSpec(
        name=name,
        cpu_percent=parsed.get("cpu_percent"),
        memory_bytes=parsed.get("memory_bytes"),
        io_weight=parsed.get("io_weight"),
        io_read_bps=parsed.get("io_read_bps"),
        io_write_bps=parsed.get("io_write_bps"),
        attached_pids=attached_pids,
    )
    return rtnval


def _parse_filename_list(*, text: str) -> list[str]:
    """
        Parse the ``ConvertTo-Json -Compress`` output of
        ``Select-Object -ExpandProperty Name``.

        Single-element shape may render as a bare string; the
        helper normalises to a list.

        :param text: Raw stdout text.

        :returns: List of filenames.  Empty when unparseable
                  or empty.
    """
    if text is None:
        rtnval: list[str] = []
        return rtnval
    candidate = text.strip()
    if len(candidate) == 0:
        rtnval = []
        return rtnval

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        rtnval = []
        return rtnval

    if isinstance(parsed, list):
        rtnval = []
        for entry in parsed:
            if isinstance(entry, str):
                rtnval.append(entry)
        return rtnval
    if isinstance(parsed, str):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _parse_get_process_output(*, text: str) -> tuple:
    """
        Parse the JSON shape ``{"WorkingSet64": <int>,
        "CPU": <float>}`` returned by ``Get-Process | Select
        WorkingSet64,CPU | ConvertTo-Json -Compress``.

        :param text: Raw stdout text.

        :returns: ``(memory_bytes, cpu_seconds)``; either
                  element may be ``None`` when the field was
                  absent or unparseable.
    """
    if text is None:
        rtnval = (None, None)
        return rtnval
    candidate = text.strip()
    if len(candidate) == 0:
        rtnval = (None, None)
        return rtnval

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        rtnval = (None, None)
        return rtnval

    if not isinstance(parsed, dict):
        rtnval = (None, None)
        return rtnval

    mem_raw = parsed.get("WorkingSet64")
    cpu_raw = parsed.get("CPU")

    try:
        mem_value: Optional[int] = int(mem_raw)
    except (TypeError, ValueError):
        mem_value = None
    try:
        cpu_value: Optional[float] = float(cpu_raw)
    except (TypeError, ValueError):
        cpu_value = None

    rtnval = (mem_value, cpu_value)
    return rtnval

"""
    Windows virtualization-management feature.

    Implements the cross-OS virtualization-management contract for
    Windows using the Hyper-V PowerShell module (``Get-VM``,
    ``New-VM``, ``Start-VM``, ``Stop-VM``, ``Suspend-VM``,
    ``Resume-VM``, ``Remove-VM``, ``Add-VMHardDiskDrive``,
    ``Remove-VMHardDiskDrive``, ``Checkpoint-VM``,
    ``Restore-VMSnapshot``, ``Get-VMSnapshot``).  Every operation is
    dispatched via the injected :class:`ICommandRunner`; this module
    never constructs its own subprocess.

    Required runtime:
        The Hyper-V Windows feature must be enabled (``Enable-
        WindowsOptionalFeature -Online -FeatureName
        Microsoft-Hyper-V``) and the calling user must be a member of
        the local ``Hyper-V Administrators`` group OR the manager must
        be configured with ``elevated=True`` (UAC + Administrator
        token).  WSL2 is NOT a substitute — it does not satisfy this
        contract.

    Idempotency:
        ``create_vm`` skips when ``Get-VM -Name <name>`` already
        reports the VM with matching memory / vCPU.  ``delete_vm``
        skips when the VM does not exist.  ``start_vm`` /
        ``stop_vm`` / ``pause_vm`` / ``resume_vm`` skip when the
        ``State`` field already matches the requested transition.
        ``attach_disk`` skips when ``Get-VMHardDiskDrive -VMName <vm>``
        already reports a drive whose ``Path`` matches the spec.
        ``detach_disk`` skips when no such drive is attached.
        ``snapshot_vm`` skips when a snapshot with the same name
        already exists; ``restore_snapshot`` is unconditional.

    Stop semantics:
        ``stop_vm(force=False)`` issues ``Stop-VM -Name <name>`` (the
        default — graceful shutdown via the integration services).
        ``stop_vm(force=True)`` issues
        ``Stop-VM -Name <name> -Force -TurnOff`` (a hard power-off,
        equivalent to pulling the cord).

    Elevation policy:
        Every write verb honours :attr:`FeatureConfig.elevated`.  Read
        verbs and idempotency probes never elevate.  Hyper-V cmdlets
        typically require either the Administrator token or
        ``Hyper-V Administrators`` membership; both situations are
        represented by ``elevated=True``.
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
# Module-level command tokens
# ----------------------------------------------------------------------


_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


WindowsVirtualizationActionStatus = ActionStatus


class WindowsVmState(Enum):
    """
        Coarse power / lifecycle state for a Hyper-V VM.

        The Hyper-V ``State`` field is reported either as a string
        (``Running``, ``Off``, ``Paused``, ``Saved``) or as a numeric
        enum (older PowerShell serialisations).  Both shapes are
        normalised onto these values.
    """
    RUNNING   = "running"
    STOPPED   = "stopped"
    PAUSED    = "paused"
    SUSPENDED = "suspended"
    OTHER     = "other"


@dataclass
class WindowsVirtualizationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsVirtualizationManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.
        :param default_switch: Optional default ``-SwitchName`` for
                               ``New-VM``.  Used when a
                               :class:`WindowsVmSpec` does not carry
                               its own ``network`` field.
        :param default_vhd_dir: Optional default directory for newly
                                created VHDs.  When set and the spec
                                provides ``disk_size_gb`` without
                                ``disk_path``, the manager assembles a
                                ``-NewVHDPath`` argument inside this
                                directory using ``<vm_name>.vhdx`` as
                                the filename.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    default_switch: Optional[str] = None
    default_vhd_dir: Optional[str] = None


@dataclass
class WindowsVirtualizationActionResult(FeatureActionResult):
    """
        Result of a virtualization-management write operation.

        :param vm_name: VM name targeted by the operation, when
                        applicable.
        :param snapshot_name: Snapshot name targeted by the operation,
                              when applicable.
        :param message: Human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes (e.g. data-loss advisories
                        on ``delete_vm``, ``detach_disk``, and
                        ``restore_snapshot``).
    """
    vm_name: Optional[str] = None
    snapshot_name: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsVmSpec:
    """
        Specification for :meth:`WindowsVirtualizationManager.create_vm`.

        :param name: VM name (``-Name``).
        :param cpu_count: Number of vCPUs.  Applied via
                          ``Set-VMProcessor -Count <n>`` after
                          creation.  Defaults to 1.
        :param memory_mb: Allocated memory in MiB.  Mapped onto
                          ``-MemoryStartupBytes`` (multiplied by
                          1024 * 1024).  Defaults to 1024 (1 GiB).
        :param disk_size_gb: When set and ``disk_path`` is None,
                             ``New-VM`` creates a fresh VHDX of this
                             size at
                             ``<default_vhd_dir>/<name>.vhdx`` (when
                             the config supplies one) or in the
                             default Hyper-V VHD location.
        :param disk_path: When set, used as ``-NewVHDPath`` so
                          ``New-VM`` attaches an existing VHDX or
                          creates a new one at this exact path.
        :param os_variant: Reserved for cross-OS-spec parity (Linux
                           uses ``--os-variant``); ignored on
                           Hyper-V.
        :param network: Optional ``-SwitchName`` override.  Falls back
                        to :attr:`WindowsVirtualizationManagerConfig.default_switch`
                        when None.
        :param iso_path: Reserved.  Hyper-V boots from a DVD drive
                         after creation; this field is captured on
                         the spec for callers that wish to extend
                         the renderer.
        :param graphics: Reserved for cross-OS-spec parity; ignored on
                         Hyper-V.
        :param boot_kind: Hyper-V VM **Generation** (1 or 2).
                          Defaults to 1 to match
                          ``virt-install``-style legacy boot; pick 2
                          for UEFI / Secure Boot guests.
    """
    name: str
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_gb: Optional[int] = None
    disk_path: Optional[str] = None
    os_variant: Optional[str] = None
    network: Optional[str] = None
    iso_path: Optional[str] = None
    graphics: str = "none"
    boot_kind: int = 1


@dataclass
class WindowsVmInfo:
    """
        Snapshot of a single Hyper-V VM's configuration and state.

        :param name: VM name (``Name`` field).
        :param state: Coarse power state.
        :param cpu_count: Configured vCPUs (``ProcessorCount`` from
                          ``Get-VM``).  ``None`` when not reported.
        :param memory_mb: Configured memory in MiB
                          (``MemoryAssigned`` / 1024 / 1024).
                          ``None`` when not reported.
        :param uptime_seconds: ``Uptime.TotalSeconds`` when reported
                               as a numeric value; ``None`` otherwise.
        :param os_type: ``OperatingSystem`` field when reported.
        :param vm_id: ``Id`` (GUID) field when reported.
    """
    name: str
    state: WindowsVmState
    cpu_count: Optional[int] = None
    memory_mb: Optional[int] = None
    uptime_seconds: Optional[int] = None
    os_type: Optional[str] = None
    vm_id: Optional[str] = None


@dataclass
class WindowsVmDiskSpec:
    """
        Specification for
        :meth:`WindowsVirtualizationManager.attach_disk`.

        :param path: VHDX / VHD path on the host that Hyper-V will
                     attach.  Mapped onto ``-Path``.
        :param target: Reserved for cross-OS-spec parity (Linux uses
                       guest-side device labels).  Ignored on
                       Hyper-V.
        :param controller_type: ``-ControllerType`` value
                                (``"SCSI"`` or ``"IDE"``).  Defaults
                                to ``"SCSI"`` when None.
        :param bus: Reserved.  Hyper-V uses ``-ControllerNumber`` /
                    ``-ControllerLocation`` for fine-grained control;
                    callers may set those via the future
                    ``controller_number`` / ``controller_location``
                    fields rather than the cross-OS ``bus`` field.
    """
    path: str
    target: Optional[str] = None
    controller_type: Optional[str] = None
    bus: Optional[str] = None


@dataclass
class WindowsSnapshotInfo:
    """
        Single Hyper-V snapshot entry returned by
        :meth:`list_snapshots`.

        :param name: Snapshot name (``Name`` field).
        :param created: Creation timestamp string
                        (``CreationTime``).  ``""`` when not reported.
        :param description: Reserved.  Hyper-V snapshots have no
                            description field; populated as ``None``.
        :param parent: ``ParentSnapshotName`` when reported, else
                       ``None``.
        :param vm_state: Reserved.  Populated as ``None`` because
                         Hyper-V's snapshot record does not carry a
                         coarse VM state at snapshot time.
    """
    name: str
    created: str
    description: Optional[str] = None
    parent: Optional[str] = None
    vm_state: Optional[WindowsVmState] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsVirtualizationQueryError(Exception):
    """
        Raised when a virtualization read fails for a reason other
        than the target VM / snapshot simply being absent.
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
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when
                           relevant).
            :param message: Optional pre-built message used for cases
                            where there is no failed argv to report.
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
            err_msg_lines = ["Virtualization query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# WindowsVirtualizationManager
# ----------------------------------------------------------------------


class WindowsVirtualizationManager(BaseFeatureManager):
    """
        Windows Hyper-V virtualization manager.  Every verb is
        dispatched through the injected :class:`ICommandRunner` as a
        scriptblock invocation of the Hyper-V PowerShell module.  See
        the module docstring for the Hyper-V role requirement,
        idempotency guards, stop semantics, and elevation rules.
    """

    result_class = WindowsVirtualizationActionResult
    status_class = WindowsVirtualizationActionStatus
    feature_name = "virtualization-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_vms(self) -> list:
        """
            Enumerate every VM on the Hyper-V host.

            Dispatches the scriptblock
            ``Get-VM | Select-Object Name, State, ProcessorCount,
            MemoryAssigned, Uptime, OperatingSystem, Id |
            ConvertTo-Json -Compress -Depth 5`` through the runner
            without elevation.

            :returns: One :class:`WindowsVmInfo` per VM.  Empty list
                      when no VMs are defined or when the JSON payload
                      is empty / unparseable.
            :raises WindowsVirtualizationQueryError: When ``Get-VM``
                                                     fails for an
                                                     unexpected reason.
        """
        script = (
            "Get-VM "
            "| Select-Object Name, State, ProcessorCount, "
            "MemoryAssigned, Uptime, OperatingSystem, Id "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsVirtualizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_vm_json(text=run_result.stdout)
        rtnval: list = []
        for record in records:
            if not isinstance(record, dict):
                continue
            info = _record_to_vm_info(record=record, requested_name=None)
            rtnval.append(info)
        return rtnval

    def get_vm_info(self, name: str) -> Optional[WindowsVmInfo]:
        """
            Return the :class:`WindowsVmInfo` for ``name``, or
            ``None`` when the VM is not defined.

            Reads do not elevate.

            :param name: VM name.

            :returns: The matching :class:`WindowsVmInfo`, or
                      ``None``.
            :raises WindowsVirtualizationQueryError: When the cmdlet
                                                     fails for an
                                                     unexpected reason.
        """
        record = self._read_vm_record(
            name=name, raise_on_unknown=True,
        )
        if record is None:
            rtnval: Optional[WindowsVmInfo] = None
            return rtnval
        rtnval = _record_to_vm_info(record=record, requested_name=name)
        return rtnval

    def list_snapshots(self, vm: str) -> list:
        """
            Enumerate snapshots for ``vm`` via
            ``Get-VMSnapshot -VMName <vm> | ConvertTo-Json -Compress
            -Depth 5``.

            :param vm: VM name.

            :returns: One :class:`WindowsSnapshotInfo` per snapshot.
                      Empty list when none exist.
            :raises WindowsVirtualizationQueryError: When the cmdlet
                                                     fails for an
                                                     unexpected reason.
        """
        escaped = _ps_escape_single_quoted(value=vm)
        script = (
            "Get-VMSnapshot -VMName '{}' "
            "| Select-Object Name, CreationTime, ParentSnapshotName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if _stderr_looks_no_snapshots(stderr=run_result.stderr or ""):
                rtnval: list = []
                return rtnval
            raise WindowsVirtualizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_vm_json(text=run_result.stdout)
        rtnval = []
        for record in records:
            if not isinstance(record, dict):
                continue
            info = _record_to_snapshot_info(record=record)
            rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — VM lifecycle
    # ------------------------------------------------------------------

    def create_vm(
        self,
        spec: WindowsVmSpec,
    ) -> WindowsVirtualizationActionResult:
        """
            Provision a fresh Hyper-V VM via ``New-VM`` plus a
            follow-up ``Set-VMProcessor`` to apply the vCPU count.

            Idempotent: when ``Get-VM -Name <name>`` already reports a
            VM with the same memory and vCPU configuration, returns
            ``SKIPPED``.  When the VM exists with a different
            configuration, returns ``SKIPPED`` as well — adjusting an
            existing VM in place is the responsibility of separate
            ``update_vm`` verbs (out of scope for this contract).

            :param spec: The :class:`WindowsVmSpec`.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        script = self._render_create_script(spec=spec)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=spec.name,
            )
            return dry

        existing = self._read_vm_record(name=spec.name)
        if existing is not None:
            skipped = self._build_result(
                status=WindowsVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=spec.name,
                message="VM {!r} already exists".format(spec.name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=spec.name,
        )
        return rtnval

    def delete_vm(
        self,
        name: str,
    ) -> WindowsVirtualizationActionResult:
        """
            Force-stop and delete a Hyper-V VM via
            ``Stop-VM -Name <name> -Force -TurnOff`` followed by
            ``Remove-VM -Name <name> -Force``.

            Idempotent: when the VM is not defined, returns
            ``SKIPPED``.  On ``SUCCESS`` ``message`` carries a
            data-loss advisory.

            :param name: VM name.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Stop-VM -Name '{name}' -Force -TurnOff "
            "-ErrorAction SilentlyContinue; "
            "Remove-VM -Name '{name}' -Force"
        ).format(name=escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_vm_record(name=name)
        if existing is None:
            skipped = self._build_result(
                status=WindowsVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is not defined".format(name),
            )
            return skipped

        result = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        if result.status == WindowsVirtualizationActionStatus.SUCCESS:
            advisory = (
                "VM {!r} was removed; the VM definition has been "
                "destroyed.  Backing VHDs are NOT deleted by this "
                "operation".format(name)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def start_vm(
        self,
        name: str,
    ) -> WindowsVirtualizationActionResult:
        """
            ``Start-VM -Name <name>``.  SKIPPED when the VM is already
            running.

            :param name: VM name.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = "Start-VM -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_vm_record(name=name)
        if existing is not None:
            info = _record_to_vm_info(
                record=existing, requested_name=name,
            )
            if info.state == WindowsVmState.RUNNING:
                skipped = self._build_result(
                    status=WindowsVirtualizationActionStatus.SKIPPED,
                    command=command,
                    vm_name=name,
                    message="VM {!r} is already running".format(name),
                )
                return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    def stop_vm(
        self,
        name: str,
        force: bool = False,
    ) -> WindowsVirtualizationActionResult:
        """
            Power off a running VM.

            ``force=False`` -> ``Stop-VM -Name <name>`` (graceful
            shutdown via integration services).
            ``force=True``  -> ``Stop-VM -Name <name> -Force -TurnOff``
            (hard power-off).

            Idempotent: when the VM is already stopped, returns
            ``SKIPPED``.

            :param name: VM name.
            :param force: When True, hard-power-off via ``-TurnOff``.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=name)
        if force:
            script = (
                "Stop-VM -Name '{}' -Force -TurnOff"
            ).format(escaped)
        else:
            script = "Stop-VM -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_vm_record(name=name)
        if existing is not None:
            info = _record_to_vm_info(
                record=existing, requested_name=name,
            )
            if info.state == WindowsVmState.STOPPED:
                skipped = self._build_result(
                    status=WindowsVirtualizationActionStatus.SKIPPED,
                    command=command,
                    vm_name=name,
                    message="VM {!r} is already stopped".format(name),
                )
                return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    def pause_vm(
        self,
        name: str,
    ) -> WindowsVirtualizationActionResult:
        """
            ``Suspend-VM -Name <name>``.  SKIPPED when already paused.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = "Suspend-VM -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_vm_record(name=name)
        if existing is not None:
            info = _record_to_vm_info(
                record=existing, requested_name=name,
            )
            if info.state == WindowsVmState.PAUSED:
                skipped = self._build_result(
                    status=WindowsVirtualizationActionStatus.SKIPPED,
                    command=command,
                    vm_name=name,
                    message="VM {!r} is already paused".format(name),
                )
                return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    def resume_vm(
        self,
        name: str,
    ) -> WindowsVirtualizationActionResult:
        """
            ``Resume-VM -Name <name>``.  SKIPPED when already running
            (i.e. not paused).
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = "Resume-VM -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_vm_record(name=name)
        if existing is not None:
            info = _record_to_vm_info(
                record=existing, requested_name=name,
            )
            if info.state == WindowsVmState.RUNNING:
                skipped = self._build_result(
                    status=WindowsVirtualizationActionStatus.SKIPPED,
                    command=command,
                    vm_name=name,
                    message="VM {!r} is already running".format(name),
                )
                return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — disks
    # ------------------------------------------------------------------

    def attach_disk(
        self,
        vm: str,
        disk_spec: WindowsVmDiskSpec,
    ) -> WindowsVirtualizationActionResult:
        """
            ``Add-VMHardDiskDrive -VMName <vm> -Path <disk_spec.path>
            [-ControllerType <type>]``.

            Idempotent: when ``Get-VMHardDiskDrive -VMName <vm>``
            already reports a drive whose ``Path`` matches
            ``disk_spec.path``, returns ``SKIPPED``.

            :param vm: VM name.
            :param disk_spec: Backing-VHDX path plus optional
                              controller-type.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped_vm = _ps_escape_single_quoted(value=vm)
        escaped_path = _ps_escape_single_quoted(value=disk_spec.path)

        controller_type_raw = disk_spec.controller_type
        if controller_type_raw is None or len(controller_type_raw) == 0:
            controller_type = "SCSI"
        else:
            controller_type = controller_type_raw

        script = (
            "Add-VMHardDiskDrive -VMName '{vm}' -Path '{path}' "
            "-ControllerType {ctype}"
        ).format(
            vm=escaped_vm,
            path=escaped_path,
            ctype=controller_type,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
            )
            return dry

        if self._disk_attached(vm=vm, path=disk_spec.path):
            skipped = self._build_result(
                status=WindowsVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                message=(
                    "disk {!r} is already attached to VM {!r}".format(
                        disk_spec.path, vm,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=vm,
        )
        return rtnval

    def detach_disk(
        self,
        vm: str,
        disk_id: str,
    ) -> WindowsVirtualizationActionResult:
        """
            Detach a disk from ``vm`` by VHDX path.

            ``disk_id`` is matched against the ``Path`` field of every
            ``Get-VMHardDiskDrive`` record; the first match's
            ``ControllerType`` / ``ControllerNumber`` /
            ``ControllerLocation`` are then passed to
            ``Remove-VMHardDiskDrive``.

            Idempotent: when no drive whose ``Path`` matches
            ``disk_id`` is present, returns ``SKIPPED``.  On
            ``SUCCESS`` ``message`` carries a data-loss advisory.

            :param vm: VM name.
            :param disk_id: VHDX path identifying the drive to detach.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped_vm = _ps_escape_single_quoted(value=vm)
        escaped_path = _ps_escape_single_quoted(value=disk_id)

        # Use a scriptblock that filters by Path and removes the
        # matching drive.  Hyper-V exposes Remove-VMHardDiskDrive by
        # controller coordinates, so we route the matched drive object
        # through a pipeline.
        script = (
            "$drive = Get-VMHardDiskDrive -VMName '{vm}' "
            "| Where-Object {{ $_.Path -eq '{path}' }} "
            "| Select-Object -First 1; "
            "if ($null -eq $drive) {{ "
            "  throw 'no matching drive' "
            "}} else {{ "
            "  Remove-VMHardDiskDrive -VMName '{vm}' "
            "-ControllerType $drive.ControllerType "
            "-ControllerNumber $drive.ControllerNumber "
            "-ControllerLocation $drive.ControllerLocation "
            "}}"
        ).format(vm=escaped_vm, path=escaped_path)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
            )
            return dry

        if not self._disk_attached(vm=vm, path=disk_id):
            skipped = self._build_result(
                status=WindowsVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                message=(
                    "disk {!r} is not attached to VM {!r}".format(
                        disk_id, vm,
                    )
                ),
            )
            return skipped

        result = self._dispatch_virt_write(
            command=command, vm_name=vm,
        )
        if result.status == WindowsVirtualizationActionStatus.SUCCESS:
            advisory = (
                "disk {!r} was detached from VM {!r}; the backing "
                "VHDX was NOT removed".format(disk_id, vm)
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Write verbs — snapshots
    # ------------------------------------------------------------------

    def snapshot_vm(
        self,
        vm: str,
        snapshot_name: str,
    ) -> WindowsVirtualizationActionResult:
        """
            ``Checkpoint-VM -Name <vm> -SnapshotName <snapshot_name>``.

            Idempotent: when a snapshot with the same name already
            exists for ``vm``, returns ``SKIPPED``.

            :param vm: VM name.
            :param snapshot_name: Snapshot name.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped_vm = _ps_escape_single_quoted(value=vm)
        escaped_snapshot = _ps_escape_single_quoted(value=snapshot_name)
        script = (
            "Checkpoint-VM -Name '{vm}' -SnapshotName '{snapshot}'"
        ).format(vm=escaped_vm, snapshot=escaped_snapshot)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
            )
            return dry

        if self._snapshot_exists(vm=vm, snapshot_name=snapshot_name):
            skipped = self._build_result(
                status=WindowsVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
                message=(
                    "snapshot {!r} already exists for VM {!r}".format(
                        snapshot_name, vm,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
            snapshot_name=snapshot_name,
        )
        return rtnval

    def restore_snapshot(
        self,
        vm: str,
        snapshot_name: str,
    ) -> WindowsVirtualizationActionResult:
        """
            ``Restore-VMSnapshot -VMName <vm> -Name <snapshot_name>
            -Confirm:$false``.

            Unconditional per the contract; populates a data-loss
            advisory on ``SUCCESS``.

            :param vm: VM name.
            :param snapshot_name: Snapshot name.

            :returns: The
                      :class:`WindowsVirtualizationActionResult`.
        """
        escaped_vm = _ps_escape_single_quoted(value=vm)
        escaped_snapshot = _ps_escape_single_quoted(value=snapshot_name)
        script = (
            "Restore-VMSnapshot -VMName '{vm}' -Name '{snapshot}' "
            "-Confirm:$false"
        ).format(vm=escaped_vm, snapshot=escaped_snapshot)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
            )
            return dry

        result = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
            snapshot_name=snapshot_name,
        )
        if result.status == WindowsVirtualizationActionStatus.SUCCESS:
            advisory = (
                "VM {!r} was reverted to snapshot {!r}; any state "
                "written after the snapshot was discarded".format(
                    vm, snapshot_name,
                )
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsVirtualizationManagerConfig:
        """
            Return the manager's config typed.  Falls back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, WindowsVirtualizationManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsVirtualizationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

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

    def _render_create_script(self, *, spec: WindowsVmSpec) -> str:
        """
            Render the multi-step PowerShell scriptblock that creates
            the VM.

            The script:

                1. Builds ``-MemoryStartupBytes`` from
                   ``spec.memory_mb`` (MiB -> bytes).
                2. Picks the switch from ``spec.network`` (or the
                   config's ``default_switch`` when None).
                3. Picks the VHD path from ``spec.disk_path`` or
                   composes ``<default_vhd_dir>/<name>.vhdx`` when
                   only ``disk_size_gb`` is supplied.
                4. Runs ``New-VM`` with all of the above.
                5. Runs ``Set-VMProcessor -Count <n>`` to apply the
                   vCPU count (since ``New-VM`` does not accept it
                   directly).

            :param spec: The VM spec.

            :returns: The full PowerShell scriptblock.
        """
        config = self._typed_config()
        escaped_name = _ps_escape_single_quoted(value=spec.name)

        memory_bytes = int(spec.memory_mb) * 1024 * 1024
        generation = int(spec.boot_kind)
        if generation not in (1, 2):
            generation = 1

        new_vm_args = [
            "-Name '{}'".format(escaped_name),
            "-MemoryStartupBytes {}".format(memory_bytes),
            "-Generation {}".format(generation),
        ]

        if (
            spec.disk_path is not None
            and len(spec.disk_path) > 0
        ):
            escaped_path = _ps_escape_single_quoted(value=spec.disk_path)
            new_vm_args.append("-NewVHDPath '{}'".format(escaped_path))
            if (
                spec.disk_size_gb is not None
                and spec.disk_size_gb > 0
            ):
                vhd_size_bytes = int(spec.disk_size_gb) * 1024 * 1024 * 1024
                new_vm_args.append(
                    "-NewVHDSizeBytes {}".format(vhd_size_bytes),
                )
        elif (
            spec.disk_size_gb is not None
            and spec.disk_size_gb > 0
            and config.default_vhd_dir is not None
            and len(config.default_vhd_dir) > 0
        ):
            composed_path = "{}\\{}.vhdx".format(
                config.default_vhd_dir, spec.name,
            )
            escaped_path = _ps_escape_single_quoted(value=composed_path)
            vhd_size_bytes = int(spec.disk_size_gb) * 1024 * 1024 * 1024
            new_vm_args.append("-NewVHDPath '{}'".format(escaped_path))
            new_vm_args.append(
                "-NewVHDSizeBytes {}".format(vhd_size_bytes),
            )

        switch_value: Optional[str] = None
        if spec.network is not None and len(spec.network) > 0:
            switch_value = spec.network
        elif (
            config.default_switch is not None
            and len(config.default_switch) > 0
        ):
            switch_value = config.default_switch
        if switch_value is not None:
            escaped_switch = _ps_escape_single_quoted(value=switch_value)
            new_vm_args.append("-SwitchName '{}'".format(escaped_switch))

        new_vm_command = "New-VM " + " ".join(new_vm_args)

        set_proc_command = (
            "Set-VMProcessor -VMName '{name}' -Count {count}"
        ).format(name=escaped_name, count=int(spec.cpu_count))

        rtnval = "{}; {}".format(new_vm_command, set_proc_command)
        return rtnval

    def _read_vm_record(
        self,
        *,
        name: str,
        raise_on_unknown: bool = False,
    ) -> Optional[dict]:
        """
            Probe ``Get-VM -Name <name>`` and return the parsed JSON
            record.

            :param name: VM name.
            :param raise_on_unknown: When True, unexpected non-zero
                                     exits raise
                                     :class:`WindowsVirtualizationQueryError`.
                                     When False (the default for
                                     idempotency probes), unexpected
                                     failures collapse to ``None``.

            :returns: The parsed dict, or ``None`` when the VM is
                      absent.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-VM -Name '{}' "
            "| Select-Object Name, State, ProcessorCount, "
            "MemoryAssigned, Uptime, OperatingSystem, Id "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if _stderr_looks_unknown_vm(stderr=run_result.stderr or ""):
                rtnval: Optional[dict] = None
                return rtnval
            if raise_on_unknown:
                raise WindowsVirtualizationQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = None
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            if raise_on_unknown:
                err_msg_lines = [
                    "Failed to parse Get-VM JSON payload.",
                ]
                err_msg_lines.append("    COMMAND: {}".format(
                    " ".join(command),
                ))
                err_msg_lines.append("    STDOUT: {}".format(stripped))
                err_msg = os.linesep.join(err_msg_lines)
                raise WindowsVirtualizationQueryError(
                    message=err_msg,
                ) from None
            rtnval = None
            return rtnval

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = None
                return rtnval
            first = parsed[0]
            if not isinstance(first, dict):
                rtnval = None
                return rtnval
            rtnval = first
            return rtnval

        if isinstance(parsed, dict):
            rtnval = parsed
            return rtnval

        rtnval = None
        return rtnval

    def _disk_attached(self, *, vm: str, path: str) -> bool:
        """
            Return True when
            ``Get-VMHardDiskDrive -VMName <vm>`` reports a drive whose
            ``Path`` matches ``path`` (case-insensitive on Windows).
        """
        escaped_vm = _ps_escape_single_quoted(value=vm)
        script = (
            "Get-VMHardDiskDrive -VMName '{}' "
            "| Select-Object Path, ControllerType, "
            "ControllerNumber, ControllerLocation "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped_vm)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = False
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = False
            return rtnval

        records = _normalise_to_list(parsed=parsed)
        rtnval = False
        for record in records:
            if not isinstance(record, dict):
                continue
            attached_raw = record.get("Path")
            if attached_raw is None:
                continue
            attached_text = str(attached_raw)
            if attached_text.lower() == path.lower():
                rtnval = True
                break
        return rtnval

    def _snapshot_exists(
        self,
        *,
        vm: str,
        snapshot_name: str,
    ) -> bool:
        """
            Return True when
            ``Get-VMSnapshot -VMName <vm>`` reports a snapshot whose
            ``Name`` matches ``snapshot_name``.
        """
        escaped_vm = _ps_escape_single_quoted(value=vm)
        script = (
            "Get-VMSnapshot -VMName '{}' "
            "| Select-Object Name, CreationTime, ParentSnapshotName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped_vm)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = False
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = False
            return rtnval

        records = _normalise_to_list(parsed=parsed)
        rtnval = False
        for record in records:
            if not isinstance(record, dict):
                continue
            existing_name_raw = record.get("Name")
            if existing_name_raw is None:
                continue
            if str(existing_name_raw) == snapshot_name:
                rtnval = True
                break
        return rtnval

    def _dispatch_virt_write(
        self,
        *,
        command: list,
        vm_name: Optional[str] = None,
        snapshot_name: Optional[str] = None,
    ) -> WindowsVirtualizationActionResult:
        """
            Dispatch a write command and wrap the result with the
            virtualization-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=vm_name,
            operation="virtualization_write",
            vm_name=vm_name,
            snapshot_name=snapshot_name,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsVirtualizationActionStatus,
        command: list,
        vm_name: Optional[str] = None,
        snapshot_name: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsVirtualizationActionResult:
        """
            Construct a :class:`WindowsVirtualizationActionResult`
            without invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = vm_name
        rtnval = WindowsVirtualizationActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers / helpers
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _normalise_to_list(*, parsed) -> list:
    """
        Normalise a ``ConvertTo-Json`` payload into a list (bare-object
        shape -> single-element list).
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _parse_get_vm_json(*, text: str) -> list:
    """
        Parse the JSON output of ``Get-VM | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: A list of dicts (single-object payloads are wrapped
                  in a one-element list).  Returns an empty list when
                  the payload is absent or unparseable.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: list = []
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = []
        return rtnval
    rtnval = _normalise_to_list(parsed=parsed)
    return rtnval


def _stderr_looks_unknown_vm(*, stderr: str) -> bool:
    """
        Heuristically detect a Hyper-V "VM not found" stderr.
    """
    lower = stderr.lower()
    if "no virtual machine" in lower:
        rtnval = True
        return rtnval
    if "objectnotfound" in lower:
        rtnval = True
        return rtnval
    if "hyper-v was unable to find a virtual machine" in lower:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _stderr_looks_no_snapshots(*, stderr: str) -> bool:
    """
        Heuristically detect a Hyper-V "no snapshots" stderr emitted by
        ``Get-VMSnapshot`` when the VM is known but has no snapshots.

        Note: Hyper-V's ``Get-VMSnapshot`` typically returns success
        with an empty payload in this case; this helper exists for the
        rarer error variants reported by older PowerShell versions.
    """
    lower = stderr.lower()
    if "no checkpoint" in lower:
        rtnval = True
        return rtnval
    if "no snapshot" in lower:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _normalise_state(*, state_raw) -> WindowsVmState:
    """
        Map a raw Hyper-V state payload onto :class:`WindowsVmState`.

        Accepts:

            * Strings: ``"Running"``, ``"Off"``, ``"Paused"``,
              ``"Saved"``, etc.
            * Integers: PowerShell occasionally serialises the enum
              as a numeric value.

        :param state_raw: The raw JSON value from the ``State`` field.

        :returns: The normalised state.  Unknown values map to
                  :attr:`WindowsVmState.OTHER`.
    """
    if state_raw is None:
        rtnval = WindowsVmState.OTHER
        return rtnval

    if isinstance(state_raw, int):
        # Hyper-V VirtualMachineState enum values:
        #   2 = Running, 3 = Off, 32768 = Paused, 32769 = Suspended.
        if state_raw == 2:
            rtnval = WindowsVmState.RUNNING
            return rtnval
        if state_raw == 3:
            rtnval = WindowsVmState.STOPPED
            return rtnval
        if state_raw == 32768:
            rtnval = WindowsVmState.PAUSED
            return rtnval
        if state_raw == 32769:
            rtnval = WindowsVmState.SUSPENDED
            return rtnval
        rtnval = WindowsVmState.OTHER
        return rtnval

    text = str(state_raw).strip().lower()
    if text == "running":
        rtnval = WindowsVmState.RUNNING
        return rtnval
    if text == "off":
        rtnval = WindowsVmState.STOPPED
        return rtnval
    if text == "paused":
        rtnval = WindowsVmState.PAUSED
        return rtnval
    if text == "saved":
        rtnval = WindowsVmState.SUSPENDED
        return rtnval
    rtnval = WindowsVmState.OTHER
    return rtnval


def _record_to_vm_info(
    *,
    record: dict,
    requested_name: Optional[str],
) -> WindowsVmInfo:
    """
        Convert a parsed ``Get-VM`` record into a
        :class:`WindowsVmInfo`.
    """
    name_raw = record.get("Name")
    if name_raw is None or len(str(name_raw)) == 0:
        if requested_name is None:
            name_value = ""
        else:
            name_value = requested_name
    else:
        name_value = str(name_raw)

    state_raw = record.get("State")
    state = _normalise_state(state_raw=state_raw)

    proc_raw = record.get("ProcessorCount")
    if proc_raw is None:
        cpu_count: Optional[int] = None
    else:
        try:
            cpu_count = int(proc_raw)
        except (ValueError, TypeError):
            cpu_count = None

    mem_raw = record.get("MemoryAssigned")
    memory_mb: Optional[int] = None
    if mem_raw is not None:
        try:
            mem_bytes = int(mem_raw)
            memory_mb = int(mem_bytes / (1024 * 1024))
        except (ValueError, TypeError):
            memory_mb = None

    uptime_seconds: Optional[int] = None
    uptime_raw = record.get("Uptime")
    if isinstance(uptime_raw, dict):
        total_seconds_raw = uptime_raw.get("TotalSeconds")
        if total_seconds_raw is not None:
            try:
                uptime_seconds = int(float(total_seconds_raw))
            except (ValueError, TypeError):
                uptime_seconds = None

    os_type_raw = record.get("OperatingSystem")
    if os_type_raw is None or len(str(os_type_raw)) == 0:
        os_type: Optional[str] = None
    else:
        os_type = str(os_type_raw)

    id_raw = record.get("Id")
    if id_raw is None or len(str(id_raw)) == 0:
        vm_id: Optional[str] = None
    else:
        vm_id = str(id_raw)

    rtnval = WindowsVmInfo(
        name=name_value,
        state=state,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        uptime_seconds=uptime_seconds,
        os_type=os_type,
        vm_id=vm_id,
    )
    return rtnval


def _record_to_snapshot_info(*, record: dict) -> WindowsSnapshotInfo:
    """
        Convert a parsed ``Get-VMSnapshot`` record into a
        :class:`WindowsSnapshotInfo`.
    """
    name_raw = record.get("Name")
    if name_raw is None:
        name_value = ""
    else:
        name_value = str(name_raw)

    created_raw = record.get("CreationTime")
    if created_raw is None or len(str(created_raw)) == 0:
        created_value = ""
    else:
        created_value = str(created_raw)

    parent_raw = record.get("ParentSnapshotName")
    if parent_raw is None or len(str(parent_raw)) == 0:
        parent_value: Optional[str] = None
    else:
        parent_value = str(parent_raw)

    rtnval = WindowsSnapshotInfo(
        name=name_value,
        created=created_value,
        description=None,
        parent=parent_value,
        vm_state=None,
    )
    return rtnval

"""
    Windows storage-management feature.

    Implements the cross-OS storage-management contract for Windows
    using the PowerShell Storage cmdlets (``Get-Disk``, ``Get-Volume``,
    ``Get-Partition``, ``Get-PhysicalDisk``, ``Get-StoragePool``,
    ``Get-VirtualDisk``, ``New-Volume``, ``Remove-Volume``,
    ``New-Partition``, ``Remove-Partition``, ``Resize-Partition``,
    ``New-StoragePool``, ``Remove-StoragePool``, ``Add-PhysicalDisk``,
    ``Remove-PhysicalDisk``).  Every cmdlet is dispatched through the
    injected :class:`ICommandRunner` as a PowerShell script block.

    Concept mapping:
        The cross-OS contract talks about *pools* and *volumes*.  On
        Windows these map to:

        * **Pool** — Storage Spaces **Storage Pool**
          (``New-StoragePool`` / ``Remove-StoragePool``).
        * **Volume** — Either a Storage-Spaces **Virtual Disk** /
          **Volume** carved out of a pool (``New-Volume
          -StoragePoolFriendlyName ...``) **or** a raw partition on a
          plain disk (``New-Partition`` + ``Format-Volume``).  The
          implementation dispatches on the spec: when ``pool`` is
          provided we use the pool flow; when ``pool`` is ``None`` we
          use the partition flow.
        * **Disk** — A ``Get-Disk`` row keyed by its ``Number``.

        See ``specs/3-storage-management/design.md`` for the full
        concept-mapping table.

    Volume identifier syntax:
        Volumes are identified by their drive letter (``"X"``) for
        partition-style volumes, or by the Storage-Spaces virtual-disk
        friendly name when the volume lives in a pool.  The
        :meth:`WindowsStorageManager.delete_volume` /
        :meth:`WindowsStorageManager.resize_volume` /
        :meth:`WindowsStorageManager.get_volume_info` verbs accept
        either form; the implementation detects which is present.

    PowerShell argv shape:
        Every script is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so ``$PROFILE`` cannot mutate cmdlet
        behaviour and PowerShell never blocks on interactive input.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``list_disks``, ``list_volumes``, ``get_volume_info``,
        ``get_disk_info``, all idempotency probes) never elevate.
"""

import json
import os

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


# Tolerance in bytes for `resize_volume`.  NTFS allocates in 4 KiB
# clusters; we pick 1 MiB to avoid surprising callers with a SKIPPED
# outcome on tiny rounding deltas.
_RESIZE_TOLERANCE_BYTES = 1 * 1024 * 1024


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


WindowsStorageActionStatus = ActionStatus


@dataclass
class WindowsStorageManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsStorageManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``
                                      (PATH-resolved).
        :param default_filesystem: Filesystem token passed to
                                   ``New-Volume -FileSystem``.  Defaults
                                   to ``"NTFS"``.
        :param default_resiliency: Default ``-ResiliencySettingName``
                                   for ``New-StoragePool`` /
                                   ``New-Volume`` calls when the
                                   :class:`WindowsPoolSpec` /
                                   :class:`WindowsVolumeSpec` does not
                                   specify.  Defaults to ``"Mirror"``.
        :param default_subsystem: Storage-subsystem friendly-name
                                  pattern passed to
                                  ``-StorageSubSystemFriendlyName``.
                                  Defaults to ``"Windows Storage*"``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    default_filesystem: str = "NTFS"
    default_resiliency: str = "Mirror"
    default_subsystem: str = "Windows Storage*"


@dataclass
class WindowsStorageActionResult(FeatureActionResult):
    """
        Result of a storage-management write operation.

        :param target: Generic target identifier (volume id, pool name,
                       or disk number-as-string).
        :param pool: The pool friendly-name targeted, when applicable.
        :param volume: The volume identifier targeted, when applicable.
        :param message: Human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes (e.g. data-loss advisories
                        on delete and remove-from-pool verbs).
    """
    target: Optional[str] = None
    pool: Optional[str] = None
    volume: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsDiskInfo:
    """
        Information about a single physical disk.

        :param id: Stable disk identifier (the disk number as a string,
                   ``"0"`` / ``"1"`` / ...).  Cmdlet parameters use
                   ``-Number`` typed as int; the dataclass keeps a
                   string to match the cross-OS contract's ``id: str``.
        :param name: Friendly display name (``FriendlyName``).
        :param size_bytes: Total disk size in bytes.
        :param model: Device model (typically ``Model``).
        :param serial: Device serial.
        :param partition_style: ``GPT``, ``MBR``, or ``RAW``.
        :param health_status: ``Get-Disk`` health status (``Healthy``,
                              ``Warning``, ``Unhealthy``, ``Unknown``).
        :param mount_points: Drive letters / access paths for every
                             descendant volume.
        :param is_pool_member: True when the disk is currently a member
                               of a Storage Pool.
    """
    id: str
    name: str
    size_bytes: int
    model: Optional[str] = None
    serial: Optional[str] = None
    partition_style: Optional[str] = None
    health_status: Optional[str] = None
    mount_points: list = field(default_factory=list)
    is_pool_member: bool = False


@dataclass
class WindowsVolumeInfo:
    """
        Information about a single volume.

        :param id: Stable volume identifier — drive letter for
                   partition-style volumes, virtual-disk friendly name
                   for Storage-Spaces volumes.
        :param name: ``FileSystemLabel`` when reported, else the id.
        :param size_bytes: Total size in bytes.
        :param used_bytes: Used bytes (``Size - SizeRemaining``) when
                           reported.
        :param filesystem: Filesystem type (``NTFS``, ``FAT32``,
                           ``ReFS``, ...) as reported by
                           ``Get-Volume.FileSystemType``.
        :param mount_point: Drive letter token (``"X:"``) for mounted
                            volumes, else ``None``.
        :param pool: Storage-Pool friendly name when the volume lives in
                     a pool, else ``None``.
        :param health_status: ``Get-Volume`` health status.
    """
    id: str
    name: str
    size_bytes: int
    used_bytes: Optional[int] = None
    filesystem: Optional[str] = None
    mount_point: Optional[str] = None
    pool: Optional[str] = None
    health_status: Optional[str] = None


@dataclass
class WindowsPoolSpec:
    """
        Specification for :meth:`WindowsStorageManager.create_pool`.

        :param name: Storage-Pool friendly name.
        :param disks: Disk identifiers (disk numbers as strings).
                      Translated to ``-PhysicalDisks`` via
                      ``Get-PhysicalDisk -CanPool $True | Where-Object
                      Number -in @(<disks>)``.
        :param resiliency: Optional ``-ResiliencySettingName`` for
                           subsequent ``New-Volume`` calls in this
                           pool: ``"Mirror"``, ``"Parity"``, or
                           ``"Simple"``.  When ``None`` the config's
                           default is used.
    """
    name: str
    disks: list
    resiliency: Optional[str] = None


@dataclass
class WindowsVolumeSpec:
    """
        Specification for :meth:`WindowsStorageManager.create_volume`.

        :param name: Volume friendly name (``FriendlyName`` for
                     pool-backed volumes; ``FileSystemLabel`` for
                     partition-style volumes).
        :param size_bytes: Requested size in bytes.
        :param pool: Storage-Pool friendly name when the volume lives
                     in a pool.  ``None`` selects the partition flow,
                     in which case the spec's ``disk_id`` field MUST be
                     populated.
        :param filesystem: Filesystem token passed to ``-FileSystem``.
                           Defaults to ``"NTFS"``.
        :param resiliency: Optional ``-ResiliencySettingName`` override
                           for the pool path.
        :param disk_id: Disk number (as string) targeted by the
                        partition flow when ``pool`` is ``None``.
    """
    name: str
    size_bytes: int
    pool: Optional[str] = None
    filesystem: str = "NTFS"
    resiliency: Optional[str] = None
    disk_id: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsStorageQueryError(Exception):
    """
        Raised when a storage read fails for a reason other than the
        target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Storage query command failed."]
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
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _normalize_to_list(*, parsed):
    """
        Normalise a ``ConvertTo-Json`` payload into a list.
        ``ConvertTo-Json`` drops the array wrapper for one-element
        pipelines.
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _coerce_int(*, raw_value) -> int:
    """
        Best-effort coercion of a parsed JSON field to int.  Returns
        ``0`` on failure.
    """
    if raw_value is None:
        rtnval = 0
        return rtnval
    if isinstance(raw_value, bool):
        rtnval = 0
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value
        return rtnval
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if len(text) == 0:
            rtnval = 0
            return rtnval
        try:
            rtnval = int(text)
        except ValueError:
            rtnval = 0
        return rtnval
    rtnval = 0
    return rtnval


def _coerce_optional_int(*, raw_value):
    """
        Like :func:`_coerce_int` but returns ``None`` on failure.
    """
    if raw_value is None:
        rtnval = None
        return rtnval
    if isinstance(raw_value, bool):
        rtnval = None
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value
        return rtnval
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval
        try:
            rtnval = int(text)
        except ValueError:
            rtnval = None
        return rtnval
    rtnval = None
    return rtnval


def _parse_powershell_json(*, text: str):
    """
        Parse the JSON output of a ``ConvertTo-Json`` invocation,
        normalising the bare-object shape into a list when appropriate.
        Returns ``None`` on failure.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval = None
        return rtnval
    try:
        rtnval = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = None
    return rtnval


def _normalise_drive_letter(*, mountpoint: str) -> str:
    """
        Strip a single trailing colon and any following characters from
        a mountpoint to obtain the bare drive-letter name.

        ``"X:"`` -> ``"X"``; ``"X:\\"`` -> ``"X"``; ``"X"`` ->
        ``"X"``.
    """
    if len(mountpoint) == 0:
        rtnval = mountpoint
        return rtnval
    colon_index = mountpoint.find(":")
    if colon_index < 0:
        rtnval = mountpoint
        return rtnval
    rtnval = mountpoint[:colon_index]
    return rtnval


def _format_drive_letter_token(*, drive_name: str) -> str:
    """
        Render a drive-letter mountpoint token as ``"<letter>:"``.
    """
    rtnval = "{}:".format(drive_name)
    return rtnval


def _looks_like_drive_letter(*, value: str) -> bool:
    """
        Return True when ``value`` looks like a single-letter drive
        identifier (``"X"`` or ``"X:"``).  Used to dispatch
        ``delete_volume`` / ``resize_volume`` between the partition
        flow and the storage-spaces flow.
    """
    if len(value) == 0:
        rtnval = False
        return rtnval
    bare = _normalise_drive_letter(mountpoint=value)
    if len(bare) != 1:
        rtnval = False
        return rtnval
    rtnval = bare.isalpha()
    return rtnval


# ----------------------------------------------------------------------
# WindowsStorageManager
# ----------------------------------------------------------------------


class WindowsStorageManager(BaseFeatureManager):
    """
        Windows storage manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner` as a PowerShell script.  See
        the module docstring for the Storage-Spaces concept mapping
        and elevation policy.
    """

    result_class = WindowsStorageActionResult
    status_class = WindowsStorageActionStatus
    feature_name = "storage-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_disks(self) -> list:
        """
            Enumerate physical disks via ``Get-Disk | Select-Object
            Number, FriendlyName, Size, HealthStatus,
            OperationalStatus, PartitionStyle, SerialNumber, Model |
            ConvertTo-Json -Compress -Depth 5``.

            The pool-membership flag is derived from
            ``Get-PhysicalDisk | Select-Object Number, CanPool``: a
            disk that ``CanPool=False`` AND is reported as belonging to
            a pool by ``Get-StoragePool`` is treated as a member; a
            disk that ``CanPool=True`` is NOT a member.

            :returns: A list (possibly empty) of
                      :class:`WindowsDiskInfo`.
        """
        get_disk_command = self._build_powershell_argv(
            script=(
                "Get-Disk "
                "| Select-Object Number, FriendlyName, Size, "
                "HealthStatus, OperationalStatus, PartitionStyle, "
                "SerialNumber, Model "
                "| ConvertTo-Json -Compress -Depth 5"
            ),
        )
        run_result = self._runner.run(
            list(get_disk_command), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        records = _parse_powershell_json(text=run_result.stdout)
        if records is None:
            rtnval = []
            return rtnval
        records_list = _normalize_to_list(parsed=records)
        pool_member_numbers = self._collect_pool_member_disk_numbers()
        rtnval = _build_disks_from_records(
            records=records_list,
            pool_member_numbers=pool_member_numbers,
        )
        return rtnval

    def list_volumes(self) -> list:
        """
            Enumerate volumes via ``Get-Volume | Select-Object
            DriveLetter, FileSystemLabel, FileSystemType,
            SizeRemaining, Size, HealthStatus, FileSystem | ConvertTo-Json
            -Compress -Depth 5`` plus a ``Get-VirtualDisk`` walk to
            attribute pool ownership.

            :returns: A list (possibly empty) of
                      :class:`WindowsVolumeInfo`.
        """
        get_volume_command = self._build_powershell_argv(
            script=(
                "Get-Volume "
                "| Select-Object DriveLetter, FileSystemLabel, "
                "FileSystemType, SizeRemaining, Size, HealthStatus, "
                "FileSystem "
                "| ConvertTo-Json -Compress -Depth 5"
            ),
        )
        run_result = self._runner.run(
            list(get_volume_command), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        parsed = _parse_powershell_json(text=run_result.stdout)
        if parsed is None:
            rtnval = []
            return rtnval
        volume_records = _normalize_to_list(parsed=parsed)

        virtual_disks = self._list_virtual_disks()
        rtnval = _build_volumes_from_records(
            volume_records=volume_records,
            virtual_disks=virtual_disks,
        )
        return rtnval

    def get_volume_info(
        self,
        volume_id: str,
    ) -> Optional[WindowsVolumeInfo]:
        """
            Return the :class:`WindowsVolumeInfo` matching
            ``volume_id``, or ``None`` when no such volume exists.

            :param volume_id: Drive letter (``"X"`` or ``"X:"``) or
                              virtual-disk friendly name.
        """
        all_volumes = self.list_volumes()
        bare = _normalise_drive_letter(mountpoint=volume_id)
        rtnval: Optional[WindowsVolumeInfo] = None
        for volume in all_volumes:
            if volume.id == volume_id:
                rtnval = volume
                break
            if volume.id == bare:
                rtnval = volume
                break
        return rtnval

    def get_disk_info(
        self,
        disk_id: str,
    ) -> Optional[WindowsDiskInfo]:
        """
            Return the :class:`WindowsDiskInfo` matching ``disk_id``,
            or ``None`` when no such disk exists.

            :param disk_id: Disk number as a string.
        """
        all_disks = self.list_disks()
        rtnval: Optional[WindowsDiskInfo] = None
        for disk in all_disks:
            if disk.id == disk_id:
                rtnval = disk
                break
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — volumes
    # ------------------------------------------------------------------

    def create_volume(
        self,
        spec: WindowsVolumeSpec,
    ) -> WindowsStorageActionResult:
        """
            Create a volume.  When ``spec.pool`` is set the
            storage-spaces flow runs ``New-Volume
            -StoragePoolFriendlyName <pool> -FriendlyName <name> -Size
            <bytes> -ResiliencySettingName <r> -FileSystem <fs>``.
            When ``spec.pool`` is ``None`` the partition flow runs
            ``New-Partition -DiskNumber <n> -Size <bytes>
            -AssignDriveLetter`` followed by ``Format-Volume
            -DriveLetter <letter> -FileSystem <fs> -NewFileSystemLabel
            <name>``.

            Idempotent: when a volume with the same identity (drive
            letter or pool/friendly-name) already exists with a
            matching size (within :data:`_RESIZE_TOLERANCE_BYTES`),
            returns ``SKIPPED``.
        """
        if spec.pool is not None and len(spec.pool) > 0:
            rtnval = self._create_volume_pool_flow(spec=spec)
        else:
            rtnval = self._create_volume_partition_flow(spec=spec)
        return rtnval

    def delete_volume(
        self,
        volume_id: str,
    ) -> WindowsStorageActionResult:
        """
            Delete a volume.  When ``volume_id`` looks like a drive
            letter the partition flow dispatches ``Get-Partition
            -DriveLetter <letter> | Remove-Partition -Confirm:$false``.
            Otherwise the storage-spaces flow dispatches
            ``Remove-VirtualDisk -FriendlyName <id> -Confirm:$false``.

            Idempotent: when no matching volume exists, returns
            ``SKIPPED``.  On ``SUCCESS`` the result's ``message``
            carries a data-loss advisory.
        """
        if _looks_like_drive_letter(value=volume_id):
            rtnval = self._delete_volume_partition_flow(volume_id=volume_id)
        else:
            rtnval = self._delete_volume_virtualdisk_flow(
                volume_id=volume_id,
            )
        return rtnval

    def resize_volume(
        self,
        volume_id: str,
        size_bytes: int,
    ) -> WindowsStorageActionResult:
        """
            Resize a volume via ``Resize-Partition -DriveLetter
            <letter> -Size <bytes>``.

            Idempotent: when the partition's current size is within
            :data:`_RESIZE_TOLERANCE_BYTES` of ``size_bytes``, returns
            ``SKIPPED``.

            Currently only the partition / drive-letter flow is
            supported; resizing a Storage-Spaces virtual disk by
            friendly name is reserved for a follow-up batch (see
            ``tasks.md``).
        """
        bare = _normalise_drive_letter(mountpoint=volume_id)
        normalised = _format_drive_letter_token(drive_name=bare)
        escaped_drive = _ps_escape_single_quoted(value=bare)
        script = (
            "Resize-Partition -DriveLetter '{}' -Size {}"
        ).format(escaped_drive, size_bytes)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=normalised,
                volume=normalised,
            )
            return dry

        existing = self.get_volume_info(volume_id=bare)
        if existing is None:
            failed = self._build_result(
                status=WindowsStorageActionStatus.FAILED,
                command=command,
                target=normalised,
                volume=normalised,
                message="volume {} does not exist".format(normalised),
                stderr="missing volume",
            )
            return failed

        size_delta = abs(existing.size_bytes - size_bytes)
        if size_delta <= _RESIZE_TOLERANCE_BYTES:
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=normalised,
                volume=normalised,
                message=(
                    "volume {} already at {} bytes".format(
                        normalised, existing.size_bytes,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=normalised,
            volume=normalised,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — pools
    # ------------------------------------------------------------------

    def create_pool(
        self,
        spec: WindowsPoolSpec,
    ) -> WindowsStorageActionResult:
        """
            Create a Storage Pool via ``New-StoragePool
            -FriendlyName <name>
            -StorageSubSystemFriendlyName 'Windows Storage*'
            -PhysicalDisks (Get-PhysicalDisk -CanPool $True | Where-Object
            Number -in @(<numbers>))``.

            Idempotent: when a pool with the same friendly name already
            exists, returns ``SKIPPED``.
        """
        config = self._typed_config()
        subsystem = config.default_subsystem
        escaped_name = _ps_escape_single_quoted(value=spec.name)
        escaped_subsystem = _ps_escape_single_quoted(value=subsystem)
        disks_token = _render_disk_number_list(disks=spec.disks)
        script = (
            "New-StoragePool "
            "-FriendlyName '{}' "
            "-StorageSubSystemFriendlyName '{}' "
            "-PhysicalDisks (Get-PhysicalDisk -CanPool $True | "
            "Where-Object Number -in @({}))"
        ).format(escaped_name, escaped_subsystem, disks_token)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                pool=spec.name,
            )
            return dry

        if self._pool_exists(pool_name=spec.name):
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=spec.name,
                pool=spec.name,
                message="storage pool {} already exists".format(spec.name),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=spec.name,
            pool=spec.name,
        )
        return rtnval

    def delete_pool(
        self,
        pool_name: str,
    ) -> WindowsStorageActionResult:
        """
            Delete a Storage Pool via ``Remove-StoragePool
            -FriendlyName <name> -Confirm:$false``.

            Idempotent: when no pool with that name exists, returns
            ``SKIPPED``.  On ``SUCCESS`` the result's ``message``
            carries a data-loss advisory.
        """
        escaped_name = _ps_escape_single_quoted(value=pool_name)
        script = (
            "Remove-StoragePool -FriendlyName '{}' -Confirm:$false"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=pool_name,
                pool=pool_name,
            )
            return dry

        if not self._pool_exists(pool_name=pool_name):
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=pool_name,
                pool=pool_name,
                message="storage pool {} does not exist".format(pool_name),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=pool_name,
            pool=pool_name,
        )
        if result.status == WindowsStorageActionStatus.SUCCESS:
            advisory = (
                "storage pool {} was removed; every virtual disk in "
                "the pool has been destroyed".format(pool_name)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def add_to_pool(
        self,
        pool_name: str,
        disk_id: str,
    ) -> WindowsStorageActionResult:
        """
            Add a physical disk to a Storage Pool via
            ``Add-PhysicalDisk -StoragePoolFriendlyName <pool>
            -PhysicalDisks (Get-PhysicalDisk -SerialNumber ... | ...)``.
            ``disk_id`` is interpreted as a disk number (the
            Get-Disk-derived id); the script resolves to a
            PhysicalDisk via ``Get-PhysicalDisk | Where-Object Number
            -eq <n>``.

            Idempotent: when the disk is already a member of
            ``pool_name``, returns ``SKIPPED``.
        """
        escaped_pool = _ps_escape_single_quoted(value=pool_name)
        script = (
            "Add-PhysicalDisk "
            "-StoragePoolFriendlyName '{}' "
            "-PhysicalDisks (Get-PhysicalDisk | Where-Object "
            "Number -eq {})"
        ).format(escaped_pool, _disk_number_token(disk_id=disk_id))
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=disk_id,
                pool=pool_name,
            )
            return dry

        if self._disk_in_pool(pool_name=pool_name, disk_id=disk_id):
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=disk_id,
                pool=pool_name,
                message=(
                    "disk {} is already a member of pool {}".format(
                        disk_id, pool_name,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=disk_id,
            pool=pool_name,
        )
        return rtnval

    def remove_from_pool(
        self,
        pool_name: str,
        disk_id: str,
    ) -> WindowsStorageActionResult:
        """
            Remove a physical disk from a Storage Pool via
            ``Remove-PhysicalDisk -StoragePoolFriendlyName <pool>
            -PhysicalDisks (...) -Confirm:$false``.

            Idempotent: when the disk is not a member of ``pool_name``,
            returns ``SKIPPED``.  On ``SUCCESS`` the result's
            ``message`` carries a data-loss advisory.
        """
        escaped_pool = _ps_escape_single_quoted(value=pool_name)
        script = (
            "Remove-PhysicalDisk "
            "-StoragePoolFriendlyName '{}' "
            "-PhysicalDisks (Get-PhysicalDisk | Where-Object "
            "Number -eq {}) "
            "-Confirm:$false"
        ).format(escaped_pool, _disk_number_token(disk_id=disk_id))
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=disk_id,
                pool=pool_name,
            )
            return dry

        if not self._disk_in_pool(pool_name=pool_name, disk_id=disk_id):
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=disk_id,
                pool=pool_name,
                message=(
                    "disk {} is not a member of pool {}".format(
                        disk_id, pool_name,
                    )
                ),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=disk_id,
            pool=pool_name,
        )
        if result.status == WindowsStorageActionStatus.SUCCESS:
            advisory = (
                "disk {} was removed from storage pool {}; the "
                "physical-disk association has been wiped".format(
                    disk_id, pool_name,
                )
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Internal helpers — create_volume dispatch
    # ------------------------------------------------------------------

    def _create_volume_pool_flow(
        self,
        *,
        spec: WindowsVolumeSpec,
    ) -> WindowsStorageActionResult:
        """
            Carve a volume out of an existing Storage Pool via
            ``New-Volume -StoragePoolFriendlyName <pool>
            -FriendlyName <name> -Size <bytes>
            -ResiliencySettingName <r> -FileSystem <fs>``.
        """
        config = self._typed_config()
        if spec.resiliency is not None and len(spec.resiliency) > 0:
            resiliency_value = spec.resiliency
        else:
            resiliency_value = config.default_resiliency
        escaped_pool = _ps_escape_single_quoted(value=spec.pool)
        escaped_name = _ps_escape_single_quoted(value=spec.name)
        escaped_resiliency = _ps_escape_single_quoted(
            value=resiliency_value,
        )
        escaped_filesystem = _ps_escape_single_quoted(
            value=spec.filesystem,
        )
        script = (
            "New-Volume "
            "-StoragePoolFriendlyName '{}' "
            "-FriendlyName '{}' "
            "-Size {} "
            "-ResiliencySettingName '{}' "
            "-FileSystem '{}'"
        ).format(
            escaped_pool, escaped_name, spec.size_bytes,
            escaped_resiliency, escaped_filesystem,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                pool=spec.pool,
                volume=spec.name,
            )
            return dry

        existing = self._lookup_virtual_disk_by_name(name=spec.name)
        if existing is not None:
            existing_size = _coerce_int(raw_value=existing.get("Size", 0))
            size_delta = abs(existing_size - spec.size_bytes)
            if size_delta <= _RESIZE_TOLERANCE_BYTES:
                skipped = self._build_result(
                    status=WindowsStorageActionStatus.SKIPPED,
                    command=command,
                    target=spec.name,
                    pool=spec.pool,
                    volume=spec.name,
                    message=(
                        "virtual disk {} already exists in pool "
                        "{}".format(spec.name, spec.pool)
                    ),
                )
                return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=spec.name,
            pool=spec.pool,
            volume=spec.name,
        )
        return rtnval

    def _create_volume_partition_flow(
        self,
        *,
        spec: WindowsVolumeSpec,
    ) -> WindowsStorageActionResult:
        """
            Carve a partition out of a plain disk via ``New-Partition
            -DiskNumber <n> -Size <bytes> -AssignDriveLetter |
            Format-Volume -FileSystem <fs> -NewFileSystemLabel
            <name>``.
        """
        if spec.disk_id is None or len(spec.disk_id) == 0:
            failed = self._build_result(
                status=WindowsStorageActionStatus.FAILED,
                command=[],
                target=spec.name,
                volume=spec.name,
                message=(
                    "WindowsVolumeSpec.disk_id is required when pool "
                    "is None (partition flow)"
                ),
                stderr="missing disk_id",
            )
            return failed

        escaped_name = _ps_escape_single_quoted(value=spec.name)
        escaped_filesystem = _ps_escape_single_quoted(
            value=spec.filesystem,
        )
        script = (
            "New-Partition -DiskNumber {} -Size {} "
            "-AssignDriveLetter | Format-Volume "
            "-FileSystem '{}' -NewFileSystemLabel '{}' "
            "-Confirm:$false"
        ).format(
            _disk_number_token(disk_id=spec.disk_id),
            spec.size_bytes,
            escaped_filesystem,
            escaped_name,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                volume=spec.name,
            )
            return dry

        rtnval = self._dispatch_storage_write(
            command=command,
            target=spec.name,
            volume=spec.name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — delete_volume dispatch
    # ------------------------------------------------------------------

    def _delete_volume_partition_flow(
        self,
        *,
        volume_id: str,
    ) -> WindowsStorageActionResult:
        """
            Delete a partition-style volume via ``Get-Partition
            -DriveLetter <letter> | Remove-Partition -Confirm:$false``.
        """
        bare = _normalise_drive_letter(mountpoint=volume_id)
        normalised = _format_drive_letter_token(drive_name=bare)
        escaped_drive = _ps_escape_single_quoted(value=bare)
        script = (
            "Get-Partition -DriveLetter '{}' "
            "| Remove-Partition -Confirm:$false"
        ).format(escaped_drive)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=normalised,
                volume=normalised,
            )
            return dry

        existing = self.get_volume_info(volume_id=bare)
        if existing is None:
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=normalised,
                volume=normalised,
                message="volume {} does not exist".format(normalised),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=normalised,
            volume=normalised,
        )
        if result.status == WindowsStorageActionStatus.SUCCESS:
            advisory = (
                "volume {} was removed; all data on the volume has "
                "been destroyed".format(normalised)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def _delete_volume_virtualdisk_flow(
        self,
        *,
        volume_id: str,
    ) -> WindowsStorageActionResult:
        """
            Delete a Storage-Spaces virtual disk by friendly name via
            ``Remove-VirtualDisk -FriendlyName <id> -Confirm:$false``.
        """
        escaped_name = _ps_escape_single_quoted(value=volume_id)
        script = (
            "Remove-VirtualDisk -FriendlyName '{}' -Confirm:$false"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsStorageActionStatus.DRY_RUN,
                command=command,
                target=volume_id,
                volume=volume_id,
            )
            return dry

        existing = self._lookup_virtual_disk_by_name(name=volume_id)
        if existing is None:
            skipped = self._build_result(
                status=WindowsStorageActionStatus.SKIPPED,
                command=command,
                target=volume_id,
                volume=volume_id,
                message=(
                    "virtual disk {} does not exist".format(volume_id)
                ),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=volume_id,
            volume=volume_id,
        )
        if result.status == WindowsStorageActionStatus.SUCCESS:
            advisory = (
                "virtual disk {} was removed; all data on the "
                "underlying volumes has been destroyed".format(
                    volume_id,
                )
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Internal helpers — probes
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsStorageManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`WindowsStorageManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, WindowsStorageManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsStorageManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv used by the manager.
        """
        config = self._typed_config()
        rtnval = [
            config.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _list_virtual_disks(self) -> list:
        """
            Probe ``Get-VirtualDisk`` for the friendly-name → pool /
            size mapping used to attribute volumes to pools.
        """
        command = self._build_powershell_argv(
            script=(
                "Get-VirtualDisk "
                "| Select-Object FriendlyName, Size, "
                "ResiliencySettingName, HealthStatus, "
                "@{Name='StoragePool';Expression={"
                "(Get-StoragePool -Name "
                "(Get-VirtualDiskToStoragePoolName "
                "$_.FriendlyName))}} "
                "| ConvertTo-Json -Compress -Depth 5"
            ),
        )
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        parsed = _parse_powershell_json(text=run_result.stdout)
        if parsed is None:
            rtnval = []
            return rtnval
        rtnval = _normalize_to_list(parsed=parsed)
        return rtnval

    def _lookup_virtual_disk_by_name(self, *, name: str) -> Optional[dict]:
        """
            Return the ``Get-VirtualDisk`` record matching ``name``, or
            ``None``.
        """
        all_records = self._list_virtual_disks()
        rtnval: Optional[dict] = None
        for record in all_records:
            if not isinstance(record, dict):
                continue
            if record.get("FriendlyName") == name:
                rtnval = record
                break
        return rtnval

    def _pool_exists(self, *, pool_name: str) -> bool:
        """
            Return True when ``Get-StoragePool`` reports a pool with
            the given friendly name.
        """
        escaped_name = _ps_escape_single_quoted(value=pool_name)
        script = (
            "Get-StoragePool -FriendlyName '{}' -ErrorAction "
            "SilentlyContinue "
            "| Select-Object FriendlyName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        parsed = _parse_powershell_json(text=run_result.stdout)
        if parsed is None:
            rtnval = False
            return rtnval
        records = _normalize_to_list(parsed=parsed)
        rtnval = False
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("FriendlyName") == pool_name:
                rtnval = True
                break
        return rtnval

    def _disk_in_pool(self, *, pool_name: str, disk_id: str) -> bool:
        """
            Return True when ``Get-PhysicalDisk
            -StoragePoolFriendlyName <pool>`` reports a disk with the
            given number.
        """
        escaped_pool = _ps_escape_single_quoted(value=pool_name)
        script = (
            "Get-PhysicalDisk "
            "-StoragePoolFriendlyName '{}' "
            "| Select-Object Number "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped_pool)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        parsed = _parse_powershell_json(text=run_result.stdout)
        if parsed is None:
            rtnval = False
            return rtnval
        records = _normalize_to_list(parsed=parsed)
        target_number = _disk_number_token(disk_id=disk_id)
        rtnval = False
        for record in records:
            if not isinstance(record, dict):
                continue
            number_field = record.get("Number")
            if number_field is None:
                continue
            try:
                rec_number = int(number_field)
            except (TypeError, ValueError):
                continue
            if str(rec_number) == target_number:
                rtnval = True
                break
        return rtnval

    def _collect_pool_member_disk_numbers(self) -> set:
        """
            Return the set of disk numbers currently associated with a
            Storage Pool.  Derived from ``Get-PhysicalDisk
            -CanPool $false``: any disk that ``CanPool=False`` AND has
            a non-empty ``StoragePool`` association is treated as a
            pool member.  We use the simpler ``Get-PhysicalDisk |
            Select-Object Number, CanPool`` shape because the
            cross-pool join is brittle on older PowerShell versions.
        """
        command = self._build_powershell_argv(
            script=(
                "Get-PhysicalDisk "
                "| Select-Object Number, CanPool "
                "| ConvertTo-Json -Compress -Depth 5"
            ),
        )
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: set = set()
            return rtnval
        parsed = _parse_powershell_json(text=run_result.stdout)
        if parsed is None:
            rtnval = set()
            return rtnval
        records = _normalize_to_list(parsed=parsed)
        rtnval = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            number_field = record.get("Number")
            can_pool = record.get("CanPool")
            if number_field is None:
                continue
            if can_pool is True:
                # CanPool=True means the disk is NOT yet in a pool.
                continue
            try:
                rec_number = int(number_field)
            except (TypeError, ValueError):
                continue
            rtnval.add(str(rec_number))
        return rtnval

    def _dispatch_storage_write(
        self,
        *,
        command: list,
        target: Optional[str] = None,
        pool: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> WindowsStorageActionResult:
        """
            Dispatch a write command and wrap the result with the
            relevant storage-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="storage_write",
            target=target,
            pool=pool,
            volume=volume,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsStorageActionStatus,
        command: list,
        target: Optional[str] = None,
        pool: Optional[str] = None,
        volume: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsStorageActionResult:
        """
            Construct a :class:`WindowsStorageActionResult` without
            invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = target
        rtnval = WindowsStorageActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            target=target,
            pool=pool,
            volume=volume,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level construction helpers
# ----------------------------------------------------------------------


def _disk_number_token(*, disk_id: str) -> str:
    """
        Render a disk-id string as a PowerShell-safe number token.
        Strips whitespace; falls back to the raw value when the input
        is not numeric (the resulting cmdlet will fail at runtime, but
        the failure surfaces normally as a FAILED result).
    """
    text = disk_id.strip()
    if len(text) == 0:
        rtnval = "0"
        return rtnval
    try:
        rtnval = str(int(text))
    except ValueError:
        rtnval = text
    return rtnval


def _render_disk_number_list(*, disks: list) -> str:
    """
        Render a list of disk-id strings as a PowerShell array
        literal: ``"0,1,2"``.
    """
    tokens: list = []
    for disk_id in disks:
        token = _disk_number_token(disk_id=disk_id)
        tokens.append(token)
    rtnval = ",".join(tokens)
    return rtnval


def _build_disks_from_records(
    *,
    records: list,
    pool_member_numbers: set,
) -> list:
    """
        Build a list of :class:`WindowsDiskInfo` from
        ``Get-Disk | ConvertTo-Json`` records.

        :param records: Normalised list of dicts.
        :param pool_member_numbers: Disk numbers (as strings) that are
                                    currently pool members.
    """
    rtnval: list = []
    for record in records:
        if not isinstance(record, dict):
            continue
        number_raw = record.get("Number")
        if number_raw is None:
            continue
        try:
            disk_number = int(number_raw)
        except (TypeError, ValueError):
            continue
        disk_id = str(disk_number)
        size_bytes = _coerce_int(raw_value=record.get("Size", 0))
        friendly = record.get("FriendlyName", disk_id)
        if isinstance(friendly, str) and len(friendly) > 0:
            name_value = friendly
        else:
            name_value = disk_id

        model_raw = record.get("Model")
        if isinstance(model_raw, str) and len(model_raw) > 0:
            model_value: Optional[str] = model_raw
        else:
            model_value = None

        serial_raw = record.get("SerialNumber")
        if isinstance(serial_raw, str) and len(serial_raw) > 0:
            serial_value: Optional[str] = serial_raw
        else:
            serial_value = None

        partition_style_raw = record.get("PartitionStyle")
        if (
            isinstance(partition_style_raw, str)
            and len(partition_style_raw) > 0
        ):
            partition_style: Optional[str] = partition_style_raw
        else:
            partition_style = None

        health_raw = record.get("HealthStatus")
        if isinstance(health_raw, str) and len(health_raw) > 0:
            health_value: Optional[str] = health_raw
        else:
            health_value = None

        is_member = disk_id in pool_member_numbers

        info = WindowsDiskInfo(
            id=disk_id,
            name=name_value,
            size_bytes=size_bytes,
            model=model_value,
            serial=serial_value,
            partition_style=partition_style,
            health_status=health_value,
            mount_points=[],
            is_pool_member=is_member,
        )
        rtnval.append(info)
    return rtnval


def _build_volumes_from_records(
    *,
    volume_records: list,
    virtual_disks: list,
) -> list:
    """
        Build a list of :class:`WindowsVolumeInfo` from ``Get-Volume``
        records, attributing pool ownership via the
        ``virtual_disks`` table.

        :param volume_records: ``Get-Volume | ConvertTo-Json`` records.
        :param virtual_disks: ``Get-VirtualDisk | ConvertTo-Json``
                              records (used to infer pool ownership).
    """
    rtnval: list = []
    by_friendly_name: dict = {}
    for vd in virtual_disks:
        if not isinstance(vd, dict):
            continue
        friendly = vd.get("FriendlyName")
        if isinstance(friendly, str) and len(friendly) > 0:
            by_friendly_name[friendly] = vd

    for record in volume_records:
        if not isinstance(record, dict):
            continue
        drive_letter_raw = record.get("DriveLetter")
        label = record.get("FileSystemLabel")
        if drive_letter_raw is not None and len(str(drive_letter_raw)) > 0:
            drive_letter = str(drive_letter_raw)
            volume_id = drive_letter
            mount_point: Optional[str] = _format_drive_letter_token(
                drive_name=drive_letter,
            )
        else:
            if isinstance(label, str) and len(label) > 0:
                volume_id = label
            else:
                continue
            mount_point = None

        if isinstance(label, str) and len(label) > 0:
            name_value = label
        else:
            name_value = volume_id

        size_bytes = _coerce_int(raw_value=record.get("Size", 0))
        size_remaining = _coerce_optional_int(
            raw_value=record.get("SizeRemaining"),
        )
        if size_remaining is not None:
            used_bytes: Optional[int] = max(0, size_bytes - size_remaining)
        else:
            used_bytes = None

        fstype_raw = record.get("FileSystemType")
        if fstype_raw is None:
            fstype_raw = record.get("FileSystem")
        if isinstance(fstype_raw, str) and len(fstype_raw) > 0:
            filesystem_value: Optional[str] = fstype_raw
        else:
            filesystem_value = None

        health_raw = record.get("HealthStatus")
        if isinstance(health_raw, str) and len(health_raw) > 0:
            health_value: Optional[str] = health_raw
        else:
            health_value = None

        # Pool attribution: volumes created via New-Volume from a pool
        # carry their friendly name as ``FileSystemLabel``; if that name
        # matches a Get-VirtualDisk row we attribute the pool.
        pool_value: Optional[str] = None
        candidate_name = label if isinstance(label, str) else None
        if candidate_name is not None and candidate_name in by_friendly_name:
            virtual_disk = by_friendly_name[candidate_name]
            pool_field = virtual_disk.get("StoragePool")
            if isinstance(pool_field, dict):
                pool_value = pool_field.get("FriendlyName")
            elif isinstance(pool_field, str):
                pool_value = pool_field

        info = WindowsVolumeInfo(
            id=volume_id,
            name=name_value,
            size_bytes=size_bytes,
            used_bytes=used_bytes,
            filesystem=filesystem_value,
            mount_point=mount_point,
            pool=pool_value,
            health_status=health_value,
        )
        rtnval.append(info)
    return rtnval

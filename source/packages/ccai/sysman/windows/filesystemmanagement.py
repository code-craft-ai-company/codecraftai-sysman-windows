"""
    Windows filesystem-management feature.

    Implements the cross-OS filesystem-management contract for Windows
    using PowerShell file-system cmdlets (``New-PSDrive`` /
    ``Remove-PSDrive`` for runtime drive-letter mounts,
    ``Get-PSDrive`` / ``Get-Volume`` for enumeration, ``Get-Item`` for
    per-path attributes, ``Get-Acl`` / ``Set-Acl`` for ACL reads and
    owner writes) plus the legacy ``icacls.exe`` utility for ACL
    grants.  Every verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    File I/O policy:
        Mount-table state lives in volume / drive-letter assignments,
        not in a flat file.  This module does NOT consume an
        :class:`IFileOperator`; every read / write / probe flows
        through the runner so dry-run, elevation, and aspect
        composition compose uniformly.

    Elevation policy:
        Every write verb (``mount``, ``unmount``, ``chown``, ``chmod``)
        honours ``config.elevated``.  Reads (``get_mounts``, ``stat``,
        the idempotency probes) never elevate.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input.

    fstab semantics:
        Windows has no ``/etc/fstab`` analogue.  Persistent mounts on
        Windows are represented by drive-letter assignments
        (``Set-Partition -DriveLetter`` / ``mountvol``) and access
        paths (``Add-PartitionAccessPath``); both are first-class
        volume-configuration writes, not flat-file edits.
        :meth:`add_fstab_entry` and :meth:`remove_fstab_entry`
        therefore return ``FAILED`` with a descriptive ``message``
        documenting the deviation; cross-OS callers that need
        persistence on Windows should use :meth:`mount` directly with
        ``-Persist`` semantics or layer on a Windows-specific
        access-path verb when one is added in a follow-up.

    Mode-to-ACL mapping:
        Windows ACLs are far richer than POSIX modes; a perfect
        round-trip is impossible.  This module bundles a small
        translation table covering the canonical Linux modes
        (``0o600``, ``0o644``, ``0o700``, ``0o755``, ``0o775``,
        ``0o777``).  Modes outside that table return ``FAILED``;
        callers needing exact NTFS semantics should use a future
        ``set_acl`` verb (out of scope for this pilot).  See
        :func:`_mode_to_icacls_grants`.

    stat semantics:
        :meth:`stat` runs ``Get-Item`` for the size / modification
        timestamp and ``Get-Acl`` for the owner.  ``Get-Acl`` does not
        expose a single "primary group" the way POSIX does — Windows
        simply has many ACEs.  The :class:`WindowsFileInfo`
        ``group`` field is populated from the ACL's ``Group`` column
        when present (it is empty on most NTFS volumes) and is
        otherwise ``None``.  The ``mode`` field is approximated from
        the per-principal access mask: when no canonical mode reverse-
        translates from the observed ACL, ``mode`` is rendered as the
        principal-tagged ACL attribute string verbatim.
"""

import json
import os

from dataclasses import dataclass
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

# Partial mode-to-ACL mapping table.  Each entry maps a numeric mode to
# an ordered list of ``icacls /grant`` argument strings.  ``%OWNER%``
# is a placeholder substituted at dispatch time with the path's current
# owner (resolved via ``Get-Acl``).  Modes not listed here cause
# :meth:`WindowsFilesystemManager.chmod` to return ``FAILED`` with a
# descriptive ``message``.
_MODE_TO_ICACLS_GRANTS: dict = {
    0o600: ["%OWNER%:(R,W)"],
    0o644: ["%OWNER%:(R,W)", "Users:(R)"],
    0o700: ["%OWNER%:(F)"],
    0o755: ["%OWNER%:(F)", "Users:(RX)"],
    0o775: ["%OWNER%:(F)", "Users:(M)"],
    0o777: ["%OWNER%:(F)", "Users:(F)"],
}


WindowsFilesystemActionStatus = ActionStatus


@dataclass
class WindowsFilesystemManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsFilesystemManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable
                                      used to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-resolved
                                      Windows PowerShell 5.1).
    """
    powershell_executable: str = _DEFAULT_POWERSHELL


@dataclass
class WindowsFilesystemActionResult(FeatureActionResult):
    """
        Result of a filesystem-management write operation.

        :param path: The target path, when applicable.
        :param device: The device targeted, when applicable (e.g. a
                       UNC path or a disk image path).
        :param mountpoint: The mountpoint targeted, when applicable
                           (e.g. a drive-letter token like ``"X:"``).
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    device: Optional[str] = None
    mountpoint: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsFstabEntry:
    """
        Representation of an "fstab-style" persistent mount entry.

        Windows has no ``/etc/fstab`` analogue; this dataclass is kept
        for cross-OS-contract parity.  The :meth:`add_fstab_entry` /
        :meth:`remove_fstab_entry` verbs return ``FAILED`` and document
        the deviation.

        :param device: Device or UNC path.
        :param mountpoint: Drive letter or directory mountpoint.
        :param fstype: Filesystem type.
        :param options: Mount options.  Defaults to ``"rw"``.
    """
    device: str
    mountpoint: str
    fstype: str
    options: str = "rw"


@dataclass
class WindowsMountInfo:
    """
        Information about an active mount as enumerated by
        :meth:`WindowsFilesystemManager.get_mounts`.  Merged from
        ``Get-PSDrive`` and ``Get-Volume``.

        :param device: Underlying provider root (``DisplayRoot`` for
                       network shares, ``Root`` for local drives, the
                       volume's ``Path`` otherwise).
        :param mountpoint: Drive letter token (e.g. ``"X:"``) or
                           directory mountpoint.
        :param fstype: Filesystem type as reported by ``Get-Volume``;
                       ``None`` when only ``Get-PSDrive`` reported the
                       row.
        :param provider: PSDrive provider name (``"FileSystem"``,
                         etc.) or ``None``.
        :param size_bytes: Total size, or ``None`` when not reported.
        :param free_bytes: Free space, or ``None``.
    """
    device: str
    mountpoint: str
    fstype: Optional[str] = None
    provider: Optional[str] = None
    size_bytes: Optional[int] = None
    free_bytes: Optional[int] = None


@dataclass
class WindowsFileInfo:
    """
        Information returned by :meth:`WindowsFilesystemManager.stat`.

        :param path: The absolute path queried.
        :param owner: Owner principal name (``Get-Acl Owner`` field).
        :param group: Group principal name (``Get-Acl Group`` field) or
                      ``None`` (Windows ACLs commonly leave this
                      empty).
        :param mode: Approximate mode string.  When the observed ACL
                     reverse-translates to a canonical mode in
                     :data:`_MODE_TO_ICACLS_GRANTS`, the value is
                     rendered as ``"0oNNN"``.  Otherwise the raw
                     ``Get-Acl`` ``AccessToString`` value is returned
                     verbatim.
        :param size: Size in bytes (``Get-Item Length``); ``0`` for
                     directories.
        :param modified: Modification timestamp string
                         (``Get-Item LastWriteTime``).
    """
    path: str
    owner: str
    group: Optional[str]
    mode: str
    size: int
    modified: str


class WindowsFilesystemQueryError(Exception):
    """
        Raised when a filesystem read fails for a reason other than the
        target simply being absent.
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
            :param message: Optional pre-built message for cases where
                            there is no failed argv to report.
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
            err_msg_lines = ["Filesystem query command failed."]
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
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _normalize_to_list(*, parsed):
    """
        Normalise a ``ConvertTo-Json`` payload into a list.  See the
        sibling helper in :mod:`networkconfiguration` for the rationale
        (ConvertTo-Json drops the array wrapper for one-element
        pipelines).
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _coerce_int(*, raw_value) -> Optional[int]:
    """
        Best-effort coercion of a parsed JSON field to int.

        :param raw_value: The raw value.

        :returns: The integer, or ``None`` when not coercible.
    """
    if raw_value is None:
        rtnval: Optional[int] = None
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


def _normalise_drive_letter(*, mountpoint: str) -> str:
    """
        Strip a single trailing colon and any following characters from
        a mountpoint to obtain the bare drive-letter name expected by
        ``-Name`` parameters of ``New-PSDrive`` / ``Remove-PSDrive``.

        ``"X:"`` -> ``"X"``; ``"X:\\"`` -> ``"X"``; ``"X"`` ->
        ``"X"``.

        :param mountpoint: The caller-supplied mountpoint.

        :returns: The bare drive-letter name (or the mountpoint
                  unchanged when no colon is present).
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

        :param drive_name: The bare letter (``"X"``).

        :returns: ``"<letter>:"``.
    """
    rtnval = "{}:".format(drive_name)
    return rtnval


def _mode_to_icacls_grants(*, mode_int: int) -> Optional[list[str]]:
    """
        Translate a numeric POSIX mode into a list of ``icacls /grant``
        argument strings.

        Only a small set of canonical modes are supported (see
        :data:`_MODE_TO_ICACLS_GRANTS`).  Unsupported modes return
        ``None`` so the caller can surface a typed FAILED result.

        :param mode_int: The integer mode (e.g. ``0o644``).

        :returns: A list of grant strings (``"%OWNER%:(R,W)"`` etc.),
                  or ``None``.
    """
    if mode_int not in _MODE_TO_ICACLS_GRANTS:
        rtnval: Optional[list[str]] = None
        return rtnval
    rtnval = list(_MODE_TO_ICACLS_GRANTS[mode_int])
    return rtnval


def _coerce_mode_to_int(*, mode) -> Optional[int]:
    """
        Coerce a caller-supplied mode value into an integer, accepting
        either a Python int or an octal-style string (``"0644"`` /
        ``"0o644"`` / ``"644"``).

        :param mode: The caller-supplied mode value.

        :returns: The integer mode, or ``None`` when unparseable.
    """
    if mode is None:
        rtnval: Optional[int] = None
        return rtnval
    if isinstance(mode, int):
        rtnval = mode
        return rtnval
    if isinstance(mode, str):
        text = mode.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval
        if text.startswith("0o") or text.startswith("0O"):
            try:
                rtnval = int(text[2:], 8)
            except ValueError:
                rtnval = None
            return rtnval
        if text.startswith("0") and len(text) > 1:
            try:
                rtnval = int(text, 8)
            except ValueError:
                rtnval = None
            return rtnval
        try:
            rtnval = int(text, 8)
        except ValueError:
            rtnval = None
        return rtnval
    rtnval = None
    return rtnval


def _parse_get_psdrive(*, text: str) -> list[dict]:
    """
        Parse the JSON output of ``Get-PSDrive | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: A list of dicts (bare-object shape normalised).
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: list[dict] = []
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = []
        return rtnval
    rtnval = _normalize_to_list(parsed=parsed)
    return rtnval


def _parse_get_volume(*, text: str) -> list[dict]:
    """
        Parse the JSON output of ``Get-Volume | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: A list of dicts (bare-object shape normalised).
    """
    rtnval = _parse_get_psdrive(text=text)
    return rtnval


def _parse_get_acl(*, text: str) -> Optional[dict]:
    """
        Parse the JSON output of ``Get-Acl | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: The parsed dict, or ``None``.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: Optional[dict] = None
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = None
        return rtnval
    if isinstance(parsed, list):
        if len(parsed) == 0:
            rtnval = None
            return rtnval
        first = parsed[0]
        if isinstance(first, dict):
            rtnval = first
            return rtnval
        rtnval = None
        return rtnval
    if isinstance(parsed, dict):
        rtnval = parsed
        return rtnval
    rtnval = None
    return rtnval


def _parse_get_item(*, text: str) -> Optional[dict]:
    """
        Parse the JSON output of ``Get-Item | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: The parsed dict, or ``None``.
    """
    rtnval = _parse_get_acl(text=text)
    return rtnval


def _merge_psdrive_and_volume(
    *,
    psdrive_records: list[dict],
    volume_records: list[dict],
) -> list[WindowsMountInfo]:
    """
        Merge ``Get-PSDrive`` and ``Get-Volume`` rows into
        :class:`WindowsMountInfo` rows keyed on the drive-letter token.

        Drives reported only by ``Get-PSDrive`` (network shares, etc.)
        carry ``fstype=None``.  Volumes reported only by ``Get-Volume``
        (volumes without an assigned drive letter) are omitted from the
        merge — :meth:`WindowsFilesystemManager.get_mounts` returns
        only entries the caller can address as a mountpoint.

        :param psdrive_records: Output of :func:`_parse_get_psdrive`.
        :param volume_records: Output of :func:`_parse_get_volume`.

        :returns: One :class:`WindowsMountInfo` per addressable mount.
    """
    volume_by_letter: dict = {}
    for record in volume_records:
        if not isinstance(record, dict):
            continue
        letter_raw = record.get("DriveLetter")
        if letter_raw is None:
            continue
        letter_text = str(letter_raw)
        if len(letter_text) == 0:
            continue
        volume_by_letter[letter_text.upper()] = record

    rtnval: list[WindowsMountInfo] = []
    for record in psdrive_records:
        if not isinstance(record, dict):
            continue
        provider_raw = record.get("Provider")
        if isinstance(provider_raw, dict):
            # Some PowerShell versions serialize Provider as a
            # ProviderInfo dict.
            provider_value: Optional[str] = str(
                provider_raw.get("Name", "FileSystem"),
            )
        elif provider_raw is None:
            provider_value = None
        else:
            provider_value = str(provider_raw)

        # Skip non-FileSystem PSDrives (Registry, Variable, etc.).
        if (
            provider_value is not None
            and provider_value.lower() not in ("filesystem",)
            and provider_value != "Microsoft.PowerShell.Core\\FileSystem"
        ):
            continue

        name_raw = record.get("Name")
        if name_raw is None:
            continue
        name_text = str(name_raw)
        if len(name_text) == 0:
            continue
        mountpoint = _format_drive_letter_token(drive_name=name_text)

        display_root = record.get("DisplayRoot")
        if display_root is None or len(str(display_root)) == 0:
            root_raw = record.get("Root")
            if root_raw is None:
                device_value = mountpoint
            else:
                device_value = str(root_raw)
        else:
            device_value = str(display_root)

        size_value = _coerce_int(raw_value=record.get("Used"))
        free_value = _coerce_int(raw_value=record.get("Free"))
        # ``Used`` + ``Free`` approximates total size; record total
        # when both halves are present.
        if size_value is not None and free_value is not None:
            total_size: Optional[int] = size_value + free_value
        else:
            total_size = None

        fstype_value: Optional[str] = None
        volume_record = volume_by_letter.get(name_text.upper())
        if volume_record is not None:
            fstype_raw = volume_record.get("FileSystemType")
            if fstype_raw is None:
                fstype_raw = volume_record.get("FileSystem")
            if fstype_raw is not None and len(str(fstype_raw)) > 0:
                fstype_value = str(fstype_raw)

            # Prefer Get-Volume's Size / SizeRemaining when available.
            volume_size = _coerce_int(raw_value=volume_record.get("Size"))
            if volume_size is not None:
                total_size = volume_size
            volume_free = _coerce_int(
                raw_value=volume_record.get("SizeRemaining"),
            )
            if volume_free is not None:
                free_value = volume_free

        info = WindowsMountInfo(
            device=device_value,
            mountpoint=mountpoint,
            fstype=fstype_value,
            provider=provider_value,
            size_bytes=total_size,
            free_bytes=free_value,
        )
        rtnval.append(info)

    return rtnval


# ----------------------------------------------------------------------
# WindowsFilesystemManager
# ----------------------------------------------------------------------


class WindowsFilesystemManager(BaseFeatureManager):
    """
        Windows filesystem manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`.  Mount-table state lives
        in volume / drive-letter assignments rather than a flat file,
        so :meth:`add_fstab_entry` / :meth:`remove_fstab_entry` return
        ``FAILED`` with a documenting message.
    """

    result_class = WindowsFilesystemActionResult
    status_class = WindowsFilesystemActionStatus
    feature_name = "filesystem-management"

    # ------------------------------------------------------------------
    # Mount write verbs
    # ------------------------------------------------------------------

    def mount(
        self,
        device: str,
        mountpoint: str,
        fstype: str,
        options: Optional[str] = None,
    ) -> WindowsFilesystemActionResult:
        """
            Mount ``device`` at ``mountpoint`` via ``New-PSDrive``.

            ``mountpoint`` is interpreted as a drive-letter token
            (``"X:"``).  ``device`` is interpreted as a path or UNC
            (e.g. ``"\\\\server\\share"`` or ``"C:\\Users"``).  The
            ``fstype`` and ``options`` parameters are recorded onto the
            result for cross-OS-contract parity but are not consumed by
            ``New-PSDrive``: the cmdlet's filesystem provider is
            auto-detected from the ``-Root`` value, and persistence is
            requested with ``-Persist``.

            Idempotent: when ``Get-PSDrive`` already shows the same
            drive letter pointing at ``device``, returns ``SKIPPED``.

            :param device: The path or UNC to mount.
            :param mountpoint: The drive-letter token.
            :param fstype: Filesystem type (recorded for parity; not
                           consumed by ``New-PSDrive``).
            :param options: Mount options (recorded for parity).

            :returns: The :class:`WindowsFilesystemActionResult`.
        """
        drive_name = _normalise_drive_letter(mountpoint=mountpoint)
        normalised_mountpoint = _format_drive_letter_token(
            drive_name=drive_name,
        )
        escaped_drive = _ps_escape_single_quoted(value=drive_name)
        escaped_root = _ps_escape_single_quoted(value=device)
        script = (
            "New-PSDrive -Name '{}' -PSProvider FileSystem "
            "-Root '{}' -Persist"
        ).format(escaped_drive, escaped_root)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFilesystemActionStatus.DRY_RUN,
                command=command,
                path=normalised_mountpoint,
                device=device,
                mountpoint=normalised_mountpoint,
            )
            return dry

        if self._is_already_mounted(
            drive_name=drive_name, device=device,
        ):
            skipped = self._build_result(
                status=WindowsFilesystemActionStatus.SKIPPED,
                command=command,
                path=normalised_mountpoint,
                device=device,
                mountpoint=normalised_mountpoint,
                message="drive letter is already mapped to the same target",
            )
            return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=normalised_mountpoint,
            device=device,
            mountpoint=normalised_mountpoint,
        )
        return rtnval

    def unmount(
        self,
        mountpoint: str,
    ) -> WindowsFilesystemActionResult:
        """
            Unmount ``mountpoint`` via ``Remove-PSDrive -Name <letter>``
            (where ``<letter>`` is ``mountpoint`` with the trailing
            ``":"`` stripped).  Skipped when the drive letter is not
            currently mapped.

            :param mountpoint: The drive-letter token to unmount.

            :returns: The :class:`WindowsFilesystemActionResult`.
        """
        drive_name = _normalise_drive_letter(mountpoint=mountpoint)
        normalised_mountpoint = _format_drive_letter_token(
            drive_name=drive_name,
        )
        escaped_drive = _ps_escape_single_quoted(value=drive_name)
        script = "Remove-PSDrive -Name '{}'".format(escaped_drive)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFilesystemActionStatus.DRY_RUN,
                command=command,
                path=normalised_mountpoint,
                mountpoint=normalised_mountpoint,
            )
            return dry

        if not self._drive_letter_is_mapped(drive_name=drive_name):
            skipped = self._build_result(
                status=WindowsFilesystemActionStatus.SKIPPED,
                command=command,
                path=normalised_mountpoint,
                mountpoint=normalised_mountpoint,
                message="drive letter is not currently mapped",
            )
            return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=normalised_mountpoint,
            mountpoint=normalised_mountpoint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # fstab write verbs (always FAILED; documented deviation)
    # ------------------------------------------------------------------

    def add_fstab_entry(
        self,
        spec: WindowsFstabEntry,
    ) -> WindowsFilesystemActionResult:
        """
            Persistent-mount entry add.

            **Documented deviation.**  Windows has no ``/etc/fstab``
            analogue: persistent mounts are managed via
            ``Set-Partition -DriveLetter`` / ``Add-PartitionAccessPath``
            / ``mountvol``.  This method always returns ``FAILED`` with
            a descriptive ``message`` rather than silently no-op'ing or
            picking one of the underlying cmdlets, because the
            cross-OS-contract semantics of "fstab entry" do not map
            cleanly onto any of them.  Honours dry-run.

            :param spec: The :class:`WindowsFstabEntry` describing the
                         intended persistent mount.

            :returns: The :class:`WindowsFilesystemActionResult`.
        """
        # Synthesise a placeholder argv so the result still records
        # the caller's intent; the runner is never invoked.
        script = (
            "# Windows manages persistent mounts via Set-Partition / "
            "Add-PartitionAccessPath / mountvol; not analogous to "
            "/etc/fstab"
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFilesystemActionStatus.DRY_RUN,
                command=command,
                path=None,
                device=spec.device,
                mountpoint=spec.mountpoint,
            )
            return dry

        message = (
            "Windows manages persistent mounts via Set-Partition / "
            "Add-PartitionAccessPath / mountvol; there is no "
            "/etc/fstab analogue.  Use the mount() verb with -Persist "
            "semantics, or layer a Windows-specific access-path verb."
        )
        rtnval = self._build_result(
            status=WindowsFilesystemActionStatus.FAILED,
            command=command,
            path=None,
            device=spec.device,
            mountpoint=spec.mountpoint,
            message=message,
            stderr=message,
        )
        return rtnval

    def remove_fstab_entry(
        self,
        mountpoint: str,
    ) -> WindowsFilesystemActionResult:
        """
            Persistent-mount entry remove.

            **Documented deviation.**  See :meth:`add_fstab_entry` for
            rationale.  Always returns ``FAILED`` with a descriptive
            ``message``.  Honours dry-run.

            :param mountpoint: The mountpoint that would be removed
                               from a hypothetical fstab.

            :returns: The :class:`WindowsFilesystemActionResult`.
        """
        script = (
            "# Windows manages persistent mounts via Set-Partition / "
            "Add-PartitionAccessPath / mountvol; not analogous to "
            "/etc/fstab"
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFilesystemActionStatus.DRY_RUN,
                command=command,
                path=None,
                mountpoint=mountpoint,
            )
            return dry

        message = (
            "Windows manages persistent mounts via Set-Partition / "
            "Add-PartitionAccessPath / mountvol; there is no "
            "/etc/fstab analogue.  Use the unmount() verb to drop a "
            "drive-letter mapping."
        )
        rtnval = self._build_result(
            status=WindowsFilesystemActionStatus.FAILED,
            command=command,
            path=None,
            mountpoint=mountpoint,
            message=message,
            stderr=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Permissions write verbs
    # ------------------------------------------------------------------

    def chown(
        self,
        path: str,
        user: str,
        group: Optional[str] = None,
        recursive: bool = False,
    ) -> WindowsFilesystemActionResult:
        """
            Set ``path``'s owner via ``icacls <path> /setowner '<user>'``.

            ``group`` is recorded onto the result for cross-OS-contract
            parity but is not consumed by ``icacls /setowner`` —
            Windows ACLs do not have a single "primary group" the way
            POSIX does.  When ``recursive`` is True, ``/T`` is added to
            the icacls invocation.

            Idempotent (non-recursive): when ``Get-Acl`` already
            reports the desired owner, returns ``SKIPPED``.  Recursive
            invocations always run because per-entry comparison is out
            of scope.

            :param path: The target path.
            :param user: The desired owner principal name.
            :param group: Recorded for parity; ignored by icacls.
            :param recursive: True to recurse via ``/T``.

            :returns: The :class:`WindowsFilesystemActionResult`.
        """
        command = self._build_chown_command(
            path=path, user=user, recursive=recursive,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFilesystemActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        if not recursive:
            current_owner = self._get_owner(path=path)
            if current_owner is not None:
                # Owner names from Get-Acl are typically rendered as
                # ``"<DOMAIN>\<account>"``; tolerate a bare-name match.
                if (
                    current_owner == user
                    or _strip_domain_prefix(raw_name=current_owner) == user
                ):
                    skipped = self._build_result(
                        status=WindowsFilesystemActionStatus.SKIPPED,
                        command=command,
                        path=path,
                        message="path already has the requested owner",
                    )
                    return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=path,
        )
        return rtnval

    def chmod(
        self,
        path: str,
        mode,
        recursive: bool = False,
    ) -> WindowsFilesystemActionResult:
        """
            Approximate POSIX ``chmod`` semantics on Windows by mapping
            ``mode`` to a list of ``icacls /grant`` arguments via
            :func:`_mode_to_icacls_grants` and dispatching ``icacls
            <path> /inheritance:r /grant <grant1> /grant <grant2>
            ...``.

            Only a small canonical set of modes is supported (see
            :data:`_MODE_TO_ICACLS_GRANTS`).  Modes outside that table
            return ``FAILED`` with a descriptive ``message`` so the
            caller can detect the unsupported case.

            Idempotent (non-recursive): when ``Get-Acl``'s
            access-rule set, after canonicalisation, already matches
            the requested mode's grant list, returns ``SKIPPED``.
            Recursive invocations always run.

            :param path: The target path.
            :param mode: The mode (int or octal-style string).
            :param recursive: True to add ``/T``.

            :returns: The :class:`WindowsFilesystemActionResult`.
        """
        mode_int = _coerce_mode_to_int(mode=mode)
        if mode_int is None:
            failed = self._build_result(
                status=WindowsFilesystemActionStatus.FAILED,
                command=[],
                path=path,
                message="mode {!r} is not a recognised octal value".format(
                    mode,
                ),
                stderr="invalid mode",
            )
            return failed

        grants = _mode_to_icacls_grants(mode_int=mode_int)
        if grants is None:
            placeholder_command = self._build_chmod_command(
                path=path, grants=[], recursive=recursive,
            )
            failed = self._build_result(
                status=WindowsFilesystemActionStatus.FAILED,
                command=placeholder_command,
                path=path,
                message=(
                    "mode 0o{:o} has no entry in the partial mode-to-ACL "
                    "table; supported modes: {}".format(
                        mode_int,
                        ", ".join(
                            "0o{:o}".format(k)
                            for k in sorted(_MODE_TO_ICACLS_GRANTS.keys())
                        ),
                    )
                ),
                stderr="unsupported mode",
            )
            return failed

        if self._config.dry_run:
            # Dry-run records a placeholder owner because we do not
            # consult the live ACL.
            dry_grants = self._render_grants(
                grants=grants, owner_value="%OWNER%",
            )
            dry_command = self._build_chmod_command(
                path=path, grants=dry_grants, recursive=recursive,
            )
            dry = self._build_result(
                status=WindowsFilesystemActionStatus.DRY_RUN,
                command=dry_command,
                path=path,
            )
            return dry

        owner_value = self._get_owner(path=path)
        if owner_value is None:
            placeholder_command = self._build_chmod_command(
                path=path, grants=[], recursive=recursive,
            )
            failed = self._build_result(
                status=WindowsFilesystemActionStatus.FAILED,
                command=placeholder_command,
                path=path,
                message="unable to read current owner via Get-Acl",
                stderr="get-acl failed",
            )
            return failed

        rendered_grants = self._render_grants(
            grants=grants, owner_value=owner_value,
        )
        command = self._build_chmod_command(
            path=path, grants=rendered_grants, recursive=recursive,
        )

        if not recursive:
            if self._acl_matches_grants(
                path=path, expected_grants=rendered_grants,
            ):
                skipped = self._build_result(
                    status=WindowsFilesystemActionStatus.SKIPPED,
                    command=command,
                    path=path,
                    message="path already has the requested ACL",
                )
                return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_mounts(self) -> list[WindowsMountInfo]:
        """
            Enumerate active filesystem mounts.

            Runs ``Get-PSDrive -PSProvider FileSystem | ConvertTo-Json
            -Compress -Depth 5`` and ``Get-Volume | ConvertTo-Json
            -Compress -Depth 5`` and merges the two payloads on the
            drive-letter token.  Reads do not elevate.

            :returns: One :class:`WindowsMountInfo` per addressable
                      mount.
            :raises WindowsFilesystemQueryError: When the
                                                 ``Get-PSDrive``
                                                 cmdlet exits non-zero
                                                 with stderr.
        """
        psdrive_script = (
            "Get-PSDrive -PSProvider FileSystem "
            "| Select-Object Name, Provider, Root, DisplayRoot, "
            "Used, Free "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        psdrive_command = self._build_powershell_argv(script=psdrive_script)
        psdrive_run = self._runner.run(list(psdrive_command), elevated=False)
        if psdrive_run.exit_code != 0:
            raise WindowsFilesystemQueryError(
                command=psdrive_command,
                exit_code=psdrive_run.exit_code,
                stderr=psdrive_run.stderr,
            )

        volume_script = (
            "Get-Volume "
            "| Select-Object DriveLetter, FileSystem, FileSystemType, "
            "Size, SizeRemaining "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        volume_command = self._build_powershell_argv(script=volume_script)
        volume_run = self._runner.run(list(volume_command), elevated=False)
        if volume_run.exit_code != 0:
            # ``Get-Volume`` is not present on every PowerShell host
            # (older Windows Server SKUs without the Storage module).
            # Treat that as "no volume detail available" rather than
            # raising.
            volume_records: list[dict] = []
        else:
            volume_records = _parse_get_volume(text=volume_run.stdout)

        psdrive_records = _parse_get_psdrive(text=psdrive_run.stdout)
        rtnval = _merge_psdrive_and_volume(
            psdrive_records=psdrive_records,
            volume_records=volume_records,
        )
        return rtnval

    def stat(
        self,
        path: str,
    ) -> Optional[WindowsFileInfo]:
        """
            Return :class:`WindowsFileInfo` for ``path``.

            Runs ``Get-Item <path> | Select Mode, Length, LastWriteTime,
            FullName | ConvertTo-Json -Compress -Depth 5`` for size /
            modification, plus ``Get-Acl <path> | Select Owner, Group,
            AccessToString | ConvertTo-Json -Compress -Depth 5`` for
            the owner / group.  Reads do not elevate.

            :param path: The absolute path to query.

            :returns: The :class:`WindowsFileInfo`, or ``None`` when
                      the path does not exist.
        """
        item_record = self._get_item_record(path=path)
        if item_record is None:
            rtnval: Optional[WindowsFileInfo] = None
            return rtnval

        acl_record = self._get_acl_record(path=path)

        size_value = _coerce_int(raw_value=item_record.get("Length"))
        if size_value is None:
            size_value = 0

        modified_raw = item_record.get("LastWriteTime")
        if modified_raw is None:
            modified_value = ""
        else:
            modified_value = str(modified_raw)

        if acl_record is None:
            owner_value = ""
            group_value: Optional[str] = None
            mode_value = ""
        else:
            owner_raw = acl_record.get("Owner")
            if owner_raw is None:
                owner_value = ""
            else:
                owner_value = str(owner_raw)
            group_raw = acl_record.get("Group")
            if group_raw is None or len(str(group_raw)) == 0:
                group_value = None
            else:
                group_value = str(group_raw)
            access_raw = acl_record.get("AccessToString")
            if access_raw is None:
                mode_value = ""
            else:
                mode_value = str(access_raw)

        rtnval = WindowsFileInfo(
            path=path,
            owner=owner_value,
            group=group_value,
            mode=mode_value,
            size=size_value,
            modified=modified_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / dispatch / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsFilesystemManagerConfig:
        """
            Return ``self._config`` typed.
        """
        if isinstance(self._config, WindowsFilesystemManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsFilesystemManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list[str]:
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

    def _build_chown_command(
        self,
        *,
        path: str,
        user: str,
        recursive: bool,
    ) -> list[str]:
        """
            Build the ``icacls`` argv for ``chown``.

            Dispatched as a PowerShell scriptblock so the standard
            argv-shape contract is preserved across every verb in this
            manager (icacls.exe is invoked from inside the
            scriptblock).

            :param path: Target path.
            :param user: Desired owner.
            :param recursive: True to add ``/T``.

            :returns: The full PowerShell argv.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        escaped_user = _ps_escape_single_quoted(value=user)
        if recursive:
            recurse_token = " /T"
        else:
            recurse_token = ""
        script = (
            "icacls '{path}' /setowner '{user}'{recurse}"
        ).format(path=escaped_path, user=escaped_user, recurse=recurse_token)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_chmod_command(
        self,
        *,
        path: str,
        grants: list[str],
        recursive: bool,
    ) -> list[str]:
        """
            Build the ``icacls`` argv for ``chmod``.

            :param path: Target path.
            :param grants: Pre-rendered grant strings (with owner
                           substitution already applied).
            :param recursive: True to add ``/T``.

            :returns: The full PowerShell argv.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        if recursive:
            recurse_token = " /T"
        else:
            recurse_token = ""
        grant_fragments: list[str] = []
        for grant in grants:
            escaped_grant = _ps_escape_single_quoted(value=grant)
            grant_fragments.append("/grant '{}'".format(escaped_grant))
        if len(grant_fragments) == 0:
            grant_section = ""
        else:
            grant_section = " " + " ".join(grant_fragments)
        script = (
            "icacls '{path}' /inheritance:r{grants}{recurse}"
        ).format(
            path=escaped_path,
            grants=grant_section,
            recurse=recurse_token,
        )
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _render_grants(
        self,
        *,
        grants: list[str],
        owner_value: str,
    ) -> list[str]:
        """
            Substitute ``%OWNER%`` placeholders in each grant string
            with ``owner_value``.

            :param grants: The raw grant strings (with placeholders).
            :param owner_value: The owner principal to substitute in.

            :returns: A new list with placeholders replaced.
        """
        rtnval: list[str] = []
        for grant in grants:
            rendered = grant.replace("%OWNER%", owner_value)
            rtnval.append(rendered)
        return rtnval

    def _dispatch_filesystem_write(
        self,
        *,
        command: list[str],
        path: Optional[str] = None,
        device: Optional[str] = None,
        mountpoint: Optional[str] = None,
    ) -> WindowsFilesystemActionResult:
        """
            Dispatch a write command and wrap the result.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsFilesystemActionStatus.SUCCESS
        else:
            mapped_status = WindowsFilesystemActionStatus.FAILED

        if path is not None:
            detail_value = path
        else:
            detail_value = mountpoint

        rtnval = WindowsFilesystemActionResult(
            status=mapped_status,
            detail=detail_value,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            path=path,
            device=device,
            mountpoint=mountpoint,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsFilesystemActionStatus,
        command: list[str],
        path: Optional[str] = None,
        device: Optional[str] = None,
        mountpoint: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsFilesystemActionResult:
        """
            Construct a :class:`WindowsFilesystemActionResult` without
            invoking the runner.
        """
        if path is not None:
            detail_value = path
        else:
            detail_value = mountpoint
        rtnval = WindowsFilesystemActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            path=path,
            device=device,
            mountpoint=mountpoint,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — read probes
    # ------------------------------------------------------------------

    def _is_already_mounted(
        self,
        *,
        drive_name: str,
        device: str,
    ) -> bool:
        """
            Return True when ``Get-PSDrive -Name <letter>`` reports a
            mapping with a ``Root`` / ``DisplayRoot`` matching
            ``device``.

            :param drive_name: The bare drive letter.
            :param device: The desired backing root.

            :returns: True when the mapping already exists.
        """
        record = self._lookup_psdrive(drive_name=drive_name)
        if record is None:
            rtnval = False
            return rtnval

        display_root_raw = record.get("DisplayRoot")
        if display_root_raw is not None and len(str(display_root_raw)) > 0:
            if str(display_root_raw) == device:
                rtnval = True
                return rtnval

        root_raw = record.get("Root")
        if root_raw is not None and len(str(root_raw)) > 0:
            if str(root_raw) == device:
                rtnval = True
                return rtnval

        rtnval = False
        return rtnval

    def _drive_letter_is_mapped(self, *, drive_name: str) -> bool:
        """
            Return True when ``Get-PSDrive -Name <letter>`` exits zero
            (the drive letter has an active mapping).
        """
        record = self._lookup_psdrive(drive_name=drive_name)
        rtnval = record is not None
        return rtnval

    def _lookup_psdrive(self, *, drive_name: str) -> Optional[dict]:
        """
            Probe ``Get-PSDrive -Name <letter>`` and return the parsed
            JSON record.

            :param drive_name: The bare drive letter.

            :returns: The parsed dict, or ``None`` when the cmdlet
                      fails or no record is reported.
        """
        escaped = _ps_escape_single_quoted(value=drive_name)
        script = (
            "Get-PSDrive -Name '{}' "
            "| Select-Object Name, Provider, Root, DisplayRoot, "
            "Used, Free "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        records = _parse_get_psdrive(text=run_result.stdout)
        if len(records) == 0:
            rtnval = None
            return rtnval
        rtnval = records[0]
        return rtnval

    def _get_owner(self, *, path: str) -> Optional[str]:
        """
            Return the owner principal name reported by ``Get-Acl``.

            :param path: The path to query.

            :returns: The owner string, or ``None`` on failure.
        """
        record = self._get_acl_record(path=path)
        if record is None:
            rtnval: Optional[str] = None
            return rtnval
        owner_raw = record.get("Owner")
        if owner_raw is None or len(str(owner_raw)) == 0:
            rtnval = None
            return rtnval
        rtnval = str(owner_raw)
        return rtnval

    def _get_acl_record(self, *, path: str) -> Optional[dict]:
        """
            Run ``Get-Acl <path> | ConvertTo-Json``.
        """
        escaped = _ps_escape_single_quoted(value=path)
        script = (
            "Get-Acl '{}' "
            "| Select-Object Owner, Group, AccessToString "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_get_acl(text=run_result.stdout)
        return rtnval

    def _get_item_record(self, *, path: str) -> Optional[dict]:
        """
            Run ``Get-Item <path> | ConvertTo-Json``.
        """
        escaped = _ps_escape_single_quoted(value=path)
        script = (
            "Get-Item '{}' "
            "| Select-Object Mode, Length, LastWriteTime, FullName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_get_item(text=run_result.stdout)
        return rtnval

    def _acl_matches_grants(
        self,
        *,
        path: str,
        expected_grants: list[str],
    ) -> bool:
        """
            Tolerant comparison: True when ``Get-Acl``'s
            ``AccessToString`` already contains every expected grant
            string as a substring.

            ``AccessToString`` is a multi-line text rendering of the
            access-rule set; a substring search is the simplest
            cross-PS-version match that does not require an explicit
            ACL parser.  False negatives are tolerated (the chmod is
            attempted) but false positives are not (we never claim a
            path matches when it does not).

            :param path: The target path.
            :param expected_grants: Rendered grant strings to look
                                    for.

            :returns: True when every grant is present.
        """
        record = self._get_acl_record(path=path)
        if record is None:
            rtnval = False
            return rtnval
        access_raw = record.get("AccessToString")
        if access_raw is None:
            rtnval = False
            return rtnval
        access_text = str(access_raw)
        for grant in expected_grants:
            if grant not in access_text:
                rtnval = False
                return rtnval
        rtnval = True
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers used by parsers but kept at the bottom so
# importers can reach the classes / functions referenced above.
# ----------------------------------------------------------------------


def _strip_domain_prefix(*, raw_name: str) -> str:
    """
        Return the bare account name from a ``"<domain>\\<account>"``
        compound name.  Names without a backslash are returned
        unchanged.

        :param raw_name: The compound or bare name.

        :returns: The bare account name.
    """
    last_backslash = raw_name.rfind("\\")
    if last_backslash < 0:
        rtnval = raw_name
        return rtnval
    rtnval = raw_name[last_backslash + 1:]
    return rtnval

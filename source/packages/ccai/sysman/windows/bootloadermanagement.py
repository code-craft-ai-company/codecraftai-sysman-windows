"""
    Windows bootloader-management feature.

    Implements the cross-OS bootloader-management contract for
    Windows.  Windows' OS-level bootloader is the Boot Configuration
    Data (BCD) store, manipulated through the ``bcdedit`` command-
    line utility.

    The BCD store splits into two relevant namespaces:

        * ``{bootmgr}`` -- the Windows Boot Manager.  Its ``default``
          property names the GUID of the active OS loader entry.
        * ``osloader`` -- the collection of OS loader entries
          (``{<guid>}``) that the boot manager can launch.

    Verb plumbing:

        * ``list_entries``
            ``bcdedit /enum {fwbootmgr}`` AND
            ``bcdedit /enum osloader``.  The resulting blocks are
            parsed and merged.
        * ``get_default_entry``
            ``bcdedit /enum {bootmgr} /v`` -> parse ``default`` line.
        * ``set_default_entry``
            ``bcdedit /default {<guid>}``.
        * ``add_entry``
            ``bcdedit /create {<guid>} /d "<description>"
            /application <application>``,
            then a ``/set`` per option in the spec,
            then ``bcdedit /displayorder {<guid>} /addlast``.
        * ``remove_entry``
            ``bcdedit /delete {<guid>}``.
        * ``update_bootloader_config`` / ``regenerate_config``
            BCD writes are immediate -- there is no separate apply
            step.  Both verbs return SUCCESS no-ops.

    **Important distinction.**  This feature is NOT
    ``boot-management``.  ``boot-management`` (in
    ``codecraftai-sysman-uefi``) addresses **firmware-level UEFI boot
    entries**, which on Windows are surfaced through ``bcdedit
    /enum firmware`` and the ``{fwbootmgr}`` global.  This feature
    addresses the **OS-level BCD osloader / bootmgr namespaces** that
    sit between the firmware boot entry and the running kernel.  The
    two layers are independent and must remain so.

    GUID format:
        BCD identifiers are GUIDs wrapped in braces.  Built-in
        identifiers also accept aliases such as ``{bootmgr}`` and
        ``{default}``.  The manager normalises every GUID to the
        canonical brace-wrapped form before dispatching to
        ``bcdedit``.

    Elevation policy:
        Every write verb honours ``config.elevated`` -- BCD writes
        require Administrator on every Windows release.  Reads
        (``bcdedit /enum``) typically also require Administrator on
        UAC-protected hosts but the contract specifies that reads
        MUST NOT pass ``elevated=True``; the runner is responsible
        for surfacing the resulting access-denied error when the
        host process lacks elevation.

    Destructive nature:
        BCD misconfiguration can render Windows unbootable.  Callers
        SHOULD pair every write with a ``bcdedit /export`` backup and
        SHOULD verify the resulting state through ``list_entries``
        before rebooting.
"""

import os
import re

from dataclasses import dataclass
from enum import Enum
from typing import Optional


from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_DEFAULT_BCDEDIT = "bcdedit.exe"

_BOOTMGR_ALIAS = "{bootmgr}"
_FWBOOTMGR_ALIAS = "{fwbootmgr}"
_OSLOADER_TYPE = "osloader"

_GUID_RE = re.compile(
    r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$",
)


WindowsBootloaderActionStatus = ActionStatus


@dataclass
class WindowsBootloaderManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsBootloaderManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param bcdedit_executable: Path or PATH-resolvable name of
                                   the ``bcdedit`` executable.
                                   Default ``"bcdedit.exe"``.
    """
    bcdedit_executable: str = _DEFAULT_BCDEDIT


@dataclass
class WindowsBootloaderActionResult(FeatureActionResult):
    """
        Result of a bootloader-management write operation.

        :param entry_id: The bootloader entry identifier acted upon
                         (a brace-wrapped GUID).  ``None`` for
                         non-entry verbs.
        :param requires_reboot: ``True`` for any successful BCD
                                write -- the boot manager consults
                                the store only at boot.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` / no-op outcomes.
    """
    entry_id: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class WindowsBootEntryInfo:
    """
        Snapshot of a single BCD bootloader entry.

        :param id: Brace-wrapped GUID (canonical lower-case form).
        :param name: Human-readable description (``description`` BCD
                     property).
        :param kernel: ``path`` BCD property (e.g.
                       ``\\Windows\\system32\\winload.efi``).  Maps
                       loosely onto the cross-OS ``kernel`` field.
        :param initrd: Reserved -- Windows does not use an
                       initramfs.  Always ``None``.
        :param cmdline: Reserved -- Windows entries express options
                        as discrete BCD properties rather than a
                        single command line.  Always ``None``.
        :param device: ``device`` BCD property (e.g.
                       ``partition=C:``).
        :param is_default: ``True`` when the entry's GUID matches the
                           ``{bootmgr}`` default.
        :param boot_count: Reserved.  Always ``None``.
    """
    id: str
    name: str
    kernel: Optional[str] = None
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
    is_default: bool = False
    boot_count: Optional[int] = None


@dataclass
class WindowsBootEntrySpec:
    """
        Desired BCD entry shape for ``add_entry``.

        :param id: Caller-chosen GUID (brace-wrapped).  When ``None``
                   the manager lets ``bcdedit /create`` auto-generate
                   one.
        :param name: Human-readable description (becomes the
                     ``/d`` argument).
        :param application: BCD application class.  Default
                            ``"osloader"``.  Other accepted values
                            include ``"bootsector"`` and
                            ``"resume"``.
        :param device: Optional ``device`` BCD property value.
        :param path: Optional ``path`` BCD property value (the
                     loader image path).
        :param options: Optional dict of additional BCD properties
                        to ``/set`` after creation.  Keys are BCD
                        property names; values are the corresponding
                        scalar values.
    """
    id: Optional[str]
    name: str
    application: str = "osloader"
    device: Optional[str] = None
    path: Optional[str] = None
    options: Optional[dict] = None


class WindowsBootloaderQueryError(Exception):
    """
        Raised when a bootloader-management read fails for a reason
        other than the target simply being absent.
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

        err_msg_lines = ["Windows bootloader query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers -- GUID handling
# ----------------------------------------------------------------------


def _normalise_guid(*, value: str) -> str:
    """
        Normalise ``value`` to the canonical brace-wrapped lower-
        case GUID form used by ``bcdedit``.

        Accepts both bare GUIDs (``a1b2...``) and brace-wrapped
        GUIDs (``{a1b2...}``); built-in aliases such as
        ``{bootmgr}`` are returned untouched.

        :param value: Raw text.

        :returns: The normalised identifier.

        :raises ValueError: When ``value`` is not a valid GUID and
                            not a recognised alias.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        err_msg = "GUID value must be non-empty"
        raise ValueError(err_msg)

    lowered = stripped.lower()
    aliases = (
        "{bootmgr}", "{fwbootmgr}", "{default}", "{current}",
        "{ramdiskoptions}", "{globalsettings}", "{bootloadersettings}",
        "{badmemory}", "{emssettings}", "{dbgsettings}",
        "{hypervisorsettings}", "{memdiag}",
    )
    if lowered in aliases:
        rtnval = lowered
        return rtnval

    if lowered.startswith("{") and lowered.endswith("}"):
        candidate = lowered
    else:
        candidate = "{" + lowered + "}"

    if _GUID_RE.match(candidate) is None:
        err_msg = "Value {!r} is not a valid GUID or BCD alias".format(value)
        raise ValueError(err_msg)

    rtnval = candidate
    return rtnval


def _is_valid_guid(*, value: str) -> bool:
    """
        Return ``True`` when ``value`` normalises to a canonical
        GUID form.

        :param value: The raw text.

        :returns: ``True`` when valid.
    """
    try:
        _normalise_guid(value=value)
    except ValueError:
        rtnval = False
        return rtnval
    rtnval = True
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers -- bcdedit parsing
# ----------------------------------------------------------------------


def _split_bcdedit_blocks(*, text: str) -> list[list[str]]:
    """
        Split ``bcdedit`` output into blank-line-delimited blocks.

        :param text: Raw stdout.

        :returns: One list of stripped lines per block.
    """
    rtnval: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if len(line.strip()) == 0:
            if len(current) > 0:
                rtnval.append(current)
                current = []
            continue
        current.append(line)
    if len(current) > 0:
        rtnval.append(current)
    return rtnval


def _parse_bcdedit_block(*, block: list[str]) -> dict:
    """
        Parse a single ``bcdedit`` block into a dict of normalised
        keys.

        Each block has the shape::

            Windows Boot Loader
            -------------------
            identifier              {a1b2c3d4-...}
            device                  partition=C:
            path                    \\Windows\\system32\\winload.efi
            description             Windows 11
            ...

        :param block: The list of stripped lines comprising the
                      block.

        :returns: A dict.  ``"_kind"`` carries the (lower-cased)
                  header line.  All other keys are the BCD property
                  names (lower-cased), and values are the raw right-
                  hand side text.
    """
    rtnval: dict = {}
    if len(block) == 0:
        return rtnval

    header = block[0].strip().lower()
    rtnval["_kind"] = header

    body_start = 1
    if len(block) > 1 and set(block[1].strip()) <= {"-"} and len(block[1].strip()) > 0:
        body_start = 2

    for raw_line in block[body_start:]:
        stripped = raw_line.rstrip()
        if len(stripped) == 0:
            continue
        if stripped.startswith(" ") or stripped.startswith("\t"):
            # Continuation lines -- append to the most-recent key.
            keys = list(rtnval.keys())
            if len(keys) > 0:
                last_key = keys[-1]
                if last_key != "_kind":
                    rtnval[last_key] = "{} {}".format(
                        rtnval[last_key], stripped.strip(),
                    )
            continue

        # The bcdedit field separator is two-or-more spaces between
        # the key column and the value column.
        match = re.match(r"^(\S+)\s{2,}(.*)$", stripped)
        if match is None:
            # Single-token line (no value); ignore.
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if len(key) == 0:
            continue
        rtnval[key] = value

    return rtnval


def _parse_bcdedit_entries(
    *,
    text: str,
) -> list[WindowsBootEntryInfo]:
    """
        Parse ``bcdedit /enum osloader`` (or
        ``bcdedit /enum {fwbootmgr}``) output into a list of
        :class:`WindowsBootEntryInfo`.

        Blocks whose ``identifier`` is a built-in alias (e.g.
        ``{bootmgr}``, ``{fwbootmgr}``) are skipped -- those are
        bootloader-management metadata, not entries.

        :param text: Raw stdout.

        :returns: One entry per parsed block.
    """
    rtnval: list[WindowsBootEntryInfo] = []
    for block in _split_bcdedit_blocks(text=text):
        parsed = _parse_bcdedit_block(block=block)
        identifier = parsed.get("identifier")
        if identifier is None or len(identifier) == 0:
            continue
        normalised_id = identifier.strip().lower()
        if normalised_id.startswith("{") and not _is_valid_guid(
            value=normalised_id,
        ):
            # Skip built-in aliases such as {bootmgr} / {fwbootmgr}.
            continue
        info = WindowsBootEntryInfo(
            id=normalised_id,
            name=parsed.get("description") or normalised_id,
            kernel=parsed.get("path"),
            initrd=None,
            cmdline=None,
            device=parsed.get("device"),
            is_default=False,
            boot_count=None,
        )
        rtnval.append(info)
    return rtnval


def _parse_bcdedit_default(*, text: str) -> Optional[str]:
    """
        Parse ``bcdedit /enum {bootmgr} /v`` output for the
        ``default`` property.

        :param text: Raw stdout.

        :returns: The brace-wrapped GUID, or ``None`` when absent.
    """
    rtnval: Optional[str] = None
    for block in _split_bcdedit_blocks(text=text):
        parsed = _parse_bcdedit_block(block=block)
        default_value = parsed.get("default")
        if default_value is not None and len(default_value) > 0:
            rtnval = default_value.strip().lower()
            return rtnval
    return rtnval


def _parse_bcdedit_create_guid(*, text: str) -> Optional[str]:
    """
        Parse the GUID printed by ``bcdedit /create``.

        ``bcdedit /create`` emits a line like::

            The entry {a1b2c3d4-...} was successfully created.

        :param text: Raw stdout.

        :returns: The brace-wrapped GUID, or ``None``.
    """
    rtnval: Optional[str] = None
    match = re.search(
        r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}",
        text,
    )
    if match is not None:
        rtnval = match.group(0).lower()
    return rtnval


# ----------------------------------------------------------------------
# WindowsBootloaderManager
# ----------------------------------------------------------------------


class WindowsBootloaderManager(BaseFeatureManager):
    """
        Windows OS-level bootloader manager.  Translates cross-OS
        bootloader-management verbs onto ``bcdedit`` invocations
        dispatched through the injected :class:`ICommandRunner`.

        Writes (``set_default_entry``, ``add_entry``,
        ``remove_entry``) honour ``config.elevated``; the underlying
        BCD store is Administrator-only.  Successful BCD writes
        always carry ``requires_reboot=True``.  Reads
        (``list_entries``, ``get_default_entry``) and idempotency
        probes always pass ``elevated=False``; the runner surfaces
        the access-denied error on UAC-protected hosts when the
        process lacks elevation.

        ``update_bootloader_config`` and ``regenerate_config`` are
        SUCCESS no-ops because BCD writes take effect immediately
        and Windows does not expose a separate regenerate / apply
        phase.
    """

    result_class = WindowsBootloaderActionResult
    status_class = WindowsBootloaderActionStatus
    feature_name = "bootloader-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_entries(self) -> list[WindowsBootEntryInfo]:
        """
            Enumerate BCD osloader entries plus any firmware-side
            entries surfaced by ``{fwbootmgr}``.  Marks the entry
            whose GUID equals the ``{bootmgr}`` default with
            ``is_default=True``.

            :returns: A list of :class:`WindowsBootEntryInfo`.
            :raises WindowsBootloaderQueryError: When the underlying
                                                 ``bcdedit /enum``
                                                 calls fail.
        """
        osloader_command = [
            self._config.bcdedit_executable, "/enum", _OSLOADER_TYPE,
        ]
        osloader_run = self._runner.run(
            list(osloader_command), elevated=False,
        )
        if osloader_run.exit_code != 0:
            raise WindowsBootloaderQueryError(
                command=osloader_command,
                exit_code=osloader_run.exit_code,
                stderr=osloader_run.stderr or "",
            )
        entries = _parse_bcdedit_entries(text=osloader_run.stdout)

        fwbootmgr_command = [
            self._config.bcdedit_executable, "/enum", _FWBOOTMGR_ALIAS,
        ]
        fwbootmgr_run = self._runner.run(
            list(fwbootmgr_command), elevated=False,
        )
        if fwbootmgr_run.exit_code == 0:
            fw_entries = _parse_bcdedit_entries(text=fwbootmgr_run.stdout)
            existing_ids = {entry.id for entry in entries}
            for fw_entry in fw_entries:
                if fw_entry.id not in existing_ids:
                    entries.append(fw_entry)

        default_guid = self._read_default_guid()
        if default_guid is not None:
            for entry in entries:
                if entry.id == default_guid:
                    entry.is_default = True

        rtnval = entries
        return rtnval

    def get_default_entry(self) -> WindowsBootEntryInfo:
        """
            Return the BCD osloader entry currently marked as the
            ``{bootmgr}`` default.

            :returns: The :class:`WindowsBootEntryInfo` whose
                      ``is_default`` field is ``True``.
            :raises WindowsBootloaderQueryError: When the default
                                                 cannot be resolved
                                                 or no entries
                                                 exist.
        """
        default_guid = self._read_default_guid()
        if default_guid is None:
            raise WindowsBootloaderQueryError(
                command=[
                    self._config.bcdedit_executable,
                    "/enum", _BOOTMGR_ALIAS, "/v",
                ],
                exit_code=None,
                stderr="no_default_entry_resolved",
            )

        entries = self.list_entries()
        for entry in entries:
            if entry.id == default_guid:
                rtnval = entry
                return rtnval

        raise WindowsBootloaderQueryError(
            command=[
                self._config.bcdedit_executable, "/enum", _OSLOADER_TYPE,
            ],
            exit_code=None,
            stderr="default_guid_not_in_osloader",
        )

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_default_entry(
        self,
        entry_id: str,
    ) -> WindowsBootloaderActionResult:
        """
            Persist ``entry_id`` (a GUID) as the ``{bootmgr}``
            default via ``bcdedit /default {<guid>}``.

            :param entry_id: The brace-wrapped GUID (or bare GUID;
                             the manager normalises it).

            :returns: The :class:`WindowsBootloaderActionResult` with
                      ``requires_reboot=True`` on success.
        """
        action = "set_default_entry"
        try:
            normalised = _normalise_guid(value=entry_id)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsBootloaderActionStatus.FAILED,
                detail=action,
                command=None,
                entry_id=entry_id,
                requires_reboot=False,
                message=str(exc),
                stderr="invalid_guid",
            )
            return failed

        command = [
            self._config.bcdedit_executable, "/default", normalised,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(command),
                entry_id=normalised,
                requires_reboot=True,
            )
            return dry

        current_default = self._read_default_guid()
        if current_default == normalised:
            skipped = self._build_result(
                status=WindowsBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(command),
                entry_id=normalised,
                requires_reboot=False,
                message="bcdedit already reports this GUID as default",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="set_default_entry",
            entry_id=normalised,
            requires_reboot=True,
        )
        return rtnval

    def add_entry(
        self,
        spec: WindowsBootEntrySpec,
    ) -> WindowsBootloaderActionResult:
        """
            Create a new BCD entry from ``spec``.

            Runs ``bcdedit /create [{<guid>}] /d "<name>"
            /application <application>`` first; then a ``/set`` per
            ``spec.device``, ``spec.path``, and per ``spec.options``
            entry; and finally
            ``bcdedit /displayorder {<guid>} /addlast`` so the entry
            appears in the boot menu.

            :param spec: The :class:`WindowsBootEntrySpec`.

            :returns: The :class:`WindowsBootloaderActionResult`.
        """
        action = "add_entry"

        if spec.id is not None:
            try:
                requested_guid: Optional[str] = _normalise_guid(value=spec.id)
            except ValueError as exc:
                failed = self._build_result(
                    status=WindowsBootloaderActionStatus.FAILED,
                    detail=action,
                    command=None,
                    entry_id=spec.id,
                    requires_reboot=False,
                    message=str(exc),
                    stderr="invalid_guid",
                )
                return failed
        else:
            requested_guid = None

        create_command = [self._config.bcdedit_executable, "/create"]
        if requested_guid is not None:
            create_command.append(requested_guid)
        create_command.extend(["/d", spec.name])
        create_command.extend(["/application", spec.application])

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(create_command),
                entry_id=requested_guid,
                requires_reboot=True,
            )
            return dry

        if requested_guid is not None:
            existing_entries = self.list_entries()
            for entry in existing_entries:
                if entry.id == requested_guid and entry.name == spec.name:
                    skipped = self._build_result(
                        status=WindowsBootloaderActionStatus.SKIPPED,
                        detail=action,
                        command=list(create_command),
                        entry_id=requested_guid,
                        requires_reboot=False,
                        message="entry already present with matching description",
                    )
                    return skipped

        create_run = self._runner.run(
            list(create_command), elevated=self._config.elevated,
        )
        if create_run.exit_code != 0:
            failed = self._build_result(
                status=WindowsBootloaderActionStatus.FAILED,
                detail=action,
                command=list(create_command),
                entry_id=requested_guid,
                requires_reboot=False,
                message="bcdedit /create failed",
                exit_code=create_run.exit_code,
                stderr=create_run.stderr,
            )
            return failed

        if requested_guid is not None:
            created_guid = requested_guid
        else:
            parsed_guid = _parse_bcdedit_create_guid(text=create_run.stdout)
            if parsed_guid is None:
                failed = self._build_result(
                    status=WindowsBootloaderActionStatus.FAILED,
                    detail=action,
                    command=list(create_command),
                    entry_id=None,
                    requires_reboot=False,
                    message="could not parse GUID from bcdedit /create stdout",
                    exit_code=create_run.exit_code,
                    stderr=create_run.stderr,
                )
                return failed
            created_guid = parsed_guid

        set_pairs: list[tuple] = []
        if spec.device is not None and len(spec.device) > 0:
            set_pairs.append(("device", spec.device))
        if spec.path is not None and len(spec.path) > 0:
            set_pairs.append(("path", spec.path))
        if spec.options is not None:
            for key, value in spec.options.items():
                set_pairs.append((str(key), str(value)))

        for option_key, option_value in set_pairs:
            set_command = [
                self._config.bcdedit_executable, "/set",
                created_guid, option_key, option_value,
            ]
            set_run = self._runner.run(
                list(set_command), elevated=self._config.elevated,
            )
            if set_run.exit_code != 0:
                failed = self._build_result(
                    status=WindowsBootloaderActionStatus.FAILED,
                    detail=action,
                    command=list(set_command),
                    entry_id=created_guid,
                    requires_reboot=False,
                    message=(
                        "bcdedit /set {} failed for option {!r}".format(
                            created_guid, option_key,
                        )
                    ),
                    exit_code=set_run.exit_code,
                    stderr=set_run.stderr,
                )
                return failed

        displayorder_command = [
            self._config.bcdedit_executable,
            "/displayorder", created_guid, "/addlast",
        ]
        order_run = self._runner.run(
            list(displayorder_command), elevated=self._config.elevated,
        )
        if order_run.exit_code != 0:
            failed = self._build_result(
                status=WindowsBootloaderActionStatus.FAILED,
                detail=action,
                command=list(displayorder_command),
                entry_id=created_guid,
                requires_reboot=False,
                message="bcdedit /displayorder failed",
                exit_code=order_run.exit_code,
                stderr=order_run.stderr,
            )
            return failed

        rtnval = self._build_result(
            status=WindowsBootloaderActionStatus.SUCCESS,
            detail=action,
            command=list(create_command),
            entry_id=created_guid,
            requires_reboot=True,
            exit_code=create_run.exit_code,
            stderr=create_run.stderr,
        )
        return rtnval

    def remove_entry(
        self,
        entry_id: str,
    ) -> WindowsBootloaderActionResult:
        """
            Remove the BCD entry identified by ``entry_id`` via
            ``bcdedit /delete {<guid>}``.

            :param entry_id: The brace-wrapped GUID (or bare GUID;
                             the manager normalises it).

            :returns: The :class:`WindowsBootloaderActionResult`.
        """
        action = "remove_entry"
        try:
            normalised = _normalise_guid(value=entry_id)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsBootloaderActionStatus.FAILED,
                detail=action,
                command=None,
                entry_id=entry_id,
                requires_reboot=False,
                message=str(exc),
                stderr="invalid_guid",
            )
            return failed

        command = [
            self._config.bcdedit_executable, "/delete", normalised,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(command),
                entry_id=normalised,
                requires_reboot=True,
            )
            return dry

        existing_entries = self.list_entries()
        present = any(entry.id == normalised for entry in existing_entries)
        if not present:
            skipped = self._build_result(
                status=WindowsBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(command),
                entry_id=normalised,
                requires_reboot=False,
                message="entry is not present in the BCD store",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="remove_entry",
            entry_id=normalised,
            requires_reboot=True,
        )
        return rtnval

    def update_bootloader_config(self) -> WindowsBootloaderActionResult:
        """
            Windows BCD writes are immediate -- there is no separate
            apply step.  Returns ``SUCCESS`` without invoking the
            runner.

            :returns: The :class:`WindowsBootloaderActionResult`.
        """
        action = "update_bootloader_config"
        rtnval = self._build_result(
            status=WindowsBootloaderActionStatus.SUCCESS,
            detail=action,
            command=None,
            entry_id=None,
            requires_reboot=False,
            message=(
                "Windows BCD writes are immediate; no separate "
                "update step exists."
            ),
        )
        return rtnval

    def regenerate_config(self) -> WindowsBootloaderActionResult:
        """
            Windows does not expose a regenerate-from-source step;
            BCD is the source of truth.  Returns ``SUCCESS`` without
            invoking the runner.

            :returns: The :class:`WindowsBootloaderActionResult`.
        """
        action = "regenerate_config"
        rtnval = self._build_result(
            status=WindowsBootloaderActionStatus.SUCCESS,
            detail=action,
            command=None,
            entry_id=None,
            requires_reboot=False,
            message=(
                "Windows BCD is its own source; no regenerate step "
                "exists."
            ),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_default_guid(self) -> Optional[str]:
        """
            Run ``bcdedit /enum {bootmgr} /v`` and parse the
            ``default`` property.  Reads never elevate.

            :returns: The brace-wrapped GUID, or ``None``.
        """
        command = [
            self._config.bcdedit_executable,
            "/enum", _BOOTMGR_ALIAS, "/v",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = _parse_bcdedit_default(text=run_result.stdout)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsBootloaderActionStatus,
        detail: Optional[str],
        command: Optional[list[str]],
        entry_id: Optional[str] = None,
        requires_reboot: bool = False,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> WindowsBootloaderActionResult:
        """
            Construct a :class:`WindowsBootloaderActionResult`
            without dispatching.

            :param status: The outcome status.
            :param detail: Short identifier of the action.
            :param command: Would-have-been argv (or executed argv).
            :param entry_id: The entry identifier acted upon.
            :param requires_reboot: ``True`` when the change takes
                                    effect only after the next boot.
            :param message: Optional human-readable explanation.
            :param stderr: Optional synthetic stderr payload.
            :param exit_code: Optional exit code.

            :returns: The :class:`WindowsBootloaderActionResult`.
        """
        if command is None:
            command_copy: Optional[list[str]] = None
        else:
            command_copy = list(command)
        rtnval = WindowsBootloaderActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            entry_id=entry_id,
            requires_reboot=requires_reboot,
            message=message,
            stderr=stderr,
            exit_code=exit_code,
        )
        return rtnval

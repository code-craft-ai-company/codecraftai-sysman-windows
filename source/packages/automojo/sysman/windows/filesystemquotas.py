"""
    Windows filesystem-quotas feature.

    Implements the cross-OS filesystem-quotas contract for Windows
    using ``fsutil quota`` — the volume-level NTFS user-quota
    subsystem built into Windows.  Every command is dispatched
    through the injected :class:`ICommandRunner` so the manager
    works identically under local and WinRM / SSH execution.

    fsutil-direct argv:
        ``fsutil.exe`` is a native Windows binary that accepts
        argv directly without requiring PowerShell mediation.
        Every dispatched command is therefore of the form
        ``["fsutil", "quota", "<verb>", ...]``.  No PowerShell
        scriptblock wrapper is layered on top; that simplifies
        argv auditing and makes the manager portable to any
        runner that can launch a native executable.

    Major limitation — NTFS has no per-group quota:
        ``fsutil quota`` is per-USER only.  NTFS volume-level
        quotas address Windows security identifiers (SIDs), and
        Windows does NOT have a "primary group" concept the way
        POSIX does.  Calling :meth:`set_group_quota` returns
        ``FAILED`` with ``detail="ntfs_group_quota_unsupported"``
        without invoking the runner.  Calling
        :meth:`get_group_quota` raises
        :class:`WindowsQuotaQueryError` carrying the same token.

        Operators needing per-group quotas should use the FSRM
        ``dirquota`` family (Windows Server only); FSRM is a
        documented follow-up extension.

    Bytes are bytes:
        Windows ``fsutil quota`` arguments and outputs are in
        bytes natively.  Unlike the Unix peers (which speak
        1-KiB blocks), the Windows manager performs NO
        unit conversion: ``soft_bytes`` and ``hard_bytes`` from
        the cross-OS contract pass through to ``fsutil`` argv as
        decimal integers.

    Elevation policy:
        Every supported write verb honours ``config.elevated``.
        Reads NEVER elevate, regardless of ``config.elevated``.
"""

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


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_FSUTIL = "fsutil"

_GROUP_UNSUPPORTED_DETAIL = "ntfs_group_quota_unsupported"

# Hex sentinel meaning "no limit" in fsutil quota output.
_NO_LIMIT_SENTINEL = 0xFFFFFFFFFFFFFFFF


WindowsQuotaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class WindowsQuotaPrincipalKind(Enum):
    """
        Identifies whether a quota record applies to a user or
        group principal.

        +-----------+------------------------+
        | Value     | Meaning                |
        +===========+========================+
        | ``USER``  | NTFS per-user quota    |
        +-----------+------------------------+
        | ``GROUP`` | (unsupported by NTFS)  |
        +-----------+------------------------+
    """
    USER = "user"
    GROUP = "group"


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsFilesystemQuotasManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`WindowsFilesystemQuotasManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param quota_unit_kb: Documentation field.  Windows
            ``fsutil quota`` arguments are in BYTES, not KiB
            blocks like Unix.  This field is preserved as a
            documentation cue (``1`` = bytes-as-bytes) so cross-
            OS callers immediately see that no scaling is
            needed.  Setting this to anything other than ``1``
            has NO effect on the manager — the field exists
            only to document the byte-native semantics.
    """
    quota_unit_kb: int = 1


@dataclass
class WindowsQuotaActionResult(FeatureActionResult):
    """
        Result of a filesystem-quotas write operation.

        :param mountpoint: The volume token acted upon.
        :param principal: The user acted upon (when applicable).
        :param message: Optional human-readable detail for
                        ``SKIPPED`` or ``FAILED`` outcomes.
    """
    mountpoint: Optional[str] = None
    principal: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsQuotaInfo:
    """
        Per-user quota record on a single NTFS volume.

        :param mountpoint: Volume token the record applies to.
        :param principal: User name (e.g. ``"DOMAIN\\alice"``)
                          or SID string.
        :param principal_kind: Always USER on NTFS volumes.
        :param used_bytes: Current usage in bytes; ``None`` when
                           not reported.
        :param soft_bytes: Soft (threshold) limit in bytes;
                           ``None`` for "no limit" sentinels.
        :param hard_bytes: Hard limit in bytes; ``None`` for
                           "no limit" sentinels.
        :param used_inodes: Always ``None`` — NTFS has no
                            inode-quota concept.
        :param soft_inodes: Always ``None``.
        :param hard_inodes: Always ``None``.
        :param grace_period_seconds: Always ``None`` — NTFS
                                     volume quotas have no
                                     grace-period concept.
    """
    mountpoint: str
    principal: str
    principal_kind: WindowsQuotaPrincipalKind
    used_bytes: Optional[int]
    soft_bytes: Optional[int]
    hard_bytes: Optional[int]
    used_inodes: Optional[int]
    soft_inodes: Optional[int]
    hard_inodes: Optional[int]
    grace_period_seconds: Optional[int]


@dataclass
class WindowsQuotaStatus:
    """
        Volume-wide quota enforcement posture.

        :param mountpoint: Volume token.
        :param enabled: True when quota tracking
                        (``Logging Status``) is enabled.
        :param enforcing: True when ``Enforce Limit Status`` is
                          enabled.
        :param user_quotas_active: Mirrors ``enabled`` (NTFS
                                   tracks per-user only, so
                                   "logging" is always
                                   user-scoped).
        :param group_quotas_active: Always ``False`` — NTFS
                                    volume quotas have no group
                                    concept.
        :param filesystem_type: Always ``"ntfs"`` when the query
                                succeeded; ``None`` when the
                                tool reported a different type
                                (rare).
    """
    mountpoint: str
    enabled: bool
    enforcing: bool
    user_quotas_active: bool
    group_quotas_active: bool
    filesystem_type: Optional[str]


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsQuotaQueryError(Exception):
    """
        Raised when a quota read fails for a reason other than
        the principal simply being absent, or when the read is
        an unsupported NTFS group operation.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int] = None,
        stderr: str = "",
        detail: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (or would have).
            :param exit_code: The runner's exit code, when
                              relevant.
            :param stderr: The runner's captured stderr, when
                           relevant.
            :param detail: Optional detail token (e.g.
                           ``"ntfs_group_quota_unsupported"``).
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr
        self.detail = detail

        err_msg_lines = ["Quota query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        if detail is not None:
            err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsQuotaFilesystemNotMountedError(Exception):
    """
        Reserved for future use.  ``fsutil quota`` does not
        provide a clean "not mounted" signal; failed volume
        access surfaces through ``fsutil`` exit codes.
    """


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsFilesystemQuotasManager(BaseFeatureManager):
    """
        Windows filesystem-quotas manager.  Wraps ``fsutil
        quota`` through the injected :class:`ICommandRunner`.
    """

    result_class = WindowsQuotaActionResult
    status_class = WindowsQuotaActionStatus
    feature_name = "filesystem-quotas"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_quotas(
        self,
        mountpoint: str,
    ) -> WindowsQuotaActionResult:
        """
            Enable quota tracking and enforcement on
            ``mountpoint``.

            Runs ``fsutil quota track <volume>`` (begin
            accounting) followed by ``fsutil quota enforce
            <volume>`` (begin enforcement).  Returns ``SKIPPED``
            when the volume is already enforcing.

            :param mountpoint: The volume token (e.g. ``"C:"``).

            :returns: The :class:`WindowsQuotaActionResult`.
        """
        planned_command = self._planned_command_for_enable(
            mountpoint=mountpoint,
        )

        if self._config.dry_run:
            dry_result = WindowsQuotaActionResult(
                status=WindowsQuotaActionStatus.DRY_RUN,
                detail=mountpoint,
                command=list(planned_command),
                mountpoint=mountpoint,
            )
            return dry_result

        current_status = self.get_quota_status(mountpoint=mountpoint)
        if current_status.enforcing:
            skipped = WindowsQuotaActionResult(
                status=WindowsQuotaActionStatus.SKIPPED,
                detail=mountpoint,
                command=list(planned_command),
                mountpoint=mountpoint,
                message="quotas are already being enforced",
            )
            return skipped

        track_result = self._dispatch(
            command=planned_command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )

        if track_result.status != WindowsQuotaActionStatus.SUCCESS:
            return track_result

        enforce_command = [_FSUTIL, "quota", "enforce", mountpoint]
        enforce_result = self._dispatch(
            command=enforce_command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )
        return enforce_result

    def disable_quotas(
        self,
        mountpoint: str,
    ) -> WindowsQuotaActionResult:
        """
            Disable quota tracking and enforcement on
            ``mountpoint`` via ``fsutil quota disable
            <volume>``.  Returns ``SKIPPED`` when quotas are
            already disabled.

            :param mountpoint: The volume token.

            :returns: The :class:`WindowsQuotaActionResult`.
        """
        command = [_FSUTIL, "quota", "disable", mountpoint]

        if self._config.dry_run:
            dry_result = WindowsQuotaActionResult(
                status=WindowsQuotaActionStatus.DRY_RUN,
                detail=mountpoint,
                command=list(command),
                mountpoint=mountpoint,
            )
            return dry_result

        current_status = self.get_quota_status(mountpoint=mountpoint)
        if not current_status.enabled:
            skipped = WindowsQuotaActionResult(
                status=WindowsQuotaActionStatus.SKIPPED,
                detail=mountpoint,
                command=list(command),
                mountpoint=mountpoint,
                message="quotas are already disabled",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )
        return rtnval

    def set_user_quota(
        self,
        mountpoint: str,
        user: str,
        soft_bytes: int,
        hard_bytes: int,
    ) -> WindowsQuotaActionResult:
        """
            Set the soft (threshold) and hard byte quota for
            ``user`` on ``mountpoint`` via ``fsutil quota
            modify <volume> <hard_bytes> <soft_bytes> <user>``.

            :param mountpoint: The volume token.
            :param user: User name or SID.
            :param soft_bytes: Soft / threshold limit in bytes.
            :param hard_bytes: Hard limit in bytes.

            :returns: The :class:`WindowsQuotaActionResult`.
        """
        command = [
            _FSUTIL,
            "quota",
            "modify",
            mountpoint,
            str(hard_bytes),
            str(soft_bytes),
            user,
        ]

        if self._config.dry_run:
            dry_result = WindowsQuotaActionResult(
                status=WindowsQuotaActionStatus.DRY_RUN,
                detail=user,
                command=list(command),
                mountpoint=mountpoint,
                principal=user,
            )
            return dry_result

        current = self.get_user_quota(mountpoint=mountpoint, user=user)
        if current is not None:
            soft_matches = current.soft_bytes == soft_bytes
            hard_matches = current.hard_bytes == hard_bytes
            if soft_matches and hard_matches:
                skipped = WindowsQuotaActionResult(
                    status=WindowsQuotaActionStatus.SKIPPED,
                    detail=user,
                    command=list(command),
                    mountpoint=mountpoint,
                    principal=user,
                    message="current quota already matches the requested limits",
                )
                return skipped

        rtnval = self._dispatch(
            command=command,
            detail=user,
            mountpoint=mountpoint,
            principal=user,
        )
        return rtnval

    def set_group_quota(
        self,
        mountpoint: str,
        group: str,
        soft_bytes: int,
        hard_bytes: int,
    ) -> WindowsQuotaActionResult:
        """
            NTFS volume-level quotas (``fsutil quota``) do NOT
            support per-group enforcement.  This method always
            returns ``FAILED`` with
            ``detail="ntfs_group_quota_unsupported"`` and never
            invokes the runner — even under dry-run mode,
            because the operation is unsupported by the
            underlying tool rather than skipped by policy.

            :param mountpoint: The volume token (echoed onto the
                               result).
            :param group: The group name (echoed onto the result
                          as ``principal``).
            :param soft_bytes: Soft limit (recorded but not
                               applied).
            :param hard_bytes: Hard limit (recorded but not
                               applied).

            :returns: A FAILED :class:`WindowsQuotaActionResult`
                      with the documented detail token.
        """
        # The argv is recorded only as documentation of what the
        # caller asked for; the runner is NOT invoked.
        documented_argv = [
            _FSUTIL,
            "quota",
            "modify",
            mountpoint,
            str(hard_bytes),
            str(soft_bytes),
            group,
        ]

        rtnval = WindowsQuotaActionResult(
            status=WindowsQuotaActionStatus.FAILED,
            detail=_GROUP_UNSUPPORTED_DETAIL,
            command=list(documented_argv),
            mountpoint=mountpoint,
            principal=group,
            message="NTFS volume quotas do not support per-group enforcement; FSRM dirquota is a server-only follow-up",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_user_quota(
        self,
        mountpoint: str,
        user: str,
    ) -> Optional[WindowsQuotaInfo]:
        """
            Return :class:`WindowsQuotaInfo` for ``user`` on
            ``mountpoint`` or ``None`` when the user has no
            record on this volume.

            :param mountpoint: The volume token.
            :param user: User name or SID.

            :returns: The parsed info or ``None``.

            :raises WindowsQuotaQueryError: when ``fsutil quota
                query`` exits non-zero.
        """
        _status_dict, user_records = self._fetch_query_records(
            mountpoint=mountpoint,
        )

        rtnval: Optional[WindowsQuotaInfo] = None
        normalised_target = user.lower()
        for record in user_records:
            candidate_name = record.principal.lower()
            if candidate_name == normalised_target:
                rtnval = record
                break
        return rtnval

    def get_group_quota(
        self,
        mountpoint: str,
        group: str,
    ) -> Optional[WindowsQuotaInfo]:
        """
            NTFS volume-level quotas do NOT support per-group
            enforcement.  This method always raises
            :class:`WindowsQuotaQueryError` carrying
            ``detail="ntfs_group_quota_unsupported"`` and never
            invokes the runner.

            :param mountpoint: The volume token (echoed).
            :param group: The group name (echoed).

            :raises WindowsQuotaQueryError: always.
        """
        documented_argv = [_FSUTIL, "quota", "query", mountpoint]
        raise WindowsQuotaQueryError(
            command=documented_argv,
            exit_code=None,
            stderr="",
            detail=_GROUP_UNSUPPORTED_DETAIL,
        )

    def list_quotas(
        self,
        mountpoint: str,
    ) -> list[WindowsQuotaInfo]:
        """
            Enumerate every per-user quota on ``mountpoint`` via
            ``fsutil quota query <volume>``.  Only USER-kind
            entries are emitted because NTFS volume quotas have
            no group surface.

            :param mountpoint: The volume token.

            :returns: One :class:`WindowsQuotaInfo` per per-user
                      block.

            :raises WindowsQuotaQueryError: when ``fsutil quota
                query`` exits non-zero.
        """
        _status_dict, user_records = self._fetch_query_records(
            mountpoint=mountpoint,
        )
        rtnval = list(user_records)
        return rtnval

    def get_quota_status(
        self,
        mountpoint: str,
    ) -> WindowsQuotaStatus:
        """
            Query volume-wide quota state via ``fsutil quota
            query <volume>``.

            :param mountpoint: The volume token.

            :returns: The :class:`WindowsQuotaStatus`.

            :raises WindowsQuotaQueryError: when ``fsutil quota
                query`` exits non-zero.
        """
        status_dict, _user_records = self._fetch_query_records(
            mountpoint=mountpoint,
        )

        logging_value = status_dict.get("logging_status", "").lower()
        enforce_value = status_dict.get("enforce_limit_status", "").lower()
        fstype_value = status_dict.get("file_system_type_status")

        logging_active = logging_value == "enabled"
        enforce_active = enforce_value == "enabled"

        if fstype_value is None:
            fstype_normalised: Optional[str] = "ntfs"
        else:
            fstype_normalised = fstype_value.lower()

        rtnval = WindowsQuotaStatus(
            mountpoint=mountpoint,
            enabled=logging_active,
            enforcing=enforce_active,
            user_quotas_active=logging_active,
            group_quotas_active=False,
            filesystem_type=fstype_normalised,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _planned_command_for_enable(
        self,
        *,
        mountpoint: str,
    ) -> list[str]:
        """
            Return the ``fsutil quota track`` argv used as the
            dry-run record for :meth:`enable_quotas`.

            :param mountpoint: The volume token.
        """
        rtnval = [_FSUTIL, "quota", "track", mountpoint]
        return rtnval

    def _fetch_query_records(
        self,
        *,
        mountpoint: str,
    ) -> tuple[dict, list[WindowsQuotaInfo]]:
        """
            Run ``fsutil quota query <mountpoint>`` and return
            the parsed (status_dict, user_records) tuple.
            Reads do not elevate.

            :param mountpoint: The volume token.

            :returns: ``(status_dict, user_records)``.

            :raises WindowsQuotaQueryError: when ``fsutil`` exits
                non-zero.
        """
        command = [_FSUTIL, "quota", "query", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise WindowsQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_fsutil_quota_query(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
        )
        return rtnval


# ----------------------------------------------------------------------
# Parser helpers (module-scoped, pure functions)
# ----------------------------------------------------------------------


def _parse_fsutil_quota_query(
    *,
    stdout: str,
    mountpoint: str,
) -> tuple[dict, list[WindowsQuotaInfo]]:
    """
        Parse ``fsutil quota query <volume>`` output into a
        ``(status_dict, list_of_user_records)`` tuple.

        Output shape::

            *  Volume Name = ...
            *  File System Type Status: NTFS
            *  Logging Status:          Enabled
            *  Enforce Limit Status:    Enabled
            *  Default Quota Threshold: 0xffffffffffffffff
            *  Default Quota Limit:     0xffffffffffffffff

              Quota User Name:          DOMAIN\\alice
              Quota User SID:            S-1-5-...
              Change time:              ...
              Quota Used:               1048576
              Quota Threshold:          524288000
              Quota Limit:              1073741824

              Quota User Name:          ...

        :param stdout: Raw fsutil stdout.
        :param mountpoint: Volume token to record on each
                           record.

        :returns: ``(status_dict, list_of_user_records)``.
    """
    status_dict: dict = {}
    user_records: list[WindowsQuotaInfo] = []

    current_block: Optional[dict] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if len(line.strip()) == 0:
            continue

        is_header = line.lstrip().startswith("*")
        body = line.lstrip()
        if is_header:
            body = body[1:].strip()

        # Split on the first ``:``.
        if ":" not in body:
            continue

        key_token, value_token = body.split(":", 1)
        key_normalised = _normalise_status_key(token=key_token)
        value_stripped = value_token.strip()

        if is_header:
            status_dict[key_normalised] = value_stripped
            continue

        # Per-user block: a new block starts at "Quota User Name".
        if key_normalised == "quota_user_name":
            if current_block is not None:
                record = _build_user_record(
                    block=current_block,
                    mountpoint=mountpoint,
                )
                if record is not None:
                    user_records.append(record)
            current_block = {"quota_user_name": value_stripped}
            continue

        if current_block is None:
            continue
        current_block[key_normalised] = value_stripped

    if current_block is not None:
        record = _build_user_record(
            block=current_block,
            mountpoint=mountpoint,
        )
        if record is not None:
            user_records.append(record)

    rtnval = (status_dict, user_records)
    return rtnval


def _build_user_record(
    *,
    block: dict,
    mountpoint: str,
) -> Optional[WindowsQuotaInfo]:
    """
        Map a single ``Quota User Name`` block (parsed from
        ``fsutil quota query``) into a
        :class:`WindowsQuotaInfo`.  Returns ``None`` when the
        block lacks a principal name.

        :param block: Dict of normalised key -> value.
        :param mountpoint: Volume token.
    """
    principal_name = block.get("quota_user_name")
    if principal_name is None:
        rtnval: Optional[WindowsQuotaInfo] = None
        return rtnval

    used_token = block.get("quota_used", "")
    threshold_token = block.get("quota_threshold", "")
    limit_token = block.get("quota_limit", "")

    used_bytes = _quota_token_to_bytes(token=used_token)
    soft_bytes = _quota_token_to_bytes(token=threshold_token)
    hard_bytes = _quota_token_to_bytes(token=limit_token)

    rtnval = WindowsQuotaInfo(
        mountpoint=mountpoint,
        principal=principal_name,
        principal_kind=WindowsQuotaPrincipalKind.USER,
        used_bytes=used_bytes,
        soft_bytes=soft_bytes,
        hard_bytes=hard_bytes,
        used_inodes=None,
        soft_inodes=None,
        hard_inodes=None,
        grace_period_seconds=None,
    )
    return rtnval


def _quota_token_to_bytes(*, token: str) -> Optional[int]:
    """
        Convert an ``fsutil quota`` byte-count token to an
        integer byte count, or ``None`` for "no limit"
        sentinels and unparseable tokens.

        Accepted forms:
            * Decimal integer: ``"1048576"``.
            * Hex sentinel: ``"0xffffffffffffffff"`` → ``None``.
            * Hex non-sentinel: ``"0x100000"`` → ``1048576``.

        :param token: The raw token.
    """
    cleaned = token.strip()
    if len(cleaned) == 0:
        rtnval: Optional[int] = None
        return rtnval

    lowered = cleaned.lower()
    try:
        if lowered.startswith("0x"):
            value = int(lowered, 16)
        else:
            value = int(cleaned)
    except ValueError:
        rtnval = None
        return rtnval

    if value == _NO_LIMIT_SENTINEL:
        rtnval = None
        return rtnval

    rtnval = value
    return rtnval


def _normalise_status_key(*, token: str) -> str:
    """
        Normalise a status / per-user-block key (e.g.
        ``"  Quota User Name"`` →
        ``"quota_user_name"``).

        :param token: The raw key token.
    """
    body = token.strip().lower()
    rtnval_parts: list[str] = []
    for ch in body:
        if ch.isalnum():
            rtnval_parts.append(ch)
        elif ch in (" ", "\t", "-", "/"):
            if len(rtnval_parts) > 0 and rtnval_parts[-1] != "_":
                rtnval_parts.append("_")
        # Anything else is dropped.

    rtnval = "".join(rtnval_parts).strip("_")
    return rtnval

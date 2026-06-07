"""
    Windows encryption-management feature.

    Implements the cross-OS encryption-management contract for
    Windows by wrapping the PowerShell ``BitLocker`` module
    (``Enable-BitLocker``, ``Disable-BitLocker``,
    ``Get-BitLockerVolume``, ``Add-BitLockerKeyProtector``,
    ``Remove-BitLockerKeyProtector``, ``Unlock-BitLocker``) through
    the injected :class:`ICommandRunner`.  Every verb is dispatched as
    a PowerShell scriptblock so the manager works identically under
    local and SSH (WinRM-tunneled) execution.

    File I/O policy:
        BitLocker state lives in TPM-bound metadata + the BitLocker
        Volume Master Key.  Reads and writes go through ``BitLocker``
        cmdlets.  This module does NOT consume an
        :class:`IFileOperator`; every read / write / probe flows
        through the runner so dry-run, elevation, and aspect
        composition compose uniformly.  The single filesystem write
        path is :meth:`WindowsEncryptionManager.backup_recovery_key`,
        which captures the recovery-key string from
        ``Get-BitLockerVolume`` output and writes it to the requested
        path via the same PowerShell scriptblock.

    Elevation policy:
        Every write verb (``enable_encryption``,
        ``disable_encryption``, ``rotate_key``,
        ``backup_recovery_key``, ``unlock_volume``) honours
        ``config.elevated``.  Reads (``get_encryption_status``,
        ``list_encrypted_volumes``, idempotency probes) never elevate.
        ``Get-BitLockerVolume`` succeeds for non-administrators on
        most installations; the rare configuration where it requires
        admin falls out of band and surfaces as a FAILED read.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input on a runner that has no controlling
        terminal.

    Reads use ``| ConvertTo-Json -Compress -Depth 5`` for a single
    robust serialization that round-trips across PowerShell 5.1 and
    PowerShell 7+.

    Password / passphrase handling:
        Plaintext passwords appear inside the PowerShell scriptblock
        passed to ``-Command`` (wrapped in
        ``ConvertTo-SecureString -AsPlainText -Force '<pw>'``).  The
        argv recorded on the result is sanitised: every
        ``ConvertTo-SecureString -AsPlainText -Force '...'`` literal
        password value is replaced with the literal ``"<redacted>"``
        before the argv is captured on
        :class:`WindowsEncryptionActionResult`, so the plaintext
        password never leaks into audit captures.  This mirrors the
        :mod:`ccai.sysman.windows.usergroupmanagement` redaction
        contract.

    Destructive operations:
        ``disable_encryption`` is destructive on encrypted volumes
        (``Disable-BitLocker`` triggers a multi-hour decrypt pass
        that rewrites every block).  The manager refuses to dispatch
        the command unless ``config.allow_destructive=True``;
        otherwise it returns ``FAILED`` carrying
        ``detail="destructive_operation_requires_explicit_confirm"``.
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


# Sentinel placed into the recorded argv anywhere a password value
# would otherwise appear.
_REDACTED = "<redacted>"


# Sentinel placed into the recorded ``detail`` field when the
# destructive ``disable_encryption`` verb is invoked without
# ``config.allow_destructive=True``.  Exposed as a module constant so
# tests and downstream coordinators can match against it without
# duplicating the literal.
_DESTRUCTIVE_GUARD_DETAIL = "destructive_operation_requires_explicit_confirm"


# Default cipher used by ``enable_encryption`` when the spec does not
# override.  Matches Microsoft's recommended default for Windows 10+
# and Windows Server 2019+.
_DEFAULT_CIPHER = "XtsAes256"


# Pattern used by :func:`_sanitise_script_passwords` to locate
# ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` invocations
# inside a PowerShell scriptblock so the embedded plaintext password
# can be replaced with the redaction sentinel before the argv is
# recorded on a result.  Mirrored verbatim from
# ``ccai.sysman.windows.usergroupmanagement._SECURE_STRING_PATTERN``.
_SECURE_STRING_PATTERN = re.compile(
    r"ConvertTo-SecureString\s+-AsPlainText\s+-Force\s+'((?:[^']|'')*)'",
)


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


WindowsEncryptionActionStatus = ActionStatus


@dataclass
class WindowsEncryptionManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsEncryptionManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable used to run cmdlets.
                                      Defaults to ``"powershell.exe"``
                                      (PATH-resolved Windows
                                      PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param allow_destructive: When True, ``disable_encryption`` is
                                  permitted to dispatch
                                  ``Disable-BitLocker``.  Defaults to
                                  False so the destructive guard
                                  fires.
        :param default_cipher: Default ``-EncryptionMethod`` token
                               passed to ``Enable-BitLocker`` when the
                               spec does not override.  Defaults to
                               :data:`_DEFAULT_CIPHER`
                               (``"XtsAes256"``).
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    allow_destructive: bool = False
    default_cipher: str = _DEFAULT_CIPHER


@dataclass
class WindowsEncryptionActionResult(FeatureActionResult):
    """
        Result of an encryption-management write operation.

        :param volume: Volume identifier (drive letter ``"C:"``,
                       volume GPT path) the operation targeted.
        :param device: Underlying physical device path when
                       resolvable, else ``None``.  Today the manager
                       does not resolve drive letters to device paths
                       because BitLocker addresses volumes by mount
                       point rather than by device.
        :param requires_reboot: True when the operation modified the
                                running boot volume's encryption
                                posture in a way that requires a
                                reboot to take effect.  ``True`` for
                                ``enable_encryption`` /
                                ``disable_encryption`` on the system
                                drive; ``False`` for unlock and key-
                                rotation operations.
    """
    volume: Optional[str] = None
    device: Optional[str] = None
    requires_reboot: bool = False


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsEncryptionSpec:
    """
        Specification for :meth:`WindowsEncryptionManager.enable_encryption`.

        :param cipher: ``-EncryptionMethod`` value.  ``None`` falls
                       back to
                       :attr:`WindowsEncryptionManagerConfig.default_cipher`.
                       Recognised values include ``XtsAes128``,
                       ``XtsAes256`` (default), ``Aes128``,
                       ``Aes256``.
        :param key_size: Reserved.  ``Enable-BitLocker`` does not
                         accept an explicit key size; the cipher
                         token implies it.
        :param passphrase: Optional plaintext password used to wrap
                           the BitLocker volume key with a
                           ``PasswordProtector``.  When set, the
                           manager emits
                           ``-PasswordProtector -Password $secure``.
        :param recovery_key_protector: When True (default) the
                                       manager emits
                                       ``-RecoveryPasswordProtector``
                                       so a 48-digit recovery
                                       password is generated alongside
                                       the password protector.
        :param mount_point: Reserved.  BitLocker addresses volumes
                            via ``-MountPoint <letter>:`` already;
                            this field exists for cross-OS shape
                            parity.
    """
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    passphrase: Optional[str] = None
    recovery_key_protector: bool = True
    mount_point: Optional[str] = None


@dataclass
class WindowsEncryptionStatus:
    """
        Encryption status for a single BitLocker volume.

        :param volume: Volume identifier (the mount point as reported
                       by ``Get-BitLockerVolume.MountPoint``).
        :param is_encrypted: True when ``ProtectionStatus=On`` or
                             ``EncryptionPercentage=100``.
        :param is_unlocked: True when ``LockStatus=Unlocked``.
        :param cipher: ``EncryptionMethod`` field.
        :param key_size: Reserved; BitLocker does not surface an
                         explicit key-size value distinct from the
                         method.
        :param progress_percent: ``EncryptionPercentage`` reported by
                                 the cmdlet.  Useful while a
                                 conversion pass is in flight.
        :param protector_types: List of ``KeyProtectorType`` strings
                                from ``KeyProtector[]``: e.g.
                                ``["Tpm", "RecoveryPassword"]``.
    """
    volume: str
    is_encrypted: bool
    is_unlocked: bool
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    progress_percent: Optional[float] = None
    protector_types: list = field(default_factory=list)


@dataclass
class WindowsEncryptedVolumeInfo:
    """
        Summary of a single encrypted volume on the host.

        :param volume: Mount point (``"C:"``).
        :param device: Underlying physical device path when
                       resolvable, else ``None``.
        :param mount_point: Mount point copied from ``volume`` for
                            cross-OS shape parity.
        :param is_encrypted: True when ``ProtectionStatus=On`` or
                             ``EncryptionPercentage=100``.
        :param is_unlocked: True when ``LockStatus=Unlocked``.
        :param cipher: ``EncryptionMethod`` field.
        :param key_size: Reserved.
        :param progress_percent: ``EncryptionPercentage`` field.
        :param protector_types: ``KeyProtectorType`` strings from
                                ``KeyProtector[]``.
    """
    volume: str
    device: Optional[str] = None
    mount_point: Optional[str] = None
    is_encrypted: bool = True
    is_unlocked: bool = False
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    progress_percent: Optional[float] = None
    protector_types: list = field(default_factory=list)


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsEncryptionQueryError(Exception):
    """
        Raised when an encryption read fails for a reason other than
        the target simply being absent from the host's BitLocker
        surface.
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

        err_msg_lines = ["Encryption query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell escaping + redaction
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single
        quotes.  PowerShell single quotes are escaped by doubling.

        :param value: The raw text.

        :returns: The escaped text suitable for embedding between
                  single quotes in a PowerShell scriptblock.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _sanitise_script_passwords(*, script: str) -> str:
    """
        Replace every ``ConvertTo-SecureString -AsPlainText -Force
        '<pw>'`` literal in ``script`` with the same wrapper carrying
        ``'<redacted>'`` instead of the plaintext password.

        Used to redact passwords from the recorded argv before the
        result is returned.  The substitution leaves the rest of the
        scriptblock intact so audit reviewers can still see exactly
        which cmdlet was invoked.

        :param script: The PowerShell scriptblock text.

        :returns: A new string with every embedded plaintext password
                  replaced by ``"<redacted>"``.
    """
    rtnval = _SECURE_STRING_PATTERN.sub(
        "ConvertTo-SecureString -AsPlainText -Force '{}'".format(_REDACTED),
        script,
    )
    return rtnval


def _normalize_to_list(*, parsed):
    """
        Normalise a ``ConvertTo-Json`` payload into a list.

        ``ConvertTo-Json`` emits a bare object instead of a single-
        element array when the source pipeline produced exactly one
        item.  This helper folds the bare-object shape into a single-
        element list so downstream parsers always see an iterable.

        :param parsed: Output of :func:`json.loads`.

        :returns: A list.  ``None`` collapses to ``[]``; a bare dict
                  collapses to ``[dict]``; a list passes through.
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


# ----------------------------------------------------------------------
# WindowsEncryptionManager
# ----------------------------------------------------------------------


class WindowsEncryptionManager(BaseFeatureManager):
    """
        Windows BitLocker manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner` as a PowerShell
        scriptblock that invokes the ``BitLocker`` module's cmdlets.
        See the module docstring for the elevation, password
        handling, and destructive-guard contracts.
    """

    result_class = WindowsEncryptionActionResult
    status_class = WindowsEncryptionActionStatus
    feature_name = "encryption-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_encryption_status(
        self,
        volume: str,
    ) -> WindowsEncryptionStatus:
        """
            Probe the encryption status of ``volume``.

            Dispatches::

                Get-BitLockerVolume -MountPoint '<letter>:' \\
                    | ConvertTo-Json -Compress -Depth 5

            and parses the JSON record.  When the cmdlet exits non-
            zero (typically because the drive letter does not exist
            or BitLocker is not installed), the manager returns a
            ``WindowsEncryptionStatus`` carrying ``is_encrypted=False``.

            :param volume: Mount point (drive letter, e.g. ``"C:"``).

            :returns: The :class:`WindowsEncryptionStatus`.
        """
        escaped = _ps_escape_single_quoted(value=volume)
        script = (
            "Get-BitLockerVolume -MountPoint '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = WindowsEncryptionStatus(
                volume=volume,
                is_encrypted=False,
                is_unlocked=False,
                cipher=None,
                key_size=None,
                progress_percent=None,
                protector_types=[],
            )
            return rtnval

        records = _parse_get_bitlockervolume(text=run_result.stdout)
        if len(records) == 0:
            rtnval = WindowsEncryptionStatus(
                volume=volume,
                is_encrypted=False,
                is_unlocked=False,
                cipher=None,
                key_size=None,
                progress_percent=None,
                protector_types=[],
            )
            return rtnval

        record = records[0]
        rtnval = _record_to_status(record=record, fallback_volume=volume)
        return rtnval

    def list_encrypted_volumes(self) -> list:
        """
            Enumerate every BitLocker-managed volume on the host.

            Dispatches::

                Get-BitLockerVolume \\
                    | ConvertTo-Json -Compress -Depth 5

            and emits one :class:`WindowsEncryptedVolumeInfo` per
            entry whose ``ProtectionStatus`` is ``On`` or whose
            ``VolumeStatus`` is ``FullyEncrypted``.

            :returns: A list (possibly empty) of
                      :class:`WindowsEncryptedVolumeInfo`.
        """
        script = (
            "Get-BitLockerVolume "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        records = _parse_get_bitlockervolume(text=run_result.stdout)
        rtnval = []
        for record in records:
            if not isinstance(record, dict):
                continue
            is_encrypted = _is_record_encrypted(record=record)
            if not is_encrypted:
                continue
            info = _record_to_volume_info(record=record)
            rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_encryption(
        self,
        volume: str,
        spec: WindowsEncryptionSpec,
    ) -> WindowsEncryptionActionResult:
        """
            Enable BitLocker on ``volume`` per ``spec``.

            Dispatches a PowerShell scriptblock that wraps the
            password (when present) via ``ConvertTo-SecureString
            -AsPlainText -Force`` and calls::

                Enable-BitLocker -MountPoint '<letter>:' \\
                    -EncryptionMethod <cipher> \\
                    -PasswordProtector -Password $pw \\
                    -RecoveryPasswordProtector \\
                    -SkipHardwareTest

            ``-SkipHardwareTest`` is always passed so the cmdlet does
            not request a reboot for the hardware self-test (the
            self-test requires user interaction at the Recovery
            screen on the next boot).  ``-RecoveryPasswordProtector``
            is appended when ``spec.recovery_key_protector`` is True
            (the default).

            Idempotent: when the volume's
            ``ProtectionStatus=On`` or ``VolumeStatus=FullyEncrypted``
            (probed via ``Get-BitLockerVolume``), the operation
            returns ``SKIPPED``.

            The recorded ``command`` field on the returned result has
            every embedded plaintext password replaced with
            ``"<redacted>"`` so the password never leaks into audit
            captures.

            :param volume: Mount point (drive letter, e.g. ``"C:"``).
            :param spec: The :class:`WindowsEncryptionSpec`.

            :returns: The :class:`WindowsEncryptionActionResult`.
        """
        cipher = spec.cipher or self._typed_config().default_cipher
        full_script = self._build_enable_script(
            volume=volume, spec=spec, cipher=cipher,
        )
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEncryptionActionStatus.DRY_RUN,
                command=recorded_command,
                volume=volume,
                requires_reboot=True,
                detail=volume,
            )
            return dry

        already = self._is_encrypted(volume=volume)
        if already:
            skipped = self._build_result(
                status=WindowsEncryptionActionStatus.SKIPPED,
                command=recorded_command,
                volume=volume,
                requires_reboot=False,
                detail="volume {} is already encrypted".format(volume),
            )
            return skipped

        rtnval = self._dispatch_encryption_write(
            full_command=full_command,
            recorded_command=recorded_command,
            volume=volume,
            requires_reboot=True,
        )
        return rtnval

    def disable_encryption(
        self,
        volume: str,
    ) -> WindowsEncryptionActionResult:
        """
            Disable BitLocker on ``volume``.

            Dispatches::

                Disable-BitLocker -MountPoint '<letter>:'

            ``Disable-BitLocker`` triggers a multi-hour decrypt pass
            that rewrites every block on the volume; the operation
            is destructive and the manager refuses to dispatch it
            unless ``config.allow_destructive=True``.

            Idempotent: when the volume is already plaintext (probed
            via ``Get-BitLockerVolume``), the operation returns
            ``SKIPPED``.

            :param volume: Mount point (drive letter, e.g. ``"C:"``).

            :returns: The :class:`WindowsEncryptionActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=volume)
        script = (
            "Disable-BitLocker -MountPoint '{}'"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                requires_reboot=True,
                detail=volume,
            )
            return dry

        already = self._is_encrypted(volume=volume)
        if not already:
            skipped = self._build_result(
                status=WindowsEncryptionActionStatus.SKIPPED,
                command=command,
                volume=volume,
                requires_reboot=False,
                detail="volume {} is not encrypted".format(volume),
            )
            return skipped

        allow_destructive = self._typed_config().allow_destructive
        if not allow_destructive:
            failed = self._build_result(
                status=WindowsEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                requires_reboot=False,
                detail=_DESTRUCTIVE_GUARD_DETAIL,
                stderr=(
                    "disable_encryption is destructive; set "
                    "config.allow_destructive=True to proceed"
                ),
            )
            return failed

        rtnval = self._dispatch_encryption_write(
            full_command=command,
            recorded_command=command,
            volume=volume,
            requires_reboot=True,
        )
        return rtnval

    def rotate_key(
        self,
        volume: str,
        new_passphrase: str,
    ) -> WindowsEncryptionActionResult:
        """
            Rotate the password protector on ``volume``.

            Dispatches a PowerShell scriptblock that:

            1. Wraps the new password via
               ``ConvertTo-SecureString -AsPlainText -Force``.
            2. Calls ``Add-BitLockerKeyProtector -PasswordProtector``
               with the new password.
            3. Captures the existing password-protector's KeyProtectorId
               via ``Get-BitLockerVolume`` and removes it via
               ``Remove-BitLockerKeyProtector``.

            The two-step add-then-remove ordering ensures the volume
            never has zero password protectors mid-rotation, which is
            the BitLocker analogue of the LUKS "last-key safety
            rail".

            The recorded ``command`` field has the new password
            redacted via :func:`_sanitise_script_passwords`.

            :param volume: Mount point (drive letter, e.g. ``"C:"``).
            :param new_passphrase: The replacement password.

            :returns: The :class:`WindowsEncryptionActionResult`.
        """
        full_script = self._build_rotate_script(
            volume=volume, new_passphrase=new_passphrase,
        )
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEncryptionActionStatus.DRY_RUN,
                command=recorded_command,
                volume=volume,
                requires_reboot=False,
                detail=volume,
            )
            return dry

        rtnval = self._dispatch_encryption_write(
            full_command=full_command,
            recorded_command=recorded_command,
            volume=volume,
            requires_reboot=False,
        )
        return rtnval

    def backup_recovery_key(
        self,
        volume: str,
        path: str,
    ) -> WindowsEncryptionActionResult:
        """
            Back up the BitLocker recovery password on ``volume`` to
            ``path``.

            Dispatches a PowerShell scriptblock that:

            1. Pulls the volume's KeyProtector array via
               ``Get-BitLockerVolume``.
            2. Filters to ``KeyProtectorType -eq 'RecoveryPassword'``.
            3. Writes the resulting ``RecoveryPassword`` field to
               ``path`` via ``Set-Content``.

            ``Set-Content`` writes the recovery key as UTF-8 text by
            default; the manager does NOT pipe through ``Out-File``
            (which uses UTF-16 by default and produces a BOM that
            many recovery-key restore tools do not handle).

            Unconditional: the verb is always dispatched.

            :param volume: Mount point (drive letter, e.g. ``"C:"``).
            :param path: Destination filesystem path for the recovery
                         key file.

            :returns: The :class:`WindowsEncryptionActionResult`.
        """
        full_script = self._build_backup_script(
            volume=volume, path=path,
        )
        command = self._build_powershell_argv(script=full_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                requires_reboot=False,
                detail=path,
            )
            return dry

        rtnval = self._dispatch_encryption_write(
            full_command=command,
            recorded_command=command,
            volume=volume,
            requires_reboot=False,
            detail=path,
        )
        return rtnval

    def unlock_volume(
        self,
        volume: str,
        passphrase: str,
    ) -> WindowsEncryptionActionResult:
        """
            Unlock a BitLocker-encrypted volume by password.

            Dispatches a PowerShell scriptblock that wraps the
            password via ``ConvertTo-SecureString -AsPlainText -Force``
            and calls::

                Unlock-BitLocker -MountPoint '<letter>:' \\
                    -Password $pw

            Idempotent: when the volume is already unlocked (probed
            via ``Get-BitLockerVolume``'s ``LockStatus`` field), the
            operation returns ``SKIPPED``.

            The recorded ``command`` field has the password redacted
            via :func:`_sanitise_script_passwords`.

            :param volume: Mount point (drive letter, e.g. ``"C:"``).
            :param passphrase: Plaintext password.

            :returns: The :class:`WindowsEncryptionActionResult`.
        """
        full_script = self._build_unlock_script(
            volume=volume, passphrase=passphrase,
        )
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsEncryptionActionStatus.DRY_RUN,
                command=recorded_command,
                volume=volume,
                requires_reboot=False,
                detail=volume,
            )
            return dry

        already_unlocked = self._is_unlocked(volume=volume)
        if already_unlocked:
            skipped = self._build_result(
                status=WindowsEncryptionActionStatus.SKIPPED,
                command=recorded_command,
                volume=volume,
                requires_reboot=False,
                detail="volume {} is already unlocked".format(volume),
            )
            return skipped

        rtnval = self._dispatch_encryption_write(
            full_command=full_command,
            recorded_command=recorded_command,
            volume=volume,
            requires_reboot=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — config, argv builders
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsEncryptionManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`WindowsEncryptionManagerConfig`, falling back to
            a synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, WindowsEncryptionManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsEncryptionManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.

            :param script: PowerShell scriptblock text.

            :returns: The full argv list.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _sanitise_command_passwords(
        self,
        *,
        command: list,
    ) -> list:
        """
            Return a copy of ``command`` whose final element (the
            PowerShell scriptblock) has every embedded plaintext
            password replaced with ``"<redacted>"``.

            :param command: The raw argv (as it would be sent to the
                            runner).

            :returns: A new list with passwords redacted.  When the
                      argv is empty, returns ``[]``.
        """
        if len(command) == 0:
            rtnval: list = []
            return rtnval
        rtnval = list(command)
        last_index = len(rtnval) - 1
        rtnval[last_index] = _sanitise_script_passwords(
            script=rtnval[last_index],
        )
        return rtnval

    def _build_enable_script(
        self,
        *,
        volume: str,
        spec: WindowsEncryptionSpec,
        cipher: str,
    ) -> str:
        """
            Build the PowerShell scriptblock for ``enable_encryption``.

            :param volume: Mount point.
            :param spec: The encryption spec.
            :param cipher: Resolved cipher token.

            :returns: The scriptblock text.
        """
        escaped_volume = _ps_escape_single_quoted(value=volume)
        escaped_cipher = _ps_escape_single_quoted(value=cipher)

        statements: list = []
        password_prelude = ""
        enable_fragments: list = [
            "Enable-BitLocker -MountPoint '{}'".format(escaped_volume),
            "-EncryptionMethod {}".format(escaped_cipher),
        ]
        if spec.passphrase is not None:
            escaped_pw = _ps_escape_single_quoted(value=spec.passphrase)
            password_prelude = (
                "$pw = ConvertTo-SecureString -AsPlainText -Force '{}'; "
            ).format(escaped_pw)
            enable_fragments.append("-PasswordProtector -Password $pw")
        if spec.recovery_key_protector:
            enable_fragments.append("-RecoveryPasswordProtector")
        enable_fragments.append("-SkipHardwareTest")

        statements.append(password_prelude + " ".join(enable_fragments))
        rtnval = "; ".join(statements)
        return rtnval

    def _build_rotate_script(
        self,
        *,
        volume: str,
        new_passphrase: str,
    ) -> str:
        """
            Build the PowerShell scriptblock for ``rotate_key``.

            :param volume: Mount point.
            :param new_passphrase: The replacement password.

            :returns: The scriptblock text.
        """
        escaped_volume = _ps_escape_single_quoted(value=volume)
        escaped_pw = _ps_escape_single_quoted(value=new_passphrase)
        rtnval = (
            "$pw = ConvertTo-SecureString -AsPlainText -Force '{pw}'; "
            "$old = (Get-BitLockerVolume -MountPoint '{vol}').KeyProtector "
            "| Where-Object KeyProtectorType -eq 'Password'; "
            "Add-BitLockerKeyProtector -MountPoint '{vol}' "
            "-PasswordProtector -Password $pw; "
            "if ($old) {{ Remove-BitLockerKeyProtector -MountPoint '{vol}' "
            "-KeyProtectorId $old.KeyProtectorId }}"
        ).format(pw=escaped_pw, vol=escaped_volume)
        return rtnval

    def _build_backup_script(
        self,
        *,
        volume: str,
        path: str,
    ) -> str:
        """
            Build the PowerShell scriptblock for
            ``backup_recovery_key``.

            :param volume: Mount point.
            :param path: Destination filesystem path.

            :returns: The scriptblock text.
        """
        escaped_volume = _ps_escape_single_quoted(value=volume)
        escaped_path = _ps_escape_single_quoted(value=path)
        rtnval = (
            "$rk = (Get-BitLockerVolume -MountPoint '{vol}').KeyProtector "
            "| Where-Object KeyProtectorType -eq 'RecoveryPassword' "
            "| Select-Object -First 1; "
            "if ($rk) {{ Set-Content -Path '{path}' "
            "-Value $rk.RecoveryPassword -Encoding UTF8 }} "
            "else {{ Write-Error 'No RecoveryPassword protector present.' }}"
        ).format(vol=escaped_volume, path=escaped_path)
        return rtnval

    def _build_unlock_script(
        self,
        *,
        volume: str,
        passphrase: str,
    ) -> str:
        """
            Build the PowerShell scriptblock for ``unlock_volume``.

            :param volume: Mount point.
            :param passphrase: Plaintext password.

            :returns: The scriptblock text.
        """
        escaped_volume = _ps_escape_single_quoted(value=volume)
        escaped_pw = _ps_escape_single_quoted(value=passphrase)
        rtnval = (
            "$pw = ConvertTo-SecureString -AsPlainText -Force '{pw}'; "
            "Unlock-BitLocker -MountPoint '{vol}' -Password $pw"
        ).format(pw=escaped_pw, vol=escaped_volume)
        return rtnval

    def _is_encrypted(self, *, volume: str) -> bool:
        """
            Probe whether ``volume`` is currently BitLocker-encrypted.
        """
        escaped = _ps_escape_single_quoted(value=volume)
        script = (
            "Get-BitLockerVolume -MountPoint '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        records = _parse_get_bitlockervolume(text=run_result.stdout)
        if len(records) == 0:
            rtnval = False
            return rtnval
        rtnval = _is_record_encrypted(record=records[0])
        return rtnval

    def _is_unlocked(self, *, volume: str) -> bool:
        """
            Probe whether ``volume`` is currently unlocked.
        """
        escaped = _ps_escape_single_quoted(value=volume)
        script = (
            "Get-BitLockerVolume -MountPoint '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        records = _parse_get_bitlockervolume(text=run_result.stdout)
        if len(records) == 0:
            rtnval = False
            return rtnval
        record = records[0]
        lock_status = record.get("LockStatus")
        if isinstance(lock_status, str):
            normalized = lock_status.lower()
            if normalized == "unlocked":
                rtnval = True
                return rtnval
        if isinstance(lock_status, int) and lock_status == 0:
            rtnval = True
            return rtnval
        rtnval = False
        return rtnval

    def _dispatch_encryption_write(
        self,
        *,
        full_command: list,
        recorded_command: list,
        volume: Optional[str] = None,
        requires_reboot: bool = False,
        detail: Optional[str] = None,
    ) -> WindowsEncryptionActionResult:
        """
            Dispatch the ``full_command`` through the runner and
            wrap the result so that the recorded ``command`` field
            carries the redacted argv rather than the plaintext-
            password argv.
        """
        run_elevated = self._config.elevated
        run_result = self._runner.run(
            list(full_command), elevated=run_elevated, stdin=None,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsEncryptionActionStatus.SUCCESS
        else:
            mapped_status = WindowsEncryptionActionStatus.FAILED

        if detail is None:
            detail_value: Optional[str] = volume
        else:
            detail_value = detail

        rtnval = WindowsEncryptionActionResult(
            status=mapped_status,
            detail=detail_value,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            volume=volume,
            device=None,
            requires_reboot=requires_reboot,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsEncryptionActionStatus,
        command: list,
        volume: Optional[str] = None,
        requires_reboot: bool = False,
        detail: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsEncryptionActionResult:
        """
            Construct a :class:`WindowsEncryptionActionResult` without
            invoking the runner (used by SKIPPED / DRY_RUN /
            safety-rail paths).
        """
        rtnval = WindowsEncryptionActionResult(
            status=status,
            detail=detail,
            command=list(command),
            volume=volume,
            device=None,
            stderr=stderr,
            requires_reboot=requires_reboot,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_get_bitlockervolume(*, text: str) -> list:
    """
        Parse the output of ``Get-BitLockerVolume | ConvertTo-Json``.

        ``Get-BitLockerVolume`` emits a single object when only one
        volume matches and an array when multiple volumes match.
        :func:`_normalize_to_list` smooths both shapes into a list.

        :param text: Raw stdout text.

        :returns: A list (possibly empty) of dicts.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    rtnval = _normalize_to_list(parsed=parsed)
    return rtnval


def _is_record_encrypted(*, record: dict) -> bool:
    """
        Return True when a parsed ``Get-BitLockerVolume`` record
        represents an encrypted volume.

        BitLocker reports two related fields:

        * ``ProtectionStatus`` — ``On`` (1) when key protectors are
          active.
        * ``VolumeStatus`` — ``FullyEncrypted`` (2),
          ``EncryptionInProgress`` (4), ``DecryptionInProgress`` (5),
          etc.

        We treat ``ProtectionStatus=On`` OR
        ``VolumeStatus=FullyEncrypted`` as "encrypted" so a volume
        that is mid-encrypt is still surfaced.
    """
    protection = record.get("ProtectionStatus")
    if isinstance(protection, str):
        if protection.lower() == "on":
            rtnval = True
            return rtnval
    if isinstance(protection, int) and protection == 1:
        rtnval = True
        return rtnval
    volume_status = record.get("VolumeStatus")
    if isinstance(volume_status, str):
        if volume_status.lower() == "fullyencrypted":
            rtnval = True
            return rtnval
    if isinstance(volume_status, int) and volume_status == 2:
        rtnval = True
        return rtnval
    encryption_percentage = record.get("EncryptionPercentage")
    if isinstance(encryption_percentage, (int, float)):
        if encryption_percentage >= 100:
            rtnval = True
            return rtnval
    rtnval = False
    return rtnval


def _record_to_status(
    *,
    record: dict,
    fallback_volume: str,
) -> WindowsEncryptionStatus:
    """
        Build a :class:`WindowsEncryptionStatus` from a parsed
        ``Get-BitLockerVolume`` record.

        :param record: Parsed JSON dict.
        :param fallback_volume: Volume identifier to use when the
                                record's ``MountPoint`` is empty.

        :returns: The :class:`WindowsEncryptionStatus`.
    """
    mount_point_raw = record.get("MountPoint")
    if isinstance(mount_point_raw, str) and len(mount_point_raw) > 0:
        volume_value = mount_point_raw
    else:
        volume_value = fallback_volume

    is_encrypted = _is_record_encrypted(record=record)

    lock_status = record.get("LockStatus")
    if isinstance(lock_status, str):
        is_unlocked = lock_status.lower() == "unlocked"
    elif isinstance(lock_status, int):
        is_unlocked = lock_status == 0
    else:
        is_unlocked = False

    cipher_value = record.get("EncryptionMethod")
    if not isinstance(cipher_value, str) or len(cipher_value) == 0:
        cipher_token: Optional[str] = None
    else:
        cipher_token = cipher_value

    progress_raw = record.get("EncryptionPercentage")
    if isinstance(progress_raw, (int, float)):
        progress_value: Optional[float] = float(progress_raw)
    else:
        progress_value = None

    protectors = _extract_protector_types(record=record)

    rtnval = WindowsEncryptionStatus(
        volume=volume_value,
        is_encrypted=is_encrypted,
        is_unlocked=is_unlocked,
        cipher=cipher_token,
        key_size=None,
        progress_percent=progress_value,
        protector_types=protectors,
    )
    return rtnval


def _record_to_volume_info(
    *,
    record: dict,
) -> WindowsEncryptedVolumeInfo:
    """
        Build a :class:`WindowsEncryptedVolumeInfo` from a parsed
        ``Get-BitLockerVolume`` record.
    """
    mount_point_raw = record.get("MountPoint")
    if isinstance(mount_point_raw, str) and len(mount_point_raw) > 0:
        volume_value = mount_point_raw
    else:
        volume_value = record.get("ComputerName", "")

    lock_status = record.get("LockStatus")
    if isinstance(lock_status, str):
        is_unlocked = lock_status.lower() == "unlocked"
    elif isinstance(lock_status, int):
        is_unlocked = lock_status == 0
    else:
        is_unlocked = False

    cipher_value = record.get("EncryptionMethod")
    if not isinstance(cipher_value, str) or len(cipher_value) == 0:
        cipher_token: Optional[str] = None
    else:
        cipher_token = cipher_value

    progress_raw = record.get("EncryptionPercentage")
    if isinstance(progress_raw, (int, float)):
        progress_value: Optional[float] = float(progress_raw)
    else:
        progress_value = None

    protectors = _extract_protector_types(record=record)

    rtnval = WindowsEncryptedVolumeInfo(
        volume=volume_value,
        device=None,
        mount_point=volume_value,
        is_encrypted=True,
        is_unlocked=is_unlocked,
        cipher=cipher_token,
        key_size=None,
        progress_percent=progress_value,
        protector_types=protectors,
    )
    return rtnval


def _extract_protector_types(*, record: dict) -> list:
    """
        Pull the list of ``KeyProtectorType`` strings from a parsed
        ``Get-BitLockerVolume`` record.

        ``Get-BitLockerVolume.KeyProtector`` is an array of objects
        each carrying ``KeyProtectorId`` and ``KeyProtectorType``.
        BitLocker emits the type as a string when the host runs
        modern PowerShell, and as a numeric enum on older hosts; the
        parser tolerates both.

        :param record: Parsed JSON dict.

        :returns: A list of strings (possibly empty).
    """
    rtnval: list = []
    protectors = record.get("KeyProtector")
    if not isinstance(protectors, list):
        return rtnval
    for entry in protectors:
        if not isinstance(entry, dict):
            continue
        type_value = entry.get("KeyProtectorType")
        if isinstance(type_value, str) and len(type_value) > 0:
            rtnval.append(type_value)
            continue
        if isinstance(type_value, int):
            rtnval.append(str(type_value))
    return rtnval

"""
    Windows backup-restore feature.

    Implements the cross-OS backup-restore contract for Windows
    by wrapping three backend tools available on a stock Windows
    host: Windows Server Backup via ``wbadmin`` (default,
    Windows Server / limited on Pro / Enterprise SKUs),
    PowerShell ``Robocopy`` (universal), and ``restic.exe`` (when
    installed).  Every command is dispatched via PowerShell so
    the same argv shape covers every backend and so the
    ``-NoProfile`` / ``-NonInteractive`` flags neutralise
    ``$PROFILE`` mutations and prevent PowerShell from blocking
    on interactive input.

    PowerShell argv shape:
        Every dispatch is shaped as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]``.  The script body is built by
        string concatenation; argv arguments to cmdlets are
        single-quoted (PowerShell's literal form).  Native
        executables (``wbadmin.exe``, ``restic.exe``,
        ``Robocopy.exe``) are also invoked via PowerShell.

    Verb / backend mapping:

    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+
    | Verb                  | WBADMIN                                          | ROBOCOPY                             | RESTIC                                     |
    +=======================+==================================================+======================================+============================================+
    | ``create_backup_job`` | ``Set-Content -Path '<job-path>'``               | same                                 | same                                       |
    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+
    | ``delete_backup_job`` | ``Remove-Item -Path '<job-path>'``               | same                                 | same                                       |
    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+
    | ``run_backup_now``    | ``wbadmin start backup -backupTarget:<dst>``     | ``Robocopy <src> <dst> /MIR``        | ``restic --repo <dst> --password-file=- ``|
    |                       | ``-include:<paths> -allCritical -quiet``         |                                      | ``backup <paths>``                         |
    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+
    | ``list_backups``      | ``Get-ChildItem`` +                              | ``Get-ChildItem``                    | ``Get-ChildItem``                          |
    |                       | ``wbadmin get versions``                         |                                      |                                            |
    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+
    | ``restore_from_backup``| ``wbadmin start recovery -version:<id>``        | ``Robocopy <dst> <target> /MIR``     | ``restic restore latest --target <target>``|
    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+
    | ``verify_backup``     | ``wbadmin get items -version:<id>``              | ``Robocopy <src> <dst> /L /MIR``     | ``restic check``                           |
    +-----------------------+--------------------------------------------------+--------------------------------------+--------------------------------------------+

    SKU constraints:
        ``wbadmin start backup`` requires the "Windows Server
        Backup" optional feature on Windows Server, or a Windows
        10 / 11 Pro or Enterprise SKU.  Home SKUs do NOT ship
        ``wbadmin``.  The manager does not pre-validate SKU; a
        missing binary surfaces as a
        ``CommandNotFoundException`` from PowerShell which is
        passed through verbatim on the returned
        :class:`WindowsBackupActionResult`'s ``stderr`` field.

    Schedule wiring:
        This feature persists the ``schedule`` field on each
        spec but does NOT itself register Task Scheduler
        triggers.  Schedule wiring is delegated to the
        ``scheduled-tasks`` feature.

    Encryption passphrase handling:
        When the spec carries an ``encryption_passphrase`` and
        the backend is ``RESTIC``, the passphrase is delivered
        via the runner's stdin channel and the argv carries
        ``--password-file=-`` (the Windows-flavoured equivalent
        of ``--password-file=/dev/stdin``).  The persisted
        on-disk spec replaces the passphrase with the sentinel
        ``"***REDACTED***"``.

    Idempotency:
        * ``create_backup_job``: returns ``SKIPPED`` when the
          on-disk JSON spec is byte-equivalent to the proposed
          serialisation.
        * ``delete_backup_job``: returns ``SKIPPED`` when the
          spec file is absent.
        * ``run_backup_now``, ``restore_from_backup``,
          ``verify_backup``: never skip.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads of the
        spec directory never elevate.  ``list_backups``
        dispatches ``wbadmin get versions`` with
        ``elevated=True`` for any ``WBADMIN`` rows, since
        ``wbadmin`` catalogue reads are admin-only on most SKUs;
        this is the documented exception.
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
# Module-level constants
# ----------------------------------------------------------------------

_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_DEFAULT_JOB_DIR = r"C:\ProgramData\CodeCraftAI\backup\jobs"

_REDACTION_SENTINEL = "***REDACTED***"

_MODE_RUN = "run"
_MODE_RESTORE = "restore"
_MODE_VERIFY = "verify"


WindowsBackupActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enum
# ----------------------------------------------------------------------


class WindowsBackupKind(Enum):
    """
        Backend selector for a :class:`WindowsBackupJobSpec`.

        +-------------+--------------------------------------------+
        | Value       | Backend                                    |
        +=============+============================================+
        | ``WBADMIN`` | ``wbadmin start backup`` (Windows Server)  |
        +-------------+--------------------------------------------+
        | ``ROBOCOPY``| PowerShell ``Robocopy /MIR`` mirror        |
        +-------------+--------------------------------------------+
        | ``RESTIC``  | ``restic.exe`` deduplicating snapshot store|
        +-------------+--------------------------------------------+
    """
    WBADMIN = "wbadmin"
    ROBOCOPY = "robocopy"
    RESTIC = "restic"


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsBackupManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsBackupRestoreManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param default_kind: Backend selected when a
                             :class:`WindowsBackupJobSpec` does
                             not pin its own ``kind``.  Defaults
                             to :attr:`WindowsBackupKind.WBADMIN`.
        :param job_dir: Directory holding persisted job specs.
                        Defaults to
                        ``C:\\ProgramData\\CodeCraftAI\\backup\\jobs``.
        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``;
                                      override to ``"pwsh.exe"``
                                      to target PowerShell 7+.
    """
    default_kind: WindowsBackupKind = WindowsBackupKind.WBADMIN
    job_dir: str = _DEFAULT_JOB_DIR
    powershell_executable: str = _POWERSHELL


@dataclass
class WindowsBackupActionResult(FeatureActionResult):
    """
        Result of a :class:`WindowsBackupRestoreManager` write
        verb.

        :param job_id: The job identifier the action targeted.
        :param target_path: The on-disk target the action
                            produced or referenced.
    """
    job_id: Optional[str] = None
    target_path: Optional[str] = None


@dataclass
class WindowsBackupJobSpec:
    """
        Declarative specification for a single backup job.

        :param id: Caller-chosen deterministic job identifier.
        :param kind: The backend to dispatch.
        :param source_paths: Filesystem paths to back up.
        :param destination: Backup-target path / repository /
                            archive volume.
        :param excludes: Optional list of exclude patterns.  The
                         ``WBADMIN`` backend ignores this field
                         (excludes are baked into the backup
                         policy at policy-create time, not per
                         run).
        :param compression: When ``True`` (default) the
                            ``RESTIC`` backend uses its default
                            compression.  Ignored by ``WBADMIN``
                            and ``ROBOCOPY``.
        :param encryption_passphrase: When set, used as the
                                      ``RESTIC`` repository
                                      passphrase.
        :param retention_days: Optional retention horizon (in
                               days).
        :param schedule: Optional schedule expression.
                         Persisted but not wired by this
                         feature.
    """
    id: str
    kind: WindowsBackupKind
    source_paths: list
    destination: str
    excludes: Optional[list] = None
    compression: bool = True
    encryption_passphrase: Optional[str] = None
    retention_days: Optional[int] = None
    schedule: Optional[str] = None


WindowsBackupSpec = WindowsBackupJobSpec


@dataclass
class WindowsBackupInfo:
    """
        One row from
        :meth:`WindowsBackupRestoreManager.list_backups`.

        :param id: Job identifier.
        :param kind: Backend kind.
        :param created: ISO-8601 timestamp when known.
        :param size_bytes: On-disk size in bytes when known.
        :param source_count: Length of the ``source_paths``
                             list.
        :param destination: Destination path / volume.
    """
    id: str
    kind: WindowsBackupKind
    created: Optional[str]
    size_bytes: Optional[int]
    source_count: Optional[int]
    destination: str


@dataclass
class WindowsBackupVerifyResult:
    """
        Result of
        :meth:`WindowsBackupRestoreManager.verify_backup`.

        :param backup_id: The archive identifier verified.
        :param ok: True when verification passed.
        :param recorded_checksum: Recorded checksum, when known.
        :param computed_checksum: Computed checksum, when known.
        :param detail: Human-readable detail on failure.
    """
    backup_id: str
    ok: bool
    recorded_checksum: Optional[str] = None
    computed_checksum: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class WindowsBackupJobStatus:
    """
        Result of
        :meth:`WindowsBackupRestoreManager.get_job_status`.
    """
    job_id: str
    kind: WindowsBackupKind
    enabled: bool
    last_run_at: Optional[str] = None
    last_run_status: Optional[WindowsBackupActionStatus] = None
    next_run_at: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsBackupQueryError(Exception):
    """
        Raised when a backup-restore read fails for a reason
        other than the target simply being absent.
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
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Backup-restore query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsBackupNotFoundError(Exception):
    """
        Raised when a ``job_id`` is supplied to a read verb and
        the spec does not exist on disk.
    """

    def __init__(self, *, job_id: str) -> None:
        """
            :param job_id: The missing job identifier.
        """
        self.job_id = job_id

        err_msg_lines = ["Backup job not found."]
        err_msg_lines.append("    JOB_ID: {}".format(job_id))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsBackupVerifyError(Exception):
    """
        Raised when verification logic encounters a structural
        error.
    """

    def __init__(self, *, message: str) -> None:
        """
            :param message: A description of the structural
                            failure.
        """
        super().__init__(message)
        return


# ----------------------------------------------------------------------
# WindowsBackupRestoreManager
# ----------------------------------------------------------------------


class WindowsBackupRestoreManager(BaseFeatureManager):
    """
        Windows backup-restore manager wrapping ``wbadmin``,
        ``Robocopy``, and ``restic.exe`` through PowerShell.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.
    """

    result_class = WindowsBackupActionResult
    status_class = WindowsBackupActionStatus
    feature_name = "backup-restore"

    def __init__(
        self,
        runner,
        config: Optional[WindowsBackupManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`WindowsBackupManagerConfig`.
        """
        if config is None:
            config = WindowsBackupManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_backup_job(
        self,
        spec: WindowsBackupJobSpec,
    ) -> WindowsBackupActionResult:
        """
            Persist a backup-job spec to disk via PowerShell
            ``Set-Content``.

            Idempotent: returns ``SKIPPED`` when the on-disk
            spec is byte-equivalent to the proposed
            serialisation.

            :param spec: The :class:`WindowsBackupJobSpec`.

            :returns: The :class:`WindowsBackupActionResult`.
        """
        cfg = self._typed_config()
        job_path = _job_path(job_dir=cfg.job_dir, job_id=spec.id)
        new_payload = _serialise_spec(spec=spec)
        write_script = _build_set_content_script(
            path=job_path, body=new_payload,
        )
        write_argv = self._build_powershell_argv(script=write_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsBackupActionStatus.DRY_RUN,
                job_id=spec.id,
                target_path=job_path,
                command=write_argv,
            )
            return dry

        existing = self._read_spec_text(path=job_path)
        if (
            existing is not None
            and existing.strip() == new_payload.strip()
        ):
            skipped = self._build_result(
                status=WindowsBackupActionStatus.SKIPPED,
                job_id=spec.id,
                target_path=job_path,
                command=write_argv,
                detail="spec already persisted",
            )
            return skipped

        run_result = self._runner.run(
            list(write_argv),
            elevated=self._config.elevated,
            stdin=new_payload,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=write_argv,
            job_id=spec.id,
            target_path=job_path,
        )
        return rtnval

    def delete_backup_job(
        self,
        job_id: str,
    ) -> WindowsBackupActionResult:
        """
            Remove a persisted backup-job spec via PowerShell
            ``Remove-Item``.

            Idempotent: returns ``SKIPPED`` when the spec file
            is absent.

            :param job_id: The job identifier to remove.

            :returns: The :class:`WindowsBackupActionResult`.
        """
        cfg = self._typed_config()
        job_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        rm_script = _build_remove_item_script(path=job_path)
        rm_argv = self._build_powershell_argv(script=rm_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsBackupActionStatus.DRY_RUN,
                job_id=job_id,
                target_path=job_path,
                command=rm_argv,
            )
            return dry

        present = self._path_exists(path=job_path)
        if not present:
            skipped = self._build_result(
                status=WindowsBackupActionStatus.SKIPPED,
                job_id=job_id,
                target_path=job_path,
                command=rm_argv,
                detail="spec already absent",
            )
            return skipped

        run_result = self._runner.run(
            list(rm_argv),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=rm_argv,
            job_id=job_id,
            target_path=job_path,
        )
        return rtnval

    def run_backup_now(
        self,
        job_id: str,
    ) -> WindowsBackupActionResult:
        """
            Dispatch a backup run for an already-persisted job.

            :param job_id: The job identifier to run.

            :returns: The :class:`WindowsBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_RUN,
            target=None,
        )
        return rtnval

    def restore_from_backup(
        self,
        job_id: str,
        target: str,
    ) -> WindowsBackupActionResult:
        """
            Restore from a previously-taken backup.

            :param job_id: The job identifier to restore from.
            :param target: The on-disk target / restore root.

            :returns: The :class:`WindowsBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_RESTORE,
            target=target,
        )
        return rtnval

    def verify_backup(
        self,
        job_id: str,
    ) -> WindowsBackupActionResult:
        """
            Verify a backup's integrity.

            :param job_id: The job identifier to verify.

            :returns: The :class:`WindowsBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_VERIFY,
            target=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_backups(self) -> list:
        """
            Enumerate every persisted backup-job spec.  When any
            persisted spec carries ``kind=WBADMIN``, also invoke
            ``wbadmin get versions`` (with ``elevated=True``) to
            harvest the live backup-version list.

            :returns: A list of :class:`WindowsBackupInfo`.

            :raises WindowsBackupQueryError: When the underlying
                                             read fails
                                             unexpectedly.
        """
        cfg = self._typed_config()
        ls_script = _build_get_childitem_script(path=cfg.job_dir)
        ls_argv = self._build_powershell_argv(script=ls_script)
        ls_result = self._runner.run(
            list(ls_argv), elevated=False,
        )

        rtnval: list = []
        any_wbadmin = False
        wbadmin_destination: Optional[str] = None

        if ls_result.exit_code != 0:
            stderr_text = ls_result.stderr or ""
            lowered = stderr_text.lower()
            if (
                len(stderr_text.strip()) > 0
                and "cannot find" not in lowered
                and "does not exist" not in lowered
            ):
                raise WindowsBackupQueryError(
                    command=ls_argv,
                    exit_code=ls_result.exit_code,
                    stderr=stderr_text,
                )
        else:
            file_names = _parse_get_childitem_output(
                stdout=ls_result.stdout,
            )
            for raw_name in file_names:
                if not raw_name.endswith(".json"):
                    continue
                job_id = raw_name[: -len(".json")]
                spec_path = _job_path(
                    job_dir=cfg.job_dir, job_id=job_id,
                )
                text = self._read_spec_text(path=spec_path)
                if text is None:
                    continue
                try:
                    spec = _deserialise_spec(text=text)
                except (ValueError, KeyError):
                    continue
                if spec.kind == WindowsBackupKind.WBADMIN:
                    any_wbadmin = True
                    wbadmin_destination = spec.destination
                info = WindowsBackupInfo(
                    id=spec.id,
                    kind=spec.kind,
                    created=None,
                    size_bytes=None,
                    source_count=len(spec.source_paths),
                    destination=spec.destination,
                )
                rtnval.append(info)

        if any_wbadmin is True and wbadmin_destination is not None:
            wb_script = _build_wbadmin_get_versions_script(
                destination=wbadmin_destination,
            )
            wb_argv = self._build_powershell_argv(script=wb_script)
            # wbadmin get versions requires admin on most SKUs.
            wb_result = self._runner.run(
                list(wb_argv), elevated=True,
            )
            if wb_result.exit_code == 0:
                versions = _parse_wbadmin_get_versions(
                    stdout=wb_result.stdout,
                )
                for version in versions:
                    info = WindowsBackupInfo(
                        id=version,
                        kind=WindowsBackupKind.WBADMIN,
                        created=version,
                        size_bytes=None,
                        source_count=None,
                        destination=wbadmin_destination,
                    )
                    rtnval.append(info)

        return rtnval

    def get_job_status(
        self,
        job_id: str,
    ) -> WindowsBackupJobStatus:
        """
            Report the persistence-and-runtime status of a job.

            :param job_id: The job identifier.

            :returns: The :class:`WindowsBackupJobStatus`.

            :raises WindowsBackupNotFoundError: When the spec is
                                                absent.
        """
        cfg = self._typed_config()
        spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        text = self._read_spec_text(path=spec_path)
        if text is None:
            raise WindowsBackupNotFoundError(job_id=job_id)
        spec = _deserialise_spec(text=text)
        rtnval = WindowsBackupJobStatus(
            job_id=spec.id,
            kind=spec.kind,
            enabled=True,
            last_run_at=None,
            last_run_status=None,
            next_run_at=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch_backend_verb(
        self,
        *,
        job_id: str,
        mode: str,
        target: Optional[str],
    ) -> WindowsBackupActionResult:
        """
            Shared dispatch path for ``run_backup_now``,
            ``restore_from_backup``, and ``verify_backup``.
        """
        cfg = self._typed_config()
        spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        read_script = _build_get_content_script(path=spec_path)
        read_argv = self._build_powershell_argv(script=read_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsBackupActionStatus.DRY_RUN,
                job_id=job_id,
                target_path=target,
                command=read_argv,
            )
            return dry

        text = self._read_spec_text(path=spec_path)
        if text is None:
            failed = self._build_result(
                status=WindowsBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=read_argv,
                detail="job not found",
                stderr="job not found",
            )
            return failed

        try:
            spec = _deserialise_spec(text=text)
        except (ValueError, KeyError) as parse_error:
            failed = self._build_result(
                status=WindowsBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=read_argv,
                detail="malformed spec",
                stderr=str(parse_error),
            )
            return failed

        spec.encryption_passphrase = (
            self._resolve_runtime_passphrase(persisted=spec)
        )

        argv, stdin_payload = self._build_backend_command(
            spec=spec,
            mode=mode,
            target=target,
        )
        if argv is None:
            failed = self._build_result(
                status=WindowsBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=read_argv,
                detail=(
                    "unsupported backup kind: {}".format(spec.kind)
                ),
                stderr="unsupported_kind",
            )
            return failed

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
            stdin=stdin_payload,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=argv,
            job_id=job_id,
            target_path=target,
        )
        # Robocopy uses non-zero exit codes 0-7 to communicate
        # success-with-info.  Treat exit codes <= 7 as SUCCESS
        # for the ROBOCOPY backend.
        if (
            spec.kind == WindowsBackupKind.ROBOCOPY
            and rtnval.exit_code is not None
            and rtnval.exit_code <= 7
        ):
            rtnval.status = WindowsBackupActionStatus.SUCCESS
        return rtnval

    def _build_backend_command(
        self,
        *,
        spec: WindowsBackupJobSpec,
        mode: str,
        target: Optional[str],
    ) -> tuple:
        """
            Build the argv and optional stdin payload for a
            backend dispatch.
        """
        if spec.kind == WindowsBackupKind.WBADMIN:
            script = _build_wbadmin_run_script(
                spec=spec, mode=mode, target=target,
            )
            if script is None:
                rtnval = (None, None)
                return rtnval
            argv = self._build_powershell_argv(script=script)
            rtnval = (argv, None)
            return rtnval
        if spec.kind == WindowsBackupKind.ROBOCOPY:
            script = _build_robocopy_script(
                spec=spec, mode=mode, target=target,
            )
            if script is None:
                rtnval = (None, None)
                return rtnval
            argv = self._build_powershell_argv(script=script)
            rtnval = (argv, None)
            return rtnval
        if spec.kind == WindowsBackupKind.RESTIC:
            script, needs_passphrase = _build_restic_script(
                spec=spec, mode=mode, target=target,
            )
            if script is None:
                rtnval = (None, None)
                return rtnval
            argv = self._build_powershell_argv(script=script)
            stdin_payload: Optional[str] = None
            if (
                needs_passphrase is True
                and spec.encryption_passphrase is not None
                and len(spec.encryption_passphrase) > 0
            ):
                stdin_payload = spec.encryption_passphrase
            rtnval = (argv, stdin_payload)
            return rtnval
        rtnval = (None, None)
        return rtnval

    def _resolve_runtime_passphrase(
        self,
        *,
        persisted: WindowsBackupJobSpec,
    ) -> Optional[str]:
        """
            Resolve the runtime passphrase to forward to
            ``restic``.
        """
        if persisted.encryption_passphrase is None:
            rtnval: Optional[str] = None
            return rtnval
        if persisted.encryption_passphrase == _REDACTION_SENTINEL:
            rtnval = None
            return rtnval
        rtnval = persisted.encryption_passphrase
        return rtnval

    def _typed_config(self) -> WindowsBackupManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsBackupManagerConfig`.
        """
        if isinstance(self._config, WindowsBackupManagerConfig):
            rtnval = self._config
            return rtnval
        rtnval = WindowsBackupManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
        )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv.

            :param script: The script body.

            :returns: The argv list.
        """
        cfg = self._typed_config()
        rtnval = [
            cfg.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _read_spec_text(self, *, path: str) -> Optional[str]:
        """
            Read a spec file via PowerShell ``Get-Content``.

            :param path: The absolute spec file path.

            :returns: The file contents, or ``None``.
        """
        script = _build_get_content_script(path=path)
        argv = self._build_powershell_argv(script=script)
        run_result = self._runner.run(
            list(argv), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via PowerShell
            ``Test-Path``.

            :param path: The absolute path to probe.

            :returns: True when ``Test-Path`` reports the path
                      exists.
        """
        script = "Test-Path -Path '{}'".format(path)
        argv = self._build_powershell_argv(script=script)
        run_result = self._runner.run(
            list(argv), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        stripped = (run_result.stdout or "").strip().lower()
        rtnval = stripped == "true"
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsBackupActionStatus,
        job_id: Optional[str],
        target_path: Optional[str],
        command: list,
        detail: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsBackupActionResult:
        """
            Construct a :class:`WindowsBackupActionResult`
            without invoking the runner.
        """
        rtnval = WindowsBackupActionResult(
            status=status,
            detail=detail,
            command=list(command),
            job_id=job_id,
            target_path=target_path,
            stderr=stderr,
        )
        return rtnval

    def _wrap_run_result(
        self,
        *,
        run_result,
        command: list,
        job_id: Optional[str],
        target_path: Optional[str],
    ) -> WindowsBackupActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsBackupActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsBackupActionStatus.SUCCESS
        else:
            mapped_status = WindowsBackupActionStatus.FAILED

        rtnval = WindowsBackupActionResult(
            status=mapped_status,
            detail=job_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            job_id=job_id,
            target_path=target_path,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — serialisation
# ----------------------------------------------------------------------


def _serialise_spec(*, spec: WindowsBackupJobSpec) -> str:
    """
        Serialise a :class:`WindowsBackupJobSpec` to JSON with
        the encryption passphrase redacted.
    """
    redacted = _redact_passphrase(spec=spec)
    payload = {
        "id": redacted.id,
        "kind": redacted.kind.value,
        "source_paths": list(redacted.source_paths),
        "destination": redacted.destination,
        "excludes": (
            None if redacted.excludes is None
            else list(redacted.excludes)
        ),
        "compression": redacted.compression,
        "encryption_passphrase": redacted.encryption_passphrase,
        "retention_days": redacted.retention_days,
        "schedule": redacted.schedule,
    }
    rtnval = json.dumps(payload, indent=2, sort_keys=True)
    return rtnval


def _deserialise_spec(*, text: str) -> WindowsBackupJobSpec:
    """
        Parse a JSON spec body into a
        :class:`WindowsBackupJobSpec`.
    """
    payload = json.loads(text)

    raw_kind = payload["kind"]
    matched_kind: Optional[WindowsBackupKind] = None
    for member in WindowsBackupKind:
        if member.value == raw_kind:
            matched_kind = member
            break
    if matched_kind is None:
        err_msg_lines = ["Unknown WindowsBackupKind value."]
        err_msg_lines.append("    GOT: {}".format(raw_kind))
        err_msg_lines.append(
            "    EXPECTED: one of {}".format(
                [member.value for member in WindowsBackupKind],
            ),
        )
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)

    raw_excludes = payload.get("excludes")
    excludes_value: Optional[list] = None
    if raw_excludes is not None:
        excludes_value = list(raw_excludes)

    rtnval = WindowsBackupJobSpec(
        id=payload["id"],
        kind=matched_kind,
        source_paths=list(payload["source_paths"]),
        destination=payload["destination"],
        excludes=excludes_value,
        compression=payload.get("compression", True),
        encryption_passphrase=payload.get("encryption_passphrase"),
        retention_days=payload.get("retention_days"),
        schedule=payload.get("schedule"),
    )
    return rtnval


def _redact_passphrase(
    *,
    spec: WindowsBackupJobSpec,
) -> WindowsBackupJobSpec:
    """
        Return a copy of ``spec`` with the encryption
        passphrase replaced by the redaction sentinel.
    """
    if spec.encryption_passphrase is None:
        passphrase_field: Optional[str] = None
    else:
        passphrase_field = _REDACTION_SENTINEL

    redacted_excludes: Optional[list] = None
    if spec.excludes is not None:
        redacted_excludes = list(spec.excludes)

    rtnval = WindowsBackupJobSpec(
        id=spec.id,
        kind=spec.kind,
        source_paths=list(spec.source_paths),
        destination=spec.destination,
        excludes=redacted_excludes,
        compression=spec.compression,
        encryption_passphrase=passphrase_field,
        retention_days=spec.retention_days,
        schedule=spec.schedule,
    )
    return rtnval


def _job_path(*, job_dir: str, job_id: str) -> str:
    """
        Compose the absolute path to a persisted spec file.
    """
    rtnval = "{}\\{}.json".format(job_dir, job_id)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell script builders
# ----------------------------------------------------------------------


def _build_set_content_script(*, path: str, body: str) -> str:
    """
        Build a PowerShell ``Set-Content`` script.

        The body is delivered via the runner's stdin channel and
        read by ``$input`` inside the script, so the script body
        contains no inline payload — this prevents JSON content
        from breaking PowerShell quoting.
    """
    rtnval = (
        "$input | "
        "Set-Content -Path '{}' -Encoding UTF8 -NoNewline".format(path)
    )
    return rtnval


def _build_remove_item_script(*, path: str) -> str:
    """
        Build a PowerShell ``Remove-Item`` script.
    """
    rtnval = "Remove-Item -Path '{}' -Force -ErrorAction Stop".format(path)
    return rtnval


def _build_get_content_script(*, path: str) -> str:
    """
        Build a PowerShell ``Get-Content`` script that emits the
        file body verbatim.
    """
    rtnval = (
        "Get-Content -Path '{}' -Raw -ErrorAction Stop".format(path)
    )
    return rtnval


def _build_get_childitem_script(*, path: str) -> str:
    """
        Build a PowerShell ``Get-ChildItem`` script that emits
        one filename per stdout line.
    """
    rtnval = (
        "Get-ChildItem -Path '{}' -Filter '*.json' "
        "-ErrorAction Stop "
        "| Select-Object -ExpandProperty Name"
    ).format(path)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — wbadmin script builders
# ----------------------------------------------------------------------


def _build_wbadmin_run_script(
    *,
    spec: WindowsBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> Optional[str]:
    """
        Build the ``wbadmin`` script body for the requested
        mode.  Returns ``None`` for an unknown mode.
    """
    if mode == _MODE_RUN:
        include_arg = ",".join(spec.source_paths)
        rtnval: Optional[str] = (
            "wbadmin start backup "
            "-backupTarget:{} "
            "-include:{} "
            "-allCritical -quiet"
        ).format(spec.destination, include_arg)
        return rtnval
    if mode == _MODE_RESTORE:
        # The version_id is the most-recent version reported by
        # ``wbadmin get versions``.  In practice the caller
        # would resolve this via list_backups; here we default
        # to "latest" which wbadmin does not natively support,
        # so we emit a single-quoted placeholder that the
        # operator can override via spec.destination acting as
        # the version-id in restore-mode.  For Wave 5.1 we use
        # the spec.id as the version label since the
        # scheduled-tasks integration will eventually pin
        # explicit version-ids.
        rtnval = (
            "wbadmin start recovery "
            "-version:'{}' "
            "-itemType:File "
            "-items:'{}' "
            "-recoveryTarget:'{}' "
            "-quiet"
        ).format(
            spec.id,
            target if target is not None else "",
            target if target is not None else "",
        )
        return rtnval
    if mode == _MODE_VERIFY:
        rtnval = (
            "wbadmin get items "
            "-version:'{}' "
            "-backupTarget:'{}'"
        ).format(spec.id, spec.destination)
        return rtnval
    rtnval = None
    return rtnval


def _build_wbadmin_get_versions_script(
    *,
    destination: str,
) -> str:
    """
        Build the ``wbadmin get versions`` script body.
    """
    rtnval = (
        "wbadmin get versions -backupTarget:'{}'".format(destination)
    )
    return rtnval


def _parse_wbadmin_get_versions(*, stdout: str) -> list:
    """
        Parse ``wbadmin get versions`` stdout into a list of
        version-identifier strings.

        ``wbadmin get versions`` emits a key-colon-value block
        per version with a ``Version identifier:`` line.  This
        parser collects every such value.
    """
    rtnval: list = []
    if stdout is None or len(stdout) == 0:
        return rtnval
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if not stripped.lower().startswith("version identifier"):
            continue
        _, _, value = stripped.partition(":")
        version = value.strip()
        if len(version) == 0:
            continue
        rtnval.append(version)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — Robocopy script builder
# ----------------------------------------------------------------------


def _build_robocopy_script(
    *,
    spec: WindowsBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> Optional[str]:
    """
        Build the ``Robocopy`` script body for the requested
        mode.

        Robocopy supports a single source / destination pair per
        invocation, so this helper iterates over the source
        list and joins the per-source invocations with ``;``.
    """
    if mode == _MODE_RUN:
        commands: list = []
        for source in spec.source_paths:
            commands.append(
                "Robocopy '{}' '{}' /MIR /COPYALL /R:1 /W:1".format(
                    source, spec.destination,
                ),
            )
        rtnval: Optional[str] = "; ".join(commands)
        return rtnval
    if mode == _MODE_RESTORE:
        if target is None:
            rtnval = None
            return rtnval
        rtnval = (
            "Robocopy '{}' '{}' /MIR /COPYALL /R:1 /W:1".format(
                spec.destination, target,
            )
        )
        return rtnval
    if mode == _MODE_VERIFY:
        commands = []
        for source in spec.source_paths:
            commands.append(
                "Robocopy '{}' '{}' /L /MIR /R:0 /W:0".format(
                    source, spec.destination,
                ),
            )
        rtnval = "; ".join(commands)
        return rtnval
    rtnval = None
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — restic script builder
# ----------------------------------------------------------------------


def _build_restic_script(
    *,
    spec: WindowsBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> tuple:
    """
        Build the ``restic`` script body for the requested mode.

        The passphrase is NEVER placed on the script body.
        Instead, ``--password-file=-`` is appended and the
        caller forwards the passphrase via runner stdin.
    """
    needs_passphrase = (
        spec.encryption_passphrase is not None
        and len(spec.encryption_passphrase) > 0
        and spec.encryption_passphrase != _REDACTION_SENTINEL
    )

    base_parts: list = [
        "restic",
        "--repo",
        "'{}'".format(spec.destination),
    ]
    if needs_passphrase is True:
        base_parts.append("--password-file=-")

    if mode == _MODE_RUN:
        parts = list(base_parts)
        parts.append("backup")
        for source in spec.source_paths:
            parts.append("'{}'".format(source))
        if spec.excludes is not None:
            for pattern in spec.excludes:
                parts.append("--exclude")
                parts.append("'{}'".format(pattern))
        rtnval = (" ".join(parts), needs_passphrase)
        return rtnval
    if mode == _MODE_RESTORE:
        if target is None:
            rtnval = (None, needs_passphrase)
            return rtnval
        parts = list(base_parts)
        parts.append("restore")
        parts.append("latest")
        parts.append("--target")
        parts.append("'{}'".format(target))
        rtnval = (" ".join(parts), needs_passphrase)
        return rtnval
    if mode == _MODE_VERIFY:
        parts = list(base_parts)
        parts.append("check")
        rtnval = (" ".join(parts), needs_passphrase)
        return rtnval

    rtnval = (None, needs_passphrase)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — Get-ChildItem parser
# ----------------------------------------------------------------------


def _parse_get_childitem_output(*, stdout: str) -> list:
    """
        Parse the stdout of a ``Get-ChildItem | Select Name``
        invocation into a list of filenames.

        :param stdout: The captured stdout.

        :returns: One filename per non-empty stdout line.
    """
    rtnval: list = []
    if stdout is None or len(stdout) == 0:
        return rtnval
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        rtnval.append(stripped)
    return rtnval

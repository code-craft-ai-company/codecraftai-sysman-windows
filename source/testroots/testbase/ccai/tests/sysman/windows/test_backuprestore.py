"""
    Tests for :class:`WindowsBackupRestoreManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult`
    values.  PowerShell scripts are matched by prefix.  No live
    PowerShell is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.backuprestore import (
    WindowsBackupActionStatus,
    WindowsBackupJobSpec,
    WindowsBackupKind,
    WindowsBackupManagerConfig,
    WindowsBackupQueryError,
    WindowsBackupRestoreManager,
    _serialise_spec,
)


_DEFAULT_JOB_DIR = r"C:\ProgramData\CodeCraftAI\backup\jobs"


def _spec_path(*, job_id: str) -> str:
    """
        Compose the canonical spec path for a given job id.
    """
    rtnval = "{}\\{}.json".format(_DEFAULT_JOB_DIR, job_id)
    return rtnval


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)
    script_results: dict = field(default_factory=dict)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
            "env": env,
        }
        self.calls.append(call_record)

        if (
            len(args) == 5
            and args[0].lower().startswith("powershell")
        ):
            script = args[4]
            for key_prefix, result in self.script_results.items():
                if script.startswith(key_prefix):
                    return result

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _seed_test_path(
    runner: _MockCommandRunner,
    *,
    path: str,
    exists: bool,
) -> None:
    """
        Seed ``Test-Path -Path '<path>'`` to report ``True`` /
        ``False`` per ``exists``.
    """
    prefix = "Test-Path -Path '{}'".format(path)
    if exists:
        result = RunResult(exit_code=0, stdout="True\n", stderr="")
    else:
        result = RunResult(exit_code=0, stdout="False\n", stderr="")
    runner.script_results[prefix] = result
    return


def _seed_get_content(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed ``Get-Content -Path '<path>' -Raw -ErrorAction Stop``.
    """
    prefix = "Get-Content -Path '{}' -Raw".format(path)
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="Cannot find path",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.script_results[prefix] = result
    return


def _seed_get_childitem(
    runner: _MockCommandRunner,
    *,
    path: str,
    listing: Optional[list],
) -> None:
    """
        Seed ``Get-ChildItem -Path '<path>' -Filter '*.json' ...``.
    """
    prefix = (
        "Get-ChildItem -Path '{}' -Filter '*.json' "
        "-ErrorAction Stop"
    ).format(path)
    if listing is None:
        result = RunResult(
            exit_code=1,
            stdout="",
            stderr="Cannot find path '{}': does not exist".format(path),
        )
    else:
        result = RunResult(
            exit_code=0,
            stdout="\n".join(listing),
            stderr="",
        )
    runner.script_results[prefix] = result
    return


def _make_spec_wbadmin() -> WindowsBackupJobSpec:
    rtnval = WindowsBackupJobSpec(
        id="server-system",
        kind=WindowsBackupKind.WBADMIN,
        source_paths=[r"C:\\"],
        destination=r"E:",
    )
    return rtnval


def _make_spec_robocopy() -> WindowsBackupJobSpec:
    rtnval = WindowsBackupJobSpec(
        id="docs-mirror",
        kind=WindowsBackupKind.ROBOCOPY,
        source_paths=[r"C:\Users\Public\Documents"],
        destination=r"D:\Backups\PublicDocs",
    )
    return rtnval


def _make_spec_restic_encrypted() -> WindowsBackupJobSpec:
    rtnval = WindowsBackupJobSpec(
        id="restic-repo",
        kind=WindowsBackupKind.RESTIC,
        source_paths=[r"C:\Users\me\work"],
        destination=r"D:\Backups\restic-repo",
        encryption_passphrase="topsecret-passphrase",
    )
    return rtnval


# ----------------------------------------------------------------------
# create_backup_job
# ----------------------------------------------------------------------


def test_create_backup_job_persists_spec():
    """
        create_backup_job dispatches a PowerShell ``Set-Content``
        script with the JSON payload on stdin and
        ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_wbadmin()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(runner, path=spec_path, body=None)

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    set_content_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and c["args"][0].lower().startswith("powershell")
            and "Set-Content" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(set_content_calls), 1)
    testbase.assert_true(spec_path in set_content_calls[0]["args"][4])
    testbase.assert_equal(set_content_calls[0]["elevated"], True)
    testbase.assert_true(set_content_calls[0]["stdin"] is not None)
    testbase.assert_true(
        "\"id\": \"server-system\"" in set_content_calls[0]["stdin"],
    )
    return


def test_create_backup_job_skipped_when_present():
    """
        create_backup_job returns SKIPPED on byte-equivalent
        on-disk content.
    """
    runner = _MockCommandRunner()
    spec = _make_spec_robocopy()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SKIPPED,
    )
    set_content_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "Set-Content" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(set_content_calls), 0)
    return


def test_create_backup_job_dry_run():
    """
        create_backup_job under dry-run does not invoke the
        runner.
    """
    runner = _MockCommandRunner()
    config = WindowsBackupManagerConfig(elevated=True, dry_run=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)

    result = manager.create_backup_job(spec=_make_spec_wbadmin())

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_backup_job
# ----------------------------------------------------------------------


def test_delete_backup_job_invokes_remove():
    """
        delete_backup_job dispatches PowerShell ``Remove-Item``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec_path = _spec_path(job_id="server-system")
    _seed_test_path(runner, path=spec_path, exists=True)

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.delete_backup_job(job_id="server-system")

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    rm_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "Remove-Item" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_true(spec_path in rm_calls[0]["args"][4])
    testbase.assert_equal(rm_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# run_backup_now
# ----------------------------------------------------------------------


def test_run_backup_now_dispatches_wbadmin():
    """
        run_backup_now with kind=WBADMIN dispatches
        ``wbadmin start backup``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_wbadmin()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    wb_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "wbadmin start backup" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(wb_calls), 1)
    script = wb_calls[0]["args"][4]
    testbase.assert_true("-backupTarget:E:" in script)
    testbase.assert_true("-include:" in script)
    testbase.assert_true("-allCritical" in script)
    testbase.assert_true("-quiet" in script)
    testbase.assert_equal(wb_calls[0]["elevated"], True)
    return


def test_run_backup_now_dispatches_robocopy():
    """
        run_backup_now with kind=ROBOCOPY dispatches Robocopy
        with /MIR /COPYALL flags.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=1, stdout="", stderr="")
    spec = _make_spec_robocopy()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    # Robocopy exit code 1 indicates files copied; treated as
    # SUCCESS.
    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    rc_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "Robocopy" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(rc_calls), 1)
    script = rc_calls[0]["args"][4]
    testbase.assert_true("/MIR" in script)
    testbase.assert_true("/COPYALL" in script)
    testbase.assert_true(r"C:\Users\Public\Documents" in script)
    testbase.assert_true(r"D:\Backups\PublicDocs" in script)
    return


def test_run_backup_now_dispatches_restic():
    """
        run_backup_now with kind=RESTIC dispatches ``restic``
        with ``--password-file=-`` and the passphrase on stdin.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = _spec_path(job_id=spec.id)
    forged = _serialise_spec(spec=spec).replace(
        "\"***REDACTED***\"",
        "\"topsecret-passphrase\"",
    )
    _seed_get_content(runner, path=spec_path, body=forged)

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    restic_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and c["args"][4].startswith("restic ")
        )
    ]
    testbase.assert_equal(len(restic_calls), 1)
    script = restic_calls[0]["args"][4]
    testbase.assert_true("--password-file=-" in script)
    testbase.assert_true("backup" in script)
    testbase.assert_true("topsecret-passphrase" not in script)
    testbase.assert_equal(
        restic_calls[0]["stdin"], "topsecret-passphrase",
    )
    return


# ----------------------------------------------------------------------
# list_backups
# ----------------------------------------------------------------------


def test_list_backups_parses_get_childitem_output():
    """
        list_backups invokes Get-ChildItem and Get-Content per
        spec, plus ``wbadmin get versions`` for any WBADMIN
        spec.
    """
    runner = _MockCommandRunner()
    wb_spec = _make_spec_wbadmin()
    rc_spec = _make_spec_robocopy()
    _seed_get_childitem(
        runner,
        path=_DEFAULT_JOB_DIR,
        listing=[
            "{}.json".format(wb_spec.id),
            "{}.json".format(rc_spec.id),
        ],
    )
    _seed_get_content(
        runner,
        path=_spec_path(job_id=wb_spec.id),
        body=_serialise_spec(spec=wb_spec),
    )
    _seed_get_content(
        runner,
        path=_spec_path(job_id=rc_spec.id),
        body=_serialise_spec(spec=rc_spec),
    )
    runner.script_results["wbadmin get versions"] = RunResult(
        exit_code=0,
        stdout=(
            "Backup time: 04/24/2026-12:00\n"
            "Version identifier: 04/24/2026-12:00\n"
            "Backup time: 04/23/2026-12:00\n"
            "Version identifier: 04/23/2026-12:00\n"
        ),
        stderr="",
    )

    manager = WindowsBackupRestoreManager(runner=runner)
    results = manager.list_backups()

    testbase.assert_equal(len(results), 4)
    persisted_ids = [r.id for r in results]
    testbase.assert_true(wb_spec.id in persisted_ids)
    testbase.assert_true(rc_spec.id in persisted_ids)
    testbase.assert_true("04/24/2026-12:00" in persisted_ids)
    return


# ----------------------------------------------------------------------
# restore_from_backup
# ----------------------------------------------------------------------


def test_restore_from_backup_invokes_wbadmin():
    """
        restore_from_backup with kind=WBADMIN dispatches
        ``wbadmin start recovery``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_wbadmin()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.restore_from_backup(
        job_id=spec.id, target=r"D:\restore",
    )

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    wb_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "wbadmin start recovery" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(wb_calls), 1)
    script = wb_calls[0]["args"][4]
    testbase.assert_true("-version:'server-system'" in script)
    testbase.assert_true("-itemType:File" in script)
    testbase.assert_true(r"D:\restore" in script)
    return


def test_restore_from_backup_invokes_robocopy():
    """
        restore_from_backup with kind=ROBOCOPY reverses
        direction (destination -> target).
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_robocopy()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.restore_from_backup(
        job_id=spec.id, target=r"D:\restored",
    )

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    rc_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "Robocopy" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(rc_calls), 1)
    script = rc_calls[0]["args"][4]
    testbase.assert_true(r"D:\Backups\PublicDocs" in script)
    testbase.assert_true(r"D:\restored" in script)
    return


# ----------------------------------------------------------------------
# verify_backup
# ----------------------------------------------------------------------


def test_verify_backup_invokes_wbadmin_get_items():
    """
        verify_backup with kind=WBADMIN dispatches
        ``wbadmin get items``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_wbadmin()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.verify_backup(job_id=spec.id)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    wb_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "wbadmin get items" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(wb_calls), 1)
    return


def test_verify_backup_invokes_restic_check():
    """
        verify_backup with kind=RESTIC dispatches
        ``restic check`` with the passphrase on stdin.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = _spec_path(job_id=spec.id)
    forged = _serialise_spec(spec=spec).replace(
        "\"***REDACTED***\"",
        "\"topsecret-passphrase\"",
    )
    _seed_get_content(runner, path=spec_path, body=forged)

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.verify_backup(job_id=spec.id)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    restic_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and c["args"][4].startswith("restic ")
        )
    ]
    testbase.assert_equal(len(restic_calls), 1)
    script = restic_calls[0]["args"][4]
    testbase.assert_true("check" in script)
    testbase.assert_true("--password-file=-" in script)
    testbase.assert_equal(
        restic_calls[0]["stdin"], "topsecret-passphrase",
    )
    return


# ----------------------------------------------------------------------
# Cross-cutting invariants
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs honour ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_robocopy()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(runner, path=spec_path, body=None)

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    manager.create_backup_job(spec=spec)

    set_content_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "Set-Content" in c["args"][4]
        )
    ]
    testbase.assert_true(len(set_content_calls) >= 1)
    for call in set_content_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Spec read calls in ``list_backups`` never elevate.
        ``wbadmin get versions`` is a documented exception that
        elevates regardless.
    """
    runner = _MockCommandRunner()
    rc_spec = _make_spec_robocopy()
    _seed_get_childitem(
        runner,
        path=_DEFAULT_JOB_DIR,
        listing=["{}.json".format(rc_spec.id)],
    )
    _seed_get_content(
        runner,
        path=_spec_path(job_id=rc_spec.id),
        body=_serialise_spec(spec=rc_spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    manager.list_backups()

    # Since no WBADMIN spec was persisted, no elevated read
    # should have happened.
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        FAILED results carry the captured stderr unchanged.
    """
    runner = _MockCommandRunner()
    spec = _make_spec_wbadmin()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )
    runner.script_results["wbadmin start backup"] = RunResult(
        exit_code=1,
        stdout="",
        stderr=(
            "ERROR - Windows Server Backup is not installed."
        ),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true(
        result.stderr is not None
        and "not installed" in result.stderr,
    )
    return


def test_passphrase_redacted_when_restic_encrypted():
    """
        The persisted JSON replaces the plaintext passphrase
        with the redaction sentinel.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(runner, path=spec_path, body=None)

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, WindowsBackupActionStatus.SUCCESS,
    )
    set_content_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and "Set-Content" in c["args"][4]
        )
    ]
    testbase.assert_equal(len(set_content_calls), 1)
    persisted_text = set_content_calls[0]["stdin"]
    testbase.assert_true(persisted_text is not None)
    testbase.assert_true(
        "topsecret-passphrase" not in persisted_text,
    )
    testbase.assert_true("***REDACTED***" in persisted_text)
    # And confirm the command field on the result also lacks it.
    for token in result.command:
        testbase.assert_true(
            "topsecret-passphrase" not in token,
        )
    return


def test_wbadmin_dispatch_for_wbadmin_kind():
    """
        Canonical WBADMIN dispatch test pinning the
        ``wbadmin start backup`` script body.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_wbadmin()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(
        runner, path=spec_path, body=_serialise_spec(spec=spec),
    )

    config = WindowsBackupManagerConfig(elevated=True)
    manager = WindowsBackupRestoreManager(runner=runner, config=config)
    manager.run_backup_now(job_id=spec.id)
    manager.verify_backup(job_id=spec.id)

    wb_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) == 5
            and c["args"][4].startswith("wbadmin")
        )
    ]
    # One for run, one for verify.
    testbase.assert_equal(len(wb_calls), 2)
    return


def test_list_backups_query_error_when_get_childitem_fails_unexpectedly():
    """
        list_backups raises WindowsBackupQueryError on
        unexpected ``Get-ChildItem`` failure.
    """
    runner = _MockCommandRunner()
    prefix = (
        "Get-ChildItem -Path '{}' -Filter '*.json' -ErrorAction Stop"
    ).format(_DEFAULT_JOB_DIR)
    runner.script_results[prefix] = RunResult(
        exit_code=2,
        stdout="",
        stderr="Access is denied.",
    )

    manager = WindowsBackupRestoreManager(runner=runner)
    raised = False
    try:
        manager.list_backups()
    except WindowsBackupQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_action_result_carries_target_path():
    """
        Successful results populate the ``target_path`` field.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_robocopy()
    spec_path = _spec_path(job_id=spec.id)
    _seed_get_content(runner, path=spec_path, body=None)

    manager = WindowsBackupRestoreManager(
        runner=runner,
        config=WindowsBackupManagerConfig(elevated=True),
    )
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(result.target_path, spec_path)
    testbase.assert_equal(result.job_id, spec.id)
    return

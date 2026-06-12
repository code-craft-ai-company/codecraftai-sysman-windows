"""
    Tests for :class:`WindowsKdumpManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv.  No live PowerShell cmdlets are executed.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.kdumpmanagement import (
    WindowsKdumpActionStatus,
    WindowsKdumpManager,
    WindowsKdumpManagerConfig,
    WindowsKdumpQueryError,
    WindowsKdumpTargetKind,
    WindowsKdumpTargetSpec,
)


_CRASHCONTROL_PATH = r"HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl"
_MINIDUMP_DIR = r"C:\Windows\Minidump"
_MEMORY_DMP = r"C:\Windows\MEMORY.DMP"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

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
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build an exit-code-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
    )
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    """
        Build a non-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
    )
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
) -> WindowsKdumpManager:
    """
        Construct a manager wired to ``runner`` with deterministic
        Windows paths.
    """
    config = WindowsKdumpManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        crashcontrol_path=_CRASHCONTROL_PATH,
        minidump_dir=_MINIDUMP_DIR,
        memory_dmp=_MEMORY_DMP,
    )
    rtnval = WindowsKdumpManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# enable_kdump
# ----------------------------------------------------------------------


def test_enable_kdump_invokes_set_itemproperty():
    """
        ``enable_kdump`` writes to CrashDumpEnabled via
        ``Set-ItemProperty -Type DWord``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 0, "DumpFile": "C:\\\\Windows\\\\MEMORY.DMP"}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.SUCCESS)
    testbase.assert_true(result.requires_reboot)
    last_call = runner.calls[-1]
    script = last_call["args"][-1]
    testbase.assert_true("Set-ItemProperty" in script)
    testbase.assert_true("CrashDumpEnabled" in script)
    testbase.assert_true("DWord" in script)
    testbase.assert_true(_CRASHCONTROL_PATH in script)
    return


def test_enable_kdump_skipped_when_enabled():
    """
        ``enable_kdump`` returns ``SKIPPED`` when CrashDumpEnabled
        already matches the desired mode.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 7, "DumpFile": "C:\\\\Windows\\\\MEMORY.DMP"}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.SKIPPED)
    for call in runner.calls:
        script = call["args"][-1]
        testbase.assert_false("Set-ItemProperty" in script)
    return


def test_enable_kdump_dry_run_short_circuits():
    """
        ``enable_kdump`` does not invoke the runner under
        ``dry_run=True``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(result.requires_reboot)
    return


# ----------------------------------------------------------------------
# disable_kdump
# ----------------------------------------------------------------------


def test_disable_kdump_invokes_set_itemproperty():
    """
        ``disable_kdump`` writes ``0`` to CrashDumpEnabled.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 7, "DumpFile": "C:\\\\Windows\\\\MEMORY.DMP"}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.disable_kdump()

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.SUCCESS)
    testbase.assert_true(result.requires_reboot)
    last_call = runner.calls[-1]
    script = last_call["args"][-1]
    testbase.assert_true("Set-ItemProperty" in script)
    testbase.assert_true("CrashDumpEnabled" in script)
    testbase.assert_true("-Value 0" in script)
    return


# ----------------------------------------------------------------------
# configure_kdump_target
# ----------------------------------------------------------------------


def test_configure_kdump_target_invokes_set_itemproperty():
    """
        ``configure_kdump_target`` with LOCAL_PATH writes DumpFile via
        ``Set-ItemProperty -Type ExpandString``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 7, "DumpFile": "C:\\\\Windows\\\\MEMORY.DMP"}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    new_path = r"D:\dumps\MEMORY.DMP"
    spec = WindowsKdumpTargetSpec(
        target_kind=WindowsKdumpTargetKind.LOCAL_PATH,
        path=new_path,
    )
    result = manager.configure_kdump_target(spec=spec)

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.SUCCESS)
    testbase.assert_true(result.requires_reboot)
    testbase.assert_equal(result.target, new_path)
    last_call = runner.calls[-1]
    script = last_call["args"][-1]
    testbase.assert_true("DumpFile" in script)
    testbase.assert_true("ExpandString" in script)
    testbase.assert_true(new_path in script)
    return


def test_configure_kdump_target_rejects_ssh():
    """
        ``configure_kdump_target`` with SSH returns ``FAILED`` with
        stderr token ``unsupported_target_kind``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    spec = WindowsKdumpTargetSpec(
        target_kind=WindowsKdumpTargetKind.SSH,
        ssh_user="root",
        ssh_host="dump.example.com",
    )
    result = manager.configure_kdump_target(spec=spec)

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "unsupported_target_kind")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_configure_kdump_target_skipped_when_already_set():
    """
        ``configure_kdump_target`` returns ``SKIPPED`` when DumpFile
        already matches the requested path.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 7, "DumpFile": "D:\\\\dumps\\\\MEMORY.DMP"}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    spec = WindowsKdumpTargetSpec(
        target_kind=WindowsKdumpTargetKind.LOCAL_PATH,
        path=r"D:\dumps\MEMORY.DMP",
    )
    result = manager.configure_kdump_target(spec=spec)

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# get_kdump_status
# ----------------------------------------------------------------------


def test_get_kdump_status_parses_get_itemproperty_output():
    """
        ``get_kdump_status`` parses the ``Get-ItemProperty | ConvertTo-Json``
        payload.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout=(
            '{"CrashDumpEnabled": 2, "DumpFile": '
            '"C:\\\\Windows\\\\MEMORY.DMP", '
            '"PSPath": "Microsoft.PowerShell.Core\\\\Registry::..."}'
        ),
    )
    manager = _make_manager(runner=runner)

    status = manager.get_kdump_status()

    testbase.assert_true(status.enabled)
    testbase.assert_true(status.running)
    testbase.assert_equal(status.target_kind, WindowsKdumpTargetKind.LOCAL_PATH)
    testbase.assert_equal(status.target_path, r"C:\Windows\MEMORY.DMP")
    testbase.assert_true(status.message is None)
    return


def test_get_kdump_status_disabled_when_value_is_zero():
    """
        ``get_kdump_status`` reports ``enabled=False`` when
        CrashDumpEnabled is ``0``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 0, "DumpFile": ""}',
    )
    manager = _make_manager(runner=runner)

    status = manager.get_kdump_status()

    testbase.assert_false(status.enabled)
    testbase.assert_false(status.running)
    return


# ----------------------------------------------------------------------
# list_recent_dumps
# ----------------------------------------------------------------------


def test_list_recent_dumps_parses_directory_listing():
    """
        ``list_recent_dumps`` parses the ``Get-ChildItem ... |
        ConvertTo-Json`` payload.
    """
    runner = _MockCommandRunner()
    json_payload = (
        '['
        '{"Name": "MEMORY.DMP", '
        '"FullName": "C:\\\\Windows\\\\MEMORY.DMP", '
        '"LastWriteTime": "2026-04-24T09:31:31", '
        '"Length": 1234567},'
        '{"Name": "Mini042426-01.dmp", '
        '"FullName": "C:\\\\Windows\\\\Minidump\\\\Mini042426-01.dmp", '
        '"LastWriteTime": "2026-04-24T09:30:00", '
        '"Length": 8192}'
        ']'
    )
    runner.default_result = _ok(stdout=json_payload)
    manager = _make_manager(runner=runner)

    result = manager.list_recent_dumps()

    testbase.assert_equal(len(result), 2)
    testbase.assert_equal(result[0].id, "MEMORY.DMP")
    testbase.assert_equal(result[0].path, r"C:\Windows\MEMORY.DMP")
    testbase.assert_equal(result[0].size_bytes, 1234567)
    testbase.assert_equal(result[1].id, "Mini042426-01.dmp")
    testbase.assert_equal(result[1].size_bytes, 8192)
    return


# ----------------------------------------------------------------------
# clear_old_dumps
# ----------------------------------------------------------------------


def test_clear_old_dumps_invokes_find_with_mtime():
    """
        ``clear_old_dumps`` builds a ``Get-ChildItem | Where-Object {
        $_.LastWriteTime -lt $cutoff } | Remove-Item`` pipeline.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.clear_old_dumps(age_days=30)

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    argv = runner.calls[0]["args"]
    script = argv[-1]
    testbase.assert_true("Get-ChildItem" in script)
    testbase.assert_true("Where-Object" in script)
    testbase.assert_true("AddDays(-30)" in script)
    testbase.assert_true("Remove-Item" in script)
    return


# ----------------------------------------------------------------------
# get_dump_info
# ----------------------------------------------------------------------


def test_get_dump_info_parses_get_item_output():
    """
        ``get_dump_info`` parses the ``Get-Item ... | ConvertTo-Json``
        payload.
    """
    runner = _MockCommandRunner()
    json_payload = (
        '{"Name": "MEMORY.DMP", '
        '"FullName": "C:\\\\Windows\\\\MEMORY.DMP", '
        '"LastWriteTime": "2026-04-24T09:31:31", '
        '"Length": 1234567}'
    )
    runner.default_result = _ok(stdout=json_payload)
    manager = _make_manager(runner=runner)

    info = manager.get_dump_info(dump_id="MEMORY.DMP")

    testbase.assert_equal(info.id, "MEMORY.DMP")
    testbase.assert_equal(info.path, r"C:\Windows\MEMORY.DMP")
    testbase.assert_equal(info.created, "2026-04-24T09:31:31")
    testbase.assert_equal(info.size_bytes, 1234567)
    testbase.assert_true(info.kernel is None)
    testbase.assert_true(info.panic_message is None)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        The Set-ItemProperty write forwards ``elevated=True`` to the
        runner.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 0, "DumpFile": ""}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    manager.enable_kdump()

    write_calls = [
        call for call in runner.calls
        if "Set-ItemProperty" in call["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_true(write_calls[0]["elevated"])
    return


def test_reads_never_elevate():
    """
        Read verbs always pass ``elevated=False`` regardless of config.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 0, "DumpFile": ""}',
    )
    manager = _make_manager(runner=runner, elevated=True)

    manager.get_kdump_status()

    for call in runner.calls:
        testbase.assert_false(call["elevated"])
    return


# ----------------------------------------------------------------------
# Reboot advisory
# ----------------------------------------------------------------------


def test_writes_set_requires_reboot():
    """
        Successful ``enable_kdump`` and ``disable_kdump`` results carry
        ``requires_reboot=True``.
    """
    enable_runner = _MockCommandRunner()
    enable_runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 0, "DumpFile": ""}',
    )
    enable_manager = _make_manager(runner=enable_runner, elevated=True)
    enable_result = enable_manager.enable_kdump()
    testbase.assert_true(enable_result.requires_reboot)

    disable_runner = _MockCommandRunner()
    disable_runner.default_result = _ok(
        stdout='{"CrashDumpEnabled": 7, "DumpFile": ""}',
    )
    disable_manager = _make_manager(runner=disable_runner, elevated=True)
    disable_result = disable_manager.disable_kdump()
    testbase.assert_true(disable_result.requires_reboot)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A non-zero ``Set-ItemProperty`` exit produces a ``FAILED``
        result with captured stderr.
    """
    runner = _MockCommandRunner()
    read_payload = '{"CrashDumpEnabled": 0, "DumpFile": ""}'

    def patched_run(args, **kwargs):
        call_record = {
            "args": list(args),
            "elevated": kwargs.get("elevated", False),
            "stdin": kwargs.get("stdin"),
        }
        runner.calls.append(call_record)
        script = args[-1]
        if "Set-ItemProperty" in script:
            return _fail(stderr="Access denied: registry write denied")
        return _ok(stdout=read_payload)

    runner.run = patched_run

    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, WindowsKdumpActionStatus.FAILED)
    testbase.assert_true("Access denied" in (result.stderr or ""))
    return


def test_list_recent_dumps_raises_on_unexpected_failure():
    """
        ``list_recent_dumps`` raises :class:`WindowsKdumpQueryError` on
        unexpected ``Get-ChildItem`` failure.
    """
    runner = _MockCommandRunner()
    runner.default_result = _fail(stderr="Get-ChildItem: catastrophic", exit_code=2)
    manager = _make_manager(runner=runner)

    raised = False
    try:
        manager.list_recent_dumps()
    except WindowsKdumpQueryError:
        raised = True
    testbase.assert_true(raised)
    return

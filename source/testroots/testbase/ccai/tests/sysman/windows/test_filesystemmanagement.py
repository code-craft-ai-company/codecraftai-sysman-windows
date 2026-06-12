"""
    Tests for :class:`WindowsFilesystemManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.filesystemmanagement import (
    WindowsFileInfo,
    WindowsFilesystemActionResult,
    WindowsFilesystemActionStatus,
    WindowsFilesystemManager,
    WindowsFilesystemManagerConfig,
    WindowsFilesystemQueryError,
    WindowsFstabEntry,
    WindowsMountInfo,
    _coerce_mode_to_int,
    _merge_psdrive_and_volume,
    _mode_to_icacls_grants,
    _normalise_drive_letter,
    _parse_get_acl,
    _parse_get_psdrive,
    _parse_get_volume,
)


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


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _build_powershell_argv(script: str) -> list:
    """
        Build the canonical PowerShell argv used by the manager.
    """
    rtnval = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    return rtnval


def _lookup_psdrive_argv(drive_name: str) -> list:
    """
        Build the argv the manager produces for the
        ``_lookup_psdrive`` probe of ``drive_name``.
    """
    script = (
        "Get-PSDrive -Name '{}' "
        "| Select-Object Name, Provider, Root, DisplayRoot, "
        "Used, Free "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(drive_name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _get_acl_argv(path: str) -> list:
    """
        Build the argv the manager produces for ``_get_acl_record``.
    """
    script = (
        "Get-Acl '{}' "
        "| Select-Object Owner, Group, AccessToString "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(path)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _get_item_argv(path: str) -> list:
    """
        Build the argv the manager produces for ``_get_item_record``.
    """
    script = (
        "Get-Item '{}' "
        "| Select-Object Mode, Length, LastWriteTime, FullName "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(path)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _list_psdrive_argv() -> list:
    """
        Build the argv the manager produces for the ``Get-PSDrive``
        listing inside :meth:`get_mounts`.
    """
    script = (
        "Get-PSDrive -PSProvider FileSystem "
        "| Select-Object Name, Provider, Root, DisplayRoot, "
        "Used, Free "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _list_volume_argv() -> list:
    """
        Build the argv the manager produces for the ``Get-Volume``
        listing inside :meth:`get_mounts`.
    """
    script = (
        "Get-Volume "
        "| Select-Object DriveLetter, FileSystem, FileSystemType, "
        "Size, SizeRemaining "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


# ----------------------------------------------------------------------
# mount
# ----------------------------------------------------------------------


def test_mount_dispatches_new_psdrive():
    """
        mount dispatches a ``New-PSDrive`` invocation with
        ``-PSProvider FileSystem`` and ``-Persist``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    # No existing mapping for X:.
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls
        if "New-PSDrive" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected = _build_powershell_argv(
        script=(
            "New-PSDrive -Name 'X' -PSProvider FileSystem "
            "-Root '\\\\server\\share' -Persist"
        ),
    )
    testbase.assert_equal(write_calls[0]["args"], expected)
    testbase.assert_equal(result.mountpoint, "X:")
    testbase.assert_equal(result.device, "\\\\server\\share")
    return


def test_mount_skipped_when_already_mapped_to_same_target():
    """
        mount returns SKIPPED when ``Get-PSDrive`` already shows the
        drive letter mapped to the same root.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Name": "X",
            "Provider": "FileSystem",
            "Root": "X:\\",
            "DisplayRoot": "\\\\server\\share",
            "Used": 1024,
            "Free": 2048,
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "New-PSDrive" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_mount_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemManager(
        runner=runner,
        config=WindowsFilesystemManagerConfig(dry_run=True),
    )
    result = manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# unmount
# ----------------------------------------------------------------------


def test_unmount_dispatches_remove_psdrive():
    """
        unmount on a mapped drive dispatches ``Remove-PSDrive``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Name": "X",
            "Provider": "FileSystem",
            "Root": "X:\\",
            "DisplayRoot": "\\\\server\\share",
            "Used": 1024,
            "Free": 2048,
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.unmount(mountpoint="X:")
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SUCCESS,
    )
    expected = _build_powershell_argv(script="Remove-PSDrive -Name 'X'")
    write_calls = [
        c for c in runner.calls
        if "Remove-PSDrive" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_unmount_skipped_when_not_mapped():
    """
        unmount returns SKIPPED when the drive letter is not mapped.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_lookup_psdrive_argv(drive_name="Z"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.unmount(mountpoint="Z:")
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "Remove-PSDrive" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# fstab verbs (always FAILED with documenting message)
# ----------------------------------------------------------------------


def test_add_fstab_entry_returns_failed_with_message():
    """
        add_fstab_entry always returns FAILED with a documenting
        message; the runner is never invoked.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    spec = WindowsFstabEntry(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    result = manager.add_fstab_entry(spec=spec)
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.FAILED,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("/etc/fstab" in result.message)
    testbase.assert_true("Set-Partition" in result.message)
    return


def test_remove_fstab_entry_returns_failed_with_message():
    """
        remove_fstab_entry always returns FAILED with a documenting
        message; the runner is never invoked.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.remove_fstab_entry(mountpoint="X:")
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.FAILED,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("/etc/fstab" in result.message)
    return


def test_add_fstab_entry_dry_run_short_circuits():
    """
        add_fstab_entry under dry-run returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemManager(
        runner=runner,
        config=WindowsFilesystemManagerConfig(dry_run=True),
    )
    spec = WindowsFstabEntry(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    result = manager.add_fstab_entry(spec=spec)
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# chown
# ----------------------------------------------------------------------


def test_chown_dispatches_icacls_setowner():
    """
        chown dispatches an ``icacls /setowner`` invocation when the
        current owner does not match.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_acl_argv(path="C:\\test"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Owner": "MACHINE\\someoneelse",
            "Group": None,
            "AccessToString": "",
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.chown(path="C:\\test", user="alice")
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls
        if "icacls" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected = _build_powershell_argv(
        script="icacls 'C:\\test' /setowner 'alice'",
    )
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_chown_skipped_when_owner_matches_via_bare_name():
    """
        chown returns SKIPPED when the path's current owner already
        matches the requested user (after stripping any ``DOMAIN\\``
        prefix).
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_acl_argv(path="C:\\test"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Owner": "MACHINE\\alice",
            "Group": None,
            "AccessToString": "",
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.chown(path="C:\\test", user="alice")
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "icacls" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_chown_recursive_appends_T_flag():
    """
        chown with ``recursive=True`` appends ``/T`` to the icacls
        invocation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.chown(
        path="C:\\test", user="alice", recursive=True,
    )
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls
        if "icacls" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected = _build_powershell_argv(
        script="icacls 'C:\\test' /setowner 'alice' /T",
    )
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


# ----------------------------------------------------------------------
# chmod
# ----------------------------------------------------------------------


def test_chmod_dispatches_icacls_grant_for_canonical_mode():
    """
        chmod with a supported mode dispatches an ``icacls`` invocation
        with the rendered grants from the partial mode-to-ACL table.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_acl_argv(path="C:\\test"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Owner": "alice",
            "Group": None,
            "AccessToString": "",
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.chmod(path="C:\\test", mode=0o644)
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls
        if "icacls" in c["args"][-1] and "/grant" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected = _build_powershell_argv(
        script=(
            "icacls 'C:\\test' /inheritance:r "
            "/grant 'alice:(R,W)' /grant 'Users:(R)'"
        ),
    )
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_chmod_unsupported_mode_returns_failed():
    """
        chmod with a mode outside the partial mode-to-ACL table
        returns FAILED with a documenting message.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.chmod(path="C:\\test", mode=0o641)
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.FAILED,
    )
    testbase.assert_true("partial mode-to-ACL table" in result.message)
    write_calls = [
        c for c in runner.calls
        if "icacls" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_chmod_skipped_when_acl_already_matches():
    """
        chmod returns SKIPPED when ``Get-Acl`` already reports an
        access string that contains every expected grant substring.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_acl_argv(path="C:\\test"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Owner": "alice",
            "Group": None,
            "AccessToString": (
                "alice:(R,W)\nUsers:(R)\nSomethingElse:(F)"
            ),
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.chmod(path="C:\\test", mode=0o644)
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "/grant" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_mode_to_icacls_grants_canonical_modes():
    """
        Each canonical mode produces the documented grant list.
    """
    grants_644 = _mode_to_icacls_grants(mode_int=0o644)
    testbase.assert_equal(grants_644, ["%OWNER%:(R,W)", "Users:(R)"])

    grants_755 = _mode_to_icacls_grants(mode_int=0o755)
    testbase.assert_equal(grants_755, ["%OWNER%:(F)", "Users:(RX)"])

    grants_700 = _mode_to_icacls_grants(mode_int=0o700)
    testbase.assert_equal(grants_700, ["%OWNER%:(F)"])

    grants_777 = _mode_to_icacls_grants(mode_int=0o777)
    testbase.assert_equal(grants_777, ["%OWNER%:(F)", "Users:(F)"])

    no_match = _mode_to_icacls_grants(mode_int=0o641)
    testbase.assert_equal(no_match, None)
    return


def test_coerce_mode_to_int_handles_octal_strings():
    """
        ``_coerce_mode_to_int`` accepts ``int``, ``"0o644"``,
        ``"0644"``, and ``"644"`` shapes.
    """
    testbase.assert_equal(_coerce_mode_to_int(mode=0o644), 0o644)
    testbase.assert_equal(_coerce_mode_to_int(mode="0o644"), 0o644)
    testbase.assert_equal(_coerce_mode_to_int(mode="0644"), 0o644)
    testbase.assert_equal(_coerce_mode_to_int(mode="644"), 0o644)
    testbase.assert_equal(_coerce_mode_to_int(mode="bad"), None)
    testbase.assert_equal(_coerce_mode_to_int(mode=None), None)
    return


# ----------------------------------------------------------------------
# get_mounts
# ----------------------------------------------------------------------


def test_get_mounts_merges_psdrive_and_volume():
    """
        get_mounts merges ``Get-PSDrive`` and ``Get-Volume`` rows on
        the drive-letter token, populating ``fstype`` from the volume
        row when available.
    """
    runner = _MockCommandRunner()
    psdrive_records = [
        {
            "Name": "C",
            "Provider": "FileSystem",
            "Root": "C:\\",
            "DisplayRoot": None,
            "Used": 1000,
            "Free": 2000,
        },
        {
            "Name": "X",
            "Provider": "FileSystem",
            "Root": "X:\\",
            "DisplayRoot": "\\\\server\\share",
            "Used": 500,
            "Free": 1500,
        },
    ]
    volume_records = [
        {
            "DriveLetter": "C",
            "FileSystem": "NTFS",
            "FileSystemType": "NTFS",
            "Size": 4000,
            "SizeRemaining": 2000,
        },
    ]
    runner.results[tuple(_list_psdrive_argv())] = RunResult(
        exit_code=0, stdout=json.dumps(psdrive_records), stderr="",
    )
    runner.results[tuple(_list_volume_argv())] = RunResult(
        exit_code=0, stdout=json.dumps(volume_records), stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    mounts = manager.get_mounts()
    testbase.assert_equal(len(mounts), 2)

    by_mp = {m.mountpoint: m for m in mounts}
    c_mount = by_mp["C:"]
    testbase.assert_equal(c_mount.fstype, "NTFS")
    testbase.assert_equal(c_mount.size_bytes, 4000)
    testbase.assert_equal(c_mount.free_bytes, 2000)

    x_mount = by_mp["X:"]
    testbase.assert_equal(x_mount.fstype, None)
    testbase.assert_equal(x_mount.device, "\\\\server\\share")
    return


def test_get_mounts_tolerates_missing_get_volume():
    """
        get_mounts does not raise when ``Get-Volume`` is unavailable
        (older Windows hosts without the Storage module).  The
        ``fstype`` fields remain ``None``.
    """
    runner = _MockCommandRunner()
    psdrive_records = [
        {
            "Name": "C",
            "Provider": "FileSystem",
            "Root": "C:\\",
            "DisplayRoot": None,
            "Used": 1000,
            "Free": 2000,
        },
    ]
    runner.results[tuple(_list_psdrive_argv())] = RunResult(
        exit_code=0, stdout=json.dumps(psdrive_records), stderr="",
    )
    runner.results[tuple(_list_volume_argv())] = RunResult(
        exit_code=1, stdout="", stderr="not available",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    mounts = manager.get_mounts()
    testbase.assert_equal(len(mounts), 1)
    testbase.assert_equal(mounts[0].mountpoint, "C:")
    testbase.assert_equal(mounts[0].fstype, None)
    return


def test_get_mounts_raises_on_psdrive_failure():
    """
        get_mounts raises :class:`WindowsFilesystemQueryError` when the
        ``Get-PSDrive`` cmdlet exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_list_psdrive_argv())] = RunResult(
        exit_code=1, stdout="", stderr="something broke",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    raised = False
    try:
        manager.get_mounts()
    except WindowsFilesystemQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# stat
# ----------------------------------------------------------------------


def test_stat_parses_get_item_and_get_acl():
    """
        stat merges ``Get-Item`` and ``Get-Acl`` JSON outputs into a
        :class:`WindowsFileInfo`.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_item_argv(path="C:\\test"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Mode": "-a----",
            "Length": 4096,
            "LastWriteTime": "2026-04-24T10:00:00",
            "FullName": "C:\\test",
        }),
        stderr="",
    )
    runner.results[tuple(_get_acl_argv(path="C:\\test"))] = RunResult(
        exit_code=0,
        stdout=json.dumps({
            "Owner": "MACHINE\\alice",
            "Group": None,
            "AccessToString": "MACHINE\\alice:(F)",
        }),
        stderr="",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    info = manager.stat(path="C:\\test")
    testbase.assert_true(info is not None)
    testbase.assert_equal(info.path, "C:\\test")
    testbase.assert_equal(info.owner, "MACHINE\\alice")
    testbase.assert_equal(info.group, None)
    testbase.assert_equal(info.size, 4096)
    testbase.assert_equal(info.modified, "2026-04-24T10:00:00")
    testbase.assert_equal(info.mode, "MACHINE\\alice:(F)")
    return


def test_stat_returns_none_on_missing_path():
    """
        stat returns ``None`` when ``Get-Item`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_item_argv(path="C:\\missing"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    info = manager.stat(path="C:\\missing")
    testbase.assert_equal(info, None)
    return


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def test_parse_get_psdrive_handles_array_and_bare_object():
    """
        ``_parse_get_psdrive`` returns the array verbatim and folds
        the bare-object shape into a one-element list.
    """
    array_text = json.dumps([
        {"Name": "C"},
        {"Name": "D"},
    ])
    parsed = _parse_get_psdrive(text=array_text)
    testbase.assert_equal(len(parsed), 2)

    object_text = json.dumps({"Name": "C"})
    parsed_obj = _parse_get_psdrive(text=object_text)
    testbase.assert_equal(len(parsed_obj), 1)
    testbase.assert_equal(parsed_obj[0]["Name"], "C")
    return


def test_parse_get_acl_handles_dict_payload():
    """
        ``_parse_get_acl`` returns the dict verbatim.
    """
    record = {"Owner": "alice", "AccessToString": "alice:(F)"}
    parsed = _parse_get_acl(text=json.dumps(record))
    testbase.assert_equal(parsed["Owner"], "alice")
    return


def test_normalise_drive_letter_strips_colon_and_slash():
    """
        ``_normalise_drive_letter`` reduces ``"X:"``, ``"X:\\"``, and
        ``"X"`` to the bare letter ``"X"``.
    """
    testbase.assert_equal(_normalise_drive_letter(mountpoint="X"), "X")
    testbase.assert_equal(_normalise_drive_letter(mountpoint="X:"), "X")
    testbase.assert_equal(_normalise_drive_letter(mountpoint="X:\\"), "X")
    return


def test_merge_psdrive_and_volume_skips_non_filesystem_providers():
    """
        ``_merge_psdrive_and_volume`` skips PSDrive rows whose
        ``Provider`` is not the FileSystem provider.
    """
    psdrive_records = [
        {
            "Name": "Variable",
            "Provider": "Variable",
            "Root": "",
            "DisplayRoot": None,
            "Used": 0,
            "Free": 0,
        },
        {
            "Name": "C",
            "Provider": "FileSystem",
            "Root": "C:\\",
            "DisplayRoot": None,
            "Used": 1,
            "Free": 1,
        },
    ]
    merged = _merge_psdrive_and_volume(
        psdrive_records=psdrive_records,
        volume_records=[],
    )
    testbase.assert_equal(len(merged), 1)
    testbase.assert_equal(merged[0].mountpoint, "C:")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_when_configured():
    """
        Writes pass ``elevated=True`` when the config is configured to
        elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsFilesystemManager(
        runner=runner,
        config=WindowsFilesystemManagerConfig(elevated=True),
    )
    manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    write_calls = [
        c for c in runner.calls
        if "New-PSDrive" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Idempotency-probe and direct read commands never elevate, even
        when the config is configured to elevate writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsFilesystemManager(
        runner=runner,
        config=WindowsFilesystemManagerConfig(elevated=True),
    )
    manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    probe_calls = [
        c for c in runner.calls
        if "Get-PSDrive" in c["args"][-1]
    ]
    testbase.assert_true(len(probe_calls) >= 1)
    for call in probe_calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Argv shape
# ----------------------------------------------------------------------


def test_powershell_argv_construction_includes_noprofile():
    """
        Every dispatched command uses
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    for call in runner.calls:
        argv = call["args"]
        testbase.assert_equal(argv[0], "powershell.exe")
        testbase.assert_equal(argv[1], "-NoProfile")
        testbase.assert_equal(argv[2], "-NonInteractive")
        testbase.assert_equal(argv[3], "-Command")
        testbase.assert_equal(len(argv), 5)
    return


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


def test_failed_write_returns_failed_with_stderr():
    """
        A non-zero exit from the write maps to FAILED with the
        captured stderr / exit_code preserved.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_lookup_psdrive_argv(drive_name="X"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    write_argv = _build_powershell_argv(
        script=(
            "New-PSDrive -Name 'X' -PSProvider FileSystem "
            "-Root '\\\\server\\share' -Persist"
        ),
    )
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=1, stdout="", stderr="Access is denied",
    )
    manager = WindowsFilesystemManager(
        runner=runner, config=WindowsFilesystemManagerConfig(),
    )
    result = manager.mount(
        device="\\\\server\\share",
        mountpoint="X:",
        fstype="cifs",
    )
    testbase.assert_equal(
        result.status, WindowsFilesystemActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Access is denied")
    return

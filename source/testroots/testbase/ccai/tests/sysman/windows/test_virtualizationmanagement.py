"""
    Tests for :class:`WindowsVirtualizationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  Reads (``Get-VM``, ``Get-VMSnapshot``,
    ``Get-VMHardDiskDrive``) are exercised via seeded JSON payloads;
    no live PowerShell is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.virtualizationmanagement import (
    WindowsVirtualizationActionStatus,
    WindowsVirtualizationManager,
    WindowsVirtualizationManagerConfig,
    WindowsVirtualizationQueryError,
    WindowsVmDiskSpec,
    WindowsVmSpec,
    WindowsVmState,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
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

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ps_argv(script: str) -> tuple:
    """
        Build a key tuple for indexing ``runner.results`` for a
        scriptblock invocation built by
        :meth:`WindowsVirtualizationManager._build_powershell_argv`.
    """
    rtnval = (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    )
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = True,
    dry_run: bool = False,
    default_switch: Optional[str] = None,
    default_vhd_dir: Optional[str] = None,
) -> WindowsVirtualizationManager:
    """
        Construct a manager with the canonical config used in tests.
    """
    config = WindowsVirtualizationManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        default_switch=default_switch,
        default_vhd_dir=default_vhd_dir,
    )
    rtnval = WindowsVirtualizationManager(runner=runner, config=config)
    return rtnval


def _vm_record(
    *,
    name: str,
    state: str,
    cpu_count: int = 2,
    memory_bytes: int = 2 * 1024 * 1024 * 1024,
    vm_id: str = "00000000-0000-0000-0000-000000000001",
) -> dict:
    """
        Build a parsed ``Get-VM`` record dict.
    """
    rtnval = {
        "Name": name,
        "State": state,
        "ProcessorCount": cpu_count,
        "MemoryAssigned": memory_bytes,
        "Uptime": {"TotalSeconds": 3600},
        "OperatingSystem": "Microsoft Windows Server 2022",
        "Id": vm_id,
    }
    return rtnval


def _seed_get_vm(
    runner: _MockCommandRunner,
    *,
    name: str,
    state: str,
    cpu_count: int = 2,
    memory_bytes: int = 2 * 1024 * 1024 * 1024,
) -> None:
    """
        Seed ``Get-VM -Name <name>`` to return a single-VM JSON
        payload with the given state.
    """
    record = _vm_record(
        name=name,
        state=state,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
    )
    payload = json.dumps(record)
    script = (
        "Get-VM -Name '{}' "
        "| Select-Object Name, State, ProcessorCount, "
        "MemoryAssigned, Uptime, OperatingSystem, Id "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    runner.results[_ps_argv(script)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_get_vm_missing(
    runner: _MockCommandRunner,
    *,
    name: str,
) -> None:
    """
        Seed ``Get-VM -Name <name>`` to report the VM is unknown.
    """
    script = (
        "Get-VM -Name '{}' "
        "| Select-Object Name, State, ProcessorCount, "
        "MemoryAssigned, Uptime, OperatingSystem, Id "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    runner.results[_ps_argv(script)] = RunResult(
        exit_code=1,
        stdout="",
        stderr=(
            "Get-VM : Hyper-V was unable to find a virtual machine "
            "with name \"{}\".".format(name)
        ),
    )
    return


def _seed_get_vmharddiskdrive(
    runner: _MockCommandRunner,
    *,
    vm: str,
    drives: list,
) -> None:
    """
        Seed ``Get-VMHardDiskDrive -VMName <vm>`` with a JSON payload.
    """
    if len(drives) == 0:
        payload = ""
    elif len(drives) == 1:
        payload = json.dumps(drives[0])
    else:
        payload = json.dumps(drives)
    script = (
        "Get-VMHardDiskDrive -VMName '{}' "
        "| Select-Object Path, ControllerType, "
        "ControllerNumber, ControllerLocation "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(vm)
    runner.results[_ps_argv(script)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_get_vmsnapshot(
    runner: _MockCommandRunner,
    *,
    vm: str,
    snapshots: list,
) -> None:
    """
        Seed ``Get-VMSnapshot -VMName <vm>`` with a JSON payload.
    """
    if len(snapshots) == 0:
        payload = ""
    elif len(snapshots) == 1:
        payload = json.dumps(snapshots[0])
    else:
        payload = json.dumps(snapshots)
    script = (
        "Get-VMSnapshot -VMName '{}' "
        "| Select-Object Name, CreationTime, ParentSnapshotName "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(vm)
    runner.results[_ps_argv(script)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


# ----------------------------------------------------------------------
# list_vms
# ----------------------------------------------------------------------


def test_list_vms_parses_get_vm_output():
    """
        list_vms parses ``Get-VM | ConvertTo-Json`` and returns
        :class:`WindowsVmInfo` rows.
    """
    runner = _MockCommandRunner()
    payload = json.dumps([
        _vm_record(name="webvm", state="Running"),
        _vm_record(
            name="batchvm",
            state="Off",
            cpu_count=4,
            memory_bytes=4 * 1024 * 1024 * 1024,
        ),
    ])
    list_script = (
        "Get-VM "
        "| Select-Object Name, State, ProcessorCount, "
        "MemoryAssigned, Uptime, OperatingSystem, Id "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(list_script)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )

    manager = _make_manager(runner=runner)
    rows = manager.list_vms()

    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(by_name["webvm"].state, WindowsVmState.RUNNING)
    testbase.assert_equal(by_name["webvm"].cpu_count, 2)
    testbase.assert_equal(by_name["webvm"].memory_mb, 2048)
    testbase.assert_equal(
        by_name["batchvm"].state, WindowsVmState.STOPPED,
    )
    testbase.assert_equal(by_name["batchvm"].memory_mb, 4096)
    return


def test_list_vms_raises_on_unexpected_failure():
    runner = _MockCommandRunner()
    list_script = (
        "Get-VM "
        "| Select-Object Name, State, ProcessorCount, "
        "MemoryAssigned, Uptime, OperatingSystem, Id "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(list_script)] = RunResult(
        exit_code=1, stdout="", stderr="hyper-v not available",
    )

    manager = _make_manager(runner=runner)
    raised = False
    try:
        manager.list_vms()
    except WindowsVirtualizationQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_reads_never_elevate():
    """
        Read verbs always pass ``elevated=False`` to the runner.
    """
    runner = _MockCommandRunner()
    list_script = (
        "Get-VM "
        "| Select-Object Name, State, ProcessorCount, "
        "MemoryAssigned, Uptime, OperatingSystem, Id "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(list_script)] = RunResult(
        exit_code=0,
        stdout=json.dumps(_vm_record(name="webvm", state="Running")),
        stderr="",
    )
    _seed_get_vm(runner=runner, name="webvm", state="Running")
    _seed_get_vmsnapshot(runner=runner, vm="webvm", snapshots=[])

    manager = _make_manager(runner=runner, elevated=True)
    manager.list_vms()
    manager.get_vm_info(name="webvm")
    manager.list_snapshots(vm="webvm")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_vm_info
# ----------------------------------------------------------------------


def test_get_vm_info_returns_none_for_unknown_vm():
    runner = _MockCommandRunner()
    _seed_get_vm_missing(runner=runner, name="ghostvm")

    manager = _make_manager(runner=runner)
    result = manager.get_vm_info(name="ghostvm")
    testbase.assert_equal(result, None)
    return


# ----------------------------------------------------------------------
# create_vm
# ----------------------------------------------------------------------


def test_create_vm_invokes_new_vm():
    """
        create_vm dispatches a multi-step ``New-VM`` + ``Set-VMProcessor``
        scriptblock with the rendered argv.
    """
    runner = _MockCommandRunner()
    _seed_get_vm_missing(runner=runner, name="webvm")

    manager = _make_manager(
        runner=runner,
        elevated=True,
        default_switch="Default Switch",
        default_vhd_dir="C:\\Hyper-V\\Disks",
    )
    spec = WindowsVmSpec(
        name="webvm",
        cpu_count=2,
        memory_mb=2048,
        disk_size_gb=20,
        boot_kind=2,
    )
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)

    create_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "New-VM" in c["args"][-1]
    ]
    testbase.assert_equal(len(create_calls), 1)
    script = create_calls[0]["args"][-1]
    testbase.assert_equal("-Name 'webvm'" in script, True)
    expected_memory_bytes = 2048 * 1024 * 1024
    testbase.assert_equal(
        "-MemoryStartupBytes {}".format(expected_memory_bytes) in script,
        True,
    )
    testbase.assert_equal("-Generation 2" in script, True)
    testbase.assert_equal("-NewVHDPath" in script, True)
    expected_vhd_size = 20 * 1024 * 1024 * 1024
    testbase.assert_equal(
        "-NewVHDSizeBytes {}".format(expected_vhd_size) in script,
        True,
    )
    testbase.assert_equal("-SwitchName 'Default Switch'" in script, True)
    testbase.assert_equal("Set-VMProcessor" in script, True)
    testbase.assert_equal("-Count 2" in script, True)
    testbase.assert_equal(create_calls[0]["elevated"], True)
    return


def test_create_vm_skipped_when_present():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Running")

    manager = _make_manager(runner=runner)
    spec = WindowsVmSpec(name="webvm", memory_mb=2048, cpu_count=2)
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SKIPPED)
    create_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "New-VM" in c["args"][-1]
    ]
    testbase.assert_equal(create_calls, [])
    return


def test_create_vm_dry_run():
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)
    spec = WindowsVmSpec(name="webvm", memory_mb=1024, cpu_count=1)
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[0], "powershell.exe")
    testbase.assert_equal("New-VM" in result.command[-1], True)
    return


# ----------------------------------------------------------------------
# delete_vm / lifecycle
# ----------------------------------------------------------------------


def test_delete_vm_invokes_remove_vm_and_carries_advisory():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Off")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.delete_vm(name="webvm")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    delete_calls = [
        c for c in runner.calls
        if "Remove-VM" in c["args"][-1]
    ]
    testbase.assert_equal(len(delete_calls), 1)
    script = delete_calls[0]["args"][-1]
    testbase.assert_equal("Stop-VM -Name 'webvm' -Force -TurnOff" in script, True)
    testbase.assert_equal("Remove-VM -Name 'webvm' -Force" in script, True)
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("destroyed" in result.message, True)
    return


def test_delete_vm_skipped_when_absent():
    runner = _MockCommandRunner()
    _seed_get_vm_missing(runner=runner, name="ghostvm")

    manager = _make_manager(runner=runner)
    result = manager.delete_vm(name="ghostvm")
    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SKIPPED)
    return


def test_start_vm_skipped_when_running():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Running")

    manager = _make_manager(runner=runner)
    result = manager.start_vm(name="webvm")
    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SKIPPED)
    return


def test_start_vm_invokes_start_vm_when_stopped():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Off")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    start_calls = [
        c for c in runner.calls
        if c["args"][-1] == "Start-VM -Name 'webvm'"
    ]
    testbase.assert_equal(len(start_calls), 1)
    testbase.assert_equal(start_calls[0]["elevated"], True)
    return


def test_stop_vm_force_invokes_stop_vm_turnoff():
    """
        ``stop_vm(force=True)`` issues
        ``Stop-VM -Name <name> -Force -TurnOff``;
        ``stop_vm(force=False)`` issues plain ``Stop-VM``.
    """
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Running")

    manager = _make_manager(runner=runner, elevated=True)
    forced = manager.stop_vm(name="webvm", force=True)
    testbase.assert_equal(forced.status, WindowsVirtualizationActionStatus.SUCCESS)
    forced_calls = [
        c for c in runner.calls
        if c["args"][-1] == "Stop-VM -Name 'webvm' -Force -TurnOff"
    ]
    testbase.assert_equal(len(forced_calls), 1)

    graceful_calls = [
        c for c in runner.calls
        if c["args"][-1] == "Stop-VM -Name 'webvm'"
    ]
    testbase.assert_equal(graceful_calls, [])
    return


def test_stop_vm_graceful_invokes_stop_vm_default():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Running")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.stop_vm(name="webvm", force=False)

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    graceful_calls = [
        c for c in runner.calls
        if c["args"][-1] == "Stop-VM -Name 'webvm'"
    ]
    testbase.assert_equal(len(graceful_calls), 1)
    return


def test_pause_vm_invokes_suspend_vm():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Running")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.pause_vm(name="webvm")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    suspend_calls = [
        c for c in runner.calls
        if c["args"][-1] == "Suspend-VM -Name 'webvm'"
    ]
    testbase.assert_equal(len(suspend_calls), 1)
    testbase.assert_equal(suspend_calls[0]["elevated"], True)
    return


def test_resume_vm_invokes_resume_vm():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Paused")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.resume_vm(name="webvm")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    resume_calls = [
        c for c in runner.calls
        if c["args"][-1] == "Resume-VM -Name 'webvm'"
    ]
    testbase.assert_equal(len(resume_calls), 1)
    testbase.assert_equal(resume_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# attach_disk / detach_disk
# ----------------------------------------------------------------------


def test_attach_disk_invokes_add_vmharddiskdrive():
    runner = _MockCommandRunner()
    _seed_get_vmharddiskdrive(runner=runner, vm="webvm", drives=[])

    manager = _make_manager(runner=runner, elevated=True)
    disk = WindowsVmDiskSpec(
        path="C:\\VHDs\\extra.vhdx",
        controller_type="SCSI",
    )
    result = manager.attach_disk(vm="webvm", disk_spec=disk)

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    add_calls = [
        c for c in runner.calls
        if "Add-VMHardDiskDrive" in c["args"][-1]
    ]
    testbase.assert_equal(len(add_calls), 1)
    script = add_calls[0]["args"][-1]
    testbase.assert_equal("-VMName 'webvm'" in script, True)
    testbase.assert_equal("-Path 'C:\\VHDs\\extra.vhdx'" in script, True)
    testbase.assert_equal("-ControllerType SCSI" in script, True)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_attach_disk_skipped_when_attached():
    runner = _MockCommandRunner()
    _seed_get_vmharddiskdrive(
        runner=runner,
        vm="webvm",
        drives=[
            {
                "Path": "C:\\VHDs\\webvm.vhdx",
                "ControllerType": "SCSI",
                "ControllerNumber": 0,
                "ControllerLocation": 0,
            },
        ],
    )

    manager = _make_manager(runner=runner)
    disk = WindowsVmDiskSpec(path="C:\\VHDs\\webvm.vhdx")
    result = manager.attach_disk(vm="webvm", disk_spec=disk)

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SKIPPED)
    return


def test_detach_disk_invokes_remove_vmharddiskdrive():
    runner = _MockCommandRunner()
    _seed_get_vmharddiskdrive(
        runner=runner,
        vm="webvm",
        drives=[
            {
                "Path": "C:\\VHDs\\webvm.vhdx",
                "ControllerType": "SCSI",
                "ControllerNumber": 0,
                "ControllerLocation": 0,
            },
        ],
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.detach_disk(vm="webvm", disk_id="C:\\VHDs\\webvm.vhdx")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    remove_calls = [
        c for c in runner.calls
        if "Remove-VMHardDiskDrive" in c["args"][-1]
    ]
    testbase.assert_equal(len(remove_calls), 1)
    script = remove_calls[0]["args"][-1]
    testbase.assert_equal("-VMName 'webvm'" in script, True)
    testbase.assert_equal("$drive.ControllerType" in script, True)
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("detached" in result.message, True)
    return


def test_detach_disk_skipped_when_not_attached():
    runner = _MockCommandRunner()
    _seed_get_vmharddiskdrive(runner=runner, vm="webvm", drives=[])

    manager = _make_manager(runner=runner)
    result = manager.detach_disk(vm="webvm", disk_id="C:\\VHDs\\nope.vhdx")
    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------


def test_snapshot_vm_invokes_checkpoint_vm():
    runner = _MockCommandRunner()
    _seed_get_vmsnapshot(runner=runner, vm="webvm", snapshots=[])

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.snapshot_vm(vm="webvm", snapshot_name="preupgrade")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    create_calls = [
        c for c in runner.calls
        if "Checkpoint-VM" in c["args"][-1]
    ]
    testbase.assert_equal(len(create_calls), 1)
    script = create_calls[0]["args"][-1]
    testbase.assert_equal("-Name 'webvm'" in script, True)
    testbase.assert_equal("-SnapshotName 'preupgrade'" in script, True)
    testbase.assert_equal(create_calls[0]["elevated"], True)
    testbase.assert_equal(result.snapshot_name, "preupgrade")
    return


def test_snapshot_vm_skipped_when_named_snapshot_exists():
    runner = _MockCommandRunner()
    _seed_get_vmsnapshot(
        runner=runner,
        vm="webvm",
        snapshots=[
            {
                "Name": "preupgrade",
                "CreationTime": "2024-01-01T12:00:00",
                "ParentSnapshotName": None,
            },
        ],
    )

    manager = _make_manager(runner=runner)
    result = manager.snapshot_vm(vm="webvm", snapshot_name="preupgrade")
    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SKIPPED)
    return


def test_restore_snapshot_invokes_restore_vmsnapshot():
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)
    result = manager.restore_snapshot(
        vm="webvm", snapshot_name="preupgrade",
    )

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.SUCCESS)
    revert_calls = [
        c for c in runner.calls
        if "Restore-VMSnapshot" in c["args"][-1]
    ]
    testbase.assert_equal(len(revert_calls), 1)
    script = revert_calls[0]["args"][-1]
    testbase.assert_equal("-VMName 'webvm'" in script, True)
    testbase.assert_equal("-Name 'preupgrade'" in script, True)
    testbase.assert_equal("-Confirm:$false" in script, True)
    testbase.assert_equal(revert_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("reverted" in result.message, True)
    return


def test_list_snapshots_parses_get_vmsnapshot_output():
    runner = _MockCommandRunner()
    _seed_get_vmsnapshot(
        runner=runner,
        vm="webvm",
        snapshots=[
            {
                "Name": "preupgrade",
                "CreationTime": "2024-01-01T12:00:00",
                "ParentSnapshotName": None,
            },
            {
                "Name": "checkpoint",
                "CreationTime": "2024-02-15T09:30:11",
                "ParentSnapshotName": "preupgrade",
            },
        ],
    )

    manager = _make_manager(runner=runner)
    rows = manager.list_snapshots(vm="webvm")

    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(by_name["preupgrade"].parent, None)
    testbase.assert_equal(by_name["checkpoint"].parent, "preupgrade")
    testbase.assert_equal(
        by_name["preupgrade"].created.startswith("2024-01-01"), True,
    )
    return


# ----------------------------------------------------------------------
# Elevation / failure surfacing
# ----------------------------------------------------------------------


def test_writes_elevate():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Off")
    _seed_get_vmharddiskdrive(runner=runner, vm="webvm", drives=[])
    _seed_get_vmsnapshot(runner=runner, vm="webvm", snapshots=[])

    manager = _make_manager(runner=runner, elevated=True)
    manager.start_vm(name="webvm")
    manager.attach_disk(
        vm="webvm",
        disk_spec=WindowsVmDiskSpec(path="C:\\VHDs\\x.vhdx"),
    )
    manager.snapshot_vm(vm="webvm", snapshot_name="snap1")

    write_calls = [
        c for c in runner.calls
        if any(
            token in c["args"][-1]
            for token in ("Start-VM", "Add-VMHardDiskDrive", "Checkpoint-VM")
        )
    ]
    testbase.assert_equal(len(write_calls) > 0, True)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_failed_command_surfaces_stderr():
    runner = _MockCommandRunner()
    _seed_get_vm(runner=runner, name="webvm", state="Off")
    runner.results[_ps_argv("Start-VM -Name 'webvm'")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Start-VM : The operation cannot be performed.",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, WindowsVirtualizationActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr,
        "Start-VM : The operation cannot be performed.",
    )
    return


def test_state_enum_payload_normalises():
    """
        Hyper-V occasionally serialises ``State`` as the numeric enum
        value rather than a string.  ``2`` -> RUNNING; ``3`` -> STOPPED.
    """
    runner = _MockCommandRunner()
    record = _vm_record(name="webvm", state="Running")
    record["State"] = 2  # numeric serialisation
    payload = json.dumps(record)
    script = (
        "Get-VM -Name 'webvm' "
        "| Select-Object Name, State, ProcessorCount, "
        "MemoryAssigned, Uptime, OperatingSystem, Id "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )

    manager = _make_manager(runner=runner)
    info = manager.get_vm_info(name="webvm")
    testbase.assert_equal(info is not None, True)
    testbase.assert_equal(info.state, WindowsVmState.RUNNING)
    return

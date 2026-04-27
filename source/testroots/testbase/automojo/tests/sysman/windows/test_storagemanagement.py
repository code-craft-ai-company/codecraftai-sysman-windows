"""
    Tests for :class:`WindowsStorageManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.storagemanagement import (
    WindowsDiskInfo,
    WindowsPoolSpec,
    WindowsStorageActionStatus,
    WindowsStorageManager,
    WindowsStorageManagerConfig,
    WindowsVolumeInfo,
    WindowsVolumeSpec,
    _build_disks_from_records,
    _build_volumes_from_records,
    _disk_number_token,
    _looks_like_drive_letter,
    _normalise_drive_letter,
    _render_disk_number_list,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by the
        PowerShell script string (the last argv element).
    """

    script_results: dict = field(default_factory=dict)
    script_prefix_results: list = field(default_factory=list)
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

        if len(args) == 0:
            rtnval = self._fallback()
            return rtnval

        script = args[-1]
        if script in self.script_results:
            rtnval = self.script_results[script]
            return rtnval

        for prefix, response in self.script_prefix_results:
            if script.startswith(prefix):
                rtnval = response
                return rtnval

        rtnval = self._fallback()
        return rtnval

    def _fallback(self) -> RunResult:
        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval
        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _seed(
    *,
    runner: _MockCommandRunner,
    script_substring: str,
    response: RunResult,
) -> None:
    """
        Seed a script-prefix match.  The runner returns ``response`` for
        any PowerShell script whose text starts with
        ``script_substring``.
    """
    runner.script_prefix_results.append((script_substring, response))
    return


# ----------------------------------------------------------------------
# list_disks
# ----------------------------------------------------------------------


def test_list_disks_invokes_get_disk_and_parses():
    """
        list_disks invokes ``Get-Disk | Select-Object ... |
        ConvertTo-Json`` and parses one WindowsDiskInfo per row.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Disk ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "Number": 0,
                    "FriendlyName": "Samsung SSD",
                    "Size": 256000000000,
                    "HealthStatus": "Healthy",
                    "OperationalStatus": "Online",
                    "PartitionStyle": "GPT",
                    "SerialNumber": "ABC123",
                    "Model": "SAMSUNG MZVL2256",
                },
                {
                    "Number": 1,
                    "FriendlyName": "WD HDD",
                    "Size": 1000000000000,
                    "HealthStatus": "Healthy",
                    "OperationalStatus": "Online",
                    "PartitionStyle": "MBR",
                    "SerialNumber": "XYZ789",
                    "Model": "WD10EZEX",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-PhysicalDisk ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {"Number": 0, "CanPool": False},
                {"Number": 1, "CanPool": True},
            ]),
            stderr="",
        ),
    )

    manager = WindowsStorageManager(
        runner=runner, config=WindowsStorageManagerConfig(),
    )
    disks = manager.list_disks()

    by_id = {d.id: d for d in disks}
    testbase.assert_true("0" in by_id)
    testbase.assert_true("1" in by_id)
    testbase.assert_equal(by_id["0"].size_bytes, 256000000000)
    testbase.assert_equal(by_id["0"].partition_style, "GPT")
    testbase.assert_equal(by_id["0"].is_pool_member, True)
    testbase.assert_equal(by_id["1"].is_pool_member, False)
    return


# ----------------------------------------------------------------------
# list_volumes
# ----------------------------------------------------------------------


def test_list_volumes_parses_output():
    """
        list_volumes invokes ``Get-Volume | ... | ConvertTo-Json`` and
        returns one WindowsVolumeInfo per row.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "DriveLetter": "C",
                    "FileSystemLabel": "OS",
                    "FileSystemType": "NTFS",
                    "Size": 256000000000,
                    "SizeRemaining": 100000000000,
                    "HealthStatus": "Healthy",
                },
                {
                    "DriveLetter": "D",
                    "FileSystemLabel": "Data",
                    "FileSystemType": "NTFS",
                    "Size": 500000000000,
                    "SizeRemaining": 250000000000,
                    "HealthStatus": "Healthy",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "FriendlyName": "Data",
                    "Size": 500000000000,
                    "ResiliencySettingName": "Mirror",
                    "HealthStatus": "Healthy",
                    "StoragePool": {"FriendlyName": "primary-pool"},
                },
            ]),
            stderr="",
        ),
    )

    manager = WindowsStorageManager(
        runner=runner, config=WindowsStorageManagerConfig(),
    )
    volumes = manager.list_volumes()

    by_id = {v.id: v for v in volumes}
    testbase.assert_true("C" in by_id)
    testbase.assert_true("D" in by_id)
    testbase.assert_equal(by_id["C"].size_bytes, 256000000000)
    testbase.assert_equal(by_id["C"].used_bytes, 156000000000)
    testbase.assert_equal(by_id["C"].mount_point, "C:")
    testbase.assert_equal(by_id["D"].pool, "primary-pool")
    return


# ----------------------------------------------------------------------
# create_volume (pool flow)
# ----------------------------------------------------------------------


def test_create_volume_invokes_new_volume():
    """
        create_volume with pool dispatches ``New-Volume
        -StoragePoolFriendlyName <pool> -FriendlyName <name> -Size
        <bytes> -ResiliencySettingName <r> -FileSystem <fs>`` with
        elevation when configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    spec = WindowsVolumeSpec(
        name="Data",
        pool="primary-pool",
        size_bytes=500_000_000_000,
        filesystem="NTFS",
        resiliency="Mirror",
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("New-Volume ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected_script = (
        "New-Volume "
        "-StoragePoolFriendlyName 'primary-pool' "
        "-FriendlyName 'Data' "
        "-Size 500000000000 "
        "-ResiliencySettingName 'Mirror' "
        "-FileSystem 'NTFS'"
    )
    testbase.assert_equal(write_calls[0]["args"][-1], expected_script)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_create_volume_skipped_when_already_present():
    """
        create_volume returns SKIPPED when ``Get-VirtualDisk`` already
        reports a virtual disk with the same friendly name and size.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "FriendlyName": "Data",
                    "Size": 500_000_000_000,
                    "ResiliencySettingName": "Mirror",
                    "HealthStatus": "Healthy",
                },
            ]),
            stderr="",
        ),
    )
    spec = WindowsVolumeSpec(
        name="Data", pool="primary-pool", size_bytes=500_000_000_000,
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("New-Volume ")
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_create_volume_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    spec = WindowsVolumeSpec(
        name="Data", pool="primary-pool", size_bytes=500_000_000_000,
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(dry_run=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, WindowsStorageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_volume (partition flow)
# ----------------------------------------------------------------------


def test_delete_volume_invokes_remove_partition():
    """
        delete_volume with a drive-letter id dispatches
        ``Get-Partition -DriveLetter <l> | Remove-Partition
        -Confirm:$false`` and surfaces a data-loss advisory on success.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "DriveLetter": "X",
                    "FileSystemLabel": "Data",
                    "FileSystemType": "NTFS",
                    "Size": 100_000_000_000,
                    "SizeRemaining": 50_000_000_000,
                    "HealthStatus": "Healthy",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.delete_volume(volume_id="X")

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Get-Partition -DriveLetter ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("destroyed" in result.message)
    return


def test_delete_volume_skipped_when_absent():
    """
        delete_volume returns SKIPPED when the drive letter is not a
        live volume.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.delete_volume(volume_id="X")

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Get-Partition ")
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# resize_volume (partition flow)
# ----------------------------------------------------------------------


def test_resize_volume_invokes_resize_partition():
    """
        resize_volume dispatches ``Resize-Partition -DriveLetter <l>
        -Size <bytes>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "DriveLetter": "X",
                    "FileSystemLabel": "Data",
                    "FileSystemType": "NTFS",
                    "Size": 100_000_000_000,
                    "SizeRemaining": 50_000_000_000,
                    "HealthStatus": "Healthy",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.resize_volume(
        volume_id="X", size_bytes=200_000_000_000,
    )

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Resize-Partition ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"][-1],
        "Resize-Partition -DriveLetter 'X' -Size 200000000000",
    )
    return


def test_resize_volume_skipped_when_size_matches():
    """
        resize_volume returns SKIPPED when the volume's current size
        is within tolerance of the requested size.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "DriveLetter": "X",
                    "FileSystemLabel": "Data",
                    "FileSystemType": "NTFS",
                    "Size": 100_000_000_000,
                    "SizeRemaining": 50_000_000_000,
                    "HealthStatus": "Healthy",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.resize_volume(
        volume_id="X", size_bytes=100_000_000_000,
    )

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Resize-Partition ")
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# create_pool / delete_pool / add_to_pool / remove_from_pool
# ----------------------------------------------------------------------


def test_create_pool_invokes_new_storagepool():
    """
        create_pool dispatches ``New-StoragePool -FriendlyName <name>
        -StorageSubSystemFriendlyName 'Windows Storage*' -PhysicalDisks
        (Get-PhysicalDisk -CanPool $True | Where-Object Number -in
        @(<numbers>))``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-StoragePool ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    spec = WindowsPoolSpec(
        name="primary-pool",
        disks=["1", "2"],
        resiliency="Mirror",
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.create_pool(spec=spec)

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("New-StoragePool ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected_script = (
        "New-StoragePool "
        "-FriendlyName 'primary-pool' "
        "-StorageSubSystemFriendlyName 'Windows Storage*' "
        "-PhysicalDisks (Get-PhysicalDisk -CanPool $True | "
        "Where-Object Number -in @(1,2))"
    )
    testbase.assert_equal(write_calls[0]["args"][-1], expected_script)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_delete_pool_invokes_remove_storagepool():
    """
        delete_pool dispatches ``Remove-StoragePool -FriendlyName
        <name> -Confirm:$false`` and carries a data-loss advisory on
        success.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-StoragePool ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([{"FriendlyName": "primary-pool"}]),
            stderr="",
        ),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.delete_pool(pool_name="primary-pool")

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Remove-StoragePool ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"][-1],
        "Remove-StoragePool -FriendlyName 'primary-pool' -Confirm:$false",
    )
    testbase.assert_true(result.message is not None)
    testbase.assert_true("destroyed" in result.message)
    return


def test_add_to_pool_invokes_add_physicaldisk():
    """
        add_to_pool dispatches ``Add-PhysicalDisk
        -StoragePoolFriendlyName <pool> -PhysicalDisks (...)``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-PhysicalDisk -StoragePoolFriendlyName ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.add_to_pool(
        pool_name="primary-pool", disk_id="3",
    )

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Add-PhysicalDisk ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected_script = (
        "Add-PhysicalDisk "
        "-StoragePoolFriendlyName 'primary-pool' "
        "-PhysicalDisks (Get-PhysicalDisk | Where-Object Number -eq 3)"
    )
    testbase.assert_equal(write_calls[0]["args"][-1], expected_script)
    return


def test_remove_from_pool_invokes_remove_physicaldisk():
    """
        remove_from_pool dispatches ``Remove-PhysicalDisk
        -StoragePoolFriendlyName <pool> -PhysicalDisks (...)
        -Confirm:$false`` and carries a data-loss advisory on success.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-PhysicalDisk -StoragePoolFriendlyName ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([{"Number": 3}]),
            stderr="",
        ),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.remove_from_pool(
        pool_name="primary-pool", disk_id="3",
    )

    testbase.assert_equal(result.status, WindowsStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Remove-PhysicalDisk ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected_script = (
        "Remove-PhysicalDisk "
        "-StoragePoolFriendlyName 'primary-pool' "
        "-PhysicalDisks (Get-PhysicalDisk | Where-Object Number -eq 3) "
        "-Confirm:$false"
    )
    testbase.assert_equal(write_calls[0]["args"][-1], expected_script)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("wiped" in result.message)
    return


# ----------------------------------------------------------------------
# get_volume_info / get_disk_info
# ----------------------------------------------------------------------


def test_get_volume_info_parses_output():
    """
        get_volume_info returns the volume matching the drive letter.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "DriveLetter": "C",
                    "FileSystemLabel": "OS",
                    "FileSystemType": "NTFS",
                    "Size": 256000000000,
                    "SizeRemaining": 100000000000,
                    "HealthStatus": "Healthy",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner, config=WindowsStorageManagerConfig(),
    )
    info = manager.get_volume_info(volume_id="C:")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.size_bytes, 256000000000)
    testbase.assert_equal(info.used_bytes, 156000000000)
    testbase.assert_equal(info.filesystem, "NTFS")
    return


def test_get_disk_info_parses_output():
    """
        get_disk_info returns the disk matching the id.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Disk ",
        response=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {
                    "Number": 0,
                    "FriendlyName": "Samsung SSD",
                    "Size": 256000000000,
                    "HealthStatus": "Healthy",
                    "PartitionStyle": "GPT",
                    "Model": "SAMSUNG MZVL2256",
                    "SerialNumber": "ABC123",
                },
            ]),
            stderr="",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-PhysicalDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner, config=WindowsStorageManagerConfig(),
    )
    info = manager.get_disk_info(disk_id="0")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.size_bytes, 256000000000)
    testbase.assert_equal(info.partition_style, "GPT")
    testbase.assert_equal(info.serial, "ABC123")
    return


# ----------------------------------------------------------------------
# Elevation contract
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    spec = WindowsVolumeSpec(
        name="Data",
        pool="primary-pool",
        size_bytes=100_000_000_000,
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    manager.create_volume(spec=spec)
    write_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("New-Volume ")
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True`` even when the config
        requests elevation for writes.
    """
    runner = _MockCommandRunner()
    _seed(
        runner=runner,
        script_substring="Get-Disk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-PhysicalDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-Volume ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    manager.list_disks()
    manager.list_volumes()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED writes carry the runner's exit code and stderr.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1, stdout="", stderr="New-Volume failed",
        ),
    )
    _seed(
        runner=runner,
        script_substring="Get-VirtualDisk ",
        response=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    spec = WindowsVolumeSpec(
        name="Data",
        pool="primary-pool",
        size_bytes=100_000_000_000,
    )
    manager = WindowsStorageManager(
        runner=runner,
        config=WindowsStorageManagerConfig(elevated=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, WindowsStorageActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "New-Volume failed")
    return


# ----------------------------------------------------------------------
# Module-level helper tests
# ----------------------------------------------------------------------


def test_disk_number_token_renders_int():
    """
        ``_disk_number_token`` strips whitespace and re-renders an int.
    """
    testbase.assert_equal(_disk_number_token(disk_id=" 3 "), "3")
    return


def test_render_disk_number_list_joins_with_commas():
    """
        ``_render_disk_number_list`` produces ``"1,2,3"``.
    """
    testbase.assert_equal(
        _render_disk_number_list(disks=["1", "2", "3"]), "1,2,3",
    )
    return


def test_normalise_drive_letter_strips_colon():
    """
        ``_normalise_drive_letter`` strips a trailing colon.
    """
    testbase.assert_equal(_normalise_drive_letter(mountpoint="X:"), "X")
    testbase.assert_equal(
        _normalise_drive_letter(mountpoint="X:\\"), "X",
    )
    testbase.assert_equal(_normalise_drive_letter(mountpoint="X"), "X")
    return


def test_looks_like_drive_letter_detects_single_letter():
    """
        ``_looks_like_drive_letter`` recognises bare letters.
    """
    testbase.assert_equal(_looks_like_drive_letter(value="X"), True)
    testbase.assert_equal(_looks_like_drive_letter(value="X:"), True)
    testbase.assert_equal(_looks_like_drive_letter(value="Data"), False)
    return

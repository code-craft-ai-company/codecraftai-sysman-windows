"""
    Tests for :class:`WindowsHardwareInventoryManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv.  No
    live PowerShell is invoked.

    The PowerShell argv shape is fixed at::

        [<ps>, "-NoProfile", "-NonInteractive", "-Command", <script>]

    so every keyed result entry includes the full argv tuple.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.hardwareinventory import (
    WindowsHardwareInventoryManager,
    WindowsHardwareInventoryManagerConfig,
    WindowsHardwareInventoryQueryError,
)


_PS = "powershell.exe"
_NO_PROFILE = "-NoProfile"
_NON_INTERACTIVE = "-NonInteractive"
_COMMAND = "-Command"

_J = "| ConvertTo-Json -Compress -Depth 5"


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

        rtnval = RunResult(
            exit_code=1,
            stdout="",
            stderr="not implemented",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build a canonical success :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr="")
    return rtnval


def _fail(*, exit_code: int = 1, stderr: str = "boom") -> RunResult:
    """
        Build a canonical failure :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=exit_code, stdout="", stderr=stderr)
    return rtnval


def _argv(script: str) -> tuple:
    """
        Build a canonical PowerShell argv tuple suitable for use as a
        ``results`` dict key.
    """
    rtnval = (_PS, _NO_PROFILE, _NON_INTERACTIVE, _COMMAND, script)
    return rtnval


# ----------------------------------------------------------------------
# get_system_info
# ----------------------------------------------------------------------


def test_get_system_info_invokes_get_computer_info_and_parses():
    """
        get_system_info() shells out to ``Get-ComputerInfo |
        ConvertTo-Json`` and parses the resulting object.
    """
    payload = json.dumps({
        "CsManufacturer": "Dell Inc.",
        "CsModel": "Latitude 7420",
        "CsName": "DESKTOP-ABC",
        "BiosManufacturer": "Dell Inc.",
        "BiosSMBIOSBIOSVersion": "1.7.2",
        "BiosFirmwareType": "Uefi",
        "CsSystemSKUNumber": "0A1B",
        "CsSystemUUID": "AAAA-BBBB-CCCC",
        "BiosSeralNumber": "SN-AB-12",
        "OsVersion": "10.0.19045",
    })
    runner = _MockCommandRunner()
    runner.results[_argv("Get-ComputerInfo {}".format(_J))] = _ok(stdout=payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    info = manager.get_system_info()

    testbase.assert_equal(info.manufacturer, "Dell Inc.")
    testbase.assert_equal(info.product_name, "Latitude 7420")
    testbase.assert_equal(info.serial_number, "SN-AB-12")
    testbase.assert_equal(info.uuid, "AAAA-BBBB-CCCC")
    testbase.assert_equal(info.bios_version, "1.7.2")
    testbase.assert_equal(info.firmware_version, "Uefi")
    testbase.assert_equal(info.family, "0A1B")
    testbase.assert_equal(info.version, "10.0.19045")
    return


# ----------------------------------------------------------------------
# get_cpu_info
# ----------------------------------------------------------------------


def test_get_cpu_info_parses_get_ciminstance_output():
    """
        get_cpu_info() parses ``Get-CimInstance Win32_Processor |
        ConvertTo-Json`` output, decoding architecture and summing
        socket counts.
    """
    payload = json.dumps({
        "Architecture": 9,
        "Manufacturer": "GenuineIntel",
        "Name": "Intel(R) Core(TM) i7-1185G7 @ 3.00GHz",
        "NumberOfCores": 4,
        "NumberOfLogicalProcessors": 8,
        "MaxClockSpeed": 3000,
        "L2CacheSize": 5120,
        "L3CacheSize": 12288,
    })
    runner = _MockCommandRunner()
    runner.results[_argv(
        "Get-CimInstance Win32_Processor {}".format(_J),
    )] = _ok(stdout=payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    info = manager.get_cpu_info()

    testbase.assert_equal(info.architecture, "x64")
    testbase.assert_equal(info.vendor, "GenuineIntel")
    testbase.assert_equal(info.model, "Intel(R) Core(TM) i7-1185G7 @ 3.00GHz")
    testbase.assert_equal(info.physical_cores, 4)
    testbase.assert_equal(info.logical_cores, 8)
    testbase.assert_equal(info.frequency_mhz, 3000)
    testbase.assert_equal(info.cache_l2, 5120 * 1024)
    testbase.assert_equal(info.cache_l3, 12288 * 1024)
    return


# ----------------------------------------------------------------------
# get_memory_info
# ----------------------------------------------------------------------


def test_get_memory_info_aggregates_modules_and_total():
    """
        get_memory_info() aggregates ``Win32_PhysicalMemory`` modules
        and merges ``Win32_OperatingSystem`` total / free.
    """
    physical_payload = json.dumps([
        {
            "BankLabel": "BANK 0",
            "DeviceLocator": "DIMM A",
            "Capacity": 17179869184,
            "MemoryType": 26,
            "ConfiguredClockSpeed": 3200,
            "Manufacturer": "Micron",
            "SerialNumber": "12345",
        },
        {
            "BankLabel": "BANK 1",
            "DeviceLocator": "DIMM B",
            "Capacity": 17179869184,
            "MemoryType": 26,
            "ConfiguredClockSpeed": 3200,
            "Manufacturer": "Micron",
            "SerialNumber": "67890",
        },
    ])
    os_payload = json.dumps({
        "TotalVisibleMemorySize": 33554432,
        "FreePhysicalMemory": 25165824,
    })
    runner = _MockCommandRunner()
    runner.results[_argv(
        "Get-CimInstance Win32_PhysicalMemory {}".format(_J),
    )] = _ok(stdout=physical_payload)
    runner.results[_argv(
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object TotalVisibleMemorySize,FreePhysicalMemory {}".format(_J),
    )] = _ok(stdout=os_payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    info = manager.get_memory_info()

    testbase.assert_equal(len(info.modules), 2)
    testbase.assert_equal(info.modules[0].slot, "DIMM A")
    testbase.assert_equal(info.modules[0].size_bytes, 17179869184)
    testbase.assert_equal(info.modules[0].type, "DDR4")
    testbase.assert_equal(info.modules[0].speed_mhz, 3200)
    testbase.assert_equal(info.modules[0].manufacturer, "Micron")
    testbase.assert_equal(info.modules[0].serial_number, "12345")
    testbase.assert_equal(info.slots_used, 2)
    testbase.assert_equal(info.total_bytes, 33554432 * 1024)
    testbase.assert_equal(info.available_bytes, 25165824 * 1024)
    return


# ----------------------------------------------------------------------
# list_storage_devices
# ----------------------------------------------------------------------


def test_list_storage_devices_parses_get_disk_output():
    """
        list_storage_devices() emits one record per ``Get-Disk`` row
        and merges ``Get-Volume`` for filesystem / mount-point.
    """
    disk_payload = json.dumps([
        {
            "Number": 0,
            "FriendlyName": "NVMe Samsung SSD 970",
            "SerialNumber": "S1AB",
            "Size": 512110190592,
            "BusType": "NVMe",
            "MediaType": "SSD",
        }
    ])
    volume_payload = json.dumps([
        {
            "DriveLetter": "C",
            "FileSystemType": "NTFS",
            "Size": 511800000000,
            "FileSystemLabel": "OS",
        }
    ])
    runner = _MockCommandRunner()
    runner.results[_argv("Get-Disk {}".format(_J))] = _ok(stdout=disk_payload)
    runner.results[_argv("Get-Volume {}".format(_J))] = _ok(
        stdout=volume_payload,
    )

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    devices = manager.list_storage_devices()

    testbase.assert_equal(len(devices), 1)
    testbase.assert_equal(devices[0].name, "NVMe Samsung SSD 970")
    testbase.assert_equal(devices[0].size_bytes, 512110190592)
    testbase.assert_equal(devices[0].type, "SSD")
    testbase.assert_equal(devices[0].model, "NVMe Samsung SSD 970")
    testbase.assert_equal(devices[0].serial, "S1AB")
    testbase.assert_equal(devices[0].mountpoint, "C:\\")
    testbase.assert_equal(devices[0].filesystem, "NTFS")
    return


# ----------------------------------------------------------------------
# list_network_devices
# ----------------------------------------------------------------------


def test_list_network_devices_parses_get_netadapter_output():
    """
        list_network_devices() parses ``Get-NetAdapter | ConvertTo-Json``
        output.
    """
    payload = json.dumps([
        {
            "Name": "Ethernet",
            "InterfaceDescription": "Intel(R) Ethernet I219-V",
            "MacAddress": "1C-1B-B5-11-22-33",
            "Status": "Up",
            "MtuSize": 1500,
            "LinkSpeed": "1 Gbps",
            "Virtual": False,
        },
        {
            "Name": "vEthernet (Default Switch)",
            "InterfaceDescription": "Hyper-V Virtual",
            "MacAddress": "00-15-5D-AA-BB-CC",
            "Status": "Up",
            "MtuSize": 1500,
            "LinkSpeed": "10 Gbps",
            "Virtual": True,
        },
    ])
    runner = _MockCommandRunner()
    runner.results[_argv(
        "Get-NetAdapter {}".format(_J),
    )] = _ok(stdout=payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    devices = manager.list_network_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].name, "Ethernet")
    testbase.assert_equal(devices[0].mac_address, "1C-1B-B5-11-22-33")
    testbase.assert_equal(devices[0].link_state, "Up")
    testbase.assert_equal(devices[0].mtu, 1500)
    testbase.assert_equal(devices[0].speed, "1 Gbps")
    testbase.assert_equal(devices[0].is_virtual, False)
    testbase.assert_equal(devices[1].is_virtual, True)
    return


# ----------------------------------------------------------------------
# list_pci_devices
# ----------------------------------------------------------------------


def test_list_pci_devices_parses_get_ciminstance_output():
    """
        list_pci_devices() parses ``Get-CimInstance Win32_PnPEntity``
        records filtered to ``PNPClass='PCI'``.
    """
    payload = json.dumps([
        {
            "Name": "Intel(R) USB 3.1 eXtensible Host Controller",
            "Manufacturer": "Intel(R) Corporation",
            "PNPClass": "PCI",
            "DeviceID": "PCI\\VEN_8086&DEV_A36D&SUBSYS_07D71028&REV_10\\3&11583659&0&A0",
            "Service": "USBXHCI",
        }
    ])
    runner = _MockCommandRunner()
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPClass -eq 'PCI' } "
        + _J
    )
    runner.results[_argv(script)] = _ok(stdout=payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    devices = manager.list_pci_devices()

    testbase.assert_equal(len(devices), 1)
    testbase.assert_equal(
        devices[0].device,
        "Intel(R) USB 3.1 eXtensible Host Controller",
    )
    testbase.assert_equal(devices[0].vendor, "Intel(R) Corporation")
    testbase.assert_equal(devices[0].vendor_id, "8086")
    testbase.assert_equal(devices[0].device_id, "A36D")
    testbase.assert_equal(devices[0].class_name, "PCI")
    testbase.assert_equal(devices[0].driver, "USBXHCI")
    return


# ----------------------------------------------------------------------
# list_usb_devices
# ----------------------------------------------------------------------


def test_list_usb_devices_parses_get_ciminstance_output():
    """
        list_usb_devices() parses ``Get-CimInstance Win32_USBHub |
        ConvertTo-Json``.
    """
    payload = json.dumps([
        {
            "Name": "USB Root Hub (USB 3.0)",
            "DeviceID": "USB\\VID_8086&PID_A36D\\5&3196A1A0&0&0",
            "PNPDeviceID": "USB\\VID_8086&PID_A36D\\5&3196A1A0&0&0",
        }
    ])
    runner = _MockCommandRunner()
    runner.results[_argv(
        "Get-CimInstance Win32_USBHub {}".format(_J),
    )] = _ok(stdout=payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    devices = manager.list_usb_devices()

    testbase.assert_equal(len(devices), 1)
    testbase.assert_equal(devices[0].bus, "USB")
    testbase.assert_equal(devices[0].name, "USB Root Hub (USB 3.0)")
    testbase.assert_equal(devices[0].vendor_id, "8086")
    testbase.assert_equal(devices[0].product_id, "A36D")
    return


# ----------------------------------------------------------------------
# get_firmware_info
# ----------------------------------------------------------------------


def test_get_firmware_info_parses_get_ciminstance_output():
    """
        get_firmware_info() parses ``Get-CimInstance Win32_BIOS |
        ConvertTo-Json``.
    """
    payload = json.dumps({
        "Manufacturer": "Dell Inc.",
        "SMBIOSBIOSVersion": "1.7.2",
        "Version": "DELL   - 1072500",
        "ReleaseDate": "/Date(1685577600000)/",
        "BiosCharacteristics": [7, 11, 12, 32, 43],
    })
    runner = _MockCommandRunner()
    runner.results[_argv(
        "Get-CimInstance Win32_BIOS {}".format(_J),
    )] = _ok(stdout=payload)

    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )
    info = manager.get_firmware_info()

    testbase.assert_equal(info.vendor, "Dell Inc.")
    testbase.assert_equal(info.version, "1.7.2")
    testbase.assert_equal(info.release_date, "/Date(1685577600000)/")
    testbase.assert_equal(info.uefi, True)
    return


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------


def test_failed_command_raises_query_error():
    """
        A non-zero runner result raises
        :class:`WindowsHardwareInventoryQueryError` carrying the argv,
        exit code, and stderr.
    """
    runner = _MockCommandRunner()
    runner.results[_argv("Get-ComputerInfo {}".format(_J))] = _fail(
        exit_code=1, stderr="The term 'Get-ComputerInfo' is not recognized",
    )
    manager = WindowsHardwareInventoryManager(
        runner, WindowsHardwareInventoryManagerConfig(),
    )

    raised = False
    try:
        manager.get_system_info()
    except WindowsHardwareInventoryQueryError as ex:
        raised = True
        testbase.assert_equal(ex.exit_code, 1)
        testbase.assert_true("Get-ComputerInfo" in ex.command[-1])
        testbase.assert_true("not recognized" in ex.stderr)

    testbase.assert_true(raised)
    return


def test_reads_never_elevate():
    """
        Every read verb dispatches with ``elevated=False`` regardless of
        ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout="{}")
    runner.results[_argv("Get-ComputerInfo {}".format(_J))] = _ok(stdout="{}")
    runner.results[_argv(
        "Get-CimInstance Win32_Processor {}".format(_J),
    )] = _ok(stdout="{}")
    runner.results[_argv(
        "Get-CimInstance Win32_PhysicalMemory {}".format(_J),
    )] = _ok(stdout="[]")
    runner.results[_argv(
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object TotalVisibleMemorySize,FreePhysicalMemory {}".format(_J),
    )] = _ok(stdout="{}")
    runner.results[_argv("Get-Disk {}".format(_J))] = _ok(stdout="[]")
    runner.results[_argv("Get-Volume {}".format(_J))] = _ok(stdout="[]")
    runner.results[_argv("Get-NetAdapter {}".format(_J))] = _ok(stdout="[]")
    runner.results[_argv(
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPClass -eq 'PCI' } "
        + _J,
    )] = _ok(stdout="[]")
    runner.results[_argv(
        "Get-CimInstance Win32_USBHub {}".format(_J),
    )] = _ok(stdout="[]")
    runner.results[_argv("Get-CimInstance Win32_BIOS {}".format(_J))] = _ok(
        stdout="{}",
    )

    config = WindowsHardwareInventoryManagerConfig(elevated=True)
    manager = WindowsHardwareInventoryManager(runner, config)

    manager.get_system_info()
    manager.get_cpu_info()
    manager.get_memory_info()
    manager.list_storage_devices()
    manager.list_network_devices()
    manager.list_pci_devices()
    manager.list_usb_devices()
    manager.get_firmware_info()

    elevated_calls: list = []
    for record in runner.calls:
        if record["elevated"] is True:
            elevated_calls.append(record)

    testbase.assert_equal(len(elevated_calls), 0)
    return

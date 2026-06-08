"""
    Windows hardware-inventory feature.

    Implements the cross-OS hardware-inventory contract for Windows using
    the PowerShell ``Get-ComputerInfo``, ``Get-CimInstance``, ``Get-Disk``,
    ``Get-Volume``, and ``Get-NetAdapter`` cmdlets.  Every verb is
    dispatched through an :class:`ICommandRunner` so the manager works
    identically under local and SSH execution.

    This feature is strictly read-only.  No verb mutates host state, none
    of them honour an idempotency guard, and there is intentionally no
    ``DRY_RUN`` status.  The
    :class:`WindowsHardwareInventoryManagerConfig` inherits ``dry_run``
    from :class:`FeatureConfig` for structural uniformity with the rest
    of the package, but the field is never consulted by any verb.

    Elevation policy:
        Reads never elevate.  Each verb passes ``elevated=False`` to the
        runner unconditionally.  Windows has no ``sudo``; an
        Administrator token would only matter for CIM classes that
        require it (e.g., ``Win32_BIOS`` SMBIOSBIOSVersion, certain
        WMI security descriptors), and the pilot intentionally accepts
        the unelevated subset.

    Notes on tool selection:
        - ``ConvertTo-Json -Compress -Depth 5`` is the canonical
          serializer.  ``-Depth 5`` is sufficient for the cmdlets used
          here (their nested objects do not exceed five levels), and
          ``-Compress`` keeps stderr / log volume manageable.
        - ``Get-CimInstance`` has replaced the deprecated ``Get-WmiObject``
          across PowerShell 5.1 and PowerShell 7+, so every WMI-class
          read goes through ``Get-CimInstance``.
        - ``ConvertTo-Json`` returns a JSON object when the cmdlet emits
          one record, and a JSON array when it emits many.  The parsers
          centralise the "one or many" coercion through
          :func:`_coerce_json_records`.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_CONVERT_TO_JSON = "| ConvertTo-Json -Compress -Depth 5"


# ----------------------------------------------------------------------
# Status enum, exception, and config
# ----------------------------------------------------------------------


class WindowsHardwareInventoryActionStatus(Enum):
    """
        Outcome status for a hardware-inventory operation.

        This feature is read-only; the cross-OS contract drops
        ``DRY_RUN`` from the status set, so this enum carries only
        ``SUCCESS``, ``FAILED``, and ``SKIPPED``.
    """
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"


@dataclass
class WindowsHardwareInventoryManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsHardwareInventoryManager`.

        Inherits ``elevated`` and ``dry_run`` from :class:`FeatureConfig`
        for structural uniformity.  ``dry_run`` is intentionally
        ignored — the contract makes this feature read-only.
        ``elevated`` is reserved for the selective-elevation patch
        documented in ``specs/3-hardware-inventory/tasks.md``.

        :param powershell_executable: Path to the PowerShell executable
                                      used to host every cmdlet.
                                      Defaults to ``"powershell.exe"``
                                      (PATH-resolved Windows
                                      PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL


class WindowsHardwareInventoryQueryError(Exception):
    """
        Raised when an inventory read fails because PowerShell returned
        a non-zero exit code or the captured JSON could not be parsed.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional override for the exception message.
                            When not None it replaces the default
                            command/exit/stderr block.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Hardware inventory query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Result dataclasses
# ----------------------------------------------------------------------


@dataclass
class WindowsSystemInfo:
    """
        System-identity record returned by
        :meth:`WindowsHardwareInventoryManager.get_system_info`.
    """
    manufacturer: Optional[str] = None
    product_name: Optional[str] = None
    serial_number: Optional[str] = None
    uuid: Optional[str] = None
    bios_version: Optional[str] = None
    firmware_version: Optional[str] = None
    family: Optional[str] = None
    version: Optional[str] = None


@dataclass
class WindowsCpuInfo:
    """
        CPU topology and feature record returned by
        :meth:`WindowsHardwareInventoryManager.get_cpu_info`.
    """
    model: Optional[str] = None
    vendor: Optional[str] = None
    architecture: Optional[str] = None
    physical_cores: int = 0
    logical_cores: int = 0
    frequency_mhz: Optional[int] = None
    cache_l1: Optional[int] = None
    cache_l2: Optional[int] = None
    cache_l3: Optional[int] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class WindowsMemoryModule:
    """
        Per-DIMM record nested under :class:`WindowsMemoryInfo`.
    """
    slot: Optional[str] = None
    size_bytes: int = 0
    type: Optional[str] = None
    speed_mhz: Optional[int] = None
    manufacturer: Optional[str] = None
    serial_number: Optional[str] = None


@dataclass
class WindowsMemoryInfo:
    """
        Aggregate-memory record returned by
        :meth:`WindowsHardwareInventoryManager.get_memory_info`.
    """
    total_bytes: int = 0
    available_bytes: Optional[int] = None
    slots_used: Optional[int] = None
    slots_total: Optional[int] = None
    modules: list[WindowsMemoryModule] = field(default_factory=list)


@dataclass
class WindowsStorageDeviceInfo:
    """
        One storage device record returned by
        :meth:`WindowsHardwareInventoryManager.list_storage_devices`.
    """
    name: str
    size_bytes: int = 0
    type: str = ""
    model: Optional[str] = None
    serial: Optional[str] = None
    mountpoint: Optional[str] = None
    filesystem: Optional[str] = None


@dataclass
class WindowsNetworkDeviceInfo:
    """
        One network-link record returned by
        :meth:`WindowsHardwareInventoryManager.list_network_devices`.
    """
    name: str
    mac_address: Optional[str] = None
    link_state: Optional[str] = None
    speed: Optional[str] = None
    mtu: Optional[int] = None
    is_virtual: bool = False


@dataclass
class WindowsPciDeviceInfo:
    """
        One PCI device record returned by
        :meth:`WindowsHardwareInventoryManager.list_pci_devices`.
    """
    slot: str
    class_name: Optional[str] = None
    vendor: Optional[str] = None
    device: Optional[str] = None
    vendor_id: Optional[str] = None
    device_id: Optional[str] = None
    driver: Optional[str] = None


@dataclass
class WindowsUsbDeviceInfo:
    """
        One USB device record returned by
        :meth:`WindowsHardwareInventoryManager.list_usb_devices`.
    """
    bus: str
    device_id: Optional[str] = None
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    name: Optional[str] = None
    serial: Optional[str] = None


@dataclass
class WindowsFirmwareInfo:
    """
        Firmware identity returned by
        :meth:`WindowsHardwareInventoryManager.get_firmware_info`.
    """
    vendor: Optional[str] = None
    version: Optional[str] = None
    release_date: Optional[str] = None
    secure_boot: Optional[bool] = None
    uefi: Optional[bool] = None


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsHardwareInventoryManager(BaseFeatureManager):
    """
        Read-only hardware-inventory manager for Windows.

        Every verb shells out to a PowerShell scriptblock that emits
        ``ConvertTo-Json -Compress -Depth 5`` output, dispatches through
        the injected :class:`ICommandRunner`, and JSON-decodes in pure
        Python.  Every runner invocation passes ``elevated=False``
        regardless of the config so that a misconfigured caller cannot
        accidentally elevate a read.
    """

    status_class = WindowsHardwareInventoryActionStatus
    feature_name = "hardware-inventory"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_system_info(self) -> WindowsSystemInfo:
        """
            Query system identity via
            ``Get-ComputerInfo | ConvertTo-Json -Compress -Depth 5``.

            :returns: The :class:`WindowsSystemInfo`.
            :raises WindowsHardwareInventoryQueryError: When the command
                                                        fails or the
                                                        JSON payload is
                                                        malformed.
        """
        script = "Get-ComputerInfo {}".format(_CONVERT_TO_JSON)
        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        info = _parse_get_computer_info(
            stdout=run_result.stdout, command=command,
        )
        return info

    def get_cpu_info(self) -> WindowsCpuInfo:
        """
            Query CPU topology via
            ``Get-CimInstance Win32_Processor | ConvertTo-Json
            -Compress -Depth 5``.

            :returns: The :class:`WindowsCpuInfo`.
            :raises WindowsHardwareInventoryQueryError: When the command
                                                        fails or the
                                                        JSON payload is
                                                        malformed.
        """
        script = (
            "Get-CimInstance Win32_Processor {}".format(_CONVERT_TO_JSON)
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        info = _parse_win32_processor(
            stdout=run_result.stdout, command=command,
        )
        return info

    def get_memory_info(self) -> WindowsMemoryInfo:
        """
            Query memory via
            ``Get-CimInstance Win32_PhysicalMemory | ConvertTo-Json
            -Compress -Depth 5``
            and the operating-system aggregates via
            ``Get-CimInstance Win32_OperatingSystem | Select
            TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json
            -Compress -Depth 5``.

            :returns: The :class:`WindowsMemoryInfo`.
            :raises WindowsHardwareInventoryQueryError: When either
                                                        command fails or
                                                        emits malformed
                                                        JSON.
        """
        physical_script = (
            "Get-CimInstance Win32_PhysicalMemory {}".format(_CONVERT_TO_JSON)
        )
        physical_command = self._build_powershell_argv(
            script=physical_script,
        )
        physical_result = self._read(command=physical_command)

        os_script = (
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object TotalVisibleMemorySize,FreePhysicalMemory "
            "{}".format(_CONVERT_TO_JSON)
        )
        os_command = self._build_powershell_argv(script=os_script)
        os_result = self._read(command=os_command)

        info = _parse_win32_physical_memory(
            stdout=physical_result.stdout, command=physical_command,
        )
        _merge_win32_operating_system(
            info=info, stdout=os_result.stdout, command=os_command,
        )

        return info

    def list_storage_devices(self) -> list[WindowsStorageDeviceInfo]:
        """
            Enumerate storage devices via ``Get-Disk | ConvertTo-Json``
            and ``Get-Volume | ConvertTo-Json``.

            ``Get-Disk`` reports one record per physical disk; the
            parser augments those records with mount-point /
            file-system data from ``Get-Volume`` whose ``DriveLetter``
            matches.

            :returns: One record per physical disk plus one synthesised
                      record per non-disk-backed volume (e.g., a
                      mounted image).
            :raises WindowsHardwareInventoryQueryError: When either
                                                        command fails
                                                        or emits
                                                        malformed JSON.
        """
        disk_script = "Get-Disk {}".format(_CONVERT_TO_JSON)
        disk_command = self._build_powershell_argv(script=disk_script)
        disk_result = self._read(command=disk_command)

        volume_script = "Get-Volume {}".format(_CONVERT_TO_JSON)
        volume_command = self._build_powershell_argv(script=volume_script)
        volume_result = self._read(command=volume_command)

        devices = _parse_get_disk_and_volume(
            disk_stdout=disk_result.stdout,
            disk_command=disk_command,
            volume_stdout=volume_result.stdout,
            volume_command=volume_command,
        )
        return devices

    def list_network_devices(self) -> list[WindowsNetworkDeviceInfo]:
        """
            Enumerate network devices via ``Get-NetAdapter |
            ConvertTo-Json -Compress -Depth 5``.

            :returns: One record per adapter.
            :raises WindowsHardwareInventoryQueryError: When the command
                                                        fails or the
                                                        JSON payload is
                                                        malformed.
        """
        script = "Get-NetAdapter {}".format(_CONVERT_TO_JSON)
        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        devices = _parse_get_net_adapter(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def list_pci_devices(self) -> list[WindowsPciDeviceInfo]:
        """
            Enumerate PCI devices via
            ``Get-CimInstance Win32_PnPEntity | Where-Object
            { $_.PNPClass -eq 'PCI' } | ConvertTo-Json -Compress
            -Depth 5``.

            :returns: One record per PCI-class PnP entity.
            :raises WindowsHardwareInventoryQueryError: When the command
                                                        fails or the
                                                        JSON payload is
                                                        malformed.
        """
        script = (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.PNPClass -eq 'PCI' } "
            + _CONVERT_TO_JSON
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        devices = _parse_win32_pnp_entity_pci(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def list_usb_devices(self) -> list[WindowsUsbDeviceInfo]:
        """
            Enumerate USB devices via
            ``Get-CimInstance Win32_USBHub | ConvertTo-Json -Compress
            -Depth 5``.

            ``Win32_USBHub`` reports per-hub records with no
            vendor / product id columns of its own; the parser pulls
            those identifiers out of the ``DeviceID`` /
            ``PNPDeviceID`` strings (which embed
            ``USB\\VID_xxxx&PID_yyyy``).

            :returns: One record per USB hub.
            :raises WindowsHardwareInventoryQueryError: When the command
                                                        fails or the
                                                        JSON payload is
                                                        malformed.
        """
        script = (
            "Get-CimInstance Win32_USBHub {}".format(_CONVERT_TO_JSON)
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        devices = _parse_win32_usbhub(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def get_firmware_info(self) -> WindowsFirmwareInfo:
        """
            Query firmware via
            ``Get-CimInstance Win32_BIOS | ConvertTo-Json -Compress
            -Depth 5``.

            :returns: The :class:`WindowsFirmwareInfo`.
            :raises WindowsHardwareInventoryQueryError: When the command
                                                        fails or the
                                                        JSON payload is
                                                        malformed.
        """
        script = "Get-CimInstance Win32_BIOS {}".format(_CONVERT_TO_JSON)
        command = self._build_powershell_argv(script=script)
        run_result = self._read(command=command)
        info = _parse_win32_bios(
            stdout=run_result.stdout, command=command,
        )
        return info

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self, *, command: list[str]) -> RunResult:
        """
            Invoke the runner with ``elevated=False`` and raise
            :class:`WindowsHardwareInventoryQueryError` on non-zero
            exit.

            Read verbs share this single dispatch helper so that the
            "reads never elevate" property is enforced in exactly one
            place.

            :param command: The argv to dispatch.

            :returns: The raw :class:`RunResult` on success.
            :raises WindowsHardwareInventoryQueryError: When the runner
                                                        reports a
                                                        non-zero exit
                                                        code.
        """
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsHardwareInventoryQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        return run_result

    def _build_powershell_argv(self, *, script: str) -> list[str]:
        """
            Build the PowerShell argv for a given scriptblock.

            The argv shape is fixed at
            ``[<powershell>, "-NoProfile", "-NonInteractive",
            "-Command", <script>]`` so that ``$PROFILE`` cannot mutate
            cmdlet behaviour and PowerShell never blocks waiting for
            interactive input on a runner that has no controlling
            terminal.

            :param script: The PowerShell scriptblock text.

            :returns: The full argv as a list of strings.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _typed_config(self) -> WindowsHardwareInventoryManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsHardwareInventoryManagerConfig`.

            The base class stores it as :class:`FeatureConfig` for
            type-checker friendliness.  This helper centralises the
            ``isinstance`` cast so callers can read
            ``powershell_executable`` without losing static-type
            clarity.
        """
        if isinstance(self._config, WindowsHardwareInventoryManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsHardwareInventoryManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _coerce_json_records(*, stdout: str, command: list[str]) -> list:
    """
        Decode JSON, coercing the result to a list.

        ``ConvertTo-Json`` returns a JSON object when the upstream
        cmdlet emits one record, and a JSON array when it emits many.
        This helper centralises the "one or many" coercion so each
        verb can iterate uniformly.

        :param stdout: Raw stdout text.
        :param command: The argv that produced the output (used in the
                        exception message on parse failure).

        :returns: A list of records (possibly empty).
        :raises WindowsHardwareInventoryQueryError: When ``stdout`` is
                                                    not valid JSON.
    """
    text = stdout.strip()
    if len(text) == 0:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise WindowsHardwareInventoryQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="{} emitted malformed JSON: {}".format(
                " ".join(command), ex,
            ),
        ) from None

    if isinstance(data, list):
        rtnval = data
    else:
        rtnval = [data]

    return rtnval


def _decode_json_object(*, stdout: str, command: list[str]):
    """
        Decode a JSON object payload, raising on malformed input.

        :param stdout: Raw stdout text.
        :param command: The argv that produced the output.

        :returns: The parsed Python object (or ``{}`` when stdout is
                  empty).
        :raises WindowsHardwareInventoryQueryError: On parse failure.
    """
    text = stdout.strip()
    if len(text) == 0:
        return {}

    try:
        rtnval = json.loads(text)
    except json.JSONDecodeError as ex:
        raise WindowsHardwareInventoryQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="{} emitted malformed JSON: {}".format(
                " ".join(command), ex,
            ),
        ) from None

    return rtnval


def _parse_get_computer_info(
    *,
    stdout: str,
    command: list[str],
) -> WindowsSystemInfo:
    """
        Parse ``Get-ComputerInfo | ConvertTo-Json -Compress -Depth 5``.

        Real example (excerpt)::

            {"CsManufacturer": "Dell Inc.",
             "CsModel": "Latitude 7420",
             "CsName": "DESKTOP-ABC",
             "BiosManufacturer": "Dell Inc.",
             "BiosSMBIOSBIOSVersion": "1.7.2",
             "BiosVersion": ["DELL   - 1072500", "1.7.2", "Dell   - 1072500"],
             "CsSystemSKUNumber": "0A1B"}

        Parse rules:

        - ``CsManufacturer`` -> ``manufacturer``;
          ``CsModel`` -> ``product_name``.
        - ``BiosSMBIOSBIOSVersion`` -> ``bios_version``;
          ``BiosFirmwareType`` (``"Uefi"`` or ``"Bios"``) is folded
          into ``firmware_version``.
        - ``CsSystemSKUNumber`` -> ``family``.

        :returns: The populated :class:`WindowsSystemInfo`.
    """
    info = WindowsSystemInfo()
    data = _decode_json_object(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return info

    info.manufacturer = data.get("CsManufacturer")
    info.product_name = data.get("CsModel")
    info.serial_number = data.get("BiosSeralNumber") or data.get(
        "BiosSerialNumber",
    )
    info.uuid = data.get("CsSystemUUID") or data.get("BiosSystemUuid")
    info.bios_version = data.get("BiosSMBIOSBIOSVersion") or data.get(
        "BiosVersion",
    )
    info.firmware_version = data.get("BiosFirmwareType")
    info.family = data.get("CsSystemSKUNumber") or data.get("CsSystemFamily")
    info.version = data.get("OsVersion")

    return info


def _parse_win32_processor(
    *,
    stdout: str,
    command: list[str],
) -> WindowsCpuInfo:
    """
        Parse ``Get-CimInstance Win32_Processor | ConvertTo-Json``.

        Real example (excerpt)::

            {"Architecture": 9,
             "Manufacturer": "GenuineIntel",
             "Name": "Intel(R) Core(TM) i7-1185G7 @ 3.00GHz",
             "NumberOfCores": 4,
             "NumberOfLogicalProcessors": 8,
             "MaxClockSpeed": 3000,
             "L2CacheSize": 5120,
             "L3CacheSize": 12288}

        ``Win32_Processor.Architecture`` is an integer enum:
        ``0=x86``, ``5=ARM``, ``6=Itanium``, ``9=x64``, ``12=ARM64``.

        :returns: The populated :class:`WindowsCpuInfo`.  Multi-socket
                  systems return one consolidated record (logical /
                  physical core counts summed across sockets).
    """
    info = WindowsCpuInfo()
    records = _coerce_json_records(stdout=stdout, command=command)
    if len(records) == 0:
        return info

    physical_total = 0
    logical_total = 0
    for record in records:
        if not isinstance(record, dict):
            continue

        info.vendor = info.vendor or record.get("Manufacturer")
        info.model = info.model or record.get("Name")
        info.architecture = info.architecture or _decode_processor_arch(
            value=record.get("Architecture"),
        )

        cores = record.get("NumberOfCores")
        if isinstance(cores, int):
            physical_total = physical_total + cores
        logical = record.get("NumberOfLogicalProcessors")
        if isinstance(logical, int):
            logical_total = logical_total + logical

        if info.frequency_mhz is None:
            max_clock = record.get("MaxClockSpeed")
            if isinstance(max_clock, int):
                info.frequency_mhz = max_clock

        # Win32_Processor reports cache sizes in KB.
        if info.cache_l2 is None:
            l2 = record.get("L2CacheSize")
            if isinstance(l2, int) and l2 > 0:
                info.cache_l2 = l2 * 1024
        if info.cache_l3 is None:
            l3 = record.get("L3CacheSize")
            if isinstance(l3, int) and l3 > 0:
                info.cache_l3 = l3 * 1024

    info.physical_cores = physical_total
    info.logical_cores = logical_total

    return info


def _decode_processor_arch(*, value) -> Optional[str]:
    """
        Decode the ``Win32_Processor.Architecture`` enum value into a
        canonical architecture string.
    """
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if not isinstance(value, int):
        return None

    mapping = {
        0: "x86",
        1: "MIPS",
        2: "Alpha",
        3: "PowerPC",
        5: "ARM",
        6: "Itanium",
        9: "x64",
        12: "ARM64",
    }
    rtnval = mapping.get(value)
    return rtnval


def _parse_win32_physical_memory(
    *,
    stdout: str,
    command: list[str],
) -> WindowsMemoryInfo:
    """
        Parse ``Get-CimInstance Win32_PhysicalMemory | ConvertTo-Json``.

        Real example (excerpt)::

            [{"BankLabel": "BANK 0",
              "DeviceLocator": "DIMM A",
              "Capacity": 17179869184,
              "MemoryType": 26,
              "ConfiguredClockSpeed": 3200,
              "Manufacturer": "Micron",
              "SerialNumber": "12345"}]

        ``Capacity`` is reported in bytes.  ``MemoryType`` is an enum;
        a small mapping covers the common DDR / DDR4 / DDR5 cases.

        :returns: The populated :class:`WindowsMemoryInfo`.
    """
    info = WindowsMemoryInfo()
    records = _coerce_json_records(stdout=stdout, command=command)

    for record in records:
        if not isinstance(record, dict):
            continue

        capacity = record.get("Capacity", 0)
        try:
            size_bytes = int(capacity)
        except (TypeError, ValueError):
            size_bytes = 0
        if size_bytes <= 0:
            continue

        speed_value = record.get("ConfiguredClockSpeed") or record.get("Speed")
        if isinstance(speed_value, int):
            speed_mhz = speed_value
        else:
            speed_mhz = None

        module = WindowsMemoryModule(
            slot=record.get("DeviceLocator") or record.get("BankLabel"),
            size_bytes=size_bytes,
            type=_decode_memory_type(value=record.get("MemoryType")),
            speed_mhz=speed_mhz,
            manufacturer=record.get("Manufacturer"),
            serial_number=record.get("SerialNumber"),
        )
        info.modules.append(module)

    info.slots_used = len(info.modules)

    return info


def _decode_memory_type(*, value) -> Optional[str]:
    """
        Decode the ``Win32_PhysicalMemory.MemoryType`` enum value.

        ``20=DDR``, ``21=DDR2``, ``24=DDR3``, ``26=DDR4``, ``34=DDR5``
        are the typical contemporary values.  Older values are kept
        in the mapping for forward-compatibility but most modern hosts
        report a value in the 20-34 range.
    """
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if not isinstance(value, int):
        return None

    mapping = {
        20: "DDR",
        21: "DDR2",
        24: "DDR3",
        26: "DDR4",
        34: "DDR5",
    }
    rtnval = mapping.get(value)
    return rtnval


def _merge_win32_operating_system(
    *,
    info: WindowsMemoryInfo,
    stdout: str,
    command: list[str],
) -> None:
    """
        Merge ``Win32_OperatingSystem.TotalVisibleMemorySize`` /
        ``FreePhysicalMemory`` (both reported in KB) onto an existing
        :class:`WindowsMemoryInfo`.
    """
    data = _decode_json_object(stdout=stdout, command=command)
    if isinstance(data, list):
        if len(data) == 0:
            return
        record = data[0]
    else:
        record = data

    if not isinstance(record, dict):
        return

    total_kb = record.get("TotalVisibleMemorySize")
    if isinstance(total_kb, int):
        info.total_bytes = total_kb * 1024
    elif isinstance(total_kb, str):
        try:
            info.total_bytes = int(total_kb) * 1024
        except ValueError:
            info.total_bytes = info.total_bytes

    free_kb = record.get("FreePhysicalMemory")
    if isinstance(free_kb, int):
        info.available_bytes = free_kb * 1024
    elif isinstance(free_kb, str):
        try:
            info.available_bytes = int(free_kb) * 1024
        except ValueError:
            info.available_bytes = info.available_bytes

    return


def _parse_get_disk_and_volume(
    *,
    disk_stdout: str,
    disk_command: list[str],
    volume_stdout: str,
    volume_command: list[str],
) -> list[WindowsStorageDeviceInfo]:
    """
        Parse ``Get-Disk`` + ``Get-Volume`` output.

        Real ``Get-Disk`` excerpt::

            [{"Number": 0, "FriendlyName": "NVMe Samsung",
              "SerialNumber": "S1AB", "Size": 512110190592,
              "BusType": "NVMe", "MediaType": "SSD"}]

        Real ``Get-Volume`` excerpt::

            [{"DriveLetter": "C", "FileSystemType": "NTFS",
              "Size": 511800000000, "FileSystemLabel": "OS",
              "Path": "\\\\?\\Volume{...}\\\\"}]

        Parse rules:

        - One :class:`WindowsStorageDeviceInfo` per ``Get-Disk`` record.
        - The volume map (DriveLetter -> FileSystemType) populates
          ``filesystem`` / ``mountpoint`` for the disk's primary
          volume.  The pilot uses the simple "first volume" approach;
          a multi-partition disk surfaces only the first volume's
          mountpoint, with subsequent partitions documented as a
          follow-up.

        :returns: One :class:`WindowsStorageDeviceInfo` per
                  ``Get-Disk`` record.
    """
    devices: list[WindowsStorageDeviceInfo] = []

    disk_records = _coerce_json_records(
        stdout=disk_stdout, command=disk_command,
    )
    volume_records = _coerce_json_records(
        stdout=volume_stdout, command=volume_command,
    )

    # Build a map of DriveLetter -> first-seen volume record.
    volume_map: dict = {}
    for record in volume_records:
        if not isinstance(record, dict):
            continue
        drive_letter = record.get("DriveLetter")
        if drive_letter is None:
            continue
        if drive_letter in volume_map:
            continue
        volume_map[drive_letter] = record

    # Pick a deterministic first DriveLetter to associate with the
    # first disk.  ``Get-Disk`` doesn't directly correlate, so the
    # pilot uses the alphabetic order of DriveLetters.
    drive_letters = sorted([
        letter for letter in volume_map.keys() if isinstance(letter, str)
    ])

    for index, record in enumerate(disk_records):
        if not isinstance(record, dict):
            continue

        size_raw = record.get("Size", 0)
        try:
            size_bytes = int(size_raw)
        except (TypeError, ValueError):
            size_bytes = 0

        type_value = record.get("MediaType") or record.get("BusType") or ""

        mountpoint: Optional[str] = None
        filesystem: Optional[str] = None
        if index < len(drive_letters):
            letter = drive_letters[index]
            volume = volume_map.get(letter, {})
            if isinstance(volume, dict):
                mountpoint = "{}:\\".format(letter)
                filesystem = volume.get("FileSystemType")

        device = WindowsStorageDeviceInfo(
            name=record.get("FriendlyName", "") or "",
            size_bytes=size_bytes,
            type=type_value,
            model=record.get("FriendlyName"),
            serial=record.get("SerialNumber"),
            mountpoint=mountpoint,
            filesystem=filesystem,
        )
        devices.append(device)

    return devices


def _parse_get_net_adapter(
    *,
    stdout: str,
    command: list[str],
) -> list[WindowsNetworkDeviceInfo]:
    """
        Parse ``Get-NetAdapter | ConvertTo-Json -Compress -Depth 5``.

        Real example (excerpt)::

            [{"Name": "Ethernet",
              "InterfaceDescription": "Intel(R) Ethernet I219-V",
              "MacAddress": "1C-1B-B5-11-22-33",
              "Status": "Up",
              "MtuSize": 1500,
              "LinkSpeed": "1 Gbps",
              "Virtual": false}]

        Parse rules:

        - One record per adapter.  ``Name`` -> ``name``;
          ``MacAddress`` -> ``mac_address``; ``Status`` ->
          ``link_state``.
        - ``MtuSize`` -> ``mtu``.
        - ``LinkSpeed`` is the human-readable string (``"1 Gbps"``);
          surfaced verbatim through ``speed``.
        - ``Virtual`` is the canonical ``Get-NetAdapter`` boolean.
    """
    devices: list[WindowsNetworkDeviceInfo] = []
    records = _coerce_json_records(stdout=stdout, command=command)

    for record in records:
        if not isinstance(record, dict):
            continue

        mtu_value = record.get("MtuSize")
        if isinstance(mtu_value, int):
            mtu = mtu_value
        else:
            mtu = None

        is_virtual_value = record.get("Virtual")
        if isinstance(is_virtual_value, bool):
            is_virtual = is_virtual_value
        else:
            is_virtual = False

        device = WindowsNetworkDeviceInfo(
            name=record.get("Name", "") or "",
            mac_address=record.get("MacAddress"),
            link_state=record.get("Status"),
            speed=record.get("LinkSpeed"),
            mtu=mtu,
            is_virtual=is_virtual,
        )
        devices.append(device)

    return devices


def _parse_win32_pnp_entity_pci(
    *,
    stdout: str,
    command: list[str],
) -> list[WindowsPciDeviceInfo]:
    """
        Parse the JSON body of ``Get-CimInstance Win32_PnPEntity |
        Where-Object PNPClass='PCI' | ConvertTo-Json``.

        Real example (excerpt)::

            [{"Name": "Intel(R) USB 3.1 eXtensible Host Controller",
              "Manufacturer": "Intel(R) Corporation",
              "DeviceID": "PCI\\VEN_8086&DEV_A36D&SUBSYS_07D71028&REV_10\\\\3&11583659&0&A0",
              "Service": "USBXHCI"}]

        Parse rules:

        - ``DeviceID`` carries the bus-address-style identifier as
          ``PCI\\VEN_xxxx&DEV_yyyy&...``; the parser extracts
          ``vendor_id`` and ``device_id`` from the ``VEN_`` /
          ``DEV_`` tokens.
        - ``Service`` -> ``driver`` (the kernel driver service name).
        - ``Manufacturer`` -> ``vendor``; ``Name`` -> ``device``.
        - ``slot`` is the trailing token of ``DeviceID`` (the unique
          per-device portion after the last ``\\``).
    """
    devices: list[WindowsPciDeviceInfo] = []
    records = _coerce_json_records(stdout=stdout, command=command)

    for record in records:
        if not isinstance(record, dict):
            continue

        device_id_raw = record.get("DeviceID", "") or ""
        slot, vendor_id, dev_id = _parse_pnp_device_id(raw=device_id_raw)

        device = WindowsPciDeviceInfo(
            slot=slot,
            class_name=record.get("PNPClass"),
            vendor=record.get("Manufacturer"),
            device=record.get("Name"),
            vendor_id=vendor_id,
            device_id=dev_id,
            driver=record.get("Service"),
        )
        devices.append(device)

    return devices


def _parse_pnp_device_id(*, raw: str) -> tuple:
    """
        Split a ``Win32_PnPEntity.DeviceID`` string like
        ``PCI\\VEN_8086&DEV_A36D&SUBSYS_07D71028&REV_10\\3&11583659&0&A0``
        into ``(slot, vendor_id, device_id)``.

        ``slot`` is the last backslash-separated component (the unique
        per-device id).  ``vendor_id`` / ``device_id`` are extracted
        from the ``VEN_xxxx`` / ``DEV_yyyy`` tokens.
    """
    if raw is None or len(raw) == 0:
        return ("", None, None)

    parts = raw.split("\\")
    slot = parts[-1] if len(parts) > 0 else raw

    vendor_id: Optional[str] = None
    device_id: Optional[str] = None

    for chunk in raw.replace("\\", "&").split("&"):
        if chunk.startswith("VEN_"):
            vendor_id = chunk[len("VEN_"):]
        elif chunk.startswith("DEV_"):
            device_id = chunk[len("DEV_"):]
        elif chunk.startswith("VID_"):
            if vendor_id is None:
                vendor_id = chunk[len("VID_"):]
        elif chunk.startswith("PID_"):
            if device_id is None:
                device_id = chunk[len("PID_"):]

    rtnval = (slot, vendor_id, device_id)
    return rtnval


def _parse_win32_usbhub(
    *,
    stdout: str,
    command: list[str],
) -> list[WindowsUsbDeviceInfo]:
    """
        Parse ``Get-CimInstance Win32_USBHub | ConvertTo-Json``.

        Real example (excerpt)::

            [{"Name": "USB Root Hub (USB 3.0)",
              "DeviceID": "USB\\VID_8086&PID_A36D\\\\5&3196A1A0&0&0",
              "PNPDeviceID": "USB\\VID_8086&PID_A36D\\\\5&3196A1A0&0&0"}]

        Parse rules:

        - ``DeviceID`` is parsed through the same vendor / product
          extractor used for PCI to recover the four-hex-digit IDs.
        - ``Name`` -> ``name``; the trailing ``\\``-separated token
          becomes ``device_id``.
        - ``serial`` is left as ``None``; the ``Win32_USBHub`` class
          does not carry a serial column directly.
        - ``bus`` is the first backslash-separated component of
          ``DeviceID`` (``"USB"``).
    """
    devices: list[WindowsUsbDeviceInfo] = []
    records = _coerce_json_records(stdout=stdout, command=command)

    for record in records:
        if not isinstance(record, dict):
            continue

        raw_device_id = record.get("DeviceID", "") or record.get(
            "PNPDeviceID", "",
        ) or ""
        slot, vendor_id, product_id = _parse_pnp_device_id(raw=raw_device_id)

        bus_label: str
        if "\\" in raw_device_id:
            bus_label = raw_device_id.split("\\")[0]
        else:
            bus_label = "USB"

        device = WindowsUsbDeviceInfo(
            bus=bus_label,
            device_id=slot,
            vendor_id=vendor_id,
            product_id=product_id,
            name=record.get("Name"),
            serial=None,
        )
        devices.append(device)

    return devices


def _parse_win32_bios(
    *,
    stdout: str,
    command: list[str],
) -> WindowsFirmwareInfo:
    """
        Parse ``Get-CimInstance Win32_BIOS | ConvertTo-Json``.

        Real example (excerpt)::

            {"Manufacturer": "Dell Inc.",
             "SMBIOSBIOSVersion": "1.7.2",
             "Version": "DELL   - 1072500",
             "ReleaseDate": "/Date(1685577600000)/",
             "BiosCharacteristics": [7, 11, 12, 32]}

        Parse rules:

        - ``Manufacturer`` -> ``vendor``;
          ``SMBIOSBIOSVersion`` -> ``version``;
          ``ReleaseDate`` is surfaced verbatim into ``release_date``
          (the ``/Date(ms)/`` form is not transformed in this pilot).
        - ``BiosCharacteristics`` is a list of numeric capability
          tokens; presence of token ``43`` (``"UEFI Specification is
          supported"``) sets ``uefi=True``; absence sets it
          ``False``.
        - ``secure_boot`` is left ``None`` — the proper read is via
          ``Confirm-SecureBootUEFI`` and is documented as a follow-up.
    """
    info = WindowsFirmwareInfo()
    data = _decode_json_object(stdout=stdout, command=command)
    if isinstance(data, list):
        if len(data) == 0:
            return info
        record = data[0]
    else:
        record = data

    if not isinstance(record, dict):
        return info

    info.vendor = record.get("Manufacturer")
    info.version = record.get("SMBIOSBIOSVersion") or record.get("Version")
    release_value = record.get("ReleaseDate")
    if release_value is not None:
        info.release_date = str(release_value)

    characteristics = record.get("BiosCharacteristics")
    if isinstance(characteristics, list):
        if 43 in characteristics:
            info.uefi = True
        else:
            info.uefi = False
    else:
        info.uefi = None

    return info

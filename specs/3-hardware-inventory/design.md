# Design

## Overview

This spec implements the hardware-inventory contract defined at
`codecraftai-sysman-common/specs/contracts/hardware-inventory/contract.md` for
Windows using the PowerShell ``Get-ComputerInfo`` and ``Get-CimInstance``
cmdlets, plus storage / network helpers (``Get-Disk`` / ``Get-Volume`` /
``Get-NetAdapter``).

The hardware-inventory feature is implemented as a single module,
`ccai.sysman.windows.hardwareinventory`, that exposes a
`WindowsHardwareInventoryManager` class.  The coordinator instantiates
`WindowsHardwareInventoryManager` and calls its read verbs to query discrete
hardware aspects.  Every verb returns a typed `Windows*` dataclass, or raises
`WindowsHardwareInventoryQueryError` when the underlying command fails or the
captured output cannot be parsed.

This feature is strictly **read-only**.  Per the cross-OS contract:

- `WindowsHardwareInventoryActionStatus` enumerates `SUCCESS`, `FAILED`, and
  `SKIPPED` only; there is no `DRY_RUN` member.
- `WindowsHardwareInventoryManagerConfig` inherits `elevated` and `dry_run`
  from `FeatureConfig`, but neither field gates any verb.  Every runner
  invocation passes `elevated=False`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH / WinRM (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsHardwareInventoryManager      │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        WindowsHardwareInventoryManager                      │
│  ccai.sysman.windows.hardwareinventory                  │
│                                                             │
│  get_system_info()       get_cpu_info()                     │
│  get_memory_info()       list_storage_devices()             │
│  list_network_devices()  list_pci_devices()                 │
│  list_usb_devices()      get_firmware_info()                │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated=False, ...) → RunResult                 │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH/WinRM          │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsHardwareInventoryManager`

**Location:** `source/packages/ccai/sysman/windows/hardwareinventory.py`

The primary public class.  Constructed with an `ICommandRunner` and a
`WindowsHardwareInventoryManagerConfig`.

| Method | Returns | Description |
|---|---|---|
| `get_system_info()` | `WindowsSystemInfo` | Run `Get-ComputerInfo \| ConvertTo-Json -Compress -Depth 5` |
| `get_cpu_info()` | `WindowsCpuInfo` | Run `Get-CimInstance Win32_Processor \| ConvertTo-Json -Compress -Depth 5` |
| `get_memory_info()` | `WindowsMemoryInfo` | Run `Get-CimInstance Win32_PhysicalMemory \| ConvertTo-Json` and `Get-CimInstance Win32_OperatingSystem \| Select TotalVisibleMemorySize,FreePhysicalMemory \| ConvertTo-Json` |
| `list_storage_devices()` | `list[WindowsStorageDeviceInfo]` | Run `Get-Disk \| ConvertTo-Json` and `Get-Volume \| ConvertTo-Json`; correlate by sorted DriveLetter |
| `list_network_devices()` | `list[WindowsNetworkDeviceInfo]` | Run `Get-NetAdapter \| ConvertTo-Json -Compress -Depth 5` |
| `list_pci_devices()` | `list[WindowsPciDeviceInfo]` | Run `Get-CimInstance Win32_PnPEntity \| Where-Object { $_.PNPClass -eq 'PCI' } \| ConvertTo-Json` |
| `list_usb_devices()` | `list[WindowsUsbDeviceInfo]` | Run `Get-CimInstance Win32_USBHub \| ConvertTo-Json -Compress -Depth 5` |
| `get_firmware_info()` | `WindowsFirmwareInfo` | Run `Get-CimInstance Win32_BIOS \| ConvertTo-Json -Compress -Depth 5` |

### `WindowsHardwareInventoryManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Inherited from `FeatureConfig`; ignored — feature is read-only |
| `elevated` | `bool` | `False` | Inherited from `FeatureConfig`; reserved for selective elevation, not consulted by any verb today |
| `powershell_executable` | `str` | `"powershell.exe"` | PowerShell host (`"pwsh.exe"` to target PS 7+) |

---

## Data Models

```python
class WindowsHardwareInventoryActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    # DRY_RUN intentionally absent — feature is read-only.


class WindowsHardwareInventoryQueryError(Exception):
    command: list[str]
    exit_code: int | None
    stderr: str


@dataclass
class WindowsSystemInfo:
    manufacturer: str | None
    product_name: str | None
    serial_number: str | None
    uuid: str | None
    bios_version: str | None
    firmware_version: str | None
    family: str | None
    version: str | None


@dataclass
class WindowsCpuInfo:
    model: str | None
    vendor: str | None
    architecture: str | None
    physical_cores: int
    logical_cores: int
    frequency_mhz: int | None
    cache_l1: int | None
    cache_l2: int | None
    cache_l3: int | None
    flags: list[str]


@dataclass
class WindowsMemoryModule:
    slot: str | None
    size_bytes: int
    type: str | None
    speed_mhz: int | None
    manufacturer: str | None
    serial_number: str | None


@dataclass
class WindowsMemoryInfo:
    total_bytes: int
    available_bytes: int | None
    slots_used: int | None
    slots_total: int | None
    modules: list[WindowsMemoryModule]


@dataclass
class WindowsStorageDeviceInfo:
    name: str
    size_bytes: int
    type: str
    model: str | None
    serial: str | None
    mountpoint: str | None
    filesystem: str | None


@dataclass
class WindowsNetworkDeviceInfo:
    name: str
    mac_address: str | None
    link_state: str | None
    speed: str | None
    mtu: int | None
    is_virtual: bool


@dataclass
class WindowsPciDeviceInfo:
    slot: str
    class_name: str | None
    vendor: str | None
    device: str | None
    vendor_id: str | None
    device_id: str | None
    driver: str | None


@dataclass
class WindowsUsbDeviceInfo:
    bus: str
    device_id: str | None
    vendor_id: str | None
    product_id: str | None
    name: str | None
    serial: str | None


@dataclass
class WindowsFirmwareInfo:
    vendor: str | None
    version: str | None
    release_date: str | None
    secure_boot: bool | None
    uefi: bool | None
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

Same protocol as the rest of the package; see
`ccai.interfaces.icommandrunner`.

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
```

The manager's `_read` helper raises
`WindowsHardwareInventoryQueryError` on any non-zero exit code so that
every verb's caller sees a single, typed error path.

---

## Command Mappings

Every PowerShell invocation has the argv shape

```
[<powershell>, "-NoProfile", "-NonInteractive", "-Command", <script>]
```

where `<script>` is one of the following.  Every invocation passes
`elevated=False`.

| Verb | Scriptblock |
|---|---|
| `get_system_info` | `Get-ComputerInfo \| ConvertTo-Json -Compress -Depth 5` |
| `get_cpu_info` | `Get-CimInstance Win32_Processor \| ConvertTo-Json -Compress -Depth 5` |
| `get_memory_info` (1) | `Get-CimInstance Win32_PhysicalMemory \| ConvertTo-Json -Compress -Depth 5` |
| `get_memory_info` (2) | `Get-CimInstance Win32_OperatingSystem \| Select-Object TotalVisibleMemorySize,FreePhysicalMemory \| ConvertTo-Json -Compress -Depth 5` |
| `list_storage_devices` (1) | `Get-Disk \| ConvertTo-Json -Compress -Depth 5` |
| `list_storage_devices` (2) | `Get-Volume \| ConvertTo-Json -Compress -Depth 5` |
| `list_network_devices` | `Get-NetAdapter \| ConvertTo-Json -Compress -Depth 5` |
| `list_pci_devices` | `Get-CimInstance Win32_PnPEntity \| Where-Object { $_.PNPClass -eq 'PCI' } \| ConvertTo-Json -Compress -Depth 5` |
| `list_usb_devices` | `Get-CimInstance Win32_USBHub \| ConvertTo-Json -Compress -Depth 5` |
| `get_firmware_info` | `Get-CimInstance Win32_BIOS \| ConvertTo-Json -Compress -Depth 5` |

---

## Output Parsing per Verb

### One-or-Many JSON Coercion

Every PowerShell cmdlet that emits zero, one, or many records causes
``ConvertTo-Json`` to emit different top-level shapes:

- Zero records: empty string (no JSON emitted).
- One record: a JSON object.
- Many records: a JSON array of objects.

The parsers route through `_coerce_json_records()` which normalises all
three shapes into a Python list (possibly empty).  Verbs whose
upstream cmdlet always emits a single record (`Get-ComputerInfo`,
`Get-CimInstance Win32_OperatingSystem`, `Get-CimInstance Win32_BIOS`)
route through `_decode_json_object()` which accepts both an object and
a one-element array.

### `get_system_info` — `Get-ComputerInfo`

Real example (excerpt):

```json
{"CsManufacturer": "Dell Inc.",
 "CsModel": "Latitude 7420",
 "CsName": "DESKTOP-ABC",
 "BiosManufacturer": "Dell Inc.",
 "BiosSMBIOSBIOSVersion": "1.7.2",
 "BiosFirmwareType": "Uefi",
 "CsSystemSKUNumber": "0A1B",
 "CsSystemUUID": "AAAA-BBBB-CCCC",
 "BiosSeralNumber": "SN-AB-12",
 "OsVersion": "10.0.19045"}
```

Parse rules:

- Direct field map: `CsManufacturer` -> `manufacturer`;
  `CsModel` -> `product_name`; `BiosSeralNumber` (note the canonical
  Microsoft typo) -> `serial_number`; `CsSystemUUID` -> `uuid`;
  `BiosSMBIOSBIOSVersion` -> `bios_version`;
  `BiosFirmwareType` -> `firmware_version`;
  `CsSystemSKUNumber` -> `family`; `OsVersion` -> `version`.

### `get_cpu_info` — `Get-CimInstance Win32_Processor`

Real example (excerpt):

```json
{"Architecture": 9,
 "Manufacturer": "GenuineIntel",
 "Name": "Intel(R) Core(TM) i7-1185G7 @ 3.00GHz",
 "NumberOfCores": 4,
 "NumberOfLogicalProcessors": 8,
 "MaxClockSpeed": 3000,
 "L2CacheSize": 5120,
 "L3CacheSize": 12288}
```

Parse rules:

- Multi-socket hosts emit an array; the parser sums `NumberOfCores`
  and `NumberOfLogicalProcessors` across all records.
- `Architecture` is decoded via `{0: x86, 1: MIPS, 2: Alpha, 3: PowerPC,
  5: ARM, 6: Itanium, 9: x64, 12: ARM64}`.
- `MaxClockSpeed` is already in MHz.
- `L2CacheSize` / `L3CacheSize` are in KB; the parser multiplies by
  1024 to surface bytes.
- `flags` and `cache_l1` are intentionally left empty — Windows does
  not expose CPU feature flags or L1 size through `Win32_Processor`
  directly; the proper read is via `Get-CimInstance
  -Namespace root\\Microsoft\\Windows\\Storage` for cache topology and
  is documented as a follow-up.

### `get_memory_info` — `Win32_PhysicalMemory` + `Win32_OperatingSystem`

`Win32_PhysicalMemory` excerpt:

```json
[{"BankLabel": "BANK 0",
  "DeviceLocator": "DIMM A",
  "Capacity": 17179869184,
  "MemoryType": 26,
  "ConfiguredClockSpeed": 3200,
  "Manufacturer": "Micron",
  "SerialNumber": "12345"}]
```

`Win32_OperatingSystem` excerpt (after the
`Select-Object TotalVisibleMemorySize,FreePhysicalMemory`):

```json
{"TotalVisibleMemorySize": 33554432,
 "FreePhysicalMemory": 25165824}
```

Parse rules:

- `Capacity` is bytes — direct map to `size_bytes`.
- `MemoryType` is decoded via `{20:DDR, 21:DDR2, 24:DDR3, 26:DDR4,
  34:DDR5}`.
- `ConfiguredClockSpeed` is MHz; falls back to `Speed` when absent.
- `slots_used` is the count of populated modules; `slots_total` is
  left `None` because `Win32_PhysicalMemoryArray` (which carries the
  total slot count) is not queried in this pilot — see follow-ups.
- `TotalVisibleMemorySize` / `FreePhysicalMemory` are in KB; multiply
  by 1024 for bytes.

### `list_storage_devices` — `Get-Disk` + `Get-Volume`

`Get-Disk` excerpt:

```json
[{"Number": 0,
  "FriendlyName": "NVMe Samsung SSD 970",
  "SerialNumber": "S1AB",
  "Size": 512110190592,
  "BusType": "NVMe",
  "MediaType": "SSD"}]
```

`Get-Volume` excerpt:

```json
[{"DriveLetter": "C",
  "FileSystemType": "NTFS",
  "Size": 511800000000,
  "FileSystemLabel": "OS"}]
```

Parse rules:

- One `WindowsStorageDeviceInfo` per `Get-Disk` record.
- The parser sorts `Get-Volume` records by `DriveLetter` and pairs the
  Nth disk with the Nth letter.  This is the simple correlation; a
  multi-volume disk surfaces only the first volume's mount-point.
- `MediaType` (e.g., `"SSD"`) wins over `BusType` for `type`; when
  both are absent, `type` is empty string.
- `mountpoint` is `"<L>:\\"`; `filesystem` is `FileSystemType`.

### `list_network_devices` — `Get-NetAdapter`

Real example (excerpt):

```json
[{"Name": "Ethernet",
  "InterfaceDescription": "Intel(R) Ethernet I219-V",
  "MacAddress": "1C-1B-B5-11-22-33",
  "Status": "Up",
  "MtuSize": 1500,
  "LinkSpeed": "1 Gbps",
  "Virtual": false}]
```

Parse rules:

- One record per adapter.  Direct field map.
- `LinkSpeed` is the human-readable string surface (`"1 Gbps"`) —
  surfaced verbatim through `speed`.

### `list_pci_devices` — `Win32_PnPEntity` filtered to `PNPClass='PCI'`

Real example (excerpt):

```json
[{"Name": "Intel(R) USB 3.1 eXtensible Host Controller",
  "Manufacturer": "Intel(R) Corporation",
  "PNPClass": "PCI",
  "DeviceID": "PCI\\VEN_8086&DEV_A36D&SUBSYS_07D71028&REV_10\\3&11583659&0&A0",
  "Service": "USBXHCI"}]
```

Parse rules:

- `DeviceID` is split on `\` to extract the per-device id (the last
  component, used as `slot`).  The parser scans every `&`/`\\`-
  separated chunk for `VEN_xxxx`, `DEV_yyyy`, `VID_xxxx`, and
  `PID_yyyy` tokens to recover `vendor_id` / `device_id`.
- `Service` -> `driver` (the kernel driver service name).

### `list_usb_devices` — `Win32_USBHub`

Real example (excerpt):

```json
[{"Name": "USB Root Hub (USB 3.0)",
  "DeviceID": "USB\\VID_8086&PID_A36D\\5&3196A1A0&0&0",
  "PNPDeviceID": "USB\\VID_8086&PID_A36D\\5&3196A1A0&0&0"}]
```

Parse rules:

- `bus` is the leading backslash-separated component of `DeviceID`
  (typically `"USB"`).
- The same `_parse_pnp_device_id` helper used for PCI extracts
  `vendor_id` / `product_id`.
- `serial` is `None`; `Win32_USBHub` does not carry it.

### `get_firmware_info` — `Win32_BIOS`

Real example (excerpt):

```json
{"Manufacturer": "Dell Inc.",
 "SMBIOSBIOSVersion": "1.7.2",
 "Version": "DELL   - 1072500",
 "ReleaseDate": "/Date(1685577600000)/",
 "BiosCharacteristics": [7, 11, 12, 32, 43]}
```

Parse rules:

- `Manufacturer` -> `vendor`; `SMBIOSBIOSVersion` (preferred) or
  `Version` -> `version`.
- `ReleaseDate` is surfaced verbatim through `release_date`.  The
  ``/Date(<ms>)/`` form is intentionally not transformed in this pilot
  so a coordinator can correlate it with the raw CIM payload.
- `BiosCharacteristics` is a list of numeric capability tokens.
  Token `43` is the SMBIOS-defined "UEFI Specification is supported";
  presence -> `uefi=True`, absence -> `uefi=False`, missing list ->
  `uefi=None`.

---

## Error Handling

### Typed Exception

`WindowsHardwareInventoryQueryError` carries `command`, `exit_code`,
and `stderr`.  Its message is built via `os.linesep.join()` so that
command arguments, exit code, and stderr are each on their own line.

### Error Sources

| Source | Mapped To |
|---|---|
| Non-zero exit code from the runner | `WindowsHardwareInventoryQueryError` with raw stderr |
| `json.JSONDecodeError` parsing PowerShell output | `WindowsHardwareInventoryQueryError` with `message=` describing the parse failure |
| Empty stdout from a one-or-many cmdlet | Treated as "zero records" — verb returns an empty list (or default-populated dataclass for object-emitting cmdlets) |

---

## Correctness Properties

### Property 1: Read-Only Discipline

For every verb invocation, every runner call passes `elevated=False`,
regardless of `config.elevated`.  No verb invokes a mutating cmdlet.

### Property 2: One-or-Many Tolerance

For every verb that consumes a `ConvertTo-Json` output, single-record
hosts (object) and multi-record hosts (array) both yield a uniform
list-of-records pipeline; verbs never raise on the "object vs array"
shape difference.

### Property 3: Empty-Output Safety

For every cmdlet, an empty stdout (zero records) yields an empty list
(or default-populated dataclass), not an exception.

### Property 4: PowerShell Argv Discipline

For every PowerShell invocation, the argv begins with the configured
executable, then `-NoProfile`, `-NonInteractive`, `-Command`, then the
scriptblock.  ``$PROFILE`` cannot mutate cmdlet behaviour and
PowerShell never blocks waiting for interactive input.

### Property 5: PnP DeviceID Parsing Stability

For every Win32_PnPEntity / Win32_USBHub `DeviceID` of the form
`<bus>\<token>(&<token>)*\<slot>`, the parser surfaces the leading
component as `bus` (USB) or the trailing component as `slot` (PCI),
and recovers `VEN_/VID_xxxx` and `DEV_/PID_yyyy` token values
case-insensitively.

### Property 6: Multi-Socket CPU Aggregation

For every `Win32_Processor` payload that contains N socket records,
the resulting `WindowsCpuInfo.physical_cores` and `logical_cores`
equal the sum of `NumberOfCores` and `NumberOfLogicalProcessors`
across all N records.

### Property 7: Firmware UEFI Detection via Characteristics

For every `Win32_BIOS` payload, `uefi=True` iff token `43` is present
in `BiosCharacteristics`; `uefi=False` when the list is present but
lacks token 43; `uefi=None` when the list is absent entirely.

### Property 8: No Hidden Dry-Run

For every verb, a `dry_run=True` config does not change the runner-call
sequence.  The feature ignores the `dry_run` flag entirely.

---

## Module Layout

```
source/packages/ccai/sysman/windows/
└── hardwareinventory.py                ← manager + dataclasses + parsers + exception

source/testroots/testbase/ccai/tests/sysman/windows/
└── test_hardwareinventory.py           ← unit tests using a MockCommandRunner
```

---

## Usage Examples

### Local execution

```python
from ccai.sysman.windows.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.windows.hardwareinventory import (
    WindowsHardwareInventoryManager,
    WindowsHardwareInventoryManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsHardwareInventoryManagerConfig()
inventory = WindowsHardwareInventoryManager(runner, config)

system = inventory.get_system_info()
cpu = inventory.get_cpu_info()
memory = inventory.get_memory_info()
storage = inventory.list_storage_devices()
network = inventory.list_network_devices()
pci = inventory.list_pci_devices()
usb = inventory.list_usb_devices()
firmware = inventory.get_firmware_info()
```

### PowerShell 7+ host

```python
from ccai.sysman.windows.hardwareinventory import (
    WindowsHardwareInventoryManager,
    WindowsHardwareInventoryManagerConfig,
)

config = WindowsHardwareInventoryManagerConfig(
    powershell_executable="pwsh.exe",
)
inventory = WindowsHardwareInventoryManager(runner, config)
```

### Aspect composition

```python
from ccai.sysman.windows.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage

runner = SSHCommandRunner(SSHRunnerConfig(host="winserver", username="admin"))
runner.add_aspect(PersistenceAspect(PersistenceAspectConfig(
    storage_backend=SQLiteStorage("C:\\\\maintenance\\\\inventory.db"),
)))
runner.add_aspect(RetryAspect(RetryAspectConfig(
    max_retries=3, retryable_exit_codes=[1],
)))
runner.add_aspect(TimeoutAspect(TimeoutAspectConfig(default_timeout=120.0)))

inventory = WindowsHardwareInventoryManager(
    runner, WindowsHardwareInventoryManagerConfig(),
)
network = inventory.list_network_devices()
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run` call and
  returns pre-seeded `RunResult` values keyed by argv tuple.
- Mocks are seeded with canned stdout from real
  ``Get-ComputerInfo \| ConvertTo-Json``,
  ``Get-CimInstance Win32_Processor \| ConvertTo-Json``,
  ``Get-CimInstance Win32_PhysicalMemory \| ConvertTo-Json``,
  ``Get-CimInstance Win32_OperatingSystem \| Select-Object \| ConvertTo-Json``,
  ``Get-Disk \| ConvertTo-Json``,
  ``Get-Volume \| ConvertTo-Json``,
  ``Get-NetAdapter \| ConvertTo-Json``,
  ``Get-CimInstance Win32_PnPEntity \| Where-Object \| ConvertTo-Json``,
  ``Get-CimInstance Win32_USBHub \| ConvertTo-Json``, and
  ``Get-CimInstance Win32_BIOS \| ConvertTo-Json`` captures.
- Tests verify the *arguments* passed to the runner and the
  `elevated=False` flag — not system state.
- Each public verb has at minimum a success-path test demonstrating both
  argv construction and field-level parsing.
- A failure-path test asserts that a non-zero runner result raises
  `WindowsHardwareInventoryQueryError` carrying the argv, exit code,
  and stderr.
- A read-elevation test exercises every verb with
  `config.elevated=True` and asserts that no runner call received
  `elevated=True`.

### Test File

`source/testroots/testbase/ccai/tests/sysman/windows/test_hardwareinventory.py`

### Tests Implemented

1. `test_get_system_info_invokes_get_computer_info_and_parses`
2. `test_get_cpu_info_parses_get_ciminstance_output`
3. `test_get_memory_info_aggregates_modules_and_total`
4. `test_list_storage_devices_parses_get_disk_output`
5. `test_list_network_devices_parses_get_netadapter_output`
6. `test_list_pci_devices_parses_get_ciminstance_output`
7. `test_list_usb_devices_parses_get_ciminstance_output`
8. `test_get_firmware_info_parses_get_ciminstance_output`
9. `test_failed_command_raises_query_error`
10. `test_reads_never_elevate`

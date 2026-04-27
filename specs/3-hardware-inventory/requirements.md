# Requirements

## Introduction

This spec implements the hardware-inventory contract defined at
`automojo-sysman-common/specs/contracts/hardware-inventory/contract.md` for
Windows using PowerShell ``Get-ComputerInfo``, ``Get-CimInstance``,
``Get-Disk``, ``Get-Volume``, and ``Get-NetAdapter`` cmdlets.

The hardware-inventory feature provides the `automojo-sysman-windows` package
with the ability to query, classify, and aggregate hardware and platform
identity information on Windows systems: hardware identity, CPU topology,
memory (aggregate plus per-DIMM detail), storage devices, network link-layer
hardware, PCI devices, USB devices, and BIOS / UEFI firmware identity.

A maintenance coordinator drives every operation by calling into this
feature's public interfaces.  All system commands are executed exclusively
through the `automojo-interfaces` command-runner abstraction, enabling
auditing, tracing, and test mocking without requiring a live Windows host.

This feature is **strictly read-only**.  Every verb is inherently idempotent
and every verb is safe to repeat.  As a consequence:

- There is no dry-run distinction from normal behavior — no command issued
  by this module mutates host state.  The `dry_run` field is inherited from
  `FeatureConfig` for structural uniformity but is not honored.
- There is no `DRY_RUN` member on `WindowsHardwareInventoryActionStatus`.
- Every read passes `elevated=False` to the runner regardless of the
  `elevated` config flag.

Verbs are READ-ONLY.  Failures surface as
`WindowsHardwareInventoryQueryError`, never as a per-verb result dataclass.

---

## Requirements

### Requirement 1: Query System Identity

**User Story:** As a maintenance coordinator, I want to query the Windows
system identity (manufacturer, model, serial, UUID, BIOS version, firmware
type), so that I can record host identity in a maintenance report.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_system_info()` THE SYSTEM SHALL invoke
   `powershell.exe -NoProfile -NonInteractive -Command "Get-ComputerInfo |
   ConvertTo-Json -Compress -Depth 5"` via the command runner with
   `elevated=False`.
2. THE SYSTEM SHALL JSON-decode the resulting object and populate
   `manufacturer=CsManufacturer`, `product_name=CsModel`,
   `serial_number=BiosSeralNumber`, `uuid=CsSystemUUID`,
   `bios_version=BiosSMBIOSBIOSVersion`,
   `firmware_version=BiosFirmwareType`,
   `family=CsSystemSKUNumber`, and `version=OsVersion`.
3. WHEN PowerShell returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError` carrying the argv, exit code, and
   stderr.
4. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError` with a parse-failure message.

---

### Requirement 2: Query CPU Topology and Features

**User Story:** As a maintenance coordinator, I want to query CPU topology
(physical / logical cores, model, vendor, frequency, cache sizes), so that
I can record the CPU surface in a hardware report.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_cpu_info()` THE SYSTEM SHALL invoke
   `Get-CimInstance Win32_Processor | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL accept either a single object or an array (multi-socket
   hosts) and sum `NumberOfCores` / `NumberOfLogicalProcessors` across the
   records.
3. THE SYSTEM SHALL decode `Architecture` integer (0=x86, 5=ARM, 6=Itanium,
   9=x64, 12=ARM64) into `architecture`.
4. THE SYSTEM SHALL populate `vendor=Manufacturer`, `model=Name`,
   `frequency_mhz=MaxClockSpeed` (already MHz), `cache_l2=L2CacheSize*1024`,
   and `cache_l3=L3CacheSize*1024`.
5. WHEN the command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.
6. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 3: Query Memory (Aggregate and Per-DIMM)

**User Story:** As a maintenance coordinator, I want to query installed
memory and per-DIMM detail, so that I can confirm slot population, DRAM
type, and DRAM speed.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_memory_info()` THE SYSTEM SHALL invoke
   `Get-CimInstance Win32_PhysicalMemory | ConvertTo-Json` and
   `Get-CimInstance Win32_OperatingSystem | Select-Object
   TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json` in that
   order.
2. THE SYSTEM SHALL emit one `WindowsMemoryModule` per
   `Win32_PhysicalMemory` record whose `Capacity` decodes to a non-zero
   byte count.
3. THE SYSTEM SHALL set `slot=DeviceLocator|BankLabel`,
   `size_bytes=Capacity` (already bytes),
   `type=decode(MemoryType)`, `speed_mhz=ConfiguredClockSpeed|Speed`,
   `manufacturer=Manufacturer`, `serial_number=SerialNumber`.
4. THE SYSTEM SHALL set `slots_used` to the count of populated modules.
5. THE SYSTEM SHALL set `total_bytes=TotalVisibleMemorySize*1024` and
   `available_bytes=FreePhysicalMemory*1024`.
6. WHEN either command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 4: Enumerate Storage Devices

**User Story:** As a maintenance coordinator, I want to enumerate every
physical disk with name, size, type, model, serial, mount-point, and
filesystem, so that the storage layout is captured.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_storage_devices()` THE SYSTEM SHALL
   invoke `Get-Disk | ConvertTo-Json -Compress -Depth 5` and
   `Get-Volume | ConvertTo-Json -Compress -Depth 5` in that order.
2. THE SYSTEM SHALL emit one `WindowsStorageDeviceInfo` per
   ``Get-Disk`` record.
3. THE SYSTEM SHALL populate `name=FriendlyName`, `size_bytes=Size`,
   `type=MediaType|BusType`, `model=FriendlyName`, `serial=SerialNumber`.
4. THE SYSTEM SHALL build a DriveLetter -> Volume map from `Get-Volume`,
   and for each disk SHALL associate the next-in-alphabetic-order
   DriveLetter with that disk's `mountpoint` (`"<L>:\\"`) and
   `filesystem=FileSystemType`.
5. WHEN either command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 5: Enumerate Network Devices

**User Story:** As a maintenance coordinator, I want to enumerate network
adapters with name, MAC address, link state, MTU, link speed, and a
virtual / physical classification.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_network_devices()` THE SYSTEM SHALL
   invoke `Get-NetAdapter | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL emit one `WindowsNetworkDeviceInfo` per record.
3. THE SYSTEM SHALL populate `name=Name`, `mac_address=MacAddress`,
   `link_state=Status`, `speed=LinkSpeed`, `mtu=MtuSize`,
   `is_virtual=Virtual`.
4. WHEN the command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 6: Enumerate PCI Devices

**User Story:** As a maintenance coordinator, I want to enumerate PCI
devices with vendor, device, vendor / device IDs, and bound driver
service, so that I can verify driver bindings.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_pci_devices()` THE SYSTEM SHALL
   invoke `Get-CimInstance Win32_PnPEntity | Where-Object
   { $_.PNPClass -eq 'PCI' } | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL emit one `WindowsPciDeviceInfo` per filtered
   record.
3. THE SYSTEM SHALL parse `DeviceID` to recover `vendor_id` (from
   `VEN_xxxx`), `device_id` (from `DEV_yyyy`), and use the trailing
   backslash-separated component of `DeviceID` as `slot`.
4. THE SYSTEM SHALL populate `vendor=Manufacturer`, `device=Name`,
   `class_name=PNPClass`, and `driver=Service`.
5. WHEN the command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 7: Enumerate USB Devices

**User Story:** As a maintenance coordinator, I want to enumerate USB
devices on the host, so that attached peripherals are captured.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_usb_devices()` THE SYSTEM SHALL invoke
   `Get-CimInstance Win32_USBHub | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL emit one `WindowsUsbDeviceInfo` per record.
3. THE SYSTEM SHALL parse `DeviceID` (or `PNPDeviceID`) to recover
   `vendor_id` (from `VID_xxxx`) and `product_id` (from `PID_yyyy`).
4. THE SYSTEM SHALL set `bus` to the leading backslash-separated
   component of `DeviceID` (typically `"USB"`).
5. THE SYSTEM SHALL leave `serial` as `None`; the
   ``Win32_USBHub`` class does not carry a serial column.
6. WHEN the command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 8: Query Firmware (BIOS / UEFI)

**User Story:** As a maintenance coordinator, I want to query the BIOS /
UEFI firmware vendor, version, release date, and UEFI flag, so that
firmware upgrades can be scoped.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_firmware_info()` THE SYSTEM SHALL
   invoke `Get-CimInstance Win32_BIOS | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL populate `vendor=Manufacturer`,
   `version=SMBIOSBIOSVersion|Version`, `release_date=str(ReleaseDate)`.
3. THE SYSTEM SHALL set `uefi=True` when token `43` is present in
   `BiosCharacteristics` (`"UEFI Specification is supported"`); otherwise
   `uefi=False`; absent characteristics yield `uefi=None`.
4. THE SYSTEM SHALL leave `secure_boot` as `None`; the proper
   `Confirm-SecureBootUEFI` read is reserved as a follow-up.
5. WHEN the command returns a non-zero exit code THE SYSTEM SHALL raise
   `WindowsHardwareInventoryQueryError`.

---

### Requirement 9: Read-Only Discipline

**User Story:** As a maintenance coordinator, I want every
hardware-inventory verb to be strictly read-only, so that I can run it
under aspect compositions without risking host mutation.

#### Acceptance Criteria

1. THE `WindowsHardwareInventoryActionStatus` enum SHALL contain only
   `SUCCESS`, `FAILED`, and `SKIPPED` members; `DRY_RUN` is intentionally
   absent.
2. `WindowsHardwareInventoryManagerConfig` SHALL inherit `dry_run` and
   `elevated` from `FeatureConfig` for structural uniformity but SHALL
   NOT consult either field in any verb in the current pilot.
3. Every runner invocation in this module SHALL pass `elevated=False`.
4. No verb in this module SHALL invoke a command that mutates host state.

---

### Requirement 10: PowerShell Argv Discipline

**User Story:** As a maintenance coordinator, I want every PowerShell
invocation to use a fixed, profile-bypassing argv shape, so that user-
profile customisation cannot perturb cmdlet behaviour.

#### Acceptance Criteria

1. Every PowerShell invocation SHALL begin with the configured
   `powershell_executable` (default `"powershell.exe"`), followed by
   `-NoProfile`, `-NonInteractive`, `-Command`, and the scriptblock.
2. The scriptblock SHALL pipe the cmdlet output to
   `ConvertTo-Json -Compress -Depth 5` whenever the parser expects JSON.
3. The runner SHALL be free to substitute `pwsh.exe` (PowerShell 7+) when
   the config field is set; the parser SHALL accept both Windows
   PowerShell 5.1 and PowerShell 7+ JSON serializations transparently.

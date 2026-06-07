# Design

## Concept Mapping (LVM ↔ APFS ↔ Storage Spaces)

The cross-OS storage-management contract talks about *disks*, *pools*,
and *volumes*.  Each OS's native storage stack carries a different
vocabulary; the table below grounds the abstract contract in concrete
LVM / APFS / Storage-Spaces terms so a reader of this spec can compare
all three implementations side-by-side.

| Contract concept | Windows (this spec) | Ubuntu | macOS |
|---|---|---|---|
| Disk | `Get-Disk` row keyed by `Number` | Block device of `type=disk` (`/dev/sda`) | Whole-disk node (`disk0`) |
| Pool | **Storage Pool** (`New-StoragePool` / `Remove-StoragePool`) | LVM **Volume Group** | APFS **Container** |
| Pool member | `PhysicalDisk` whose `CanPool=True` was claimed via `New-StoragePool` or `Add-PhysicalDisk` | LVM **Physical Volume** | APFS **Physical Store** (a partition on a disk) |
| Volume | **Virtual Disk** carved from a pool (`New-Volume -StoragePoolFriendlyName ...`) **OR** raw partition on a plain disk (`New-Partition + Format-Volume`) | LVM **Logical Volume** | APFS **Volume** |
| Volume identifier | Drive letter (``"X"``) for partitions; **friendly name** for virtual disks | `<vg>/<lv>` | APFS volume BSD device id (`disk1s2`) |
| Free space | Pool ``Size`` − sum of ``Get-VirtualDisk.Size`` (resilient capacity is less than raw) | VG free PE × PE size | Container free space — shared dynamically |
| Resiliency level | Pool ``ResiliencySettingNameDefault`` plus per-`New-Volume` override (`Mirror` / `Parity` / `Simple`) | None natively (layer `mdadm` underneath) | Apple-managed; not exposed |

The Windows branch differs structurally from both Linux and macOS in
one important way: a **volume** can be either a raw partition on a
plain disk (no pool involved) or a virtual disk carved from a Storage
Pool.  The implementation dispatches on the
:class:`WindowsVolumeSpec` shape: when ``pool`` is set the
storage-spaces flow runs; when ``pool`` is ``None`` and ``disk_id`` is
set, the partition flow runs; both flows write through the contract's
`create_volume` verb.  The same dispatch happens on `delete_volume`,
keyed off the shape of the ``volume_id`` argument (drive letter →
partition flow, anything else → virtual-disk flow).

---

## Overview

The storage-management feature is implemented as a single module,
`ccai.sysman.windows.storagemanagement`, that exposes a
`WindowsStorageManager` class.  The coordinator instantiates
`WindowsStorageManager` and calls its methods to perform discrete
block-level storage actions: disk enumeration, volume create / delete
/ resize (partition or storage-spaces), Storage Pool create / delete /
extend / reduce, and detailed disk and volume queries.

This feature **complements — and does not duplicate** — the mount,
unmount, and access-path management provided by the
`filesystem-management` feature.  The `WindowsStorageManager` brings
disks, Storage Pools, and volumes into the desired shape; the
`WindowsFilesystemManager` is then responsible for managing
drive-letter assignments and access paths.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `WindowsStorageManager` never
directly calls `subprocess`, shell commands, or `open()`; it only
constructs PowerShell scripts and hands them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock dependencies without administrator
access or a live system.

The feature works identically whether commands execute locally (via
`LocalCommandRunner` shelling out to `powershell.exe`) or remotely
over WinRM / SSH (via `SSHCommandRunner` against an OpenSSH-on-Windows
host).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsStorageManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsStorageManager                            │
│  ccai.sysman.windows.storagemanagement                  │
│                                                             │
│  list_disks()           list_volumes()                      │
│  get_disk_info()        get_volume_info()                   │
│  create_volume()        delete_volume()                     │
│  resize_volume()                                            │
│  create_pool()          delete_pool()                       │
│  add_to_pool()          remove_from_pool()                  │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│           LocalCommandRunner / SSHCommandRunner             │
│           (dispatching powershell.exe scripts)              │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsStorageManager`

**Location:**
`source/packages/ccai/sysman/windows/storagemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_disks` | `() → list[WindowsDiskInfo]` | `Get-Disk | Select-Object ... | ConvertTo-Json` |
| `list_volumes` | `() → list[WindowsVolumeInfo]` | `Get-Volume | ... | ConvertTo-Json` plus `Get-VirtualDisk` for pool attribution |
| `create_volume` | `(spec) → WindowsStorageActionResult` | Pool flow: `New-Volume -StoragePoolFriendlyName ... -Size ... -ResiliencySettingName ... -FileSystem ...`.  Partition flow: `New-Partition -DiskNumber ... -Size ... -AssignDriveLetter | Format-Volume -FileSystem ... -NewFileSystemLabel ... -Confirm:$false` |
| `delete_volume` | `(volume_id) → WindowsStorageActionResult` | Drive letter: `Get-Partition -DriveLetter ... | Remove-Partition -Confirm:$false`.  Friendly name: `Remove-VirtualDisk -FriendlyName ... -Confirm:$false` |
| `resize_volume` | `(volume_id, size_bytes) → WindowsStorageActionResult` | `Resize-Partition -DriveLetter ... -Size ...` |
| `create_pool` | `(spec) → WindowsStorageActionResult` | `New-StoragePool -FriendlyName ... -StorageSubSystemFriendlyName 'Windows Storage*' -PhysicalDisks (Get-PhysicalDisk -CanPool $True | Where-Object Number -in @(...))` |
| `delete_pool` | `(pool_name) → WindowsStorageActionResult` | `Remove-StoragePool -FriendlyName ... -Confirm:$false` |
| `add_to_pool` | `(pool_name, disk_id) → WindowsStorageActionResult` | `Add-PhysicalDisk -StoragePoolFriendlyName ... -PhysicalDisks (Get-PhysicalDisk | Where-Object Number -eq ...)` |
| `remove_from_pool` | `(pool_name, disk_id) → WindowsStorageActionResult` | `Remove-PhysicalDisk -StoragePoolFriendlyName ... -PhysicalDisks (...) -Confirm:$false` |
| `get_volume_info` | `(volume_id) → WindowsVolumeInfo \| None` | indexed lookup over `list_volumes()` |
| `get_disk_info` | `(disk_id) → WindowsDiskInfo \| None` | indexed lookup over `list_disks()` |

### `WindowsStorageManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `powershell_executable` | `str` | `"powershell.exe"` | PATH-resolved invocation |
| `default_filesystem` | `str` | `"NTFS"` | Filesystem token for `New-Volume -FileSystem` |
| `default_resiliency` | `str` | `"Mirror"` | Default `-ResiliencySettingName` |
| `default_subsystem` | `str` | `"Windows Storage*"` | `-StorageSubSystemFriendlyName` pattern |

---

## Data Models

### Result Types

```python
class WindowsStorageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class WindowsStorageActionResult:
    status: WindowsStorageActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    target: str | None             # volume id, pool name, or disk number
    pool: str | None
    volume: str | None
    message: str | None            # SKIPPED reason or data-loss advisory
```

### Disk / Volume Types

```python
@dataclass
class WindowsDiskInfo:
    id: str                        # "0" / "1"
    name: str                      # FriendlyName
    size_bytes: int
    model: str | None
    serial: str | None
    partition_style: str | None    # "GPT" / "MBR" / "RAW"
    health_status: str | None
    mount_points: list[str]
    is_pool_member: bool

@dataclass
class WindowsVolumeInfo:
    id: str                        # "X" or virtual-disk friendly name
    name: str
    size_bytes: int
    used_bytes: int | None
    filesystem: str | None         # "NTFS" / "ReFS" / "FAT32"
    mount_point: str | None        # "X:" for partitions
    pool: str | None
    health_status: str | None

@dataclass
class WindowsPoolSpec:
    name: str
    disks: list[str]               # disk numbers as strings
    resiliency: str | None = None  # "Mirror" / "Parity" / "Simple"

@dataclass
class WindowsVolumeSpec:
    name: str
    size_bytes: int
    pool: str | None = None        # storage-spaces flow when set
    filesystem: str = "NTFS"
    resiliency: str | None = None
    disk_id: str | None = None     # required for partition flow
```

### Exceptions

```python
class WindowsStorageQueryError(Exception):
    """Raised when a Get-Disk / Get-Volume / Get-StoragePool query
    fails unexpectedly."""
```

---

## Pool / Volume Mapping

| Contract verb | Windows command surface | Idempotency probe |
|---|---|---|
| `list_disks` | `Get-Disk \| ConvertTo-Json` + `Get-PhysicalDisk -CanPool` | n/a |
| `list_volumes` | `Get-Volume \| ConvertTo-Json` + `Get-VirtualDisk` | n/a |
| `create_volume` (pool) | `New-Volume -StoragePoolFriendlyName ... -FriendlyName ... -Size ...` | `Get-VirtualDisk[FriendlyName]` |
| `create_volume` (partition) | `New-Partition -DiskNumber ... -Size ... -AssignDriveLetter \| Format-Volume ...` | n/a (always proceeds) |
| `delete_volume` (drive letter) | `Get-Partition -DriveLetter ... \| Remove-Partition -Confirm:$false` | `Get-Volume[DriveLetter]` |
| `delete_volume` (virtual disk) | `Remove-VirtualDisk -FriendlyName ... -Confirm:$false` | `Get-VirtualDisk[FriendlyName]` |
| `resize_volume` | `Resize-Partition -DriveLetter ... -Size ...` | `Get-Volume[DriveLetter].Size` within 1 MiB |
| `create_pool` | `New-StoragePool -FriendlyName ... -StorageSubSystemFriendlyName ... -PhysicalDisks (...)` | `Get-StoragePool -FriendlyName ...` |
| `delete_pool` | `Remove-StoragePool -FriendlyName ... -Confirm:$false` | `Get-StoragePool -FriendlyName ...` |
| `add_to_pool` | `Add-PhysicalDisk -StoragePoolFriendlyName ... -PhysicalDisks (...)` | `Get-PhysicalDisk -StoragePoolFriendlyName ...` membership |
| `remove_from_pool` | `Remove-PhysicalDisk -StoragePoolFriendlyName ... -PhysicalDisks (...) -Confirm:$false` | `Get-PhysicalDisk -StoragePoolFriendlyName ...` membership |
| `get_volume_info` | `list_volumes` lookup | n/a |
| `get_disk_info` | `list_disks` lookup | n/a |

### Volume Identifier Syntax

* **Drive letter** (``"X"``, ``"X:"``, ``"X:\\"``): used for raw
  partitions and any volume the OS has assigned a letter to.  The
  manager normalises every form to the bare letter for cmdlet
  parameters and renders ``"<letter>:"`` on the result's
  ``mount_point`` field.
* **Friendly name** (``"Data"``, ``"OS-Mirror"``): used for
  Storage-Spaces virtual disks that are addressable by name but have
  no drive letter assigned.

The manager dispatches `delete_volume` between the two flows by
checking whether ``volume_id`` looks like a single-letter drive
identifier (see the helper ``_looks_like_drive_letter``).

### Pool Identifier Syntax

Pools are identified by their friendly name (a single string).  This
matches Microsoft's Storage Spaces convention and the cross-OS contract
(``pool_name: str``).

### Shrink-Support Matrix

| Contained filesystem | Shrink support |
|---|---|
| NTFS | Supported via `Resize-Partition -Size` (which calls into the NTFS shrink support) |
| ReFS | NOT supported — ReFS volumes cannot be shrunk |
| FAT32 / exFAT | NOT supported via `Resize-Partition` |
| Storage-Spaces virtual disk | Resize is via `Resize-VirtualDisk` (NOT `Resize-Partition`); reserved for a follow-up batch |

---

## Command Mappings

### Write Commands (elevation honoured)

| Operation | PowerShell script |
|---|---|
| `create_volume` (pool) | `New-Volume -StoragePoolFriendlyName '...' -FriendlyName '...' -Size <bytes> -ResiliencySettingName '...' -FileSystem '...'` |
| `create_volume` (partition) | `New-Partition -DiskNumber <n> -Size <bytes> -AssignDriveLetter | Format-Volume -FileSystem '...' -NewFileSystemLabel '...' -Confirm:$false` |
| `delete_volume` (drive letter) | `Get-Partition -DriveLetter '...' | Remove-Partition -Confirm:$false` |
| `delete_volume` (virtual disk) | `Remove-VirtualDisk -FriendlyName '...' -Confirm:$false` |
| `resize_volume` | `Resize-Partition -DriveLetter '...' -Size <bytes>` |
| `create_pool` | `New-StoragePool -FriendlyName '...' -StorageSubSystemFriendlyName '...' -PhysicalDisks (Get-PhysicalDisk -CanPool $True | Where-Object Number -in @(<numbers>))` |
| `delete_pool` | `Remove-StoragePool -FriendlyName '...' -Confirm:$false` |
| `add_to_pool` | `Add-PhysicalDisk -StoragePoolFriendlyName '...' -PhysicalDisks (Get-PhysicalDisk | Where-Object Number -eq <n>)` |
| `remove_from_pool` | `Remove-PhysicalDisk -StoragePoolFriendlyName '...' -PhysicalDisks (...) -Confirm:$false` |

Every script is wrapped in
``["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
"<script>"]``.

### Read Commands (no elevation)

| Operation | PowerShell script |
|---|---|
| `list_disks` (Get-Disk) | `Get-Disk | Select-Object Number, FriendlyName, Size, HealthStatus, OperationalStatus, PartitionStyle, SerialNumber, Model | ConvertTo-Json -Compress -Depth 5` |
| `list_disks` (CanPool) | `Get-PhysicalDisk | Select-Object Number, CanPool | ConvertTo-Json -Compress -Depth 5` |
| `list_volumes` (Get-Volume) | `Get-Volume | Select-Object DriveLetter, FileSystemLabel, FileSystemType, SizeRemaining, Size, HealthStatus, FileSystem | ConvertTo-Json -Compress -Depth 5` |
| `list_volumes` (Get-VirtualDisk) | `Get-VirtualDisk | Select-Object FriendlyName, Size, ResiliencySettingName, HealthStatus, ... | ConvertTo-Json -Compress -Depth 5` |
| `_pool_exists` | `Get-StoragePool -FriendlyName '...' -ErrorAction SilentlyContinue | Select-Object FriendlyName | ConvertTo-Json -Compress -Depth 5` |
| `_disk_in_pool` | `Get-PhysicalDisk -StoragePoolFriendlyName '...' | Select-Object Number | ConvertTo-Json -Compress -Depth 5` |

All writes pass `elevated=True` when the config enables it.  Reads
never elevate.

---

## Output Parsing

### `Get-Disk | ConvertTo-Json`

Each row carries:

* ``Number`` (int) — used as the disk's ``id`` (rendered as a string).
* ``FriendlyName`` (str) — used as ``name``.
* ``Size`` (int) — total bytes.
* ``PartitionStyle`` (``"GPT"`` / ``"MBR"`` / ``"RAW"``).
* ``Model``, ``SerialNumber``, ``HealthStatus``,
  ``OperationalStatus``.

### `Get-Volume | ConvertTo-Json`

Each row carries ``DriveLetter``, ``FileSystemLabel``,
``FileSystemType`` (``"NTFS"`` / ``"ReFS"`` / ``"FAT32"`` / ...),
``Size``, ``SizeRemaining``, ``HealthStatus``, ``FileSystem``.

The parser keys volumes by ``DriveLetter`` when present; otherwise it
falls back to ``FileSystemLabel``.  Volumes with neither are skipped
(typical for hidden recovery partitions that ``Get-Volume`` reports
without a usable identifier).

### `ConvertTo-Json` Single-Element Coercion

PowerShell's ``ConvertTo-Json`` drops the array wrapper for
single-element pipelines: a one-row ``Get-Disk`` emits ``{"Number":
0,...}`` rather than ``[{"Number": 0,...}]``.  Every parser in this
module normalises the shape via
``_normalize_to_list`` before walking.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_volume` (pool) | Virtual disk with same friendly name and matching size | `Get-VirtualDisk` |
| `create_volume` (partition) | n/a — always proceeds | n/a |
| `delete_volume` (drive letter) | Drive letter absence | `Get-Volume` |
| `delete_volume` (virtual disk) | Friendly-name absence | `Get-VirtualDisk` |
| `resize_volume` | Size match within 1 MiB | `Get-Volume` |
| `create_pool` | Pool with same friendly name | `Get-StoragePool -FriendlyName ...` |
| `delete_pool` | Pool friendly-name absence | `Get-StoragePool -FriendlyName ...` |
| `add_to_pool` | Disk number already a pool member | `Get-PhysicalDisk -StoragePoolFriendlyName ...` |
| `remove_from_pool` | Disk number not a pool member | `Get-PhysicalDisk -StoragePoolFriendlyName ...` |

The 1-MiB tolerance on ``create_volume`` (pool flow) and
``resize_volume`` mirrors the Ubuntu and macOS specs.

---

## Data-Loss Advisories

| Verb | Advisory text |
|---|---|
| `delete_volume` (drive letter) | `"volume <letter:> was removed; all data on the volume has been destroyed"` |
| `delete_volume` (virtual disk) | `"virtual disk <name> was removed; all data on the underlying volumes has been destroyed"` |
| `delete_pool` | `"storage pool <name> was removed; every virtual disk in the pool has been destroyed"` |
| `remove_from_pool` | `"disk <id> was removed from storage pool <pool>; the physical-disk association has been wiped"` |

Advisories surface on `SUCCESS` results in both `message` and
`detail` so callers that already index on `detail` continue to see
them.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsStorageQueryError` | Reserved for stricter read paths added in a follow-up batch.  Wave 1 reads return empty / `None` on probe failure. |

All write failures are surfaced as
`WindowsStorageActionResult(status=FAILED, ...)` carrying the runner's
exit code, stdout, and stderr.  Writes never raise for routine I/O.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system.*

### Property 1: list_disks Argv

*For any* call to `list_disks()`, the runner sees a PowerShell argv
whose script begins with ``"Get-Disk "``.

**Validates: Requirements 1.1**

### Property 2: list_disks Pool Membership

*For any* disk reported by ``Get-Disk`` whose corresponding
``Get-PhysicalDisk`` row has ``CanPool=False``, ``is_pool_member``
MUST be True.

**Validates: Requirements 1.3**

### Property 3: list_volumes Pool Attribution

*For any* volume whose ``FileSystemLabel`` matches a
``Get-VirtualDisk[FriendlyName]`` row, ``WindowsVolumeInfo.pool`` MUST
be that virtual disk's ``StoragePool.FriendlyName``.

**Validates: Requirements 2.5**

### Property 4: create_volume Pool Flow Argv

*For any* `WindowsVolumeSpec(name=N, pool=P, size_bytes=S,
filesystem=F, resiliency=R)`, the pool-flow script MUST be exactly
``f"New-Volume -StoragePoolFriendlyName '{P}' -FriendlyName '{N}'
-Size {S} -ResiliencySettingName '{R}' -FileSystem '{F}'"``.

**Validates: Requirements 3.1**

### Property 5: create_volume Partition Flow Argv

*For any* `WindowsVolumeSpec(name=N, pool=None, disk_id=D,
size_bytes=S, filesystem=F)`, the partition-flow script MUST be
exactly ``f"New-Partition -DiskNumber {D} -Size {S}
-AssignDriveLetter | Format-Volume -FileSystem '{F}'
-NewFileSystemLabel '{N}' -Confirm:$false"``.

**Validates: Requirements 3.2**

### Property 6: create_volume Idempotency

*For any* virtual disk with the same friendly name and matching size,
the pool-flow `create_volume()` MUST return SKIPPED without
dispatching ``New-Volume``.

**Validates: Requirements 3.4**

### Property 7: delete_volume Dispatch on Drive Letter

*For any* `volume_id` that looks like a single-letter drive
identifier (with or without a trailing colon),
`delete_volume(volume_id)` MUST dispatch the partition-flow script
``f"Get-Partition -DriveLetter '{letter}' | Remove-Partition
-Confirm:$false"``.

**Validates: Requirements 4.1**

### Property 8: delete_volume Dispatch on Friendly Name

*For any* `volume_id` that does NOT look like a drive letter,
`delete_volume(volume_id)` MUST dispatch the virtual-disk-flow script
``f"Remove-VirtualDisk -FriendlyName '{volume_id}' -Confirm:$false"``.

**Validates: Requirements 4.2**

### Property 9: delete_volume Advisory

*For any* successful `delete_volume()` call, the result's `message`
MUST contain the substring ``"destroyed"``.

**Validates: Requirements 4.4**

### Property 10: resize_volume Argv

*For any* `(volume_id, size_bytes)` pair where ``volume_id`` resolves
to a drive letter ``L``, `resize_volume()` MUST dispatch
``f"Resize-Partition -DriveLetter '{L}' -Size {size_bytes}"``.

**Validates: Requirements 5.1**

### Property 11: resize_volume Idempotency

*For any* volume whose current size is within 1 MiB of `size_bytes`,
`resize_volume()` MUST return SKIPPED.

**Validates: Requirements 5.2**

### Property 12: create_pool Argv

*For any* `WindowsPoolSpec(name=N, disks=[D1, D2])`, `create_pool()`
MUST dispatch a script whose body matches ``f"New-StoragePool
-FriendlyName '{N}' -StorageSubSystemFriendlyName 'Windows Storage*'
-PhysicalDisks (Get-PhysicalDisk -CanPool $True | Where-Object Number
-in @({D1},{D2}))"``.

**Validates: Requirements 6.1**

### Property 13: create_pool Idempotency

*For any* pool friendly name already present in
`Get-StoragePool`, `create_pool()` MUST return SKIPPED.

**Validates: Requirements 6.2**

### Property 14: delete_pool Argv

*For any* `pool_name=P`, `delete_pool()` MUST dispatch
``f"Remove-StoragePool -FriendlyName '{P}' -Confirm:$false"``.

**Validates: Requirements 7.1**

### Property 15: add_to_pool Argv

*For any* `(pool_name=P, disk_id=D)`, `add_to_pool()` MUST dispatch
``f"Add-PhysicalDisk -StoragePoolFriendlyName '{P}' -PhysicalDisks
(Get-PhysicalDisk | Where-Object Number -eq {D})"``.

**Validates: Requirements 8.1**

### Property 16: remove_from_pool Argv

*For any* `(pool_name=P, disk_id=D)`, `remove_from_pool()` MUST
dispatch ``f"Remove-PhysicalDisk -StoragePoolFriendlyName '{P}'
-PhysicalDisks (Get-PhysicalDisk | Where-Object Number -eq {D})
-Confirm:$false"``.

**Validates: Requirements 9.1**

### Property 17: remove_from_pool Advisory

*For any* successful `remove_from_pool()` call, the result's
`message` MUST contain the substring ``"wiped"``.

**Validates: Requirements 9.3**

### Property 18: PowerShell Argv Wrapper

*For any* PowerShell script the manager dispatches, the wrapping argv
MUST be exactly
``[powershell_executable, "-NoProfile", "-NonInteractive",
"-Command", <script>]``.

**Validates: Requirements 12.1**

### Property 19: Single-Quote Escaping

*For any* caller-supplied string interpolated into a single-quoted
PowerShell literal, every embedded single quote MUST be doubled.

**Validates: Requirements 12.2**

### Property 20: Dry-Run Safety

*For any* write verb invoked with `dry_run=True`, the runner MUST NOT
be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 3.7, 4.5, 5.4, 6.5, 7.4, 8.4, 9.4**

### Property 21: Elevation for Writes

*For any* write verb with `elevated=True` in the config, every runner
invocation MUST receive `elevated=True`.

**Validates: Requirements 13.1**

### Property 22: No Elevation for Reads

*For any* read verb (and any idempotency probe), the runner MUST
NEVER be invoked with `elevated=True`, regardless of config.

**Validates: Requirements 13.2**

### Property 23: Error Surface

*For any* non-zero exit from a write command, the returned
`WindowsStorageActionResult` MUST have `status=FAILED` and carry the
runner's exit code and stderr.

**Validates: Requirements 3.6**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
├── featurebase.py
├── filesystemmanagement.py
└── storagemanagement.py        ← WindowsStorageManager + types

source/testroots/testbase/ccai/tests/sysman/windows/
└── test_storagemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.windows.storagemanagement import (
    WindowsPoolSpec,
    WindowsStorageManager,
    WindowsStorageManagerConfig,
    WindowsVolumeSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsStorageManagerConfig(elevated=True, dry_run=False)
storage = WindowsStorageManager(runner=runner, config=config)

# Build a 2-disk Mirror pool
pool_spec = WindowsPoolSpec(
    name="primary-pool",
    disks=["1", "2"],
    resiliency="Mirror",
)
storage.create_pool(spec=pool_spec)

# Carve a 500 GB Data volume out of it
vol_spec = WindowsVolumeSpec(
    name="Data",
    pool="primary-pool",
    size_bytes=500 * 1024 * 1024 * 1024,
    resiliency="Mirror",
    filesystem="NTFS",
)
storage.create_volume(spec=vol_spec)

# Or create a raw partition on disk 3 with no pool
part_spec = WindowsVolumeSpec(
    name="Scratch",
    disk_id="3",
    size_bytes=100 * 1024 * 1024 * 1024,
    filesystem="NTFS",
)
storage.create_volume(spec=part_spec)
```

### Dry-Run Mode

```python
config = WindowsStorageManagerConfig(elevated=True, dry_run=True)
storage = WindowsStorageManager(runner=runner, config=config)

result = storage.delete_pool(pool_name="primary-pool")
assert result.status == WindowsStorageActionStatus.DRY_RUN
print(result.command[-1])
# "Remove-StoragePool -FriendlyName 'primary-pool' -Confirm:$false"
```

### SSH Execution with Aspects

```python
from ccai.sysman.windows.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)

runner = SSHCommandRunner(
    SSHRunnerConfig(host="windows-build", username="admin"),
)
storage = WindowsStorageManager(
    runner=runner,
    config=WindowsStorageManagerConfig(elevated=True),
)
for vol in storage.list_volumes():
    print(vol.id, vol.size_bytes, vol.pool)
```

---

## Testing Strategy

* All tests use a `MockCommandRunner` keyed on the script string (the
  last argv element).  A ``script_prefix_results`` list lets tests
  match on substrings of the script body so the same fixture can serve
  the read probes that bracket every write verb.
* Argv-construction tests assert the exact PowerShell script body for
  every write verb, including the magic ``-Confirm:$false`` and the
  ``-StorageSubSystemFriendlyName 'Windows Storage*'`` token.
* Idempotency tests cover every verb's SKIPPED path.
* Dry-run tests prove the runner is not invoked.
* Elevation tests cover every write verb passing `elevated=True` and
  every read verb (including the probes) passing `elevated=False`.
* Helper tests cover ``_disk_number_token``,
  ``_render_disk_number_list``, ``_normalise_drive_letter``, and
  ``_looks_like_drive_letter``.
* Failure surface is tested by seeding the runner with a non-zero
  exit on the write step.
* No test requires a live PowerShell process or administrator rights.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `list_disks` | `Get-Disk` + `Get-PhysicalDisk` |
| `list_volumes` | `Get-Volume` + `Get-VirtualDisk` |
| `create_volume` | `New-Volume` (pool) / `New-Partition + Format-Volume` (partition) |
| `delete_volume` | `Remove-Partition` (drive letter) / `Remove-VirtualDisk` (friendly name) |
| `resize_volume` | `Resize-Partition` |
| `create_pool` | `New-StoragePool` |
| `delete_pool` | `Remove-StoragePool` |
| `add_to_pool` | `Add-PhysicalDisk` |
| `remove_from_pool` | `Remove-PhysicalDisk` |
| `get_volume_info` | `list_volumes` lookup by id |
| `get_disk_info` | `list_disks` lookup by id |

---

## Extensions Beyond the Contract

None in this Windows implementation.  Filesystem creation as a
separate verb (the `New-Partition + Format-Volume` pipeline already
formats the partition), drive-letter assignment / access-path
management, BitLocker, Storage Replicas, and ReFS-specific operations
are intentionally deferred.

The feature also intentionally omits a SMART resolver on
`WindowsDiskInfo.health_status`; the field is populated from
`Get-Disk.HealthStatus` directly but a SMART-attribute decoder is
reserved for a follow-up batch.

---

## Future Considerations

* Add `Resize-VirtualDisk` support so `resize_volume` can target
  Storage-Spaces virtual disks by friendly name.
* Surface a strict-read mode that raises `WindowsStorageQueryError`
  instead of returning empty lists / `None`.
* Add a `force` parameter on `remove_from_pool` that adds
  `-Force` / `-AsJob` for asynchronous removal.
* Wire `WindowsDiskInfo.mount_points` from `Get-Partition |
  Get-PartitionAccessPath` so a disk's drive letters surface on the
  disk row.
* Honour `WindowsPoolSpec.resiliency` by setting
  ``-ResiliencySettingNameDefault`` on the new pool (currently
  preserved on the result for audit logging only).

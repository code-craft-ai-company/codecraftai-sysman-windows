# Requirements

## Introduction

The storage management feature provides the `codecraftai-sysman-windows`
package with the ability to enumerate disks and volumes, create /
delete / resize volumes (both partition-style and Storage-Spaces
virtual disks), manage Storage Pools (the cross-OS contract's
"pool"), add and remove physical disks from a Storage Pool, and query
detailed disk and volume metadata on Windows systems.  This spec
implements the storage-management contract defined at
`codecraftai-sysman-common/specs/contracts/storage-management/contract.md`
for Windows using the PowerShell Storage cmdlets (`Get-Disk`,
`Get-Volume`, `Get-Partition`, `Get-PhysicalDisk`, `Get-StoragePool`,
`Get-VirtualDisk`, `New-Volume`, `Remove-Partition`,
`Resize-Partition`, `Format-Volume`, `New-Partition`,
`Remove-VirtualDisk`, `New-StoragePool`, `Remove-StoragePool`,
`Add-PhysicalDisk`, `Remove-PhysicalDisk`).

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* volumes should exist, *which* disks
should back a pool, or *what* size a volume should claim — it provides
the capability to *manipulate Windows storage state* reliably,
idempotently, and with full auditability.  All system commands are
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction.

The implementation dispatches between two flows on `create_volume` /
`delete_volume`:

* **Pool flow** (storage-spaces) — used when the spec references a
  pool by name; cmdlets are `New-Volume
  -StoragePoolFriendlyName ...` and `Remove-VirtualDisk`.
* **Partition flow** — used when the spec references a disk number
  directly; cmdlets are `New-Partition + Format-Volume` and
  `Get-Partition | Remove-Partition`.

Filesystem creation per se does not exist on Windows the way it does
on Linux: `New-Volume` and `Format-Volume` create a filesystem on the
new partition / virtual disk in one step.  Mount-point management
(drive-letter assignments, access paths) lives in
`filesystem-management`; this feature stops at the volume boundary.

---

## Requirements

### Requirement 1: Enumerate Physical Disks

**User Story:** As a maintenance coordinator, I want to enumerate every
physical disk on the system with its size, friendly name, partition
style, and pool membership, so that I can decide which disks are
eligible for inclusion in a Storage Pool.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_disks()` THE SYSTEM SHALL dispatch
   `Get-Disk | Select-Object Number, FriendlyName, Size,
   HealthStatus, OperationalStatus, PartitionStyle, SerialNumber, Model
   | ConvertTo-Json -Compress -Depth 5` through the command runner
   WITHOUT elevation.
2. THE SYSTEM SHALL return one `WindowsDiskInfo` per row in the
   parsed JSON, with `id` rendered as the string of `Number`.
3. THE SYSTEM SHALL set ``WindowsDiskInfo.is_pool_member=True`` when
   ``Get-PhysicalDisk`` reports ``CanPool=False`` for the same disk
   number — a disk that cannot be re-pooled is, by construction,
   already in a pool (or is a system / boot disk).
4. WHEN ``Get-Disk`` exits non-zero THE SYSTEM SHALL return an empty
   list rather than raise.
5. WHEN ``ConvertTo-Json`` collapses a single-row response into a
   bare object THE SYSTEM SHALL normalise it back into a one-element
   list before parsing.

---

### Requirement 2: Enumerate Volumes

**User Story:** As a maintenance coordinator, I want to enumerate every
volume on the system, so that I can compare actual storage state
against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_volumes()` THE SYSTEM SHALL
   dispatch `Get-Volume | Select-Object DriveLetter, FileSystemLabel,
   FileSystemType, SizeRemaining, Size, HealthStatus, FileSystem |
   ConvertTo-Json -Compress -Depth 5` through the command runner
   WITHOUT elevation.
2. THE SYSTEM SHALL also dispatch `Get-VirtualDisk | ... |
   ConvertTo-Json` to attribute pool ownership.
3. THE SYSTEM SHALL emit one `WindowsVolumeInfo` per `Get-Volume`
   row.  When ``DriveLetter`` is non-empty the volume's ``id`` is the
   drive letter; otherwise the row is keyed by ``FileSystemLabel`` and
   skipped when the label is also empty.
4. THE SYSTEM SHALL populate ``WindowsVolumeInfo.used_bytes`` as
   ``Size - SizeRemaining`` when both fields are present.
5. THE SYSTEM SHALL populate ``WindowsVolumeInfo.pool`` from
   ``Get-VirtualDisk[FriendlyName].StoragePool.FriendlyName`` when the
   volume's ``FileSystemLabel`` matches an existing virtual-disk
   friendly name.
6. WHEN either probe fails THE SYSTEM SHALL silently omit the failing
   source.

---

### Requirement 3: Create a Volume

**User Story:** As a maintenance coordinator, I want to create a
volume — either inside an existing Storage Pool or as a partition on a
plain disk — so that an application can claim it.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsVolumeSpec` whose `pool`
   is non-empty THE SYSTEM SHALL dispatch `New-Volume
   -StoragePoolFriendlyName <pool> -FriendlyName <name> -Size
   <bytes> -ResiliencySettingName <r> -FileSystem <fs>` through the
   command runner with `elevated=True` when configured, where ``r``
   defaults to the config's ``default_resiliency`` (``Mirror``).
2. WHEN the coordinator provides a `WindowsVolumeSpec` whose `pool`
   is `None` AND `disk_id` is non-empty THE SYSTEM SHALL dispatch
   `New-Partition -DiskNumber <n> -Size <bytes> -AssignDriveLetter |
   Format-Volume -FileSystem <fs> -NewFileSystemLabel <name>
   -Confirm:$false` instead.
3. WHEN both `pool` and `disk_id` are absent THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=FAILED)` with a descriptive
   `message`.
4. WHEN the pool flow runs and ``Get-VirtualDisk`` already reports a
   virtual disk with the same friendly name and a matching size
   (within 1 MiB) THE SYSTEM SHALL skip execution and return
   `WindowsStorageActionResult(status=SKIPPED)`.
5. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=SUCCESS)` populated with
   `target=<name>`, `pool=<pool>` (when applicable), and
   `volume=<name>`.
6. WHEN the operation fails THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.

---

### Requirement 4: Delete a Volume

**User Story:** As a maintenance coordinator, I want to delete a
volume by id (drive letter or virtual-disk friendly name), so that
decommissioned data is reclaimed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_volume(volume_id)` AND
   ``volume_id`` looks like a drive letter (``"X"`` / ``"X:"``) THE
   SYSTEM SHALL dispatch `Get-Partition -DriveLetter <l> |
   Remove-Partition -Confirm:$false` through the command runner with
   `elevated=True` when configured.
2. WHEN ``volume_id`` does NOT look like a drive letter THE SYSTEM
   SHALL dispatch `Remove-VirtualDisk -FriendlyName <id>
   -Confirm:$false` instead.
3. WHEN no volume matching ``volume_id`` exists THE SYSTEM SHALL skip
   execution and return
   `WindowsStorageActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=SUCCESS)` whose `message`
   carries a data-loss advisory.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.

---

### Requirement 5: Resize a Volume

**User Story:** As a maintenance coordinator, I want to grow or shrink
a partition-style volume by drive letter, so that capacity tracks
changing workload.

#### Acceptance Criteria

1. WHEN the coordinator calls `resize_volume(volume_id, size_bytes)`
   THE SYSTEM SHALL dispatch `Resize-Partition -DriveLetter <l>
   -Size <size_bytes>` through the command runner with `elevated=True`
   when configured.
2. WHEN the partition's current size is within 1 MiB of `size_bytes`
   THE SYSTEM SHALL skip execution and return
   `WindowsStorageActionResult(status=SKIPPED)`.
3. WHEN no volume matching `volume_id` exists THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=FAILED)` with a descriptive
   `message`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.
5. THE SYSTEM SHALL document that resizing a Storage-Spaces virtual
   disk by friendly name is a separate verb reserved for a follow-up
   batch (``Resize-VirtualDisk -FriendlyName ... -Size ...``).

---

### Requirement 6: Create a Storage Pool

**User Story:** As a maintenance coordinator, I want to create a
Storage Pool from one or more disks, so that virtual disks / volumes
can be carved out of it with a chosen resiliency level.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsPoolSpec` with `name` and
   `disks` THE SYSTEM SHALL dispatch `New-StoragePool -FriendlyName
   <name> -StorageSubSystemFriendlyName 'Windows Storage*'
   -PhysicalDisks (Get-PhysicalDisk -CanPool $True | Where-Object
   Number -in @(<numbers>))` through the command runner with
   `elevated=True` when configured.
2. WHEN a Storage Pool with the same friendly name already exists THE
   SYSTEM SHALL skip execution and return
   `WindowsStorageActionResult(status=SKIPPED)`.
3. THE SYSTEM SHALL preserve `WindowsPoolSpec.resiliency` on the
   result for audit logging but SHALL NOT pass it to
   `New-StoragePool` (the cmdlet does not accept a pool-level
   resiliency; the field is consumed by subsequent `New-Volume`
   calls).
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=SUCCESS)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.

---

### Requirement 7: Delete a Storage Pool

**User Story:** As a maintenance coordinator, I want to delete a
Storage Pool by friendly name, so that the underlying disks are
reclaimed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_pool(pool_name)` THE SYSTEM
   SHALL dispatch `Remove-StoragePool -FriendlyName <name>
   -Confirm:$false` through the command runner with `elevated=True`
   when configured.
2. WHEN no pool with that name exists THE SYSTEM SHALL skip execution
   and return `WindowsStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=SUCCESS)` whose `message`
   carries a data-loss advisory.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.

---

### Requirement 8: Add a Disk to a Storage Pool

**User Story:** As a maintenance coordinator, I want to add a physical
disk to an existing Storage Pool, so that the pool's free capacity
grows.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_to_pool(pool_name, disk_id)` THE
   SYSTEM SHALL dispatch `Add-PhysicalDisk -StoragePoolFriendlyName
   <pool> -PhysicalDisks (Get-PhysicalDisk | Where-Object Number -eq
   <n>)` through the command runner with `elevated=True` when
   configured.
2. WHEN ``disk_id`` is already a member of ``pool_name`` THE SYSTEM
   SHALL skip execution and return
   `WindowsStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.

---

### Requirement 9: Remove a Disk from a Storage Pool

**User Story:** As a maintenance coordinator, I want to evict a
physical disk from a Storage Pool, so that the disk can be reused or
decommissioned.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_from_pool(pool_name, disk_id)`
   THE SYSTEM SHALL dispatch `Remove-PhysicalDisk
   -StoragePoolFriendlyName <pool> -PhysicalDisks (Get-PhysicalDisk |
   Where-Object Number -eq <n>) -Confirm:$false` through the command
   runner with `elevated=True` when configured.
2. WHEN ``disk_id`` is not a member of ``pool_name`` THE SYSTEM SHALL
   skip execution and return
   `WindowsStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsStorageActionResult(status=SUCCESS)` whose `message`
   carries a data-loss advisory.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsStorageActionResult(status=DRY_RUN)`.

---

### Requirement 10: Get Volume Info

**User Story:** As a maintenance coordinator, I want to look up a
single volume by id, so that I can branch on its size, filesystem, and
pool without parsing `Get-Volume` output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_volume_info(volume_id)` THE SYSTEM
   SHALL invoke `list_volumes()` and return the matching entry.
2. THE SYSTEM SHALL accept both bare drive letters (``"X"``) and the
   ``"X:"`` form.
3. WHEN no entry matches THE SYSTEM SHALL return `None`.

---

### Requirement 11: Get Disk Info

**User Story:** As a maintenance coordinator, I want to look up a
single disk by id, so that I can branch on its size, partition style,
and pool membership.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_disk_info(disk_id)` THE SYSTEM
   SHALL invoke `list_disks()` and return the matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.

---

### Requirement 12: PowerShell Argv Shape

**User Story:** As a maintenance coordinator, I want every PowerShell
script the manager dispatches to be shielded from `$PROFILE` mutations
and never to block on interactive input, so that automated runs are
reliably non-interactive.

#### Acceptance Criteria

1. WHEN the manager dispatches any PowerShell script THE SYSTEM SHALL
   wrap the script in the canonical argv ``["powershell.exe",
   "-NoProfile", "-NonInteractive", "-Command", "<script>"]``.
2. WHEN the manager interpolates a caller-supplied string into a
   single-quoted PowerShell literal THE SYSTEM SHALL escape every
   embedded single quote by doubling it (PowerShell's quoting rule).
3. THE SYSTEM SHALL accept a config override
   ``powershell_executable`` for hosts where ``powershell.exe`` is not
   on PATH or where ``pwsh.exe`` is preferred.

---

### Requirement 13: Elevation Policy

**User Story:** As a maintenance coordinator, I want every mutating
PowerShell call to run with administrator privileges, so that
``New-Volume`` / ``Remove-Partition`` / ``Resize-Partition`` /
``New-StoragePool`` / ``Remove-StoragePool`` / ``Add-PhysicalDisk`` /
``Remove-PhysicalDisk`` / ``Remove-VirtualDisk`` succeed on a
standard system where the process does not run as administrator,
while reads stay unprivileged.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write verb
   (`create_volume`, `delete_volume`, `resize_volume`, `create_pool`,
   `delete_pool`, `add_to_pool`, `remove_from_pool`).
2. WHEN performing read-only operations (`list_disks`,
   `list_volumes`, `get_volume_info`, `get_disk_info`, all
   idempotency probes) THE SYSTEM SHALL NEVER pass `elevated=True`.
3. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL NEVER pass `elevated=True`.

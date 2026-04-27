# Requirements

## Introduction

The filesystem management feature provides the `automojo-sysman-windows` package with
the ability to mount and unmount drive letters, manage persistent drive-letter
assignments, create and remove directories and symbolic links, change ownership and
permissions, and inspect file / volume metadata on Windows systems.  This spec
implements the filesystem-management contract defined at
`automojo-sysman-common/specs/contracts/filesystem-management/contract.md` for
Windows using PowerShell cmdlets (`New-PSDrive`, `Remove-PSDrive`, `Get-Volume`,
`Get-Partition`, `Set-Partition`, `Add-PartitionAccessPath`, `Get-Item`, `New-Item`,
`Remove-Item`, `Set-Acl`), the `icacls.exe` legacy tool for POSIX-mode-to-ACL
translation, and `mountvol.exe` where a volume GUID rather than a drive letter is
required.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
paths should be mounted or which permissions are correct — it provides the
capability to *manipulate filesystem state* reliably, idempotently, and with full
auditability.  Windows has no POSIX mode; the manager documents and uses a bundled
translation table that maps the abstract contract's integer `mode` to an approximate
ACL.  All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction.

---

## Requirements

### Requirement 1: Mount a Device

**User Story:** As a maintenance coordinator, I want to assign a drive letter to a
volume or mount a volume at an empty NTFS directory, so that the mounted filesystem
is available to the running system.

#### Acceptance Criteria

1. WHEN the coordinator provides a `device`, `mountpoint`, optional `fstype`, and
   optional `options` AND `mountpoint` is a single drive letter (e.g. `"Z:"`) THE
   SYSTEM SHALL dispatch a PowerShell script that calls
   `Get-Partition -DiskNumber <disk> -PartitionNumber <part> |
   Add-PartitionAccessPath -AccessPath '<letter>:'` through the runner with
   `elevated=True` when configured; the script derives `<disk>` and `<part>` from
   the `device` parameter when the caller passes a `\\?\Volume{GUID}\` or
   `Disk#Part#` identifier.
2. WHEN `mountpoint` is a directory path THE SYSTEM SHALL dispatch
   `Add-PartitionAccessPath -AccessPath '<mountpoint>'` instead, after verifying
   the directory exists and is empty.
3. WHEN the drive letter is already assigned to the same device THE SYSTEM SHALL
   skip execution and return a `WindowsFilesystemActionResult` with
   `status=SKIPPED`.
4. WHEN the mount succeeds THE SYSTEM SHALL return a `WindowsFilesystemActionResult`
   with `status=SUCCESS`.
5. WHEN the mount fails THE SYSTEM SHALL return a `WindowsFilesystemActionResult`
   with `status=FAILED` and the captured exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 2: Unmount a Device

**User Story:** As a maintenance coordinator, I want to remove a drive-letter
assignment from a volume, so that the volume is no longer accessible at that path.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmount(mountpoint, lazy)` and `mountpoint` is a
   drive letter THE SYSTEM SHALL dispatch `Remove-PartitionAccessPath -AccessPath
   '<letter>:' -Confirm:$false` through the runner with `elevated=True` when
   configured.
2. WHEN `mountpoint` is a directory THE SYSTEM SHALL dispatch
   `Remove-PartitionAccessPath -AccessPath '<mountpoint>' -Confirm:$false`.
3. WHEN the `lazy` flag is `True` THE SYSTEM SHALL issue the removal without
   attempting `Dismount-Volume` first, accepting that handles may still be open
   (Windows' drive-letter removal is itself asynchronous); `lazy=False` remains
   equivalent but the manager records a `detail` noting the flag has no additional
   effect on Windows.
4. WHEN the drive letter / mount directory is not currently assigned THE SYSTEM
   SHALL skip execution and return a `WindowsFilesystemActionResult` with
   `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `WindowsFilesystemActionResult`
   with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 3: Add a Persistent Mount-Table Entry

**User Story:** As a maintenance coordinator, I want a persistent drive-letter
assignment so that the mount survives reboots.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsFstabEntry` THE SYSTEM SHALL resolve
   the `device` field (which may be a volume GUID, disk/partition index, or
   volume label) to a partition via `Get-Partition` / `Get-Volume`, then dispatch
   `Add-PartitionAccessPath -DiskNumber <n> -PartitionNumber <p> -AccessPath
   '<mountpoint>'` through the runner with `elevated=True` when configured.
   Drive-letter assignments made this way are persistent by default on Windows.
2. WHEN an existing persistent assignment already matches the entry THE SYSTEM
   SHALL skip execution and return a `WindowsFilesystemActionResult` with
   `status=SKIPPED`.
3. WHEN the partition already has a drive letter that differs from the requested
   one THE SYSTEM SHALL first dispatch `Remove-PartitionAccessPath` for the old
   letter and then `Add-PartitionAccessPath` for the new one.
4. WHEN the operation succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the operation fails THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN` and a `detail`
   describing the intended partition assignment.

---

### Requirement 4: Remove a Persistent Mount-Table Entry

**User Story:** As a maintenance coordinator, I want to unassign a drive letter
persistently so the mount no longer appears on future boots.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_fstab_entry(device_or_mountpoint)` THE
   SYSTEM SHALL dispatch `Remove-PartitionAccessPath -AccessPath
   '<mountpoint_or_letter>' -Confirm:$false` through the runner with
   `elevated=True` when configured.
2. WHEN no assignment for the given access path exists THE SYSTEM SHALL skip
   execution and return a `WindowsFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 5: Query Current Mounts

**User Story:** As a maintenance coordinator, I want to list every active mount
so that idempotency guards and reporting workflows can reason about what is
currently mounted.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_mounts()` THE SYSTEM SHALL dispatch a
   PowerShell script that composes `Get-Volume | Select-Object
   DriveLetter,FileSystemLabel,FileSystem,DriveType,UniqueId,Path |
   ConvertTo-Json -Depth 3` through the command runner without elevation, and
   also dispatch `Get-PSDrive -PSProvider FileSystem | Select-Object
   Name,Root,Provider,DisplayRoot | ConvertTo-Json -Depth 3`.
2. WHEN both commands succeed THE SYSTEM SHALL merge the JSON arrays into a
   single list of `WindowsMountInfo` records with `device` (from `UniqueId` or
   `DisplayRoot`), `mountpoint` (from `DriveLetter` or `Path`), `fstype` (from
   `FileSystem`), and `options` (from `DriveType`).
3. WHEN either command fails THE SYSTEM SHALL raise a
   `WindowsFilesystemQueryError` carrying the exit code and stderr of the
   failing step.

---

### Requirement 6: Create a Directory

**User Story:** As a maintenance coordinator, I want to create a directory with
specific ACL owner, ACL group, and approximate mode, so that application data
paths exist before the application runs.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_directory(path, mode, owner, group)` THE
   SYSTEM SHALL dispatch `New-Item -ItemType Directory -Path '<path>' -Force`
   through the command runner with `elevated=True` when configured, then (when
   `mode` is not `None`) dispatch `icacls '<path>' /setowner '<owner>' /grant
   ...` translated from the POSIX mode via the bundled translation table, and
   (when `owner` is not `None`) dispatch `icacls '<path>' /setowner '<owner>'`.
2. WHEN the path already exists as a directory whose owner and ACL match the
   requested values THE SYSTEM SHALL skip execution and return a
   `WindowsFilesystemActionResult` with `status=SKIPPED`.  Note: because
   mode-to-ACL translation is approximate, the idempotency comparison checks
   ACL equivalence rather than bitwise equality.
3. WHEN the path exists but is not a directory THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
4. WHEN every dispatched command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS` and a `detail` noting
   which ACE pattern was applied from the translation table.
5. WHEN any dispatched command fails THE SYSTEM SHALL stop further dispatch and
   return a `WindowsFilesystemActionResult` with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Remove a Directory

**User Story:** As a maintenance coordinator, I want to remove a directory,
optionally recursively, so that obsolete directories are cleaned up.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_directory(path, recursive=False)` THE
   SYSTEM SHALL dispatch `Remove-Item -Path '<path>' -Force` through the runner
   with `elevated=True` when configured.
2. WHEN `recursive=True` THE SYSTEM SHALL dispatch `Remove-Item -Path '<path>'
   -Recurse -Force`.
3. WHEN the path does not exist THE SYSTEM SHALL skip execution and return a
   `WindowsFilesystemActionResult` with `status=SKIPPED`.
4. WHEN the path exists but is not a directory THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Create a Symbolic Link

**User Story:** As a maintenance coordinator, I want to create a symbolic link
from `link` to `target`, so that path indirections can be established reliably
on NTFS.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_symlink(link, target)` THE SYSTEM SHALL
   dispatch `New-Item -ItemType SymbolicLink -Path '<link>' -Target '<target>'`
   through the command runner with `elevated=True` when configured (creating
   symbolic links requires `SeCreateSymbolicLinkPrivilege`, granted to
   Administrators by default).
2. WHEN `link` already exists as a symbolic link pointing to `target` (detected
   via `Get-Item '<link>' | Select-Object LinkType,Target`) THE SYSTEM SHALL skip
   execution and return a `WindowsFilesystemActionResult` with `status=SKIPPED`.
3. WHEN `link` already exists as a symbolic link pointing to a different target
   THE SYSTEM SHALL dispatch `Remove-Item '<link>'` followed by `New-Item
   -ItemType SymbolicLink -Path '<link>' -Target '<target>'` (atomic replace is
   not a single operation on Windows).
4. WHEN `link` exists but is NOT a symbolic link THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 9: Remove a Symbolic Link

**User Story:** As a maintenance coordinator, I want to remove a symbolic link
so that an obsolete indirection is undone.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_symlink(link)` THE SYSTEM SHALL dispatch
   `Remove-Item -Path '<link>' -Force` through the command runner with
   `elevated=True` when configured, but only after verifying via `Get-Item` that
   `LinkType` is one of `SymbolicLink`, `Junction`, or `HardLink` and the path
   is not a regular file or directory.
2. WHEN the link does not exist THE SYSTEM SHALL skip execution and return a
   `WindowsFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path exists but is NOT a symbolic link THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Change Ownership

**User Story:** As a maintenance coordinator, I want to change the owner (and
approximate "group") of a path via NTFS ACLs, so that authorisation matches the
running service's identity.

#### Acceptance Criteria

1. WHEN the coordinator calls `chown(path, user, group)` THE SYSTEM SHALL
   dispatch `icacls '<path>' /setowner '<user>'` through the command runner with
   `elevated=True` when configured, and — because Windows does not have a POSIX
   primary-group concept — treat a non-`None` `group` as a request to grant that
   principal `FullControl`, dispatched as a subsequent `icacls '<path>' /grant
   '<group>:(OI)(CI)F'`.
2. WHEN the current owner already matches the requested `user` (queried via
   `Get-Acl '<path>' | Select-Object Owner`) AND (when `group` is not `None`) the
   current ACL already contains an equivalent grant THE SYSTEM SHALL skip
   execution and return a `WindowsFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path does not exist THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
4. WHEN every dispatched command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS`.
5. WHEN any dispatched command fails THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED` naming the failing step.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 11: Change Mode

**User Story:** As a maintenance coordinator, I want to change the approximate
POSIX mode of a path, so that file-system permissions match the policy for the
managed service as closely as Windows allows.

#### Acceptance Criteria

1. WHEN the coordinator calls `chmod(path, mode)` THE SYSTEM SHALL look up the
   POSIX `mode` in the bundled translation table and dispatch a sequence of
   `icacls '<path>' /inheritance:r`, `icacls '<path>' /remove ...`, and
   `icacls '<path>' /grant '<principal>:<permission_flags>'` calls that together
   realise the closest-equivalent ACL.  The translation table maps POSIX
   `owner/group/other` rwx bits to `OWNER/BUILTIN\Users/Everyone` principal
   triples with NTFS `R`/`W`/`X`/`F` flags.
2. WHEN the current ACL already matches the translation-table target
   (determined by `Get-Acl` comparison) THE SYSTEM SHALL skip execution and
   return a `WindowsFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path does not exist THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
4. WHEN every dispatched command succeeds THE SYSTEM SHALL return a
   `WindowsFilesystemActionResult` with `status=SUCCESS` and a `detail` recording
   the translation-table entry that was applied.
5. WHEN any dispatched command fails THE SYSTEM SHALL stop and return a
   `WindowsFilesystemActionResult` with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 12: Stat a Path

**User Story:** As a maintenance coordinator, I want to retrieve metadata about
a path, so that downstream logic can branch on type, mode-approximation, owner,
group, and size without having to parse raw cmdlet output.

#### Acceptance Criteria

1. WHEN the coordinator calls `stat(path)` THE SYSTEM SHALL dispatch `Get-Item
   -Force '<path>' | Select-Object
   FullName,Attributes,Length,LinkType,Target,Mode | ConvertTo-Json -Depth 3`
   through the command runner without elevation, followed by `Get-Acl '<path>' |
   Select-Object Owner | ConvertTo-Json -Depth 3`.
2. WHEN both commands succeed THE SYSTEM SHALL parse the JSON payloads into a
   `WindowsFileInfo` with `path_type` (from `Attributes`), `mode` (from the
   reverse of the translation table applied to the ACL), `owner` (from `Owner`),
   `group` (always `""`), `size_bytes` (from `Length` or `0` for directories),
   and `path` (from `FullName`).
3. WHEN `Get-Item` fails with `ItemNotFoundException` (stderr contains
   `ItemNotFoundException` or `Cannot find path`) THE SYSTEM SHALL return `None`.
4. WHEN either command exits non-zero for any other reason THE SYSTEM SHALL
   raise a `WindowsFilesystemQueryError`.

---

### Requirement 13: Query Disk Usage

**User Story:** As a maintenance coordinator, I want to query disk-usage for a
path's volume, so that capacity-planning workflows can branch on current
utilisation.

#### Acceptance Criteria

1. WHEN the coordinator calls `query_disk_usage(path)` THE SYSTEM SHALL
   dispatch a PowerShell script that determines the volume containing `path`
   (via `(Get-Item '<path>').PSDrive` or by matching the drive letter prefix),
   then calls `Get-Volume -DriveLetter <letter> | Select-Object
   Size,SizeRemaining,DriveType | ConvertTo-Json -Depth 3`.
2. WHEN the command succeeds THE SYSTEM SHALL compute `used_bytes = Size -
   SizeRemaining`, `use_percent = round(100 * used_bytes / Size)`, and populate
   a `WindowsDiskUsageInfo`.
3. WHEN the command fails THE SYSTEM SHALL raise a
   `WindowsFilesystemQueryError`.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all filesystem write
operations to run with Administrator privileges, so that mount / unmount / ACL
writes succeed on a standard Windows system where the process does not already
hold an Administrator token.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for every write.
2. WHEN performing read-only operations (get_mounts, stat, query_disk_usage) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of configuration.
3. WHEN the process already holds an Administrator token THE SYSTEM SHALL NOT
   pass `elevated=True` as a redundant request.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner to surface the resulting Windows
   `ERROR_ACCESS_DENIED` (exit code `5`) as a `WindowsFilesystemActionResult`
   with `status=FAILED` for writes or as a `WindowsFilesystemQueryError` for
   reads.

# Design

## Overview

The filesystem management feature is implemented as a single module,
`ccai.sysman.windows.filesystemmanagement`, that exposes a
`WindowsFilesystemManager` class.  This spec implements the filesystem-management
contract defined at
`codecraftai-sysman-common/specs/contracts/filesystem-management/contract.md` for
Windows using the PowerShell storage cmdlets (`Get-Volume`, `Get-Partition`,
`Add-PartitionAccessPath`, `Remove-PartitionAccessPath`), the file-system cmdlets
(`Get-Item`, `New-Item`, `Remove-Item`, `Get-Acl`, `Set-Acl`), and the
`icacls.exe` legacy tool for ACL grants.  Every verb translates to a
`powershell.exe -NoProfile -NonInteractive -Command <script>` invocation whose read
paths emit `ConvertTo-Json` for pure-Python parsing.

All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct .NET or WMI interop calls are made inside
the module.  An `IFileOperator` is *not* required because Windows mount-table state
is stored in the volume configuration, not in a flat file — all persistence goes
through cmdlets, not file edits.

Windows has no POSIX mode.  The manager bundles a **mode-to-ACL translation table**
that maps each of the 512 POSIX modes (0o000–0o777) to a canonical ACL pattern
expressed as a list of `icacls /grant` arguments.  The table is an approximation —
NTFS cannot perfectly express every POSIX combination — and the translation is
documented as an OS-specific extension of the cross-OS contract.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over WinRM / SSH (via `SSHCommandRunner`
configured for Windows).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/remote execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsFilesystemManager             │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsFilesystemManager                         │
│  ccai.sysman.windows.filesystemmanagement               │
│                                                             │
│  mount()              unmount()                             │
│  add_fstab_entry()    remove_fstab_entry()                  │
│  get_mounts()                                               │
│  create_directory()   remove_directory()                    │
│  create_symlink()     remove_symlink()                      │
│  chown()              chmod()   ← mode→ACL translation      │
│  stat()               query_disk_usage()                    │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - WinRM / SSH link   │
│ - UAC elevation     │       │ - reliability metrics│
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect  - RetryAspect  - TimeoutAspect        │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsFilesystemManager`

**Location:** `source/packages/ccai/sysman/windows/filesystemmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `mount` | `(device, mountpoint, fstype, options) → WindowsFilesystemActionResult` | `Add-PartitionAccessPath` |
| `unmount` | `(mountpoint, lazy) → WindowsFilesystemActionResult` | `Remove-PartitionAccessPath` |
| `add_fstab_entry` | `(entry) → WindowsFilesystemActionResult` | `Add-PartitionAccessPath` (persistent) |
| `remove_fstab_entry` | `(device_or_mountpoint) → WindowsFilesystemActionResult` | `Remove-PartitionAccessPath` |
| `get_mounts` | `() → list[WindowsMountInfo]` | `Get-Volume` + `Get-PSDrive` merge |
| `create_directory` | `(path, mode, owner, group) → WindowsFilesystemActionResult` | `New-Item` + `icacls` |
| `remove_directory` | `(path, recursive) → WindowsFilesystemActionResult` | `Remove-Item [-Recurse]` |
| `create_symlink` | `(link, target) → WindowsFilesystemActionResult` | `New-Item -ItemType SymbolicLink` |
| `remove_symlink` | `(link) → WindowsFilesystemActionResult` | `Remove-Item` after `Get-Item LinkType` guard |
| `chown` | `(path, user, group) → WindowsFilesystemActionResult` | `icacls /setowner` [+ `/grant`] |
| `chmod` | `(path, mode) → WindowsFilesystemActionResult` | `icacls` sequence from translation table |
| `stat` | `(path) → WindowsFileInfo \| None` | `Get-Item` + `Get-Acl` |
| `query_disk_usage` | `(path) → WindowsDiskUsageInfo` | `Get-Volume` |

### `WindowsFilesystemManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `powershell_path` | `str` | `"powershell.exe"` | Full path to the PowerShell executable |
| `mode_translation_table` | `dict[int, list[str]] \| None` | `None` | Overrides the bundled table for testing |

---

## Data Models

### Result Types

```python
class WindowsFilesystemActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class WindowsFilesystemActionResult:
    status: WindowsFilesystemActionStatus
    path: str
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Mount / Fstab / File / DiskUsage Types

```python
@dataclass
class WindowsMountInfo:
    device: str                         # volume UniqueId or UNC / DisplayRoot
    mountpoint: str                     # drive letter or directory path
    fstype: str                         # NTFS / FAT32 / exFAT / ReFS / …
    options: str                        # DriveType (Fixed, Removable, Network, …)

@dataclass
class WindowsFstabEntry:
    device: str                         # volume GUID, disk/partition index, or label
    mountpoint: str                     # drive letter (e.g. "Z:") or directory
    fstype: str                         # informational
    options: str                        # informational
    dump: int                           # informational (always 0)
    passno: int                         # informational (always 0)

@dataclass
class WindowsFileInfo:
    path: str
    path_type: str                      # "Directory", "File", "SymbolicLink", …
    mode: str                           # octal approximation from reverse-translation
    owner: str                          # SID or NT name
    group: str                          # always "" — Windows has no POSIX group
    size_bytes: int

@dataclass
class WindowsDiskUsageInfo:
    path: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    use_percent: int
```

### Exceptions

```python
class WindowsFilesystemQueryError(Exception):
    """Raised when a Get-Volume / Get-Item / Get-Acl query fails unexpectedly."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

No `IFileOperator` is required: drive-letter persistence is handled by the volume
configuration; ACL state is written via `icacls` / `Set-Acl`.

---

## Mount-Table Mapping

Windows has no `/etc/fstab`.  Drive-letter assignments are part of the volume
configuration and are stored in the registry under
`HKLM\SYSTEM\MountedDevices`, keyed by the volume GUID.  The manager does NOT
edit this registry key directly; instead it uses `Add-PartitionAccessPath` and
`Remove-PartitionAccessPath`, which are documented persistent operations.

The `WindowsFstabEntry` dataclass is therefore primarily informational.  The
`device` field may contain a volume GUID (`\\?\Volume{...}\`), a `Disk<n>Part<m>`
identifier, or a volume label; the manager resolves it to a `(DiskNumber,
PartitionNumber)` tuple via `Get-Partition` before dispatching
`Add-PartitionAccessPath`.

---

## POSIX-Mode → ACL Translation Table

Windows has no POSIX mode.  The manager bundles a translation table that maps
each 3-digit octal mode to a canonical ACL pattern expressed as a list of
`icacls` arguments.  The translation is lossy and approximate — NTFS cannot
perfectly express every POSIX combination (e.g. the setuid/setgid/sticky bits
have no direct NTFS analogue) — but it covers the common patterns used in
service-account deployments.

| Mode | Owner ACE | Group / BUILTIN\Users ACE | Other / Everyone ACE |
|---|---|---|---|
| `0o755` | `(OI)(CI)F` | `(OI)(CI)RX` | `(OI)(CI)RX` |
| `0o750` | `(OI)(CI)F` | `(OI)(CI)RX` | *(remove)* |
| `0o700` | `(OI)(CI)F` | *(remove)* | *(remove)* |
| `0o644` | `(OI)(CI)M` | `(OI)(CI)R` | `(OI)(CI)R` |
| `0o640` | `(OI)(CI)M` | `(OI)(CI)R` | *(remove)* |
| `0o600` | `(OI)(CI)M` | *(remove)* | *(remove)* |
| `0o777` | `(OI)(CI)F` | `(OI)(CI)F` | `(OI)(CI)F` |

Modes outside this canonical set are mapped to the nearest matching pattern by
quantising each rwx triplet to the closest `F` / `M` / `RX` / `R` bucket.  The
translation table is exposed as a read-only module attribute
`_MODE_TRANSLATION_TABLE` and is overridable via
`WindowsFilesystemManagerConfig.mode_translation_table` for testing.

The **reverse** translation (ACL → mode) used by `stat()` inverts the table by
matching the observed ACL to the nearest canonical entry; if no entry matches
within a small tolerance, `stat()` returns `mode="0"` and records a
`detail="ACL does not map cleanly to a POSIX mode"` in the
`WindowsFilesystemActionResult` of the nearest write.  `stat()` itself never
returns such a result — the detail flows through normal action results.

---

## Command Mappings

### Write Scripts

| Operation | PowerShell / icacls invocation |
|---|---|
| mount | `Add-PartitionAccessPath -DiskNumber <n> -PartitionNumber <p> -AccessPath '<mountpoint>'` |
| unmount | `Remove-PartitionAccessPath -DiskNumber <n> -PartitionNumber <p> -AccessPath '<mountpoint>' -Confirm:$false` |
| add_fstab_entry | same as `mount` (drive-letter assignments are persistent) |
| remove_fstab_entry | same as `unmount` |
| create_directory | `New-Item -ItemType Directory -Path '<path>' -Force` [+ `icacls /setowner` + `icacls /grant ...`] |
| remove_directory | `Remove-Item -Path '<path>' [-Recurse] -Force` |
| create_symlink | `New-Item -ItemType SymbolicLink -Path '<link>' -Target '<target>'` |
| remove_symlink | `Remove-Item -Path '<link>' -Force` |
| chown | `icacls '<path>' /setowner '<user>'` [+ `icacls '<path>' /grant '<group>:(OI)(CI)F'`] |
| chmod | translation-table-driven sequence: `icacls '<path>' /inheritance:r` then one `/remove`/`/grant` per principal |

### Read Scripts (no elevation)

| Operation | Script |
|---|---|
| get_mounts | `Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,DriveType,UniqueId,Path | ConvertTo-Json -Depth 3` + `Get-PSDrive -PSProvider FileSystem | Select-Object Name,Root,Provider,DisplayRoot | ConvertTo-Json -Depth 3` |
| stat | `Get-Item -Force '<path>' | Select-Object FullName,Attributes,Length,LinkType,Target,Mode | ConvertTo-Json -Depth 3` + `Get-Acl '<path>' | Select-Object Owner | ConvertTo-Json -Depth 3` |
| query_disk_usage | `Get-Volume -DriveLetter <letter> | Select-Object Size,SizeRemaining,DriveType | ConvertTo-Json -Depth 3` |

All writes pass `elevated=True` when the config enables it.  Reads never elevate.

---

## Output Parsing

### `Get-Volume | ConvertTo-Json`

```json
[
  { "DriveLetter": "C", "FileSystemLabel": "OS", "FileSystem": "NTFS",
    "DriveType": 3, "UniqueId": "\\\\?\\Volume{...}\\", "Path": "\\\\?\\Volume{...}\\" },
  { "DriveLetter": "Z", "FileSystemLabel": "Data", "FileSystem": "NTFS",
    "DriveType": 3, "UniqueId": "\\\\?\\Volume{...}\\", "Path": "Z:\\" }
]
```

Parser rules:

- `DriveLetter` → `<letter>:` for `mountpoint` when set; else `Path` is the
  `mountpoint`.
- `UniqueId` → `device`.
- `FileSystem` → `fstype`.
- `DriveType` is an integer (`2` = Removable, `3` = Fixed, `4` = Network, `5` =
  CDRom, `6` = RAM); the parser maps the integer to the textual form for
  `options`.

### `Get-Item | ConvertTo-Json`

```json
{
  "FullName": "C:\\ProgramData\\Myapp",
  "Attributes": "Directory, Archive",
  "Length": null,
  "LinkType": null,
  "Target": null,
  "Mode": "d-----"
}
```

Parser rules:

- `Attributes` contains the native `System.IO.FileAttributes` flags as a
  comma-separated string.  The parser maps the highest-priority flag to
  `path_type`:

  | Windows attribute | `path_type` |
  |---|---|
  | `ReparsePoint` with `LinkType == "SymbolicLink"` | `"SymbolicLink"` |
  | `ReparsePoint` with `LinkType == "Junction"` | `"Junction"` |
  | `Directory` | `"Directory"` |
  | *(otherwise)* | `"File"` |

- `Length` → `size_bytes` (defaults to `0` for directories).
- `FullName` → `path`.
- `mode` is computed by reverse-translating the ACL via
  `_MODE_TRANSLATION_TABLE`.

### `Get-Acl | ConvertTo-Json`

```json
{ "Owner": "BUILTIN\\Administrators" }
```

The parser strips a leading `BUILTIN\` or `NT AUTHORITY\` prefix when presenting
`owner` to callers.

### `Remove-PartitionAccessPath` error classification

A non-zero exit with stderr containing `is not valid` or `does not exist` is
classified as "already absent" and mapped to `SKIPPED` inside the idempotency
guard layer of `unmount` and `remove_fstab_entry`.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `mount` | mount already exists for `(device, mountpoint)` | `get_mounts()` |
| `unmount` | mount point not currently assigned | `get_mounts()` |
| `add_fstab_entry` | existing persistent assignment matches entry | `get_mounts()` |
| `remove_fstab_entry` | no assignment for the access path | `get_mounts()` |
| `create_directory` | directory exists with matching owner and ACL | `stat(path)` + `Get-Acl` |
| `remove_directory` | path does not exist | `stat(path)` |
| `create_symlink` | existing symlink with matching target | `stat(link)` (`LinkType`+`Target`) |
| `remove_symlink` | link does not exist | `stat(link)` |
| `chown` | current owner (and group grant) already matches | `Get-Acl` |
| `chmod` | current ACL equivalent to translation-table target | `Get-Acl` + translation table |

### Type-Conflict Detection

- `create_symlink(link, target)` with `stat(link).path_type != "SymbolicLink"` →
  `FAILED`.
- `remove_symlink(link)` with `stat(link).path_type` not in
  `{"SymbolicLink", "Junction"}` → `FAILED`.
- `create_directory(path)` with `stat(path).path_type != "Directory"` → `FAILED`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsFilesystemQueryError` | `Get-Volume` / `Get-Item` / `Get-Acl` exits non-zero for a reason other than "ItemNotFound" |

All write failures are surfaced as
`WindowsFilesystemActionResult(status=FAILED, ...)` carrying the runner's exit
code, stdout, and stderr.  Windows `ERROR_ACCESS_DENIED` (exit code `5`) is not
special-cased; it flows through the same `FAILED` path.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: mount Drive-Letter Assignment

*For any* `(device, mountpoint="<L>:", ...)` call, `mount()` should dispatch `Add-PartitionAccessPath` with the resolved `DiskNumber` / `PartitionNumber` and the `AccessPath` set to the drive letter.

**Validates: Requirements 1.1**

### Property 2: mount Directory Mountpoint

*For any* `(device, mountpoint="C:\\mnt\\data", ...)` call, `mount()` should dispatch `Add-PartitionAccessPath` with the directory as the `AccessPath` after verifying emptiness.

**Validates: Requirements 1.2**

### Property 3: mount Idempotency

*For any* `(device, mountpoint)` already present in `get_mounts()`, `mount()` should return `SKIPPED`.

**Validates: Requirements 1.3**

### Property 4: unmount Lazy Flag Documented

*For any* `unmount(mountpoint, lazy=True)` call, the result's `detail` should note that the `lazy` flag has no additional effect on Windows; the dispatched argv should still be `Remove-PartitionAccessPath`.

**Validates: Requirements 2.3**

### Property 5: unmount Idempotency

*For any* mountpoint absent from `get_mounts()`, `unmount()` should return `SKIPPED`.

**Validates: Requirements 2.4**

### Property 6: add_fstab_entry Drive-Letter Swap

*For any* partition that already has a different drive letter, `add_fstab_entry()` should dispatch `Remove-PartitionAccessPath` for the old letter before `Add-PartitionAccessPath` for the new one.

**Validates: Requirements 3.3**

### Property 7: add_fstab_entry Idempotency

*For any* existing assignment matching the `WindowsFstabEntry`, `add_fstab_entry()` should return `SKIPPED`.

**Validates: Requirements 3.2**

### Property 8: remove_fstab_entry Idempotency

*For any* access path not currently assigned, `remove_fstab_entry()` should return `SKIPPED`.

**Validates: Requirements 4.2**

### Property 9: get_mounts Merges Two Sources

*For any* successful `Get-Volume` and `Get-PSDrive` outputs, `get_mounts()` should merge them into a single list of `WindowsMountInfo` without duplicates.

**Validates: Requirements 5.1, 5.2**

### Property 10: create_directory Orchestration

*For any* `(path, mode, owner, group)` with all three set, `create_directory()` should dispatch `New-Item -ItemType Directory`, then `icacls /setowner`, then the translation-table `/grant` sequence in that order.

**Validates: Requirements 6.1**

### Property 11: create_directory Idempotency

*For any* existing directory whose owner and ACL already match the requested state, `create_directory()` should return `SKIPPED`.

**Validates: Requirements 6.2**

### Property 12: create_directory Type Conflict

*For any* existing non-directory path, `create_directory()` should return `FAILED`.

**Validates: Requirements 6.3**

### Property 13: remove_directory Recursive Flag

*For any* `remove_directory(path, recursive=True)`, the argv should include `-Recurse`; for `recursive=False`, it should not.

**Validates: Requirements 7.1, 7.2**

### Property 14: create_symlink Existing-Match Idempotency

*For any* existing symlink pointing to `target`, `create_symlink()` should return `SKIPPED`.

**Validates: Requirements 8.2**

### Property 15: create_symlink Remove-and-Recreate

*For any* existing symlink pointing to a different target, `create_symlink()` should dispatch `Remove-Item` followed by `New-Item -ItemType SymbolicLink`.

**Validates: Requirements 8.3**

### Property 16: create_symlink Non-Symlink Conflict

*For any* existing non-symlink at `link`, `create_symlink()` should return `FAILED`.

**Validates: Requirements 8.4**

### Property 17: remove_symlink Type Check

*For any* existing non-symlink at `link`, `remove_symlink()` should return `FAILED` without dispatching `Remove-Item`.

**Validates: Requirements 9.3**

### Property 18: chown Idempotency

*For any* `(path, user, group)` whose current owner (and group grant) already matches, `chown()` should return `SKIPPED`.

**Validates: Requirements 10.2**

### Property 19: chmod Translation-Table Match

*For any* integer `mode`, `chmod()` should look up the nearest entry in `_MODE_TRANSLATION_TABLE` and dispatch the corresponding `icacls` sequence.

**Validates: Requirements 11.1**

### Property 20: chmod Idempotency

*For any* `(path, mode)` whose current ACL already matches the translation-table target, `chmod()` should return `SKIPPED`.

**Validates: Requirements 11.2**

### Property 21: chmod Detail Records Translation

*For any* successful `chmod()` call, the result's `detail` should identify the translation-table entry that was applied.

**Validates: Requirements 11.4**

### Property 22: stat JSON Parsing

*For any* `Get-Item | ConvertTo-Json` + `Get-Acl | ConvertTo-Json` outputs, `stat()` should populate a `WindowsFileInfo` with `path_type` derived from attributes and `owner` derived from ACL owner.

**Validates: Requirements 12.1, 12.2**

### Property 23: stat Missing Returns None

*For any* path that yields `ItemNotFoundException`, `stat()` should return `None`.

**Validates: Requirements 12.3**

### Property 24: query_disk_usage Computes Used and Percent

*For any* `Get-Volume` output with `Size` and `SizeRemaining`, `query_disk_usage()` should compute `used_bytes = Size - SizeRemaining` and `use_percent = round(100 * used_bytes / Size)`.

**Validates: Requirements 13.1, 13.2**

### Property 25: query_disk_usage Failure

*For any* non-zero `Get-Volume` exit, `query_disk_usage()` should raise `WindowsFilesystemQueryError`.

**Validates: Requirements 13.3**

### Property 26: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked; the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.7, 3.6, 4.5, 6.6, 7.7, 8.7, 9.6, 10.6, 11.6**

### Property 27: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner MUST be invoked with `elevated=True`.

**Validates: Requirements 14.1**

### Property 28: No Elevation for Reads

*For any* read operation (`get_mounts`, `stat`, `query_disk_usage`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 14.2**

### Property 29: Error Result Consistency

*For any* non-zero exit from a write command, the returned `WindowsFilesystemActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 1.5, 2.6, 3.5, 4.4, 6.5, 7.6, 8.6, 9.5, 10.5, 11.5**

### Property 30: Mode-Translation Table Is Deterministic

*For any* integer `mode`, the translation-table lookup is deterministic and produces the same ACL pattern across repeated calls with the same table.

**Validates: Requirements 11.1**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── filesystemmanagement.py    ← WindowsFilesystemManager, config, models, exceptions,
                                  _MODE_TRANSLATION_TABLE

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_filesystemmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.windows.filesystemmanagement import (
    WindowsFilesystemManager,
    WindowsFilesystemManagerConfig,
    WindowsFstabEntry,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = WindowsFilesystemManagerConfig(elevated=True, dry_run=False)
fs_mgr = WindowsFilesystemManager(runner=runner, config=config)

# Persistently assign drive letter Z: to a data volume
entry = WindowsFstabEntry(
    device="\\\\?\\Volume{12345678-90ab-cdef-1234-567890abcdef}\\",
    mountpoint="Z:",
    fstype="NTFS",
    options="Fixed",
    dump=0, passno=0,
)
fs_mgr.add_fstab_entry(entry=entry)

# Create an application data directory with approximate 0o750
fs_mgr.create_directory(
    path=r"C:\ProgramData\Myapp",
    mode=0o750,
    owner="BUILTIN\\Administrators",
    group="BUILTIN\\Users",
)

# Symlink release current → specific version
fs_mgr.create_symlink(
    link=r"C:\Program Files\Myapp\current",
    target=r"C:\Program Files\Myapp\releases\2025-04",
)

info = fs_mgr.stat(path=r"C:\ProgramData\Myapp")
print(f"{info.path_type} owner={info.owner} mode~={info.mode}")

usage = fs_mgr.query_disk_usage(path=r"C:\ProgramData\Myapp")
print(f"disk {usage.use_percent}%")
```

### SSH/WinRM Execution with Aspects

```python
from ccai.sysman.windows.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.windows.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage
from ccai.sysman.windows.filesystemmanagement import (
    WindowsFilesystemManager, WindowsFilesystemManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-win", username="admin"))

storage = SQLiteStorage(path=r"C:\ProgramData\ccai\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

fs_mgr = WindowsFilesystemManager(
    runner=runner,
    config=WindowsFilesystemManagerConfig(elevated=True),
)

for m in fs_mgr.get_mounts():
    print(f"{m.device}\t{m.mountpoint}\t{m.fstype}")
```

### Dry-Run Mode

```python
config = WindowsFilesystemManagerConfig(elevated=True, dry_run=True)
fs_mgr = WindowsFilesystemManager(runner=runner, config=config)

result = fs_mgr.chmod(path=r"C:\ProgramData\Myapp", mode=0o700)
assert result.status == WindowsFilesystemActionStatus.DRY_RUN
print(result.detail)  # names the translation-table entry that would be applied
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` that pattern-matches on the PowerShell
  script body.
- `Get-Volume | ConvertTo-Json` + `Get-PSDrive | ConvertTo-Json` mock responses
  drive `get_mounts` and the mount/unmount idempotency guards.
- `Get-Item | ConvertTo-Json` + `Get-Acl | ConvertTo-Json` mock responses drive
  the `create_directory`, `create_symlink`, `remove_symlink`, `chown`, `chmod`
  idempotency and type-conflict tests.
- The mode-to-ACL translation table is tested explicitly with representative
  modes (`0o755`, `0o750`, `0o700`, `0o644`, `0o640`, `0o600`, `0o777`) and with
  out-of-table modes (asserting the nearest-match quantisation).
- A dedicated test asserts that `chmod()` is idempotent when the current ACL
  matches the translation target, by seeding `Get-Acl` output that exactly
  corresponds to the table entry.
- Dry-run tests prove every write short-circuits to `DRY_RUN`.
- Elevation tests prove writes pass `elevated=True` when configured and reads
  never do.
- Symlink conflict tests cover: non-symlink at `link` (both create and remove),
  symlink with matching target (create idempotent), symlink with different
  target (remove-and-recreate sequence).
- JSON parser tests cover both the single-object and array-form output of
  `ConvertTo-Json`, and the `ItemNotFoundException` → `None` mapping for
  `stat()`.
- No test requires Administrator privileges or a live Windows volume.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `mount` | `Add-PartitionAccessPath` |
| `unmount` | `Remove-PartitionAccessPath` |
| `add_fstab_entry` | `Add-PartitionAccessPath` (persistent by default) |
| `remove_fstab_entry` | `Remove-PartitionAccessPath` |
| `get_mounts` | `Get-Volume` + `Get-PSDrive` |
| `create_directory` | `New-Item -ItemType Directory` + `icacls` |
| `remove_directory` | `Remove-Item [-Recurse]` |
| `create_symlink` | `New-Item -ItemType SymbolicLink` (remove-and-recreate on mismatch) |
| `remove_symlink` | `Remove-Item` after `LinkType` guard |
| `chown` | `icacls /setowner` + optional `/grant` for pseudo-group |
| `chmod` | `icacls` sequence from bundled translation table |
| `stat` | `Get-Item` + `Get-Acl` |
| `query_disk_usage` | `Get-Volume` |

---

## Extensions Beyond the Contract

### Mode-to-ACL Translation Table

The bundled `_MODE_TRANSLATION_TABLE` is documented as an OS-specific extension.
It is lossy — NTFS cannot perfectly express every POSIX mode — and callers that
require exact Windows ACL semantics SHOULD drive `chown` / ACL writes directly
via a future `set_acl(path: str, acl: WindowsAclSpec)` method rather than
relying on `chmod`.  That method is deferred; when added it will live behind
its own feature flag.

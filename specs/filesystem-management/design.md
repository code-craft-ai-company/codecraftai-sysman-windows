# Design

## Overview

The filesystem management feature is implemented as a single module,
`automojo.sysman.linux.windows.filesystemmanagement`, that exposes a `FilesystemManager`
class.  The coordinator instantiates `FilesystemManager` and calls its methods to perform
discrete maintenance actions on the local filesystem.

All system interaction is delegated to a command runner provided by `automojo-interfaces`.
`FilesystemManager` never directly accesses the filesystem via Python's `os` or `pathlib`
modules; it only constructs command argument lists and passes them to the runner.  This
keeps business logic separate from execution and makes the feature fully testable via a
mock runner without requiring a live filesystem or root access.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│          FilesystemManager              │  automojo.sysman.linux.windows.filesystemmanagement
│                                         │
│  create_directory()  remove_directory() │
│  create_symlink()    remove_symlink()   │
│  query_path()        query_disk_usage() │
│  mount()             unmount()          │
│  list_mounts()                          │
│  add_fstab_entry()   remove_fstab_entry │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
```

---

## Components

### `FilesystemManager`

**Location:** `source/packages/automojo/sysman/linux/windows/filesystemmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `create_directory` | `(path, mode=None, owner=None, group=None) → FilesystemActionResult` | Create directory with optional permissions |
| `remove_directory` | `(path, recursive=False) → FilesystemActionResult` | Remove a directory |
| `create_symlink` | `(link, target) → FilesystemActionResult` | Create a symbolic link |
| `remove_symlink` | `(link) → FilesystemActionResult` | Remove a symbolic link |
| `query_path` | `(path) → PathInfo \| None` | Stat a path |
| `query_disk_usage` | `(path) → DiskUsageInfo` | Get disk usage for a mount point |
| `mount` | `(spec: MountSpec) → FilesystemActionResult` | Mount a filesystem |
| `unmount` | `(mountpoint, lazy=False) → FilesystemActionResult` | Unmount a filesystem |
| `list_mounts` | `() → list[MountInfo]` | List all currently mounted filesystems |
| `add_fstab_entry` | `(entry: FstabEntry) → FilesystemActionResult` | Add entry to fstab |
| `remove_fstab_entry` | `(device_or_mountpoint) → FilesystemActionResult` | Remove entry from fstab |

### `FilesystemManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `fstab_path` | `str` | `"/etc/fstab"` | Path to fstab file; overridable for testing |

---

## Data Models

### Input Types

```python
@dataclass
class MountSpec:
    device: str
    mountpoint: str
    fstype: str | None = None
    options: str | None = None

@dataclass
class FstabEntry:
    device: str
    mountpoint: str
    fstype: str
    options: str = "defaults"
    dump: int = 0
    passno: int = 0
```

### Result Types

```python
class FilesystemActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class FilesystemActionResult:
    status: FilesystemActionStatus
    path: str
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    detail: str | None            # human-readable detail for SKIPPED or conflict cases
```

### Query Result Types

```python
@dataclass
class PathInfo:
    path: str
    path_type: str      # "regular file" | "directory" | "symbolic link" | ...
    mode: str           # octal string e.g. "755"
    owner: str
    group: str
    size_bytes: int

@dataclass
class DiskUsageInfo:
    path: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    use_percent: int

@dataclass
class MountInfo:
    device: str
    mountpoint: str
    fstype: str
    options: str
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

---

## Command Mappings

### Directory Operations

| Operation | Command |
|---|---|
| Create directory | `mkdir -p <path>` |
| Set permissions | `chmod <mode> <path>` |
| Set ownership | `chown <owner>:<group> <path>` |
| Remove empty directory | `rmdir <path>` |
| Remove directory tree | `rm -rf <path>` |
| Check path exists | `stat -c "%F %a %U %G %s" <path>` |

### Symlink Operations

| Operation | Command |
|---|---|
| Create symlink | `ln -s <target> <link>` |
| Read symlink target | `readlink <link>` |
| Remove symlink | `unlink <link>` |
| Check path type | `stat -c "%F" <path>` |

### Mount Operations

| Operation | Command |
|---|---|
| Mount filesystem | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| Unmount filesystem | `umount [-l] <mountpoint>` |
| List mounts | `findmnt --json` |

### Disk and fstab Operations

| Operation | Command |
|---|---|
| Query disk usage | `df -B1 --output=size,used,avail,pcent <path>` |
| Read fstab | `cat <fstab_path>` |
| Write fstab | `tee <fstab_path>` with content on stdin |

---

## Idempotency Guards

| Method | Guard | How determined |
|---|---|---|
| `create_directory()` | Directory already exists | `stat` exits 0 and path type is `directory` |
| `remove_directory()` | Directory absent | `stat` exits non-zero |
| `create_symlink()` | Symlink exists with same target | `readlink` output matches target |
| `create_symlink()` — retarget | Symlink exists with different target | `readlink` output differs; unlink then re-create |
| `create_symlink()` — conflict | Non-symlink exists at link path | `stat -c "%F"` returns non-symlink type; return `FAILED` |
| `remove_symlink()` | Path absent | `stat` exits non-zero |
| `remove_symlink()` — conflict | Path is not a symlink | `stat -c "%F"` is not `symbolic link`; return `FAILED` |
| `mount()` | Device already mounted at mountpoint | `list_mounts()` contains matching entry |
| `unmount()` | Mountpoint not currently mounted | `list_mounts()` has no matching entry |
| `add_fstab_entry()` | Entry already in fstab | Parse `cat fstab` — match on device or mountpoint field |
| `remove_fstab_entry()` | No matching entry | Parse `cat fstab` — no match found |

---

## fstab Management

fstab entries are read via `cat <fstab_path>` through the runner.  The content is parsed
in pure Python line by line (skipping comments and blank lines).  Each non-comment line is
split on whitespace into the six standard fields: device, mountpoint, fstype, options,
dump, passno.

For `add_fstab_entry()`: if no existing line matches on device or mountpoint, the rendered
entry line is appended to the file content and written back via `tee`.

For `remove_fstab_entry()`: lines matching the supplied device-or-mountpoint string in
field 1 or field 2 are removed.  The updated content is written back via `tee`.

A rendered `FstabEntry` follows the standard six-field format:

```
<device>  <mountpoint>  <fstype>  <options>  <dump>  <passno>
```

Example:
```
/dev/sdb1  /data  ext4  defaults,noatime  0  2
```

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `FilesystemQueryError` | `query_path()`, `query_disk_usage()`, or `list_mounts()` returns an unexpected non-zero exit code |

All other failures from write operations are surfaced as a `FilesystemActionResult` with
`status=FAILED`.  Path conflict cases (non-symlink at symlink target; non-symlink passed
to `remove_symlink`) also return `FAILED` with a `detail` message rather than raising.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── filesystemmanagement.py       ← FilesystemManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_filesystemmanagement.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `stat`, `readlink`, `findmnt`, `cat fstab`, and `df` mock responses drive all
  idempotency and query logic.
- `fstab_path` in `FilesystemManagerConfig` is overridden to a test value in all fstab
  tests so path construction is exercised without touching `/etc/fstab`.
- fstab parsing and rendering logic is tested in pure Python by supplying mock `cat`
  output strings and verifying the `tee` stdin content the runner receives.
- `list_mounts()` JSON parsing is tested with representative `findmnt --json` output.
- No test requires root access or a live filesystem.

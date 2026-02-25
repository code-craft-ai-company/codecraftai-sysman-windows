# Design

## Overview

The log management feature is implemented as a single module,
`automojo.sysman.linux.windows.logmanagement`, that exposes a `LogManager` class.  The
coordinator instantiates `LogManager` and calls its methods to query the systemd journal,
read and search traditional log files, vacuum journal data, trigger logrotate runs, and
manage logrotate drop-in configuration files.

All command execution goes through `ICommandRunner`.  All file reads, writes, existence
checks, and deletes go through `IFileOperator`.  No `subprocess`, `open`, or direct
`pathlib` calls are made inside the module.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│              LogManager                 │  automojo.sysman.linux.windows.logmanagement
│                                         │
│  query_journal()   journal_disk_usage() │
│  vacuum_by_size()  vacuum_by_time()     │
│  read_log_file()   search_log_file()    │
│  rotate_log()                           │
│  write_logrotate_dropin()               │
│  remove_logrotate_dropin()              │
│  query_logrotate_dropin()               │
└──────────┬────────────────┬─────────────┘
           │                │
           ▼                ▼
┌──────────────────┐  ┌─────────────────────┐
│  ICommandRunner  │  │   IFileOperator      │
│  run(args, ...)  │  │  read_text / write   │
│  → RunResult     │  │  exists / delete     │
└──────────────────┘  └─────────────────────┘
```

---

## Components

### `LogManager`

**Location:** `source/packages/automojo/sysman/linux/windows/logmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `query_journal` | `(params: JournalQueryParams) → JournalQueryResult` | Run `journalctl` with filters |
| `journal_disk_usage` | `() → JournalDiskUsage` | Run `journalctl --disk-usage` |
| `vacuum_by_size` | `(size: str) → LogActionResult` | Run `journalctl --vacuum-size=<size>` |
| `vacuum_by_time` | `(period: str) → LogActionResult` | Run `journalctl --vacuum-time=<period>` |
| `read_log_file` | `(path: str, lines: int) → LogFileResult` | Run `tail --lines=<n> <path>` |
| `search_log_file` | `(path: str, pattern: str, *, context_lines, max_matches) → LogFileResult` | Run `grep -E` |
| `rotate_log` | `(config_path: str) → LogActionResult` | Run `logrotate --force` or `--debug` |
| `write_logrotate_dropin` | `(name: str, config: LogrotateConfig) → LogActionResult` | Write `/etc/logrotate.d/<name>` |
| `remove_logrotate_dropin` | `(name: str) → LogActionResult` | Delete `/etc/logrotate.d/<name>` |
| `query_logrotate_dropin` | `(name: str) → LogrotateDropInState` | Check existence and content |

### `LogManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands with side effects or file writes are executed |
| `elevated` | `bool` | `False` | When `True`, write/vacuum commands pass `elevated=True` |

---

## Data Models

### Journal Types

```python
@dataclass
class JournalQueryParams:
    unit: str | None = None
    since: str | None = None
    until: str | None = None
    priority: str | None = None     # emerg/alert/crit/err/warning/notice/info/debug
    lines: int | None = None
    grep: str | None = None
    output_format: str | None = None  # short/json/cat/verbose

@dataclass
class JournalQueryResult:
    success: bool
    lines: list[str]
    stderr: str | None
    command: list[str]

@dataclass
class JournalDiskUsage:
    size_human: str     # e.g. "1.2G"
    size_bytes: int
```

### Action Result Types

```python
class LogActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class LogActionResult:
    status: LogActionStatus
    detail: str | None      # human-readable: path, size, period, or debug output
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Log File Types

```python
@dataclass
class LogFileResult:
    success: bool
    lines: list[str]
    stderr: str | None
    command: list[str]
```

### logrotate Types

```python
@dataclass
class LogrotateConfig:
    log_path: str               # glob path to rotate, e.g. "/var/log/myapp/*.log"
    rotate: int = 7             # number of rotated files to keep
    frequency: str = "daily"    # daily / weekly / monthly / yearly
    compress: bool = True
    missingok: bool = True
    notifempty: bool = True
    copytruncate: bool = False
    postrotate: str | None = None  # shell command for postrotate stanza

@dataclass
class LogrotateDropInState:
    name: str
    exists: bool
    content: str | None
```

### Exception

```python
class LogManagementError(Exception):
    """Raised when a non-optional query operation fails."""
    def __init__(self, message: str, exit_code: int | None = None, stderr: str | None = None): ...
```

---

## Command Mappings

### Read Commands (no elevation)

| Method | Command |
|---|---|
| `query_journal` | `journalctl [--unit=] [--since=] [--until=] [--priority=] [--lines=] [--grep=] [--output=] --no-pager` |
| `journal_disk_usage` | `journalctl --disk-usage --no-pager` |
| `read_log_file` | `tail --lines=<n> <path>` |
| `search_log_file` | `grep --extended-regexp [--context=] [--max-count=] <pattern> <path>` |

### Write Commands (elevation-aware)

| Method | Command |
|---|---|
| `vacuum_by_size` | `journalctl --vacuum-size=<size>` |
| `vacuum_by_time` | `journalctl --vacuum-time=<period>` |
| `rotate_log` (normal) | `logrotate --force <config_path>` |
| `rotate_log` (dry-run) | `logrotate --debug <config_path>` |

---

## `journalctl --disk-usage` Output Parsing

`journalctl --disk-usage` produces a line like:

```
Archived and active journals take up 1.2G in the file system.
```

The parser extracts the human-readable size (`1.2G`) using a regex, then converts it to
bytes:

| Suffix | Multiplier |
|---|---|
| `B` | 1 |
| `K` | 1 024 |
| `M` | 1 048 576 |
| `G` | 1 073 741 824 |
| `T` | 1 099 511 627 776 |

If the output does not match the expected pattern the parser raises `LogManagementError`.

---

## `LogrotateConfig` Rendering

`LogrotateConfig` is rendered to a configuration block string by `_render_logrotate_config()`:

```
<log_path> {
    <frequency>
    rotate <rotate>
    compress
    missingok
    notifempty
    [copytruncate]
    [postrotate
        <postrotate>
    endscript]
}
```

Only fields set to non-default/truthy values are emitted (e.g., `copytruncate` is only
written when `copytruncate=True`).

---

## Idempotency for `write_logrotate_dropin`

Before writing, `query_logrotate_dropin()` is called to read the current content.  If
the rendered config string exactly matches the existing file content the method returns
`SKIPPED`.

---

## `rotate_log` Dry-Run Behaviour

`rotate_log` is special: in dry-run mode it still executes a command (`logrotate
--debug`) so that the coordinator receives useful simulation output.  It returns status
`DRY_RUN` to make clear that no rotation occurred.  All other write methods return
`DRY_RUN` without executing anything.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── logmanagement.py        ← LogManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_logmanagement.py
```

---

## Testing Strategy

- All tests use `MockCommandRunner` and `MockFileOperator` keyed on command args prefix
  and file path respectively.
- `_render_logrotate_config()` is tested in pure Python with representative
  `LogrotateConfig` objects verifying all optional fields (compress, copytruncate,
  postrotate).
- `journal_disk_usage` parsing is tested with representative output strings including
  all size suffixes.
- `search_log_file` exit-code mapping (0 = matches, 1 = no matches, 2 = error) is tested
  independently.
- `rotate_log` dry-run path verifies that `logrotate --debug` is called and status is
  `DRY_RUN`.
- `write_logrotate_dropin` idempotency is tested by pre-populating `MockFileOperator`
  with matching content.
- No test requires root access or a live system.

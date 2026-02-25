# Design

## Overview

The scheduled tasks feature is implemented as a single module,
`automojo.sysman.linux.windows.scheduledtasks`, exposing a `ScheduledTaskManager` class.
The manager covers two scheduling backends — **cron** (drop-in files in `/etc/cron.d/`)
and **systemd timers** (`.timer` + `.service` unit-file pairs in `/etc/systemd/system/`)
— through a unified public API.

All file writes and deletes are delegated to an `IFileOperator` implementation.  All
`systemctl` invocations are delegated to an `ICommandRunner` implementation.  Cron
expression validation is performed with a small pure-Python validator so that no external
process is spawned.  The module has no dependency on third-party libraries.

---

## Architecture

```
┌─────────────────────────────────────────┐
│       Maintenance Coordinator           │  (external package)
└──────────────┬──────────────────────────┘
               │ calls
               ▼
┌──────────────────────────────────────────────────────────┐
│              ScheduledTaskManager                        │
│  add_cron_job()           remove_cron_job()              │
│  list_cron_jobs()         validate_cron_expression()     │
│  create_systemd_timer()   remove_systemd_timer()         │
│  set_systemd_timer_enabled()   list_systemd_timers()     │
│  query_task()                                            │
└──────────┬───────────────────────┬───────────────────────┘
           │ file I/O              │ commands
           ▼                       ▼
┌──────────────────┐   ┌───────────────────────┐
│  IFileOperator   │   │   ICommandRunner       │
│  write_text()    │   │   run(cmd, elevated)   │
│  delete()        │   └───────────────────────┘
│  read_text()     │
│  list_dir()      │
│  exists()        │
└──────────────────┘
```

---

## Components

### `ScheduledTaskManager`

**Location:** `source/packages/automojo/sysman/linux/windows/scheduledtasks.py`

| Method | Signature | Description |
|---|---|---|
| `add_cron_job` | `(entry: CronJobEntry) → ScheduledTaskResult` | Write a cron drop-in file |
| `remove_cron_job` | `(name: str) → ScheduledTaskResult` | Delete a cron drop-in file |
| `list_cron_jobs` | `() → list[ScheduledTaskInfo]` | Enumerate files in `cron_dir` |
| `validate_cron_expression` | `(expression: str) → bool` | Pure-Python syntax check |
| `create_systemd_timer` | `(entry: SystemdTimerEntry) → ScheduledTaskResult` | Write unit files and enable timer |
| `remove_systemd_timer` | `(name: str) → ScheduledTaskResult` | Disable and delete unit files |
| `set_systemd_timer_enabled` | `(name: str, enabled: bool) → ScheduledTaskResult` | Enable or disable timer |
| `list_systemd_timers` | `() → list[ScheduledTaskInfo]` | Parse `systemctl list-timers` output |
| `query_task` | `(name: str, task_type: str) → ScheduledTaskInfo \| None` | Query individual task state |

### `ScheduledTaskConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no files are written/deleted and no commands run |
| `cron_dir` | `str` | `"/etc/cron.d"` | Directory for cron drop-in files |
| `unit_dir` | `str` | `"/etc/systemd/system"` | Directory for systemd unit files |

---

## Data Models

### `TaskType`

```python
class TaskType(Enum):
    CRON    = "cron"
    SYSTEMD = "systemd"
```

### `CronJobEntry`

```python
@dataclass
class CronJobEntry:
    name: str           # filename written to cron_dir (alphanumeric + hyphens only)
    schedule: str       # cron expression, e.g. "0 2 * * *" or "@daily"
    command: str        # shell command to execute
    user: str           # user to run the command as, e.g. "root"
    comment: str | None = None  # optional descriptive comment
```

### `SystemdTimerEntry`

```python
@dataclass
class SystemdTimerEntry:
    name: str               # base name; produces <name>.timer and <name>.service
    description: str        # [Unit] Description= for both unit files
    on_calendar: str        # OnCalendar= value, e.g. "daily" or "*-*-* 02:00:00"
    exec_start: str         # ExecStart= value for the .service unit
    enabled: bool = True    # whether to run systemctl enable --now after writing
    after: str | None = None        # [Unit] After= directive for the .service unit
    wants_by: str | None = None     # [Install] WantedBy= for the .timer (defaults to timers.target)
```

### `ScheduledTaskInfo`

```python
@dataclass
class ScheduledTaskInfo:
    name: str
    task_type: TaskType
    schedule: str | None = None     # cron expression or OnCalendar value
    command: str | None = None      # exec_start or cron command
    user: str | None = None         # cron user field; None for systemd timers
    enabled: bool | None = None     # systemd: from systemctl show; cron: always True if file present
    active: bool | None = None      # systemd only: from systemctl show ActiveState
    readable: bool = True           # False when file exists but could not be read (cron)
```

### `ScheduledTaskResult`

```python
@dataclass
class ScheduledTaskResult:
    success: bool
    name: str
    dry_run: bool = False
    error: str | None = None
```

### `ScheduledTaskQueryError`

```python
class ScheduledTaskQueryError(Exception):
    """Raised when a query operation fails for a reason other than the task not existing."""
```

---

## Cron File Format

Each managed cron drop-in file written to `<cron_dir>/<name>` has the form:

```
# <comment>               ← omitted when CronJobEntry.comment is None
# Managed by automojo-sysman. Do not edit manually.
<schedule> <user> <command>
```

Example for `name="nightly-backup"`:

```
# Take nightly filesystem backup
# Managed by automojo-sysman. Do not edit manually.
0 2 * * * root /usr/local/bin/backup.sh
```

`list_cron_jobs` reads each file in `cron_dir`, skips lines beginning with `#`, and
parses the first non-comment, non-blank line as `schedule user command` (splitting on
whitespace with `maxsplit=2`).  Files that cannot be parsed yield a `ScheduledTaskInfo`
with `readable=False`.

---

## Systemd Unit File Templates

### `<name>.service`

```ini
[Unit]
Description=<entry.description>
After=<entry.after>            # omitted if entry.after is None

[Service]
Type=oneshot
ExecStart=<entry.exec_start>
```

### `<name>.timer`

```ini
[Unit]
Description=<entry.description> (timer)

[Timer]
OnCalendar=<entry.on_calendar>
Persistent=true

[Install]
WantedBy=<entry.wants_by or "timers.target">
```

---

## `create_systemd_timer()` Sequence

```
1. Render .service content from template
2. file_operator.write_text(<unit_dir>/<name>.service, content, elevated=True)
3. Render .timer content from template
4. file_operator.write_text(<unit_dir>/<name>.timer, content, elevated=True)
5. command_runner.run(["systemctl", "daemon-reload"], elevated=True)
6. If entry.enabled:
       command_runner.run(["systemctl", "enable", "--now", f"{name}.timer"], elevated=True)
7. Return ScheduledTaskResult(success=True, name=name)
On any failure at step 2–6: return ScheduledTaskResult(success=False, name=name, error=...)
```

## `remove_systemd_timer()` Sequence

```
1. Check file_operator.exists(<unit_dir>/<name>.timer) OR
         file_operator.exists(<unit_dir>/<name>.service)
   If neither: return ScheduledTaskResult(success=False, name=name, error="timer not found")
2. command_runner.run(["systemctl", "disable", "--now", f"{name}.timer"], elevated=True)
   (ignore non-zero exit — unit may already be inactive/disabled)
3. file_operator.delete(<unit_dir>/<name>.timer, elevated=True)   # if exists
4. file_operator.delete(<unit_dir>/<name>.service, elevated=True) # if exists
5. command_runner.run(["systemctl", "daemon-reload"], elevated=True)
6. Return ScheduledTaskResult(success=True, name=name)
On failure at step 3–5: return ScheduledTaskResult(success=False, name=name, error=...)
```

---

## `validate_cron_expression()` Logic

The validator does not invoke any external process.

```
SHORTHANDS = {"@reboot", "@daily", "@weekly", "@monthly", "@yearly", "@hourly"}

1. Strip leading/trailing whitespace.
2. If expression starts with "@": return expression in SHORTHANDS.
3. Split on whitespace → fields.  If len(fields) != 5: return False.
4. For each field at index i, call _validate_cron_field(field, field_ranges[i]).
5. Return True only if all five fields are valid.
```

`_validate_cron_field(field, (min_val, max_val))` accepts:
- `"*"` — always valid
- `"*/n"` — n must be a positive integer within range
- `"a-b"` — both a and b must be integers within range; a ≤ b
- `"a,b,c,..."` — each element must pass the single-value check
- A bare integer string within `[min_val, max_val]`

Field ranges: minute `(0,59)`, hour `(0,23)`, dom `(1,31)`, month `(1,12)`,
dow `(0,7)` (both 0 and 7 represent Sunday).

---

## `list_systemd_timers()` Parsing

`systemctl list-timers --all --no-legend --no-pager` output columns (space-separated):
`NEXT LEFT LAST PASSED UNIT ACTIVATES`

The `UNIT` column contains `<name>.timer`.  The parser strips `.timer` suffix to obtain
`name` and constructs a `ScheduledTaskInfo` with `task_type=SYSTEMD`, `enabled=None`
(not provided by this command), and `active=True` if the `NEXT` column is not `n/a`.

---

## `query_task()` Logic

**Cron:**
1. Check `file_operator.exists(<cron_dir>/<name>)`.  If absent: return `None`.
2. `content = file_operator.read_text(<cron_dir>/<name>, elevated=True)`.
3. Parse non-comment, non-blank line → `ScheduledTaskInfo(task_type=CRON, enabled=True, ...)`.
4. On parse failure: return `ScheduledTaskInfo(readable=False, ...)`.

**Systemd:**
1. Run `systemctl show <name>.timer --no-pager`.
2. If exit code indicates unit not found: return `None`.
3. Parse key=value lines; extract `ActiveState`, `UnitFileState`, `OnCalendar*` and
   `ExecStart` from the corresponding `.service` show output.
4. Return `ScheduledTaskInfo(task_type=SYSTEMD, enabled=..., active=..., schedule=..., command=...)`.
5. On command failure (exit non-zero, not "not found"): raise `ScheduledTaskQueryError`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── scheduledtasks.py   ← ScheduledTaskManager, models, config, exception

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_scheduledtasks.py
```

---

## Testing Strategy

- `MockFileOperator` records all `write_text`, `delete`, `read_text`, `list_dir`, and
  `exists` calls with their arguments for assertion.
- `MockCommandRunner` returns configurable `(exit_code, stdout, stderr)` tuples per
  command prefix, and records all invocations.
- Dry-run tests confirm zero calls to `MockFileOperator.write_text` /
  `MockCommandRunner.run`.
- `validate_cron_expression` tests are pure unit tests requiring no mocks.
- `list_cron_jobs` tests supply canned file content via `MockFileOperator.read_text`.
- `list_systemd_timers` tests supply canned `systemctl list-timers` stdout.
- `query_task` tests cover cron (file found/absent/unreadable) and systemd (unit
  found/not-found/command error) branches.
- No test requires file system access or live command execution.

# Design

## Overview

The log management feature is implemented as a single module,
`ccai.sysman.windows.logmanagement`, that exposes a `WindowsLogManager` class.
This spec implements the log-management contract defined at
`codecraftai-sysman-common/specs/contracts/log-management/contract.md` for Windows
using PowerShell's `Get-WinEvent` cmdlet for Event Log reads, `wevtutil` for
rotation, size, and clear commands, and the `IFileOperator` abstraction for plain
text log files under `%ProgramData%`.

All system commands go through the `ICommandRunner` interface; all file I/O for
plain-text logs and rotation drop-ins goes through the `IFileOperator` interface —
this dual-abstraction dependency is an extension over the contract, motivated by
the need to manage both Event Log channels and text logs on the same host.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are made inside the
module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over WinRM/SSH (via `SSHCommandRunner`).  When
using the remote runner the coordinator can track reliability metrics and apply
execution aspects without any changes to the `WindowsLogManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/remote execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into WindowsLogManager                      │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              WindowsLogManager                              │
│  ccai.sysman.windows.logmanagement                      │
│                                                             │
│  get_log()              tail_log()                          │
│  list_logs()            rotate_log()                        │
│  truncate_log()         configure_rotation()                │
│  remove_rotation_config() get_rotation_config()             │
│  query_disk_usage()                                         │
└────────────┬────────────────────────────┬───────────────────┘
             │ delegates to               │ delegates to
             ▼                            ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   PowerShell / wevtutil │      │   read_text / write  │
│   invocation         │         │   delete / copy      │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
           └────────────────┬───────────────┘
                            ▼
          ┌──────────────────────────────────┐
          │   LocalCommandRunner or          │
          │   SSHCommandRunner (WinRM-aware) │
          └─────────────┬────────────────────┘
                        │ optional
                        ▼
          ┌──────────────────────────────────┐
          │   PersistenceAspect /            │
          │   RetryAspect / TimeoutAspect    │
          └──────────────────────────────────┘
```

---

## Components

### `WindowsLogManager`

**Location:** `source/packages/ccai/sysman/windows/logmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `get_log` | `(source, lines=None, since=None, until=None, severity=None, pattern=None) → WindowsLogEntries` | Retrieve recent entries |
| `tail_log` | `(source, follow=True) → Iterator[WindowsLogEntry]` | Stream entries |
| `list_logs` | `() → list[WindowsLogInfo]` | Enumerate channels |
| `rotate_log` | `(source) → WindowsLogActionResult` | Archive + clear |
| `truncate_log` | `(source) → WindowsLogActionResult` | Clear a channel or zero a file |
| `configure_rotation` | `(source, policy) → WindowsLogActionResult` | Apply `wevtutil sl` or write drop-in |
| `remove_rotation_config` | `(source) → WindowsLogActionResult` | Revert to default or delete drop-in |
| `get_rotation_config` | `(source) → WindowsRotationPolicy \| None` | Parse `wevtutil gl` or drop-in |
| `query_disk_usage` | `(source) → WindowsLogDiskUsage` | Report file size |

### `WindowsLogManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, every write verb short-circuits with no runner or file-operator call |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

---

## Data Models

```python
class WindowsLogActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class WindowsLogEntry:
    timestamp: datetime | None
    severity: str | None
    source: str | None
    message: str

@dataclass
class WindowsLogEntries:
    source: str
    entries: list[WindowsLogEntry]
    raw: list[str]
    truncated: bool

@dataclass
class WindowsLogInfo:
    source: str
    path: str | None
    size_bytes: int | None
    rotating: bool

@dataclass
class WindowsLogActionResult:
    status: WindowsLogActionStatus
    source: str
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class WindowsRotationPolicy:
    source: str
    keep_count: int             # Archive files retained under %ProgramData%
    frequency: str              # "size:<N>" only; Windows lacks time-based native
    compress: bool              # True → .zip archive; False → raw .evtx/.txt
    max_size_bytes: int | None  # wevtutil /ms value
    post_rotate: str | None     # Not emitted today

@dataclass
class WindowsLogDiskUsage:
    source: str
    size_human: str
    size_bytes: int

class WindowsLogQueryError(Exception):
    """Raised when a read fails for a reason other than source-missing."""
```

---

## Interfaces

### `ICommandRunner` and `IFileOperator`

Both are imported from `codecraftai-interfaces` and used as defined in the contract.
The manager holds one instance of each and delegates all I/O to them.

---

## Source Mapping

| Shape | Example | Routing |
|---|---|---|
| Classic channel | `Application`, `System`, `Security` | `Get-WinEvent -LogName <name>`, `wevtutil cl <name>` |
| Modern channel | `Microsoft-Windows-Security-Auditing/Operational` | Same as classic (cmdlets accept both) |
| Filesystem path | `C:\ProgramData\MyApp\logs\app.log` | `Get-Content` / `IFileOperator` |

The routing helper `_classify_source(source)` returns:

1. If `source` contains a path separator (`\` or `/`) → `"file"`.
2. Otherwise → `"channel"`.

---

## Command Mappings

### Read Commands

| Operation | Command | Elevation |
|---|---|---|
| `get_log` — channel | `powershell -NoProfile -Command "Get-WinEvent -LogName '<name>' -MaxEvents <N> \| ConvertTo-Json -Depth 5"` | `Security` only |
| `get_log` — file | `powershell -NoProfile -Command "Get-Content -Path '<path>' -Tail <N>"` | No |
| `tail_log` — channel | polling loop on `Get-WinEvent -MaxEvents 1 -Oldest` | `Security` only |
| `tail_log` — file | `powershell -NoProfile -Command "Get-Content -Path '<path>' -Wait -Tail 0"` | No |
| `list_logs` | `powershell -NoProfile -Command "Get-WinEvent -ListLog * \| ConvertTo-Json -Depth 3"` | No |
| `get_rotation_config` — channel | `wevtutil gl <name>` | No |
| `get_rotation_config` — file | `IFileOperator.read_text` | No |
| `query_disk_usage` — channel | `wevtutil gl <name>` (parse `fileSize:`) | No |
| `query_disk_usage` — file | `powershell -NoProfile -Command "(Get-Item -Path '<path>').Length"` | No |

### Write Commands (elevation-aware)

| Operation | Command |
|---|---|
| `rotate_log` — channel | `wevtutil epl <name> <archive>` then `wevtutil cl <name>` |
| `rotate_log` — file | `IFileOperator.copy(src=path, dst=path + ".<ts>")` then `write_text(path, "", elevated=True)` |
| `truncate_log` — channel | `wevtutil cl <name>` |
| `truncate_log` — file | `IFileOperator.write_text(path, "", elevated=True)` |
| `configure_rotation` — channel | `wevtutil sl <name> /ms:<max_size_bytes> /rt:<true\|false>` |
| `configure_rotation` — file | `IFileOperator.write_text(drop-in path, JSON, elevated=True)` |
| `remove_rotation_config` — channel | `wevtutil sl <name> /ms:20971520 /rt:false` (default) |
| `remove_rotation_config` — file | `IFileOperator.delete(drop-in path, elevated=True)` |

---

## `Get-WinEvent … ConvertTo-Json` Output Parsing

Example JSON (array when multiple; object when single):

```json
[{"TimeCreated":"2024-04-05T12:34:38.123Z",
  "LevelDisplayName":"Information",
  "ProviderName":"Microsoft-Windows-Security-Auditing",
  "Id":4624,
  "Message":"An account was successfully logged on."}]
```

Parse rules:

1. `timestamp` — `datetime.fromisoformat(obj["TimeCreated"].replace("Z","+00:00"))`.
2. `severity` — `_LEVEL_MAP[obj["LevelDisplayName"]]` where the map is
   `{"Critical":"crit", "Error":"err", "Warning":"warning",
     "Information":"info", "Verbose":"debug"}`.
3. `source` — `obj["ProviderName"]`.
4. `message` — `obj["Message"]` or empty string.

Single-object JSON is wrapped into a one-element list before parsing.  An empty
result (exit 1 with `No events were found`) yields an empty `WindowsLogEntries`.

---

## `wevtutil gl` Output Parsing

Example output:

```
name: Application
enabled: true
type: Admin
owningPublisher:
isolation: Application
channelAccess: O:BAG:SYD:(A;;0xf0007;;;SY)...
logging:
  logFileName: %SystemRoot%\System32\Winevt\Logs\Application.evtx
  retention: false
  autoBackup: false
  maxSize: 20971520
  fileSize: 3145728
publishing:
  fileMax: 1
```

Parser tokenises by lines, matches `^(\s*)(\w+):\s*(.*)$`, and populates:

| YAML-like key | `WindowsRotationPolicy` field |
|---|---|
| `maxSize` | `max_size_bytes` |
| `retention` | `overflow_action` (`true` → `"RETAIN"`, `false` → `"OVERWRITE"`) |
| `autoBackup` | `compress` (approximate — `true` enables archival) |
| `fileSize` | used by `query_disk_usage` (NOT policy) |

---

## `WindowsRotationPolicy` Rendering

For channels: the policy is applied via `wevtutil sl`.  For files, the policy is
serialised to JSON and written to
`C:\ProgramData\CodeCraftAI\logrotation\<basename>.json`:

```json
{
  "source": "app.log",
  "keep_count": 7,
  "frequency": "size:104857600",
  "compress": true,
  "max_size_bytes": 104857600,
  "post_rotate": null
}
```

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `configure_rotation` — channel | Current `wevtutil gl` matches requested | Parse, compare policy fields |
| `configure_rotation` — file | Rendered JSON matches existing | `read_text` → string compare |
| `remove_rotation_config` — channel | Current policy already at default | Same |
| `remove_rotation_config` — file | Drop-in absent | `IFileOperator.exists` |
| `rotate_log` | None — operational action | — |
| `truncate_log` | None — operational action | — |

---

## `rotate_log` Dry-Run Behaviour

Windows log-management has no native simulation CLI equivalent to
`logrotate --debug` or `newsyslog -nvv`.  Accordingly, **every** write verb
(including `rotate_log`) follows the strict contract rule in dry-run mode: no
runner invocation, no file-operator invocation, `status=DRY_RUN` with the planned
command/content described in `detail`.  The contract-permitted simulation
exception is intentionally not used.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsLogQueryError` | `get_log`, `tail_log` (first iter), `list_logs`, `get_rotation_config` (unparseable), or `query_disk_usage` sees a failure that is NOT "source unknown" |

All other failures from write operations are surfaced as
`WindowsLogActionResult(status=FAILED)` with exit code and stderr populated.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of the system.*

### Property 1: `get_log` Channel Command Construction
*For any* channel name and filter set, `get_log` should construct a PowerShell
invocation of `Get-WinEvent -LogName '<name>' -MaxEvents <N>` with the appropriate
`FilterHashtable` entries for `since`, `until`, and `severity`.
**Validates: Requirements 1.1**

### Property 2: `get_log` File Command Construction
*For any* file path with `lines=N`, `get_log` should construct `Get-Content -Path
'<path>' -Tail <N>`.
**Validates: Requirements 1.2**

### Property 3: `get_log` JSON Parsing
*For any* well-formed `ConvertTo-Json` stdout, `get_log` should produce a
`WindowsLogEntries` whose `entries` list contains one `WindowsLogEntry` per
object.
**Validates: Requirements 1.3**

### Property 4: `get_log` Truncation Flag
*For any* query where the parsed entry count equals `lines`, `truncated` should
be `True`.
**Validates: Requirements 1.4**

### Property 5: `get_log` Unknown Source
*For any* source with "No events were found" stderr or `Test-Path` false,
`get_log` should return an empty `WindowsLogEntries` without raising.
**Validates: Requirements 1.5**

### Property 6: `get_log` Error Raises
*For any* failure other than "source unknown", `get_log` should raise
`WindowsLogQueryError`.
**Validates: Requirements 1.6**

### Property 7: Security Channel Elevation
*For any* `get_log` invocation where `source == "Security"`, the runner should be
invoked with `elevated=True`.
**Validates: Requirements 1.7**

### Property 8: `tail_log` Channel Polling
*For any* channel and `follow=True`, `tail_log` should drive a polling loop
invoking `Get-WinEvent -MaxEvents 1 -Oldest` via the streaming API.
**Validates: Requirements 2.1**

### Property 9: `tail_log` File Command
*For any* file path and `follow=True`, `tail_log` should invoke `Get-Content
-Path '<path>' -Wait -Tail 0`.
**Validates: Requirements 2.2**

### Property 10: `tail_log` Non-Follow Termination
*For any* non-follow call, the iterator should yield the backlog and terminate.
**Validates: Requirements 2.3**

### Property 11: `list_logs` Command Construction
*For any* call, `list_logs` should invoke `Get-WinEvent -ListLog * | ConvertTo-Json`.
**Validates: Requirements 3.1**

### Property 12: `list_logs` Rotating Flag
*For any* channel where `IsLogFull` is false and `MaximumSizeInBytes > 0`, the
resulting `WindowsLogInfo.rotating` should be `True`.
**Validates: Requirements 3.2**

### Property 13: `rotate_log` Channel Sequence
*For any* channel source, `rotate_log` should invoke `wevtutil epl` followed by
`wevtutil cl`.
**Validates: Requirements 4.1**

### Property 14: `rotate_log` File Sequence
*For any* file source, `rotate_log` should call `IFileOperator.copy` to archive
and then `write_text(path, "", elevated=<cfg>)` to zero the file.
**Validates: Requirements 4.2**

### Property 15: `rotate_log` Error Rollup
*For any* failing sub-step, `rotate_log` should return `status=FAILED` populated
from the failing step.
**Validates: Requirements 4.4**

### Property 16: `rotate_log` Dry-Run Strict
*For any* call with `dry_run=True`, `rotate_log` should NOT invoke the runner or
file operator and should return `status=DRY_RUN`.
**Validates: Requirements 4.5, 11.1**

### Property 17: `truncate_log` Channel Path
*For any* channel source, `truncate_log` should invoke `wevtutil cl <name>`.
**Validates: Requirements 5.1**

### Property 18: `truncate_log` File Path
*For any* file source, `truncate_log` should call `write_text(path, "",
elevated=<cfg>)`.
**Validates: Requirements 5.2**

### Property 19: `truncate_log` Dry-Run Strict
*For any* call with `dry_run=True`, `truncate_log` should return `status=DRY_RUN`
with no runner or file-operator call.
**Validates: Requirements 5.5, 11.1**

### Property 20: `configure_rotation` Channel Idempotency
*For any* channel whose current policy matches the requested policy,
`configure_rotation` should return `status=SKIPPED`.
**Validates: Requirements 6.3**

### Property 21: `configure_rotation` Channel Command
*For any* channel where policy differs, `configure_rotation` should invoke
`wevtutil sl <name> /ms:<N> /rt:<true|false>`.
**Validates: Requirements 6.1**

### Property 22: `configure_rotation` File Drop-In
*For any* file source, `configure_rotation` should render JSON and call
`write_text(drop-in path, JSON, elevated=<cfg>)`.
**Validates: Requirements 6.2**

### Property 23: `configure_rotation` Dry-Run Strict
*For any* call with `dry_run=True`, `configure_rotation` should return
`status=DRY_RUN` with no runner or file-operator call.
**Validates: Requirements 6.5, 11.1**

### Property 24: `remove_rotation_config` Channel Default Revert
*For any* channel whose current policy is not the default, `remove_rotation_config`
should invoke `wevtutil sl <name> /ms:20971520 /rt:false`.
**Validates: Requirements 7.1**

### Property 25: `remove_rotation_config` Idempotency
*For any* channel already at default, or any file with no drop-in,
`remove_rotation_config` should return `status=SKIPPED`.
**Validates: Requirements 7.2**

### Property 26: `get_rotation_config` Parse
*For any* channel, `get_rotation_config` should parse `wevtutil gl` output into a
`WindowsRotationPolicy`.
**Validates: Requirements 8.1**

### Property 27: `query_disk_usage` Channel Parse
*For any* channel, `query_disk_usage` should parse the `fileSize:` line from
`wevtutil gl` into `size_bytes` and derive `size_human` in pure Python.
**Validates: Requirements 9.1**

### Property 28: Elevation for Writes
*For any* write operation with `elevated=True` in the config, the runner or file
operator should be invoked with `elevated=True`.
**Validates: Requirements 10.1**

### Property 29: No Elevation for Non-Security Reads
*For any* read operation against a non-Security source, the runner and file
operator should never be invoked with `elevated=True`.
**Validates: Requirements 10.2**

### Property 30: Security Read Elevation
*For any* read operation against `Security`, the runner should be invoked with
`elevated=True` (contract-permitted exception).
**Validates: Requirements 10.3**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── logmanagement.py          ← WindowsLogManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_logmanagement.py
```

---

## Usage Examples

### Local Execution

```python
from ccai.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.windows.fileoperators import LocalFileOperator
from ccai.sysman.windows.logmanagement import (
    WindowsLogManager,
    WindowsLogManagerConfig,
    WindowsRotationPolicy,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

log_mgr = WindowsLogManager(
    runner=runner,
    file_operator=file_operator,
    config=WindowsLogManagerConfig(elevated=True, dry_run=False),
)

entries = log_mgr.get_log(source="Application", lines=100, severity="err")
for entry in entries.entries:
    print(f"{entry.timestamp} {entry.severity} {entry.message}")

policy = WindowsRotationPolicy(
    source="Application",
    keep_count=7,
    frequency="size:104857600",
    compress=False,
    max_size_bytes=104857600,
    post_rotate=None,
)
result = log_mgr.configure_rotation(source="Application", policy=policy)
print(f"Configure rotation: {result.status}")

rotate_result = log_mgr.rotate_log(source="Application")
print(f"Rotate: {rotate_result.status}")
```

### Remote Execution with Aspects

```python
from ccai.sysman.windows.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.windows.aspects import (
    PersistenceAspect, PersistenceAspectConfig,
    RetryAspect, RetryAspectConfig,
    TimeoutAspect, TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage
from ccai.sysman.windows.fileoperators import SSHFileOperator
from ccai.sysman.windows.logmanagement import WindowsLogManager, WindowsLogManagerConfig

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-win", username="admin"))

storage = SQLiteStorage(path=r"C:\ProgramData\CodeCraftAI\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
log_mgr = WindowsLogManager(
    runner=runner,
    file_operator=file_operator,
    config=WindowsLogManagerConfig(elevated=True),
)

result = log_mgr.truncate_log(source="Application")
print(f"Truncate: {result.status}")
```

### Dry-Run Mode

```python
cfg = WindowsLogManagerConfig(elevated=True, dry_run=True)
log_mgr = WindowsLogManager(runner=runner, file_operator=file_operator, config=cfg)

rot_result = log_mgr.rotate_log(source="Application")
assert rot_result.status == WindowsLogActionStatus.DRY_RUN
print(rot_result.detail)  # "Would archive Application.evtx and clear channel"

trunc_result = log_mgr.truncate_log(source=r"C:\ProgramData\MyApp\logs\app.log")
assert trunc_result.status == WindowsLogActionStatus.DRY_RUN
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on command args prefix returns canned
   `ConvertTo-Json` stdout for `get_log`; asserts the runner received the
   correct PowerShell argument list for each filter combination.
2. `MockFileOperator` backed by an in-memory `dict[str, str]` drives
   `configure_rotation` idempotency tests for file sources.
3. `rotate_log` channel test asserts `wevtutil epl` then `wevtutil cl` are
   invoked in order; the file test asserts `copy` then `write_text` with empty
   content.
4. `list_logs` parser test uses a representative `Get-WinEvent -ListLog *` JSON
   array and verifies the composed `WindowsLogInfo` list, including
   `rotating` derivation.
5. `query_disk_usage` unit tests cover the `wevtutil gl fileSize:` parse for
   channels and the `(Get-Item).Length` path for files.
6. `WindowsRotationPolicy` render/parse round-trip test for the file JSON form,
   plus a `wevtutil gl` → policy parse test for channels.
7. Dry-run matrix test iterates every write verb and asserts zero mutating runner
   or file-operator calls.
8. Security-channel elevation test proves `Security` reads pass `elevated=True`
   while non-`Security` reads do not.
9. No test requires Administrator access or a live Windows host.

---

## Scope Mapping

| Abstract | Windows |
|---|---|
| Log source (channel) | Event Log channel name like `Application` or `Microsoft-Windows-…/Operational` |
| Log source (file) | Absolute path, e.g., `C:\ProgramData\MyApp\logs\app.log` |
| Rotation drop-in store | `C:\ProgramData\CodeCraftAI\logrotation\<basename>.json` for file sources; `wevtutil sl` for channels |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `IFileOperator` for file-source I/O in
  addition to `ICommandRunner`.
- **Security-channel read elevation.**  Exercises the contract-permitted
  elevation exception for protected channels.
- **Strict dry-run for `rotate_log`.**  Windows lacks a native simulation CLI
  equivalent, so the contract's simulation exception is intentionally unused —
  all write verbs short-circuit symmetrically in dry-run mode.
- **JSON-serialised policy store for file sources.**  Channels use native
  `wevtutil sl`; files persist policy as JSON in
  `C:\ProgramData\CodeCraftAI\logrotation\<basename>.json`.

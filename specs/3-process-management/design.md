# Design

## Overview

This spec implements the process-management contract defined at
`codecraftai-sysman-common/specs/contracts/process-management/contract.md`
for Windows using the PowerShell ``Get-Process``, ``Stop-Process``,
``Start-Process``, ``Wait-Process``, and ``taskkill`` cmdlets.

The process-management feature is implemented as a single module,
`ccai.sysman.windows.processmanagement`, that exposes a
`WindowsProcessManager` class.  The coordinator instantiates
`WindowsProcessManager` and calls its read and write verbs to manage
arbitrary processes on Windows.  Every verb returns a typed
`Windows*` dataclass; reads raise `WindowsProcessQueryError` on
unexpected failures while writes return
`WindowsProcessActionResult` with one of four statuses (`SUCCESS` /
`FAILED` / `SKIPPED` / `DRY_RUN`).

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
When using SSH, the coordinator can track reliability metrics and
apply execution aspects (persistence, retry, timeout) without any
changes to `WindowsProcessManager` code.

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
│  - Injects runner into WindowsProcessManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsProcessManager                            │
│  ccai.sysman.windows.processmanagement                  │
│                                                             │
│  list_processes()        get_process()                      │
│  find_processes_by_name() wait_for_process()                │
│  kill_process()          kill_process_tree()                │
│  start_process()         set_process_priority()             │
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
│ - powershell.exe     │      │ - WinRM / ssh        │
│ - admin token policy │      │ - reliability metrics│
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

### `WindowsProcessManager`

**Location:** `source/packages/ccai/sysman/windows/processmanagement.py`

| Method | Returns | Description |
|---|---|---|
| `list_processes(owner=None)` | `list[WindowsProcessInfo]` | Run `Get-Process | Select-Object … | ConvertTo-Json` |
| `get_process(pid)` | `WindowsProcessInfo \| None` | Run `Get-Process -Id <pid> | Select-Object … | ConvertTo-Json` |
| `find_processes_by_name(name)` | `list[WindowsProcessInfo]` | Run `Get-Process -Name <name> -ErrorAction SilentlyContinue | …` |
| `wait_for_process(pid, timeout_seconds)` | `WindowsProcessWaitResult` | Run `Wait-Process -Id <pid> [-Timeout <t>]` |
| `kill_process(pid, signal=None)` | `WindowsProcessActionResult` | Run `Stop-Process -Id <pid> -Force` |
| `kill_process_tree(pid, signal=None)` | `WindowsProcessActionResult` | Run `taskkill /F /T /PID <pid>` |
| `start_process(spec)` | `WindowsProcessActionResult` | Run `Start-Process -FilePath … -PassThru | Select-Object Id | ConvertTo-Json` |
| `set_process_priority(pid, priority)` | `WindowsProcessActionResult` | Run `(Get-Process -Id <pid>).PriorityClass = '<class>'` |

### `WindowsProcessManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutating command is dispatched |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `powershell_executable` | `str` | `"powershell.exe"` | Path to the PowerShell host (`"pwsh.exe"` for PowerShell 7+) |
| `priority_map` | `dict \| None` | `None` | Optional override for the integer-to-class mapping |
| `wait_poll_interval_seconds` | `float` | `0.2` | Reserved for a future Python-side `Wait-Process` shim |

---

## Data Models

```python
class WindowsProcessActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class WindowsProcessPriorityClass(Enum):
    IDLE = "Idle"
    BELOW_NORMAL = "BelowNormal"
    NORMAL = "Normal"
    ABOVE_NORMAL = "AboveNormal"
    HIGH = "High"
    REAL_TIME = "RealTime"


@dataclass
class WindowsProcessInfo:
    pid: int
    ppid: int | None
    user: str | None
    state: str | None
    priority: int | None
    nice: int | None
    vsz: int | None
    rss: int | None
    cpu_percent: float | None
    mem_percent: float | None
    elapsed: str | None
    command: str | None
    argv: list[str]


@dataclass
class WindowsProcessSpec:
    executable: str
    args: list[str]
    working_dir: str | None
    env: dict[str, str] | None
    user: str | None


@dataclass
class WindowsProcessActionResult:
    status: WindowsProcessActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    pid: int | None
    signal: str | None


@dataclass
class WindowsProcessWaitResult:
    pid: int
    exit_code: int | None
    timed_out: bool


class WindowsProcessQueryError(Exception):
    command: list[str]
    exit_code: int | None
    stderr: str
```

---

## Process-Identity Mapping

| Concept | Windows surface |
|---|---|
| PID | Numeric Windows PID; integer in every `Get-Process -Id`, `Stop-Process -Id`, `Wait-Process -Id`, `taskkill /PID` invocation |
| Owner | `UserName` from `Get-Process -IncludeUserName` (form `"DOMAIN\\user"`) |
| Signal | No POSIX signal facility; the contract's `ProcessSignal` enum maps to either `Stop-Process -Force` or `FAILED` per the table below |
| Priority | Six-class enum (`Idle / BelowNormal / Normal / AboveNormal / High / RealTime`); the contract's integer priority is mapped via the configurable `priority_map` |
| Process tree | `taskkill /T /PID <pid>` walks the entire descendant tree natively |

### Signal → Action Table

| ProcessSignal | Windows action |
|---|---|
| `TERM` | `Stop-Process -Id <pid> -Force` |
| `KILL` | `Stop-Process -Id <pid> -Force` |
| `HUP`  | FAILED with detail `"signal HUP has no Windows mapping"` |
| `INT`  | FAILED with detail `"signal INT has no Windows mapping"` |
| `QUIT` | FAILED with detail `"signal QUIT has no Windows mapping"` |
| `USR1` | FAILED with detail `"signal USR1 has no Windows mapping"` |
| `USR2` | FAILED with detail `"signal USR2 has no Windows mapping"` |

The current pilot collapses `TERM` and `KILL` to the same
`-Force` invocation.  A future iteration could send a graceful
`Stop-Process` (without `-Force`) for `TERM` and reserve `-Force`
for `KILL`; that change is recorded in `tasks.md`.

---

## Command Mappings

### Read Commands (no elevation)

| Verb | Script |
|---|---|
| `list_processes` | `Get-Process | Select-Object Id,Name,Path,UserName,CPU,WorkingSet64,VirtualMemorySize64,StartTime,PriorityClass | ConvertTo-Json -Compress -Depth 5` |
| `list_processes(owner=<u>)` | `Get-Process -IncludeUserName | Where-Object { $_.UserName -eq '<u>' } | Select-Object … | ConvertTo-Json …` |
| `get_process` | `Get-Process -Id <pid> | Select-Object … | ConvertTo-Json …` |
| `find_processes_by_name` | `Get-Process -Name '<name>' -ErrorAction SilentlyContinue | Select-Object … | ConvertTo-Json …` |
| `wait_for_process` | `Wait-Process -Id <pid> [-Timeout <s>]` |

### Write Commands (elevation honored)

| Verb | Script |
|---|---|
| `kill_process` | `Stop-Process -Id <pid> -Force` |
| `kill_process_tree` | `taskkill /F /T /PID <pid>` |
| `start_process` | `Start-Process -FilePath '<exe>' [-ArgumentList @(...)] [-WorkingDirectory '<wd>'] -PassThru | Select-Object Id | ConvertTo-Json -Compress -Depth 5` |
| `set_process_priority` | `(Get-Process -Id <pid>).PriorityClass = '<class>'` |

Every script is wrapped in the canonical PowerShell argv:

```
[<ps>, "-NoProfile", "-NonInteractive", "-Command", <script>]
```

so `$PROFILE` cannot mutate cmdlet behaviour and PowerShell never
blocks waiting for interactive input.

---

## `Get-Process` Output Parsing

`ConvertTo-Json -Compress -Depth 5` emits a single object when one
record is present and a JSON array when many records are present.
The parser uses a `_coerce_json_records`-style helper to coerce
both shapes into a list and then maps each record onto a
`WindowsProcessInfo`.

Sample record (single-process shape):

```json
{
  "Id": 4242,
  "Name": "explorer",
  "Path": "C:\\Windows\\explorer.exe",
  "UserName": "DOMAIN\\alice",
  "CPU": 0.5,
  "WorkingSet64": 41943040,
  "VirtualMemorySize64": 209715200,
  "StartTime": "/Date(1700000000000)/",
  "PriorityClass": "Normal"
}
```

Field mapping:

| Get-Process field | `WindowsProcessInfo` field |
|---|---|
| `Id` | `pid` |
| `Name` | `command` |
| `Path` | `argv[0]` (when present) |
| `UserName` | `user` |
| `CPU` | `cpu_percent` |
| `WorkingSet64` | `rss` |
| `VirtualMemorySize64` | `vsz` |
| `StartTime` | `elapsed` (kept as the original `/Date(...)/` token in this pilot) |
| `PriorityClass` | `state` (label form via `_coerce_priority_class_string`) |

`PriorityClass` may serialise as either a string label
(PowerShell 7+) or an integer (PowerShell 5.1).  The
`_coerce_priority_class_string` helper accepts both shapes and
returns the canonical six-letter label.

---

## `Start-Process` PID Recovery

`Start-Process -PassThru` returns the spawned process object;
piping that into `Select-Object Id | ConvertTo-Json -Compress
-Depth 5` produces:

```json
{"Id": 7777}
```

The parser extracts `Id` and populates the result `pid` on
`SUCCESS`.  This is the single biggest improvement over the Linux
/ macOS pilots, which fall back to `pgrep` lookups because
`setsid` / `nohup` do not surface a PID.

---

## Idempotency Guards

| Verb | Guard | Probe |
|---|---|---|
| `kill_process` | PID already gone | `get_process(pid)` is `None` |
| `kill_process_tree` | Root PID already gone | `get_process(pid)` is `None` |
| `set_process_priority` | Current `PriorityClass` already matches | `get_process(pid).state == target_class.value` |
| `start_process` | None — always runs | — |
| `wait_for_process` | None — `Wait-Process` short-circuits when the PID is gone | — |

---

## Priority Integer-to-Class Mapping

`_priority_int_to_class` maps the contract's integer priority to a
`WindowsProcessPriorityClass`.  Default mapping:

| `priority` | Class |
|---|---|
| `-20` | `RealTime` |
| `-10` | `High` |
| `-5`  | `AboveNormal` |
| `0`   | `Normal` |
| `10`  | `BelowNormal` |
| `19`  | `Idle` |

Values not in the map select the closest key by absolute distance
so callers can pass arbitrary integers without having to memorise
the canonical six.  The mapping is fully overridable via the
config's `priority_map` field.

---

## `Wait-Process` Behavior

`Wait-Process` exits 0 when the PID disappears within the timeout
and non-zero with stderr containing `TimeoutException` (or
`Timeout`) when the timeout elapses.  An unknown PID is reported
as `Cannot find a process with the process identifier`; the
manager treats that as "process is not running" → `timed_out=False`.

Per the contract, `wait_for_process` MUST NOT raise on timeout.
This pilot also chooses to NOT raise on any other Wait-Process
failure mode — unexpected non-zero exits surface as a wait result
carrying the exit code rather than an exception.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsProcessQueryError` | `list_processes`, `get_process`, or `find_processes_by_name` observes an unexpected non-zero exit OR a malformed JSON response |

### Write Errors

Writes never raise; failures surface as
`WindowsProcessActionResult(status=FAILED, exit_code=..., stderr=...)`.

---

## Correctness Properties

### Property 1: list_processes Argv Construction

*For any* call to `list_processes()`, the runner SHALL receive the
canonical PowerShell argv whose final element is
`Get-Process | Select-Object … | ConvertTo-Json -Compress -Depth 5`,
with `elevated=False`.

**Validates: Requirements 1.1, 1.6**

### Property 2: list_processes Owner Filter

*For any* call to `list_processes(owner=<u>)`, the script SHALL
include `Get-Process -IncludeUserName | Where-Object { $_.UserName
-eq '<u>' }`.

**Validates: Requirement 1.2**

### Property 3: list_processes Parser Coverage

*For any* JSON array of N records, `list_processes` SHALL emit
exactly N `WindowsProcessInfo` records.

**Validates: Requirement 1.3**

### Property 4: list_processes Failure Surfacing

*For any* non-zero PowerShell exit, `list_processes` SHALL raise
`WindowsProcessQueryError`.

**Validates: Requirement 1.4**

### Property 5: list_processes JSON Robustness

*For any* malformed JSON payload, `list_processes` SHALL raise
`WindowsProcessQueryError` with a parse-failure message rather
than surface the raw `json.JSONDecodeError`.

**Validates: Requirement 1.5**

### Property 6: get_process Argv Construction

*For any* call to `get_process(pid)`, the script SHALL be
`Get-Process -Id <pid> | Select-Object … | ConvertTo-Json …`.

**Validates: Requirement 2.1**

### Property 7: get_process Returns None for Unknown PID

*For any* `Get-Process -Id` invocation that exits non-zero with
stderr containing `"Cannot find a process"` (or empty stderr),
`get_process` SHALL return `None`.

**Validates: Requirements 2.3, 2.4**

### Property 8: find_processes_by_name Argv Construction

*For any* call to `find_processes_by_name(name)`, the script SHALL
include `Get-Process -Name '<name>' -ErrorAction SilentlyContinue`.

**Validates: Requirement 3.1**

### Property 9: find_processes_by_name Empty Match Handling

*For any* PowerShell invocation that exits non-zero with stderr
containing `"Cannot find"` (or empty stderr),
`find_processes_by_name` SHALL return an empty list, not raise.

**Validates: Requirement 3.3**

### Property 10: kill_process Mapped Signal Argv

*For any* call to `kill_process(pid, signal)` with `signal ∈
{TERM, KILL}` against an existing PID in non-dry-run mode, the
script SHALL be `Stop-Process -Id <pid> -Force`.

**Validates: Requirement 4.1**

### Property 11: kill_process Default Signal

*For any* call to `kill_process(pid)` without an explicit signal,
the manager SHALL substitute `"TERM"` and dispatch the same
`Stop-Process -Force` command.

**Validates: Requirement 4.2**

### Property 12: kill_process Unmapped Signal

*For any* signal in `{HUP, INT, QUIT, USR1, USR2}`, `kill_process`
SHALL return `FAILED` with detail `"signal <sig> has no Windows
mapping"` and SHALL NOT invoke the runner.

**Validates: Requirement 4.3**

### Property 13: kill_process Idempotency

*For any* PID that does not appear in `get_process`, `kill_process`
SHALL return `SKIPPED` without invoking `Stop-Process`.

**Validates: Requirement 4.4**

### Property 14: kill_process Dry-Run

*For any* call to `kill_process` with `config.dry_run=True`, the
runner SHALL NOT be invoked and the result SHALL carry status
`DRY_RUN` and the unrun script.

**Validates: Requirement 4.5**

### Property 15: kill_process_tree Argv

*For any* call to `kill_process_tree(pid)` against an existing PID
in non-dry-run mode with a mapped signal, the script SHALL be
`taskkill /F /T /PID <pid>`.

**Validates: Requirement 5.1**

### Property 16: start_process Argv

*For any* call to `start_process(spec)`, the script SHALL begin
with `Start-Process -FilePath '<exe>'`, include any `-ArgumentList`
/ `-WorkingDirectory` clauses derived from the spec, and end with
`-PassThru | Select-Object Id | ConvertTo-Json -Compress -Depth 5`.

**Validates: Requirement 6.1**

### Property 17: start_process Cwd / Env Forwarding

*For any* call to `start_process(spec)`, the runner SHALL receive
`cwd=spec.working_dir` and `env=spec.env`.

**Validates: Requirement 6.2**

### Property 18: start_process Dry-Run

*For any* call to `start_process` with `config.dry_run=True`, the
runner SHALL NOT be invoked and the result SHALL carry status
`DRY_RUN`.

**Validates: Requirement 6.3**

### Property 19: start_process PID From -PassThru

*For any* successful `start_process` invocation, the result `pid`
SHALL equal the `Id` field decoded from the
`Select-Object Id | ConvertTo-Json` response.

**Validates: Requirement 6.4**

### Property 20: set_process_priority Argv

*For any* call to `set_process_priority(pid, priority)` with a
mapped class that differs from the current class, the script SHALL
be `(Get-Process -Id <pid>).PriorityClass = '<class>'`.

**Validates: Requirement 7.2**

### Property 21: set_process_priority Idempotency

*For any* PID whose current `PriorityClass` already equals the
mapped class, `set_process_priority` SHALL return `SKIPPED`
without invoking the setter.

**Validates: Requirement 7.4**

### Property 22: wait_for_process Argv (No Timeout)

*For any* call to `wait_for_process(pid, timeout_seconds=None)`,
the script SHALL be `Wait-Process -Id <pid>`.

**Validates: Requirement 8.1**

### Property 23: wait_for_process Argv (With Timeout)

*For any* call to `wait_for_process(pid, timeout_seconds=t)`, the
script SHALL be `Wait-Process -Id <pid> -Timeout <int(t)>`.

**Validates: Requirement 8.2**

### Property 24: wait_for_process Timeout Path

*For any* `Wait-Process` invocation that exits non-zero with
stderr containing `"TimeoutException"`, `wait_for_process` SHALL
return `WindowsProcessWaitResult(timed_out=True)` rather than
raise.

**Validates: Requirement 8.4**

### Property 25: Reads Never Elevate

*For any* call to `list_processes`, `get_process`,
`find_processes_by_name`, or `wait_for_process`, every runner
invocation SHALL pass `elevated=False`, regardless of
`config.elevated`.

**Validates: Requirement 10.1**

### Property 26: Writes Honor Elevation

*For any* call to `kill_process`, `kill_process_tree`,
`start_process`, or `set_process_priority` with `config.elevated=
True`, every runner invocation issued by the verb SHALL pass
`elevated=True`.

**Validates: Requirements 4.8, 5.3, 6.6, 7.7**

### Property 27: No Hidden State

*For any* sequence of calls against the same `WindowsProcessManager`
instance, the manager SHALL hold no mutable state across calls
beyond the injected runner and config.

**Validates: Requirement 9.5, 11.1**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
└── processmanagement.py            ← manager + dataclasses + parsers + exception

source/testroots/testbase/ccai/tests/sysman/windows/
└── test_processmanagement.py       ← unit tests using a MockCommandRunner
```

---

## Usage Examples

### Local execution

```python
from ccai.sysman.windows.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.windows.processmanagement import (
    WindowsProcessManager,
    WindowsProcessManagerConfig,
    WindowsProcessSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsProcessManagerConfig(elevated=False)
proc = WindowsProcessManager(runner, config)

# Read
processes = proc.list_processes()
info = proc.get_process(pid=4)
matches = proc.find_processes_by_name(name="explorer")

# Write
proc.kill_process(pid=4242, signal="TERM")
proc.set_process_priority(pid=4242, priority=10)  # → BelowNormal
proc.start_process(spec=WindowsProcessSpec(
    executable="C:\\Windows\\notepad.exe",
))
```

### Dry-run

```python
config = WindowsProcessManagerConfig(dry_run=True)
proc = WindowsProcessManager(runner, config)

result = proc.kill_process(pid=4242)
assert result.status == WindowsProcessActionStatus.DRY_RUN
assert "Stop-Process -Id 4242 -Force" in result.command[-1]
```

### SSH execution with aspects

```python
from ccai.sysman.windows.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)
from ccai.sysman.windows.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="winhost", username="admin"))
runner.add_aspect(RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

proc = WindowsProcessManager(
    runner, WindowsProcessManagerConfig(elevated=True),
)
proc.kill_process_tree(pid=4242, signal="KILL")
```

---

## Testing Strategy

### Unit Testing

- Tests use a `_MockCommandRunner` keyed on argv tuples.  Each test
  seeds the responses for the exact PowerShell argvs the verb under
  test should produce and asserts on `runner.calls` to verify argv
  construction.
- Get-Process responses are built from a `_process_record` helper
  that returns a JSON-serialisable dict matching the
  `_LIST_SELECT_FIELDS` shape so adding new columns is a one-line
  change.
- Idempotency tests prime the `get_process` probe response so the
  verb sees an "already-gone" or "already-matches" state and never
  reaches the real write argv.
- Dry-run tests assert on `len(runner.calls) == 0`.
- The unmapped-signal test asserts that `kill_process(signal="HUP")`
  surfaces as `FAILED` without invoking the runner.
- The read-elevation test exercises every read verb under
  `config.elevated=True` and asserts that no recorded call carries
  `elevated=True`.

### Test File

`source/testroots/testbase/ccai/tests/sysman/windows/test_processmanagement.py`

### Tests Implemented

1. `test_list_processes_invokes_get_process_and_parses`
2. `test_get_process_parses_single_record`
3. `test_get_process_returns_none_for_unknown_pid`
4. `test_kill_process_invokes_stop_process`
5. `test_kill_process_skipped_when_pid_not_found`
6. `test_kill_process_dry_run_short_circuits`
7. `test_kill_process_unmapped_signal_returns_failed`
8. `test_kill_process_tree_uses_taskkill_with_t_flag`
9. `test_start_process_invokes_start_process_with_passthru`
10. `test_start_process_dry_run_short_circuits`
11. `test_set_process_priority_invokes_priorityclass_setter`
12. `test_set_process_priority_skipped_when_already_matches`
13. `test_find_processes_by_name_parses_get_process_output`
14. `test_failed_command_returns_failed_or_raises_on_read`
15. `test_reads_never_elevate`

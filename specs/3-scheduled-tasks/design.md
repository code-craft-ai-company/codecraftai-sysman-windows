# Design

## Overview

The scheduled-tasks feature is implemented as a single module,
`automojo.sysman.windows.scheduledtasks`, that exposes a
`WindowsScheduledTaskManager` class.  This spec implements the
scheduled-tasks contract defined at
`automojo-sysman-common/specs/contracts/scheduled-tasks/contract.md`
for Windows using the Task Scheduler through its PowerShell module.
The coordinator instantiates the manager with a command runner and a
config, then calls the contract's verbs.

All system interaction happens through `ICommandRunner`, dispatching
PowerShell invocations (`powershell.exe -NoProfile -NonInteractive
-Command <script>`).  No `IFileOperator` is required; Task Scheduler
owns its own persistence under `C:\Windows\System32\Tasks`.

Task registration composes three `New-Scheduled*` builders inside the
PowerShell script:

- `New-ScheduledTaskAction` — `Execute` / `Argument` / `WorkingDirectory`
- `New-ScheduledTaskTrigger` (or a `CalendarTrigger` CIM object) —
  translated from `spec.schedule`
- `New-ScheduledTaskPrincipal` — translated from `spec.owner` + `spec.user`

These are combined via `Register-ScheduledTask -Action $action -Trigger
$trigger -Principal $principal -Settings $settings -Force`.

The feature works identically whether commands execute locally (via
`WindowsLocalCommandRunner`) or remotely over WinRM (via
`WindowsWinRMCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/WinRM execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or WinRM)                     │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsScheduledTaskManager          │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│       WindowsScheduledTaskManager                           │
│  automojo.sysman.windows.scheduledtasks                     │
│                                                             │
│  create_task()   delete_task()                              │
│  enable_task()   disable_task()   run_task_now()            │
│  get_task()      list_tasks()                               │
│  validate_schedule()                                        │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to (argv = powershell -Command ...)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsScheduledTaskManager`

**Location:** `source/packages/automojo/sysman/windows/scheduledtasks.py`

| Method | Signature | Description |
|---|---|---|
| `create_task` | `(spec: WindowsTaskSpec) → WindowsScheduledTaskActionResult` | `Register-ScheduledTask -Action ... -Trigger ... -Principal ... -Force` |
| `delete_task` | `(name, owner) → WindowsScheduledTaskActionResult` | `Unregister-ScheduledTask -TaskName '<name>' -Confirm:$false` |
| `enable_task` | `(name, owner) → WindowsScheduledTaskActionResult` | `Enable-ScheduledTask -TaskName '<name>'` |
| `disable_task` | `(name, owner) → WindowsScheduledTaskActionResult` | `Disable-ScheduledTask -TaskName '<name>'` |
| `run_task_now` | `(name, owner) → WindowsScheduledTaskActionResult` | `Start-ScheduledTask -TaskName '<name>'` |
| `get_task` | `(name, owner) → WindowsTaskInfo \| None` | `Get-ScheduledTask -TaskName '<name>' \| ConvertTo-Json` |
| `list_tasks` | `(owner) → list[WindowsTaskInfo]` | `Get-ScheduledTask \| ConvertTo-Json` |
| `validate_schedule` | `(schedule) → bool` | Pure-Python seconds / shorthand / cron check |

### `WindowsScheduledTaskManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

---

## Data Models

### Enums

```python
class WindowsScheduledTaskActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"


class WindowsTaskOwner(Enum):
    SYSTEM = "system"
    USER   = "user"
```

### Dataclasses

```python
@dataclass
class WindowsTaskSpec:
    name: str
    schedule: str
    command: str
    arguments: list[str] | None = None
    working_dir: str | None = None
    env: dict[str, str] | None = None    # rendered as -EnvironmentVariable pairs
    user: str | None = None              # required when owner=USER
    owner: WindowsTaskOwner = WindowsTaskOwner.SYSTEM
    enabled: bool = True
    description: str | None = None


@dataclass
class WindowsTaskInfo:
    name: str
    owner: WindowsTaskOwner
    schedule: str | None = None
    command: str | None = None
    user: str | None = None
    enabled: bool | None = None
    active: bool | None = None
    last_run: datetime | None = None
    next_run: datetime | None = None
    readable: bool = True


@dataclass
class WindowsScheduledTaskActionResult:
    status: WindowsScheduledTaskActionStatus
    name: str
    owner: WindowsTaskOwner
    message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None


class WindowsScheduledTaskQueryError(Exception):
    """Raised when list_tasks fails, or get_task fails for an unexpected reason."""
```

---

## Owner-Scope Mapping

| `WindowsTaskOwner` | `New-ScheduledTaskPrincipal` | Notes |
|---|---|---|
| `SYSTEM` | `-UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest` | System-wide task; writes require Administrator |
| `USER` | `-UserId '<spec.user>' -LogonType Interactive` | `spec.user` is required; writes for another user require Administrator |

Task Scheduler stores the task under `C:\Windows\System32\Tasks\`
regardless of owner — the `Principal` object is how scope is
expressed.  There is no separate per-user task tree.

---

## PowerShell Script Templates

### `create_task` Script

```powershell
$action = New-ScheduledTaskAction `
    -Execute '<command>' `
    -Argument '<joined arguments>' `
    -WorkingDirectory '<working_dir>'           # omitted if null

$trigger = <rendered trigger — see Trigger Rendering>

$principal = New-ScheduledTaskPrincipal <principal args>

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName '<name>' `
    -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description '<description>' `
    -Force

# only when spec.enabled=False:
Disable-ScheduledTask -TaskName '<name>'
```

### Trigger Rendering

| `spec.schedule` form | Rendered trigger |
|---|---|
| Positive integer `N` (seconds) | `New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Seconds <N>)` |
| `minutely` | `New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)` |
| `hourly` | `New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)` |
| `daily` | `New-ScheduledTaskTrigger -Daily -At 03:00` |
| `weekly` | `New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 03:00` |
| `monthly` | CIM `MSFT_TaskTrigger` with `ScheduleByMonth` |
| `yearly` | CIM `MSFT_TaskTrigger` with `ScheduleByMonth` set to January |
| five-field cron (e.g. `0 2 * * *`) | CIM `MSFT_TaskTrigger` with `ScheduleByDayOfWeek` / `DaysInterval` / `StartBoundary` derived from the fields |

The five-field cron renderer maps minute/hour to `StartBoundary`
time-of-day; a wildcard in the day-of-week field produces a daily
trigger; a wildcard in the day-of-month field with a specific
day-of-week produces a weekly trigger.  Values outside the supported
mapping cause `validate_schedule` to return `False`.

---

## `create_task` Sequence

```
1. validate_schedule(spec.schedule); FAILED if invalid.
2. If spec.owner=USER and spec.user is None → FAILED
   message="user_required_for_user_owner".
3. Probe Get-ScheduledTask -TaskName '<name>' for current state.
   If (Actions, Triggers, Principal) already match the rendered
   spec AND enabled-state matches spec.enabled → SKIPPED.
4. If dry_run → DRY_RUN with the rendered Register argv captured.
5. Dispatch the composed PowerShell script (Register-ScheduledTask
   + optional Disable) through the runner, elevated per config.
6. Return SUCCESS or FAILED based on exit code.
```

## `delete_task` Sequence

```
1. Probe Get-ScheduledTask. If exit non-zero with "No
   MSFT_ScheduledTask objects found" → SKIPPED.
2. If dry_run → DRY_RUN.
3. Dispatch Unregister-ScheduledTask -TaskName '<name>'
   -Confirm:$false.
4. Return SUCCESS or FAILED.
```

---

## `validate_schedule` Logic

Pure-Python; no runner calls.

```
WINDOWS_SHORTHANDS = {"minutely", "hourly", "daily",
                      "weekly", "monthly", "yearly"}

1. Strip whitespace.
2. If string is a positive integer → True.
3. Lowercase-match against WINDOWS_SHORTHANDS → True.
4. Split on whitespace; if 5 fields, delegate to the cron
   five-field validator (same rules as the RHEL reference).
   - Additionally check that the fields map to a supported Task
     Scheduler trigger shape (daily, weekly with explicit DOW,
     or monthly).  Complex combinations that Task Scheduler
     cannot express — such as `*/7 * * * *` — return False.
5. Otherwise → False.
```

---

## `Get-ScheduledTask | ConvertTo-Json` Parsing

```json
[
  {
    "TaskName": "nightly-backup",
    "State": "Ready",
    "Principal": {"UserId": "SYSTEM", "RunLevel": "Highest"},
    "Actions": [
      {"Execute": "C:\\Scripts\\backup.ps1",
       "Arguments": "",
       "WorkingDirectory": "C:\\Scripts"}
    ],
    "Triggers": [
      {"StartBoundary": "2026-04-25T02:00:00",
       "DaysInterval": 1,
       "Repetition": null}
    ],
    "LastRunTime": "2026-04-24T02:00:00",
    "NextRunTime": "2026-04-25T02:00:00"
  }
]
```

Parser rules:

- `TaskName` → `name`.
- `Principal.UserId == "SYSTEM"` → `owner = SYSTEM`; else `USER`,
  `user = Principal.UserId`.
- `Actions[0].Execute` → `command`; `Actions[0].Arguments` appended
  to `command` when non-empty.
- `Triggers[0]` serialised to a human-readable form for `schedule`
  (best-effort; when the trigger is a CIM object with
  `ScheduleByMonth`, the manager renders `"monthly"`).
- `State == "Disabled"` → `enabled = False`; `State == "Running"` →
  `active = True`.
- `LastRunTime` / `NextRunTime` → parsed via
  `datetime.fromisoformat`.

Single-record outputs (bare object, not array) are normalised to a
one-element list before iteration.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_task` | `Get-ScheduledTask` exists AND (Actions, Triggers, Principal) match rendered spec AND enabled-state matches | `Get-ScheduledTask -TaskName '<name>' \| ConvertTo-Json` |
| `delete_task` | task does not exist | `Get-ScheduledTask -TaskName '<name>'` |
| `enable_task` | task `State != "Disabled"` | `Get-ScheduledTask` |
| `disable_task` | task `State == "Disabled"` | `Get-ScheduledTask` |
| `run_task_now` | never skips | — |

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsScheduledTaskQueryError` | `list_tasks` fails, or `get_task` fails for a reason other than "task not found" |

All write failures surface as
`WindowsScheduledTaskActionResult(status=FAILED, ...)` carrying the
runner's exit code, stdout, stderr, and a `message` identifying the
failing step.

---

## Correctness Properties

### Property 1: Create Task Schedule Validation

*For any* `create_task(spec)` with an invalid schedule, the result is `FAILED` with `message="invalid_schedule"` and the runner is not invoked.

**Validates: Requirements 1.2**

### Property 2: Create Task User-Required Check

*For any* `create_task(spec)` with `owner=USER` and `spec.user=None`, the result is `FAILED` with `message="user_required_for_user_owner"`.

**Validates: Requirements 1.6**

### Property 3: Create Task Register Dispatch

*For any* `create_task(spec)` whose guard does not skip, the dispatched script invokes `Register-ScheduledTask -TaskName '<name>' -Action ... -Trigger ... -Principal ... -Force`.

**Validates: Requirements 1.1**

### Property 4: Create Task Idempotency

*For any* `create_task(spec)` whose (Actions, Triggers, Principal) render-compare match the current task and enabled-state matches, the result is `SKIPPED` and the register script is not dispatched.

**Validates: Requirements 1.3**

### Property 5: Create Task System-Owner Principal

*For any* `create_task(spec)` with `owner=SYSTEM`, the principal is `New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest`.

**Validates: Requirements 1.4**

### Property 6: Create Task User-Owner Principal

*For any* `create_task(spec)` with `owner=USER` and `spec.user='alice'`, the principal is `New-ScheduledTaskPrincipal -UserId 'alice' -LogonType Interactive`.

**Validates: Requirements 1.5**

### Property 7: Create Task Disable When Not Enabled

*For any* `create_task(spec)` with `spec.enabled=False`, a `Disable-ScheduledTask -TaskName '<name>'` dispatch follows the successful Register.

**Validates: Requirements 1.7**

### Property 8: Delete Task Dispatch

*For any* `delete_task(name, owner)` where the task exists, the dispatched script is `Unregister-ScheduledTask -TaskName '<name>' -Confirm:$false`.

**Validates: Requirements 2.1**

### Property 9: Delete Task Idempotency

*For any* `delete_task(name, owner)` where the task does not exist, the result is `SKIPPED`.

**Validates: Requirements 2.2**

### Property 10: Enable Task Dispatch

*For any* `enable_task(name, owner)` where the task exists and is disabled, the dispatched script is `Enable-ScheduledTask -TaskName '<name>'`.

**Validates: Requirements 3.1**

### Property 11: Enable Task Idempotency

*For any* `enable_task(name, owner)` where the task's `State` is not `"Disabled"`, the result is `SKIPPED`.

**Validates: Requirements 3.3**

### Property 12: Enable Task Not Found

*For any* `enable_task(name, owner)` where the task does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 3.2**

### Property 13: Disable Task Dispatch

*For any* `disable_task(name, owner)` where the task exists and is enabled, the dispatched script is `Disable-ScheduledTask -TaskName '<name>'`.

**Validates: Requirements 4.1**

### Property 14: Disable Task Idempotency

*For any* `disable_task(name, owner)` where the task's `State == "Disabled"`, the result is `SKIPPED`.

**Validates: Requirements 4.3**

### Property 15: Disable Task Not Found

*For any* `disable_task(name, owner)` where the task does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 4.2**

### Property 16: Run Task Now Dispatch

*For any* `run_task_now(name, owner)` where the task exists, the dispatched script is `Start-ScheduledTask -TaskName '<name>'`.

**Validates: Requirements 5.1**

### Property 17: Run Task Now Not Found

*For any* `run_task_now(name, owner)` where the task does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 5.2**

### Property 18: Get Task Populated Info

*For any* `get_task(name, owner)` where the task exists, the returned `WindowsTaskInfo` contains `schedule`, `command`, `user`, `enabled`, `active`, `last_run`, and `next_run`.

**Validates: Requirements 6.2**

### Property 19: Get Task Missing Returns None

*For any* `get_task(name, owner)` where stderr matches `No MSFT_ScheduledTask objects found`, the result is `None`.

**Validates: Requirements 6.3**

### Property 20: Get Task Unreadable

*For any* `get_task(name, owner)` where the JSON payload cannot be parsed, the returned info has `readable=False`.

**Validates: Requirements 6.5**

### Property 21: Get Task Unexpected Error

*For any* other non-zero exit from `Get-ScheduledTask`, `get_task` raises `WindowsScheduledTaskQueryError`.

**Validates: Requirements 6.4**

### Property 22: List Tasks Filter By Owner

*For any* `list_tasks(owner)`, the returned list contains only tasks whose `Principal.UserId` matches (SYSTEM for `owner=SYSTEM`, non-SYSTEM for `owner=USER`).

**Validates: Requirements 7.2**

### Property 23: List Tasks Error

*For any* failure of `Get-ScheduledTask`, `list_tasks` raises `WindowsScheduledTaskQueryError`.

**Validates: Requirements 7.3**

### Property 24: List Tasks JSON Normalisation

*For any* single-task output emitted as a bare JSON object, `list_tasks` still returns a one-element list.

**Validates: Requirements 7.1, 7.2**

### Property 25: Validate Schedule Seconds

*For any* positive integer string, `validate_schedule` returns `True`.

**Validates: Requirements 8.1**

### Property 26: Validate Schedule Shorthand

*For any* shorthand in `{minutely, hourly, daily, weekly, monthly, yearly}`, `validate_schedule` returns `True`.

**Validates: Requirements 8.2**

### Property 27: Validate Schedule Cron

*For any* five-field cron expression that maps to a supported Task Scheduler trigger shape, `validate_schedule` returns `True`.

**Validates: Requirements 8.3**

### Property 28: Validate Schedule Side-Effect Free

*For any* `validate_schedule` call, the runner is never invoked.

**Validates: Requirements 8.5**

### Property 29: Dry-Run Safety

*For any* write method with `dry_run=True`, the runner is never invoked and the result status is `DRY_RUN`.

**Validates: Requirements 1.10, 2.5, 3.4, 4.4, 5.5**

### Property 30: Writes Elevate When Configured

*For any* write with `config.elevated=True`, the runner is invoked with `elevated=True`.

**Validates: Requirements 9.1**

### Property 31: Reads Do Not Elevate

*For any* read (`get_task`, `list_tasks`), the runner is never invoked with `elevated=True`.

**Validates: Requirements 9.2**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
└── scheduledtasks.py    ← WindowsScheduledTaskManager,
                           models, config, exception

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_scheduledtasks.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import (
    WindowsLocalCommandRunner, WindowsLocalRunnerConfig,
)
from automojo.sysman.windows.scheduledtasks import (
    WindowsScheduledTaskManager,
    WindowsScheduledTaskManagerConfig,
    WindowsTaskSpec,
    WindowsTaskOwner,
)

runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
config = WindowsScheduledTaskManagerConfig(elevated=True, dry_run=False)
task_mgr = WindowsScheduledTaskManager(runner=runner, config=config)

spec = WindowsTaskSpec(
    name="nightly-backup",
    schedule="daily",
    command="C:\\Scripts\\backup.ps1",
    owner=WindowsTaskOwner.SYSTEM,
    description="Nightly filesystem backup",
)
result = task_mgr.create_task(spec=spec)
print(f"create_task: {result.status.value}")
```

### WinRM Execution With Aspects

```python
from automojo.sysman.windows.runners import (
    WindowsWinRMCommandRunner, WindowsWinRMRunnerConfig,
)
from automojo.sysman.windows.aspects import (
    RetryAspect, RetryAspectConfig, TimeoutAspect, TimeoutAspectConfig,
)

runner = WindowsWinRMCommandRunner(
    WindowsWinRMRunnerConfig(host="win-fleet-01", username="DOMAIN\\ops"),
)
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

task_mgr = WindowsScheduledTaskManager(
    runner=runner,
    config=WindowsScheduledTaskManagerConfig(elevated=True),
)
task_mgr.disable_task(name="stale-probe", owner=WindowsTaskOwner.SYSTEM)
```

### Dry-Run Mode

```python
config = WindowsScheduledTaskManagerConfig(elevated=True, dry_run=True)
task_mgr = WindowsScheduledTaskManager(runner=runner, config=config)
result = task_mgr.delete_task(name="nightly-backup", owner=WindowsTaskOwner.SYSTEM)
assert result.status.value == "dry_run"
print(result.command)
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on argv prefix queues responses for
   `Get-ScheduledTask`, `Register-ScheduledTask`,
   `Unregister-ScheduledTask`, `Enable-ScheduledTask`,
   `Disable-ScheduledTask`, and `Start-ScheduledTask` independently.
2. `create_task` tests cover: valid schedule, invalid schedule (no
   dispatch), `user=None` with `owner=USER` (FAILED before
   dispatch), byte-identical idempotency, render mismatch triggers
   rewrite, `enabled=False` substitutes a Disable dispatch after
   Register, Register failure surfaces FAILED.
3. `delete_task` tests cover: task exists, task missing (SKIPPED),
   dispatch failure (FAILED).
4. `enable_task` / `disable_task` tests cover: already in the
   requested state, task not found, successful dispatch.
5. `run_task_now` tests cover: task exists (SUCCESS), task missing
   (FAILED with message), runner failure (FAILED with captured exit
   code).
6. `get_task` tests cover: populated info, "No MSFT_ScheduledTask
   objects found" → `None`, unparseable JSON → `readable=False`,
   unexpected error → `WindowsScheduledTaskQueryError`.
7. `list_tasks` tests cover: filtering by owner (SYSTEM vs USER),
   bare-object JSON normalisation, failure raises.
8. `validate_schedule` tests cover: seconds, shorthands, cron
   fallback, cron combinations Task Scheduler cannot express
   return `False`.
9. Dry-run tests prove every write short-circuits without invoking
   the runner.
10. Elevation tests prove writes pass `elevated=True` when
    configured and reads never do.
11. No test requires Administrator access or a live Task Scheduler
    instance.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `create_task` | `Register-ScheduledTask -Action ... -Trigger ... -Principal ... -Force` |
| `delete_task` | `Unregister-ScheduledTask -TaskName '<name>' -Confirm:$false` |
| `enable_task` | `Enable-ScheduledTask -TaskName '<name>'` |
| `disable_task` | `Disable-ScheduledTask -TaskName '<name>'` |
| `run_task_now` | `Start-ScheduledTask -TaskName '<name>'` |
| `get_task` | `Get-ScheduledTask -TaskName '<name>' \| ConvertTo-Json` |
| `list_tasks` | `Get-ScheduledTask \| ConvertTo-Json` |
| `validate_schedule` | pure-Python seconds / shorthand / cron check |

---

## Extensions Beyond the Contract

### Event-Driven Triggers

Task Scheduler supports `OnEvent`, `OnLogon`, and `OnIdle` triggers
that the base contract does not expose.  A future extension may add
`WindowsEventTaskSpec` subclass fields (`trigger_type="on_event"`,
`subscription_xml=...`) that render into `New-ScheduledTaskTrigger`
alternatives.

### Task-Folder Namespacing

By default the manager registers tasks at the Task Scheduler root
(`\`).  A future extension may add an optional `task_path` config
field that prepends `\\AutomojoSysman\\` (or similar) to every
`TaskName`, so operator-managed tasks are visibly separated from the
OS's own.

### XML Authoring

For tasks that require Task Scheduler XML features not exposed by the
PowerShell cmdlets (custom principals, multi-action XML, RunLevel
above Highest, etc.), a future extension may accept raw XML and
dispatch `Register-ScheduledTask -Xml ...`.  This is deliberately out
of scope for the base contract surface.

# Requirements

## Introduction

The scheduled-tasks feature provides the `automojo-sysman-windows`
package with the ability to manage periodic and calendar-triggered task
execution on Windows systems.  This spec implements the scheduled-tasks
contract defined at
`automojo-sysman-common/specs/contracts/scheduled-tasks/contract.md`
for Windows using the **Task Scheduler** via the `ScheduledTasks`
PowerShell module (`Register-ScheduledTask`,
`Unregister-ScheduledTask`, `Enable-ScheduledTask`,
`Disable-ScheduledTask`, `Start-ScheduledTask`, `Get-ScheduledTask`).

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* tasks to schedule — it provides the
capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through
the `automojo-interfaces` command-runner abstraction (invoking
`powershell.exe -NoProfile -NonInteractive -Command <script>`).  No
`IFileOperator` is required; Task Scheduler owns its own persistence
under `C:\Windows\System32\Tasks`.

Scope mapping for `TaskOwner`:

- `TaskOwner.SYSTEM` → task registered with `-Principal` bound to
  `SYSTEM` (`New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType
  ServiceAccount -RunLevel Highest`); writes require Administrator.
- `TaskOwner.USER` → task registered with `-Principal -UserId '<user>'
  -LogonType Interactive`; writes for another user require
  Administrator.

---

## Requirements

### Requirement 1: Create a Task

**User Story:** As a maintenance coordinator, I want to register a
scheduled task from a `WindowsTaskSpec`, so that a periodic command
runs at the requested schedule.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_task(spec)` AND the schedule
   is valid THE SYSTEM SHALL dispatch `Register-ScheduledTask
   -TaskName '<name>' -Action <action> -Trigger <trigger>
   -Principal <principal> -Settings <settings> -Force` through the
   runner with `elevated=True` when configured.
2. WHEN `spec.schedule` is NOT a valid Windows trigger specifier
   (interval seconds, shorthand, or five-field cron) THE SYSTEM
   SHALL return `status=FAILED` with `message="invalid_schedule"`
   and SHALL NOT invoke the runner.
3. WHEN `Get-ScheduledTask -TaskName '<name>'` already returns a
   task whose (Actions, Triggers, Principal) tuple matches the
   spec's rendered form AND the current enabled-state matches
   `spec.enabled` THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `spec.owner=TaskOwner.SYSTEM` THE SYSTEM SHALL construct
   `New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType
   ServiceAccount -RunLevel Highest`.
5. WHEN `spec.owner=TaskOwner.USER` AND `spec.user` is set THE
   SYSTEM SHALL construct `New-ScheduledTaskPrincipal -UserId
   '<user>' -LogonType Interactive` — the coordinator must supply
   `spec.user` for USER owner.
6. WHEN `spec.owner=TaskOwner.USER` AND `spec.user` is `None` THE
   SYSTEM SHALL return `status=FAILED` with `message="user_required_for_user_owner"`.
7. WHEN `spec.enabled=False` THE SYSTEM SHALL dispatch
   `Disable-ScheduledTask -TaskName '<name>'` immediately after
   `Register-ScheduledTask` succeeds.
8. WHEN `Register-ScheduledTask` succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
9. WHEN `Register-ScheduledTask` fails THE SYSTEM SHALL return
   `status=FAILED` with captured `exit_code`, `stdout`, `stderr`,
   and `command`.
10. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
    command and SHALL return `status=DRY_RUN`.

---

### Requirement 2: Delete a Task

**User Story:** As a maintenance coordinator, I want to delete a
scheduled task.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_task(name, owner)` AND the
   task exists THE SYSTEM SHALL dispatch `Unregister-ScheduledTask
   -TaskName '<name>' -Confirm:$false` through the runner with
   `elevated=True` when configured.
2. WHEN the task does not exist THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`
   with captured `exit_code` / `stderr`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 3: Enable a Task

**User Story:** As a maintenance coordinator, I want to enable an
existing task.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_task(name, owner)` AND the
   task exists THE SYSTEM SHALL dispatch `Enable-ScheduledTask
   -TaskName '<name>'` with `elevated=True` when configured.
2. WHEN the task does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN `Get-ScheduledTask -TaskName '<name>'` already reports
   `State=Ready` or `Running` (i.e. not `Disabled`) THE SYSTEM
   SHALL return `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 4: Disable a Task

**User Story:** As a maintenance coordinator, I want to disable an
existing task.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_task(name, owner)` AND the
   task exists THE SYSTEM SHALL dispatch `Disable-ScheduledTask
   -TaskName '<name>'` with `elevated=True` when configured.
2. WHEN the task does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN `Get-ScheduledTask -TaskName '<name>'` already reports
   `State=Disabled` THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 5: Run a Task Now

**User Story:** As a maintenance coordinator, I want to trigger a
task immediately.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_task_now(name, owner)` AND the
   task exists THE SYSTEM SHALL dispatch `Start-ScheduledTask
   -TaskName '<name>'` with `elevated=True` when configured.
2. WHEN the task does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`
   with captured `exit_code` / `stderr`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 6: Get a Task

**User Story:** As a maintenance coordinator, I want to resolve a
task by name.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_task(name, owner)` THE SYSTEM
   SHALL dispatch `Get-ScheduledTask -TaskName '<name>' |
   ConvertTo-Json -Depth 5` through the runner without elevation.
2. WHEN the command succeeds AND stdout is non-empty THE SYSTEM
   SHALL return a `WindowsTaskInfo` populated from the JSON payload
   (`TaskName`, `Actions[0].Execute` → `command`,
   `Actions[0].Arguments` → appended,
   `Triggers[0]` → `schedule`, `Principal.UserId` → `user`,
   `State` → `enabled` (`State != "Disabled"`) and `active`
   (`State == "Running"`), `LastRunTime` → `last_run`, `NextRunTime`
   → `next_run`).
3. WHEN the command exits non-zero with stderr matching `No
   MSFT_ScheduledTask objects found` THE SYSTEM SHALL return `None`.
4. WHEN the command exits non-zero for any other reason THE SYSTEM
   SHALL raise `WindowsScheduledTaskQueryError`.
5. WHEN the task exists but the JSON payload cannot be parsed THE
   SYSTEM SHALL return a `WindowsTaskInfo` with `readable=False`
   and `name` / `owner` populated.

---

### Requirement 7: List Tasks

**User Story:** As a maintenance coordinator, I want to enumerate
every task at a given owner scope.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_tasks(owner)` THE SYSTEM SHALL
   dispatch `Get-ScheduledTask | ConvertTo-Json -Depth 5` through
   the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON array
   and return a list of `WindowsTaskInfo` records, filtered by
   owner: `TaskOwner.SYSTEM` keeps tasks whose `Principal.UserId`
   equals `"SYSTEM"`; `TaskOwner.USER` keeps the rest.
3. WHEN the command exits non-zero THE SYSTEM SHALL raise
   `WindowsScheduledTaskQueryError`.
4. WHEN the output is empty THE SYSTEM SHALL return an empty list.

---

### Requirement 8: Validate a Schedule

**User Story:** As a maintenance coordinator, I want to validate a
schedule expression without dispatching any command.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate_schedule(schedule)` AND
   the string is a positive integer THE SYSTEM SHALL return `True`
   (treated as seconds for `New-ScheduledTaskTrigger -Once -At
   <now> -RepetitionInterval (New-TimeSpan -Seconds <n>)`).
2. WHEN the string is a shorthand in `{minutely, hourly, daily,
   weekly, monthly, yearly}` THE SYSTEM SHALL return `True` (renders
   to `New-ScheduledTaskTrigger -Daily -At 00:00` and similar).
3. WHEN the string is a five-field cron expression THE SYSTEM SHALL
   return `True` (the manager renders it by translating to a
   `CalendarTrigger` block).
4. WHEN the string matches none of the above THE SYSTEM SHALL return
   `False`.
5. THE SYSTEM SHALL NOT invoke the runner for `validate_schedule`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all scheduled-
task writes to run with an Administrator token.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (create_task, delete_task, enable_task, disable_task,
   run_task_now) THE SYSTEM SHALL pass `elevated=True` to the
   runner.
2. WHEN the verb is a read (`get_task`, `list_tasks`,
   `validate_schedule`) THE SYSTEM SHALL NEVER pass
   `elevated=True`.
3. WHEN the process is already running as Administrator THE SYSTEM
   SHALL NOT pass `elevated=True`.
4. WHEN `config.elevated=False` THE SYSTEM SHALL NEVER pass
   `elevated=True`; a resulting permission error SHALL surface as
   `status=FAILED` with the runner's exit code / stderr.
5. `run_task_now` targeting a task whose principal is `SYSTEM` or
   another user requires elevation just like other writes;
   `run_task_now` for a task whose principal is the running user
   does NOT require elevation, but the manager does not distinguish
   at the verb layer — it honors `config.elevated` uniformly.

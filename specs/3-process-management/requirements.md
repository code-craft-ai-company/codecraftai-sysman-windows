# Requirements

## Introduction

This spec implements the process-management contract defined at
`codecraftai-sysman-common/specs/contracts/process-management/contract.md`
for Windows using the PowerShell ``Get-Process``, ``Stop-Process``,
``Start-Process``, ``Wait-Process``, and ``taskkill`` cmdlets.

The process-management feature provides the
`codecraftai-sysman-windows` package with the ability to enumerate,
inspect, terminate, spawn, prioritise, wait for, and search for
arbitrary processes on Windows.  A maintenance coordinator running in
an external package drives all operations by calling into this
feature's public interfaces.  All system commands are executed
exclusively through the `codecraftai-interfaces` command-runner
abstraction, enabling dry-run simulation, auditing, and test mocking
without requiring a live Windows system.

This feature mixes **read** verbs (`list_processes`, `get_process`,
`find_processes_by_name`, `wait_for_process`) and **write** verbs
(`kill_process`, `kill_process_tree`, `start_process`,
`set_process_priority`).

### Differences from the Linux / macOS pilots

- Windows has no POSIX signal facility.  ``Stop-Process -Force`` is
  the closest equivalent to ``SIGKILL`` and the closest non-graceful
  equivalent to ``SIGTERM``.  ``HUP`` / ``INT`` / ``QUIT`` / ``USR1``
  / ``USR2`` have NO clean Windows mapping; this pilot returns
  `FAILED` with a "no Windows mapping" detail rather than coercing
  them to a stop.
- Windows priority is a discrete six-level enum
  (`Idle / BelowNormal / Normal / AboveNormal / High / RealTime`).
  The contract uses an integer-valued priority (POSIX nice range);
  the manager maps integers to classes via a configurable
  ``priority_map``.
- ``kill_process_tree`` uses ``taskkill /F /T /PID <pid>`` which
  performs the recursive walk natively in a single command — no
  separate children-then-root sequence is needed.
- Windows has no ``sudo``.  ``config.elevated=True`` is advisory: the
  runner is responsible for being launched with an Administrator
  token; the manager forwards `elevated=True` to the runner for write
  verbs.

---

## Requirements

### Requirement 1: Enumerate Running Processes

**User Story:** As a maintenance coordinator, I want to list every
running process on the host with PID, name, owner, CPU usage, working
set, and priority class, so that I can inventory the running workload.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_processes()` THE SYSTEM SHALL
   invoke
   `Get-Process | Select-Object Id,Name,Path,UserName,CPU,
   WorkingSet64,VirtualMemorySize64,StartTime,PriorityClass |
   ConvertTo-Json -Compress -Depth 5`
   via the command runner with `elevated=False`.
2. WHEN the coordinator passes `owner=<u>` THE SYSTEM SHALL prepend
   `Get-Process -IncludeUserName |
   Where-Object { $_.UserName -eq '<u>' } |` to the pipeline.
3. THE SYSTEM SHALL JSON-decode the output and emit one
   `WindowsProcessInfo` per record, populating `pid`, `command`,
   `argv`, `user`, `cpu_percent`, `rss`, `vsz`, `elapsed`, and
   `state` (`PriorityClass` label).
4. WHEN PowerShell exits non-zero THE SYSTEM SHALL raise
   `WindowsProcessQueryError`.
5. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `WindowsProcessQueryError` with a parse-failure message.
6. THE SYSTEM SHALL NOT pass `elevated=True` for this call.

---

### Requirement 2: Inspect a Single Process

**User Story:** As a maintenance coordinator, I want to look up a
single process by PID, so that I can confirm its existence and
priority before issuing a write.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_process(pid=<pid>)` THE SYSTEM
   SHALL invoke
   `Get-Process -Id <pid> | Select-Object … | ConvertTo-Json …`
   via the command runner with `elevated=False`.
2. WHEN PowerShell exits 0 with one parsed record THE SYSTEM SHALL
   return the parsed `WindowsProcessInfo`.
3. WHEN PowerShell exits non-zero with stderr containing the literal
   substring `"Cannot find a process"` (the canonical "unknown PID"
   shape from `Get-Process -Id`) THE SYSTEM SHALL return `None`.
4. WHEN PowerShell exits non-zero with empty stderr THE SYSTEM SHALL
   return `None`.
5. WHEN PowerShell exits non-zero with any other stderr THE SYSTEM
   SHALL raise `WindowsProcessQueryError`.
6. THE SYSTEM SHALL NOT pass `elevated=True` for this call.

---

### Requirement 3: Find Processes by Name

**User Story:** As a maintenance coordinator, I want to search for
running processes by name, so that I can locate every instance of an
application without enumerating the entire process table.

#### Acceptance Criteria

1. WHEN the coordinator calls `find_processes_by_name(name=<name>)`
   THE SYSTEM SHALL invoke
   `Get-Process -Name '<name>' -ErrorAction SilentlyContinue |
   Select-Object … | ConvertTo-Json …`
   via the command runner with `elevated=False`.
2. WHEN PowerShell exits 0 THE SYSTEM SHALL JSON-decode the output
   and emit one `WindowsProcessInfo` per record.
3. WHEN PowerShell exits non-zero with stderr containing
   `"Cannot find"` (or empty stderr) THE SYSTEM SHALL return an empty
   list, not raise.
4. WHEN PowerShell exits non-zero with any other stderr THE SYSTEM
   SHALL raise `WindowsProcessQueryError`.
5. THE SYSTEM SHALL NOT pass `elevated=True` for this call.

---

### Requirement 4: Terminate a Process

**User Story:** As a maintenance coordinator, I want to terminate a
running process, so that I can request graceful or force termination.

#### Acceptance Criteria

1. WHEN the coordinator calls `kill_process(pid=<pid>, signal=<sig>)`
   with `<sig>` in `{TERM, KILL}` THE SYSTEM SHALL invoke
   `Stop-Process -Id <pid> -Force` via the command runner.
2. WHEN `signal` is omitted THE SYSTEM SHALL substitute `"TERM"` and
   the same `Stop-Process -Force` argv.
3. WHEN `signal` is in `{HUP, INT, QUIT, USR1, USR2}` THE SYSTEM
   SHALL return `WindowsProcessActionResult` with status `FAILED`,
   detail `"signal <sig> has no Windows mapping"`, and stderr
   `"Windows does not support POSIX signal <sig>"`.  The runner
   SHALL NOT be invoked for an unmapped signal.
4. BEFORE invoking `Stop-Process`, THE SYSTEM SHALL probe the PID
   via `get_process`; WHEN the PID is no longer present THE SYSTEM
   SHALL return status `SKIPPED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return status `DRY_RUN`
   without invoking the runner.
6. WHEN the `Stop-Process` command exits 0 THE SYSTEM SHALL return
   status `SUCCESS`.
7. WHEN the `Stop-Process` command exits non-zero THE SYSTEM SHALL
   return status `FAILED` carrying the exit code, stdout, and stderr.
8. THE SYSTEM SHALL pass `elevated=config.elevated` to the runner for
   the `Stop-Process` invocation.

---

### Requirement 5: Terminate a Process Tree

**User Story:** As a maintenance coordinator, I want to terminate a
process and every descendant, so that runaway worker pools can be
reaped without leaving orphans behind.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `kill_process_tree(pid=<pid>, signal=<sig>)` with `<sig>` in
   `{TERM, KILL}` THE SYSTEM SHALL invoke
   `taskkill /F /T /PID <pid>` via the command runner.
2. ``taskkill`` performs the recursive walk natively, so the verb
   SHALL dispatch a single command rather than the children-then-root
   sequence used on Linux / macOS.
3. THE SYSTEM SHALL apply the same dry-run, idempotency, signal
   validation, and elevation rules as `kill_process`.

---

### Requirement 6: Start a New Process

**User Story:** As a maintenance coordinator, I want to spawn a new
process, so that arbitrary helper commands can be issued under the
same auditable runner path used by every other verb.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_process(spec=<spec>)` THE
   SYSTEM SHALL build a script of the form
   `Start-Process -FilePath '<exe>' [-ArgumentList @('<a>','<b>',...)]
   [-WorkingDirectory '<wd>'] -PassThru | Select-Object Id |
   ConvertTo-Json -Compress -Depth 5`
   and invoke it via the command runner.
2. THE SYSTEM SHALL forward `spec.working_dir` to the runner as
   `cwd` (in addition to the `-WorkingDirectory` arg) and `spec.env`
   as `env`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return status `DRY_RUN`
   without invoking the runner.
4. WHEN the runner exits 0 THE SYSTEM SHALL return status `SUCCESS`
   and SHALL populate `pid` from the JSON response (the
   `-PassThru | Select-Object Id` pipeline returns the spawned
   process's PID).
5. WHEN the runner exits non-zero THE SYSTEM SHALL return status
   `FAILED` carrying the exit code, stdout, and stderr.
6. THE SYSTEM SHALL pass `elevated=config.elevated` to the runner.

---

### Requirement 7: Adjust Process Priority

**User Story:** As a maintenance coordinator, I want to adjust the
priority of a running process, so that long-running workloads can be
backgrounded.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `set_process_priority(pid=<pid>, priority=<n>)` THE SYSTEM SHALL
   map `<n>` to a `WindowsProcessPriorityClass` via
   `config.priority_map` (or the default map: -20 → RealTime,
   -10 → High, -5 → AboveNormal, 0 → Normal, 10 → BelowNormal,
   19 → Idle).  Values not in the map SHALL select the closest key
   by absolute distance.
2. THE SYSTEM SHALL invoke
   `(Get-Process -Id <pid>).PriorityClass = '<class>'`
   via the command runner.
3. WHEN `dry_run=True` THE SYSTEM SHALL return status `DRY_RUN`
   without invoking the runner.
4. THE SYSTEM SHALL probe the current priority via `get_process`;
   when the existing `state` field already equals the mapped class
   label THE SYSTEM SHALL return status `SKIPPED` and SHALL NOT
   invoke the setter.
5. WHEN the setter exits 0 THE SYSTEM SHALL return status `SUCCESS`.
6. WHEN the setter exits non-zero THE SYSTEM SHALL return status
   `FAILED`.
7. THE SYSTEM SHALL pass `elevated=config.elevated` to the runner.

---

### Requirement 8: Wait for a Process to Exit

**User Story:** As a maintenance coordinator, I want to wait for a
process to exit with an optional timeout, so that orchestration
pipelines can pause until prior work has settled.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `wait_for_process(pid=<pid>, timeout_seconds=None)` THE SYSTEM
   SHALL invoke `Wait-Process -Id <pid>` (no timeout) via the
   command runner.
2. WHEN `timeout_seconds` is supplied THE SYSTEM SHALL invoke
   `Wait-Process -Id <pid> -Timeout <int(timeout_seconds)>`.
3. WHEN `Wait-Process` exits 0 THE SYSTEM SHALL return
   `WindowsProcessWaitResult(timed_out=False)`.
4. WHEN `Wait-Process` exits non-zero with stderr containing
   `"TimeoutException"` or `"Timeout"` THE SYSTEM SHALL return
   `WindowsProcessWaitResult(timed_out=True)` rather than raise.
5. WHEN `Wait-Process` exits non-zero with stderr containing
   `"Cannot find"` (the PID is already gone) THE SYSTEM SHALL return
   `WindowsProcessWaitResult(timed_out=False, exit_code=None)`.
6. WHEN `Wait-Process` exits non-zero for any other reason THE
   SYSTEM SHALL still return a `WindowsProcessWaitResult` (carrying
   the exit code on the result) rather than raise — the contract
   says wait MUST NOT raise on timeout.
7. THE SYSTEM SHALL NOT pass `elevated=True` for any `Wait-Process`
   invocation.

---

### Requirement 9: Write Discipline

**User Story:** As a maintenance coordinator, I want every write verb
to be safe under retry / persistence / timeout aspects, so that
process-management actions can be issued under whatever runner
configuration the coordinator already has set up.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER raise from a write verb.  Every write
   SHALL return `WindowsProcessActionResult` with one of the four
   statuses `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`.
2. ON `FAILED` THE SYSTEM SHALL populate `exit_code`, `stdout`,
   `stderr`, and `command` on the result.
3. ON `SKIPPED` THE SYSTEM SHALL populate `command` with the argv
   that *would* have been executed and `detail` with a
   human-readable reason.
4. ON `DRY_RUN` THE SYSTEM SHALL populate `command` with the argv
   that *would* have been executed and SHALL NOT invoke the runner.
5. THE SYSTEM SHALL hold no state across calls beyond the injected
   runner and config.

---

### Requirement 10: Read Discipline

**User Story:** As a maintenance coordinator, I want every read verb
to be strictly read-only and never elevate.

#### Acceptance Criteria

1. Every runner invocation issued by `list_processes`, `get_process`,
   `find_processes_by_name`, and `wait_for_process` SHALL pass
   `elevated=False` regardless of `config.elevated`.
2. No read verb SHALL invoke a command that mutates host state.

---

### Requirement 11: Aspect-Transparent Execution

**User Story:** As a maintenance coordinator, I want every
process-management verb to be safe under retry / persistence / timeout
aspects.

#### Acceptance Criteria

1. THE manager SHALL accept an `ICommandRunner` and a
   `WindowsProcessManagerConfig` in its constructor and SHALL hold no
   other mutable state.
2. EACH public verb SHALL parse only its own command's output; it
   SHALL NOT read across verbs.
3. WHEN a retry aspect re-runs a transient failure THE SYSTEM SHALL
   produce the same dataclass shape from the retried success that it
   would have produced from a first-try success.

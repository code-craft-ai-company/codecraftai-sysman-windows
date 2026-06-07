# Requirements

## Introduction

The service-management feature provides the `codecraftai-sysman-windows` package with
the ability to manage Windows Service Control Manager (SCM) services using
PowerShell's `Start-Service`, `Stop-Service`, `Restart-Service`, `Get-Service`, and
`Set-Service` cmdlets.  This spec implements the service-management contract defined
at `codecraftai-sysman-common/specs/contracts/service-management/contract.md` for
Windows using those cmdlets through an `ICommandRunner`.  It covers
start/stop/restart, reload (aliased to restart — Windows has no reload primitive),
enable/disable, mask/unmask (aliased to disable/enable — Windows has no mask), status
queries, service listing, and a no-op `reload_daemon`.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interface.  The feature does not decide *what* to
run — it provides the capability to do so reliably, idempotently, and auditably.  All
system interaction happens through `ICommandRunner`.  The abstract
`ServiceScope.SYSTEM` is the only scope supported; `ServiceScope.USER` returns
`FAILED` with a descriptive message because Windows per-user services (those with a
`_<sessionid>` suffix) are out of scope for the base contract.  Writes require the
host process to already be elevated (see the command-execution spec's elevation
pre-check extension); reads do not.

---

## Requirements

### Requirement 1: Start a Service

**User Story:** As a maintenance coordinator, I want to start a Windows service,
idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_service(name, scope)` with `scope=SYSTEM` THE
   SYSTEM SHALL invoke `["powershell.exe", "-NoProfile", "-Command",
   "Start-Service -Name <name>"]` via the command runner.
2. WHEN the current `active_state` is already `"active"` (Windows: `"Running"`) THE
   SYSTEM SHALL return `status=SKIPPED` without invoking the start command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with captured
   fields.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return
   `status=FAILED, message="USER scope not supported on Windows"` without invoking
   the runner.

---

### Requirement 2: Stop a Service

**User Story:** As a maintenance coordinator, I want to stop a Windows service,
idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `stop_service(name, scope)` with `scope=SYSTEM` THE
   SYSTEM SHALL invoke `Stop-Service -Name <name> -Force` via the command runner.
2. WHEN the current `active_state` is `"inactive"` (Windows: `"Stopped"`) THE
   SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with captured
   fields.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED` without invoking the
   runner.

---

### Requirement 3: Restart a Service

**User Story:** As a maintenance coordinator, I want to restart a Windows service
unconditionally.

#### Acceptance Criteria

1. WHEN the coordinator calls `restart_service(name, scope)` THE SYSTEM SHALL invoke
   `Restart-Service -Name <name> -Force`.
2. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `restart_service` is called the idempotency guard SHALL NOT skip the
   command regardless of state.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED` without invoking the
   runner.

---

### Requirement 4: Reload a Service (aliased to restart)

**User Story:** As a maintenance coordinator, I want a uniform `reload_service`
verb on Windows even though SCM has no reload primitive.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_service(name, scope)` AND the current
   `active_state` is `"active"` THE SYSTEM SHALL invoke `Restart-Service -Name
   <name> -Force` AND SHALL populate `message="Windows has no reload primitive;
   aliased to restart"`.
2. WHEN the current `active_state` is not `"active"` THE SYSTEM SHALL return
   `status=FAILED` with `message="service is not active; cannot reload"`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 5: Enable a Service

**User Story:** As a maintenance coordinator, I want to enable a Windows service
for automatic start at boot, idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_service(name, scope)` THE SYSTEM SHALL invoke
   `Set-Service -Name <name> -StartupType Automatic`.
2. WHEN the current `enabled_state` is already `"enabled"` (Windows: `Automatic` or
   `AutomaticDelayedStart`) THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 6: Disable a Service

**User Story:** As a maintenance coordinator, I want to disable a Windows service
so that it does not start at boot, idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_service(name, scope)` THE SYSTEM SHALL invoke
   `Set-Service -Name <name> -StartupType Disabled`.
2. WHEN the current `enabled_state` is `"disabled"` (Windows: `Disabled`) THE
   SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 7: Mask a Service (aliased to disable)

**User Story:** As a maintenance coordinator, I want a uniform `mask_service` verb
on Windows even though SCM has no mask primitive.

#### Acceptance Criteria

1. WHEN the coordinator calls `mask_service(name, scope)` THE SYSTEM SHALL invoke
   `Set-Service -Name <name> -StartupType Disabled` AND SHALL populate
   `message="Windows has no mask primitive; aliased to disable"`.
2. WHEN the current `enabled_state` is `"masked"` (i.e., `Disabled` — the two are
   conflated on Windows) THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 8: Unmask a Service (aliased to enable)

**User Story:** As a maintenance coordinator, I want `unmask_service` to reverse
`mask_service`.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmask_service(name, scope)` THE SYSTEM SHALL invoke
   `Set-Service -Name <name> -StartupType Automatic` AND SHALL populate
   `message="Windows has no mask primitive; aliased to enable"`.
2. WHEN the current `enabled_state` is already `"enabled"` THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `scope=USER` THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 9: Get Service Status

**User Story:** As a maintenance coordinator, I want to query a Windows service's
current state without elevation.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_service_status(name, scope)` THE SYSTEM SHALL
   invoke `["powershell.exe", "-NoProfile", "-Command", "Get-Service -Name <name>
   | Select-Object Name,Status,StartType,DisplayName | ConvertTo-Json
   -Compress"]` via the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON object and populate
   `active_state` from `Status` (mapped: `Running→active`, `Stopped→inactive`,
   `Paused→inactive`, `StartPending/StopPending→activating/deactivating`),
   `enabled_state` from `StartType` (`Automatic`/`AutomaticDelayedStart→enabled`,
   `Manual→static`, `Disabled→disabled`), and `description` from `DisplayName`.
3. WHEN `Get-Service` exits with the `NoServiceFoundForGivenName` error THE SYSTEM
   SHALL return
   `WindowsServiceStatus(active_state="not-found", enabled_state="not-found", ...)`
   without raising.
4. WHEN `Get-Service` fails with an unexpected error THE SYSTEM SHALL raise
   `WindowsServiceQueryError`.
5. WHEN `scope=USER` THE SYSTEM SHALL raise `WindowsServiceQueryError` with
   `"USER scope not supported on Windows"` without invoking the runner.

---

### Requirement 10: List Services

**User Story:** As a maintenance coordinator, I want to list all Windows services.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_services(scope)` THE SYSTEM SHALL invoke
   `["powershell.exe", "-NoProfile", "-Command", "Get-Service | Select-Object
   Name,Status,StartType,DisplayName | ConvertTo-Json -Compress"]` via the runner
   without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON array (or object when
   only one service) and produce one `WindowsServiceInfo` per entry.
3. WHEN the command fails THE SYSTEM SHALL raise `WindowsServiceQueryError`.
4. WHEN `scope=USER` THE SYSTEM SHALL raise `WindowsServiceQueryError` without
   invoking the runner.

---

### Requirement 11: Reload the Daemon (no-op)

**User Story:** As a maintenance coordinator, I want a uniform `reload_daemon`
verb, so that cross-OS orchestration does not have to special-case Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_daemon(scope)` THE SYSTEM SHALL NOT invoke
   the runner and SHALL return
   `WindowsServiceActionResult(status=SUCCESS, service_name="",
   message="Windows SCM has no daemon-reload equivalent", scope=scope)`.
2. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
3. WHEN the idempotency guard is evaluated for `reload_daemon` THE SYSTEM SHALL
   NEVER skip.

---

### Requirement 12: Scope Mapping

**User Story:** As a maintenance coordinator, I want a clear, typed response when
`USER` scope is requested on Windows.

#### Acceptance Criteria

1. WHEN `scope=WindowsServiceScope.SYSTEM` THE SYSTEM SHALL construct the
   `powershell.exe` / `Set-Service` / `Get-Service` commands as documented.
2. WHEN `scope=WindowsServiceScope.USER` THE SYSTEM SHALL return
   `WindowsServiceActionResult(status=FAILED, message="USER scope not supported on
   Windows; per-user services are out of scope for the base contract")` for every
   write verb, and SHALL raise `WindowsServiceQueryError` with the same message
   for every read verb.
3. WHEN an unrecognised scope is supplied THE SYSTEM SHALL raise `ValueError`
   before invoking the runner.
4. WHEN the returned `WindowsServiceActionResult` has a `scope` field, the value
   SHALL equal the scope that was supplied (even for USER rejections).

---

### Requirement 13: Elevation

**User Story:** As a maintenance coordinator, I want write verbs to pass
`elevated=True` when the config enables elevation, and reads to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write AND `scope=SYSTEM` THE
   SYSTEM SHALL pass `elevated=True` to the runner; the runner's
   `_is_process_elevated` pre-check determines whether the command actually runs.
2. WHEN the verb is a read (`get_service_status`, `list_services`) THE SYSTEM
   SHALL NEVER pass `elevated=True` regardless of config.
3. WHEN the idempotency-probe calls (`Get-Service`) are issued as part of a
   write's guard THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the host process is NOT already elevated AND a write is attempted with
   `elevated=True` THE SYSTEM SHALL rely on the command-executor / command-runner
   pre-check to return a typed failure; the service manager itself does not
   perform the pre-check.

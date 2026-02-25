# Requirements

## Introduction

The service management feature provides the `automojo-sysman-linux-windows` package with the
ability to start, stop, restart, reload, enable, disable, mask, and query `systemd` services
on Windows Linux systems.  It also supports reloading the systemd daemon after unit file
changes and querying service journal output.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* services to
manage — it provides the capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction, enabling dry-run simulation, auditing, and test mocking without
requiring a live system.

---

## Requirements

### Requirement 1: Start a Service

**User Story:** As a maintenance coordinator, I want to start a named systemd service, so
that the service is running and available for use on the system.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be started THE SYSTEM SHALL invoke
   `systemctl start <service-name>` via the command runner.
2. WHEN the service is already in the `active` (running) state THE SYSTEM SHALL skip
   execution and return a `ServiceActionResult` with status `SKIPPED`.
3. WHEN the start command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the start command fails THE SYSTEM SHALL return a `ServiceActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 2: Stop a Service

**User Story:** As a maintenance coordinator, I want to stop a named systemd service, so
that the service is no longer running on the system.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be stopped THE SYSTEM SHALL invoke
   `systemctl stop <service-name>` via the command runner.
2. WHEN the service is already in the `inactive` state THE SYSTEM SHALL skip execution and
   return a `ServiceActionResult` with status `SKIPPED`.
3. WHEN the stop command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the stop command fails THE SYSTEM SHALL return a `ServiceActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 3: Restart a Service

**User Story:** As a maintenance coordinator, I want to restart a named systemd service,
so that the service picks up new configuration or recovers from a failed state.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be restarted THE SYSTEM SHALL invoke
   `systemctl restart <service-name>` via the command runner.
2. WHEN the restart command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
3. WHEN the restart command fails THE SYSTEM SHALL return a `ServiceActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 4: Reload a Service

**User Story:** As a maintenance coordinator, I want to send a reload signal to a named
systemd service, so that the service re-reads its configuration without a full process
restart.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be reloaded THE SYSTEM SHALL invoke
   `systemctl reload <service-name>` via the command runner.
2. WHEN the service is not active THE SYSTEM SHALL return a `ServiceActionResult` with
   status `FAILED` and a message indicating the service must be running to be reloaded.
3. WHEN the reload command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the reload command fails THE SYSTEM SHALL return a `ServiceActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 5: Enable a Service

**User Story:** As a maintenance coordinator, I want to enable a systemd service so that
it starts automatically at boot, ensuring the service is available after a system restart.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be enabled THE SYSTEM SHALL invoke
   `systemctl enable <service-name>` via the command runner.
2. WHEN the service is already enabled THE SYSTEM SHALL skip execution and return a
   `ServiceActionResult` with status `SKIPPED`.
3. WHEN the enable command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the enable command fails THE SYSTEM SHALL return a `ServiceActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 6: Disable a Service

**User Story:** As a maintenance coordinator, I want to disable a systemd service so that
it no longer starts automatically at boot, without stopping the service if it is currently
running.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be disabled THE SYSTEM SHALL invoke
   `systemctl disable <service-name>` via the command runner.
2. WHEN the service is already disabled THE SYSTEM SHALL skip execution and return a
   `ServiceActionResult` with status `SKIPPED`.
3. WHEN the disable command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the disable command fails THE SYSTEM SHALL return a `ServiceActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 7: Mask a Service

**User Story:** As a maintenance coordinator, I want to mask a systemd service so that it
cannot be started manually or automatically, so that unwanted or dangerous services are
completely prevented from running.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be masked THE SYSTEM SHALL invoke
   `systemctl mask <service-name>` via the command runner.
2. WHEN the service is already masked THE SYSTEM SHALL skip execution and return a
   `ServiceActionResult` with status `SKIPPED`.
3. WHEN the mask command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the mask command fails THE SYSTEM SHALL return a `ServiceActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 8: Unmask a Service

**User Story:** As a maintenance coordinator, I want to unmask a systemd service so that it
can be started and enabled again after having been masked.

#### Acceptance Criteria

1. WHEN the coordinator requests that a service be unmasked THE SYSTEM SHALL invoke
   `systemctl unmask <service-name>` via the command runner.
2. WHEN the service is not masked THE SYSTEM SHALL skip execution and return a
   `ServiceActionResult` with status `SKIPPED`.
3. WHEN the unmask command succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
4. WHEN the unmask command fails THE SYSTEM SHALL return a `ServiceActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 9: Query Service Status

**User Story:** As a maintenance coordinator, I want to query the current active and enabled
state of a named systemd service, so that I can make conditional decisions about what
maintenance to apply without taking any action on the service.

#### Acceptance Criteria

1. WHEN the coordinator queries the state of a service THE SYSTEM SHALL invoke
   `systemctl is-active <service-name>` and `systemctl is-enabled <service-name>` via the
   command runner.
2. WHEN both commands complete THE SYSTEM SHALL return a `ServiceInfo` record containing
   `service_name`, `active_state`, and `enabled_state` populated from the command output.
3. WHEN the service unit does not exist THE SYSTEM SHALL return a `ServiceInfo` record with
   `active_state` of `not-found` and `enabled_state` of `not-found`.
4. WHEN either command fails for an unexpected reason THE SYSTEM SHALL raise a
   `ServiceQueryError` with the exit code and captured stderr.

---

### Requirement 10: Reload the systemd Daemon

**User Story:** As a maintenance coordinator, I want to reload the systemd daemon after
unit files have been modified or added, so that systemd recognises the new or changed unit
definitions before subsequent enable or start operations are attempted.

#### Acceptance Criteria

1. WHEN the coordinator requests a daemon reload THE SYSTEM SHALL invoke
   `systemctl daemon-reload` via the command runner.
2. WHEN the daemon-reload succeeds THE SYSTEM SHALL return a `ServiceActionResult` with
   status `SUCCESS`.
3. WHEN the daemon-reload fails THE SYSTEM SHALL return a `ServiceActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ServiceActionResult` with status `DRY_RUN`.

---

### Requirement 11: Query Service Journal Logs

**User Story:** As a maintenance coordinator, I want to retrieve recent journal log output
for a named service, so that I can include diagnostic context in maintenance reports or
confirm expected log output after a restart.

#### Acceptance Criteria

1. WHEN the coordinator requests journal output for a service THE SYSTEM SHALL invoke
   `journalctl -u <service-name> --no-pager -n <lines>` via the command runner.
2. WHEN the coordinator does not specify a line count THE SYSTEM SHALL default to the last
   50 lines.
3. WHEN the command succeeds THE SYSTEM SHALL return the captured stdout as a string.
4. WHEN the command fails THE SYSTEM SHALL raise a `ServiceQueryError` with the exit code
   and captured stderr.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all service write commands to run with
the privileges required by `systemctl`, so that service operations succeed on a standard
system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (start, stop, restart, reload, enable,
   disable, mask, unmask, daemon-reload).
2. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN performing read-only operations (query status, query journal) THE SYSTEM SHALL NOT
   use elevation.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.

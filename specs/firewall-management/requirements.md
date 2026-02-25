# Requirements

## Introduction

The firewall management feature provides the `automojo-sysman-linux-windows` package with
the ability to manage `firewalld` on Windows Linux systems.  This includes adding and
removing services, ports, and rich rules within zones, querying firewall state, and
reloading the firewall to apply permanent changes to the runtime configuration.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* firewall rules
to apply — it provides the capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, enabling dry-run simulation, auditing,
and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Add a Service to a Zone

**User Story:** As a maintenance coordinator, I want to add a named firewalld service to a
zone, so that traffic for that service is permitted through the firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and a zone name THE SYSTEM SHALL invoke
   `firewall-cmd --zone=<zone> --add-service=<service> --permanent` via the command runner.
2. WHEN the service is already present in the zone THE SYSTEM SHALL skip execution and
   return a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 2: Remove a Service from a Zone

**User Story:** As a maintenance coordinator, I want to remove a named firewalld service
from a zone, so that traffic for that service is no longer permitted through the firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and a zone name THE SYSTEM SHALL invoke
   `firewall-cmd --zone=<zone> --remove-service=<service> --permanent` via the command
   runner.
2. WHEN the service is not present in the zone THE SYSTEM SHALL skip execution and return
   a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 3: Add a Port to a Zone

**User Story:** As a maintenance coordinator, I want to open a specific port and protocol
in a firewalld zone, so that traffic on that port is permitted through the firewall without
requiring a named service definition.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and zone name THE SYSTEM SHALL
   invoke `firewall-cmd --zone=<zone> --add-port=<port>/<protocol> --permanent` via the
   command runner.
2. WHEN the port/protocol combination is already open in the zone THE SYSTEM SHALL skip
   execution and return a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 4: Remove a Port from a Zone

**User Story:** As a maintenance coordinator, I want to close a specific port and protocol
in a firewalld zone, so that traffic on that port is no longer permitted.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and zone name THE SYSTEM SHALL
   invoke `firewall-cmd --zone=<zone> --remove-port=<port>/<protocol> --permanent` via the
   command runner.
2. WHEN the port/protocol combination is not open in the zone THE SYSTEM SHALL skip
   execution and return a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 5: Add a Rich Rule to a Zone

**User Story:** As a maintenance coordinator, I want to add a rich rule to a firewalld
zone, so that complex traffic filtering logic that cannot be expressed as a simple service
or port can be enforced.

#### Acceptance Criteria

1. WHEN the coordinator provides a rich rule string and a zone name THE SYSTEM SHALL invoke
   `firewall-cmd --zone=<zone> --add-rich-rule='<rule>' --permanent` via the command
   runner.
2. WHEN an identical rich rule is already present in the zone THE SYSTEM SHALL skip
   execution and return a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 6: Remove a Rich Rule from a Zone

**User Story:** As a maintenance coordinator, I want to remove a rich rule from a firewalld
zone, so that a previously applied complex traffic rule is revoked.

#### Acceptance Criteria

1. WHEN the coordinator provides a rich rule string and a zone name THE SYSTEM SHALL invoke
   `firewall-cmd --zone=<zone> --remove-rich-rule='<rule>' --permanent` via the command
   runner.
2. WHEN the rich rule is not present in the zone THE SYSTEM SHALL skip execution and return
   a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 7: Set the Default Zone

**User Story:** As a maintenance coordinator, I want to set the default firewalld zone, so
that interfaces not explicitly assigned to a zone are governed by the correct policy.

#### Acceptance Criteria

1. WHEN the coordinator provides a zone name THE SYSTEM SHALL invoke
   `firewall-cmd --set-default-zone=<zone>` via the command runner.
2. WHEN the default zone is already set to the desired zone THE SYSTEM SHALL skip execution
   and return a `FirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 8: Reload the Firewall

**User Story:** As a maintenance coordinator, I want to reload firewalld so that permanent
rule changes are applied to the runtime configuration, making them effective immediately
without disrupting established connections.

#### Acceptance Criteria

1. WHEN the coordinator requests a firewall reload THE SYSTEM SHALL invoke
   `firewall-cmd --reload` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `FirewallActionResult` with status
   `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `FirewallActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FirewallActionResult` with status `DRY_RUN`.

---

### Requirement 9: Query Zone State

**User Story:** As a maintenance coordinator, I want to query the current state of a
firewalld zone including its services, ports, and rich rules, so that I can make
conditional decisions about what changes are needed without taking any action.

#### Acceptance Criteria

1. WHEN the coordinator provides a zone name THE SYSTEM SHALL invoke
   `firewall-cmd --zone=<zone> --list-all` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `ZoneInfo` record containing the
   zone name, list of services, list of open ports, and list of rich rules.
3. WHEN the zone does not exist THE SYSTEM SHALL raise a `FirewallQueryError` with the
   captured stderr.
4. WHEN the command fails for any other reason THE SYSTEM SHALL raise a
   `FirewallQueryError` with the exit code and captured stderr.

---

### Requirement 10: Query All Zones

**User Story:** As a maintenance coordinator, I want to retrieve a summary of all
configured firewalld zones and their active state, so that I can audit the overall firewall
configuration and identify zones that require changes.

#### Acceptance Criteria

1. WHEN the coordinator requests all zones THE SYSTEM SHALL invoke
   `firewall-cmd --list-all-zones` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a list of `ZoneInfo` records, one per
   zone, each populated with services, ports, and rich rules.
3. WHEN the command fails THE SYSTEM SHALL raise a `FirewallQueryError` with the exit code
   and captured stderr.

---

### Requirement 11: Query Firewalld State

**User Story:** As a maintenance coordinator, I want to query whether firewalld is running
and what the default zone is, so that I can verify the firewall is active before applying
rule changes.

#### Acceptance Criteria

1. WHEN the coordinator requests the firewall state THE SYSTEM SHALL invoke
   `firewall-cmd --state` and `firewall-cmd --get-default-zone` via the command runner.
2. WHEN both commands succeed THE SYSTEM SHALL return a `FirewallState` record containing
   `running` (bool) and `default_zone` (str).
3. WHEN `firewall-cmd --state` exits non-zero THE SYSTEM SHALL return a `FirewallState`
   with `running=False` and `default_zone` as empty string.
4. WHEN `firewall-cmd --get-default-zone` fails for an unexpected reason THE SYSTEM SHALL
   raise a `FirewallQueryError`.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all firewall write commands to run
with the privileges required by `firewall-cmd`, so that rule changes succeed on a standard
system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (add/remove service, port, rich rule,
   set default zone, reload).
2. WHEN performing read-only operations (query zone, list all zones, get state) THE SYSTEM
   SHALL NOT use elevation.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.

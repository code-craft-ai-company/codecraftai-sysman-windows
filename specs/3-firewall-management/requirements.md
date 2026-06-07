# Requirements

## Introduction

The firewall management feature provides the `codecraftai-sysman-windows` package with the
ability to manage Windows Firewall via the PowerShell `NetSecurity` module.  This spec
implements the firewall-management contract defined at
`codecraftai-sysman-common/specs/contracts/firewall-management/contract.md` for
Windows using `New-NetFirewallRule`, `Remove-NetFirewallRule`, `Get-NetFirewallRule`,
and `Get-NetFirewallProfile`.  It includes adding and removing services, ports, and
rich rules within a firewall profile, querying profile state, and providing the
contract-mandated reload verb.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* firewall
rules to apply — it provides the capability to *do it* reliably, idempotently, and with
full auditability.  All system commands are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction.  On Windows the abstract `zone`
parameter maps to a Windows Firewall profile — one of `Domain`, `Private`, `Public`,
or `Any`; any other value is rejected with `FAILED`.

---

## Requirements

### Requirement 1: Add a Service to a Zone

**User Story:** As a maintenance coordinator, I want to add a named service rule to a
Windows Firewall profile, so that traffic for that service is permitted through the
firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and a valid profile `zone` THE SYSTEM
   SHALL resolve the service name to a `(port, protocol)` pair via a bundled services
   table and invoke `powershell.exe -NoProfile -Command "New-NetFirewallRule
   -DisplayName 'codecraftai-svc-<service>' -Direction Inbound -Action Allow -Protocol
   <protocol> -LocalPort <port> -Profile <zone>"` via the command runner.
2. WHEN the service name cannot be resolved via the bundled services table THE SYSTEM
   SHALL return a `WindowsFirewallActionResult` with status `FAILED` and a `detail`
   explaining the unknown service.
3. WHEN `zone` is not one of `Domain`, `Private`, `Public`, or `Any` THE SYSTEM SHALL
   return a `WindowsFirewallActionResult` with status `FAILED` and a `detail`
   explaining the valid profile names.
4. WHEN a `NetFirewallRule` with the computed `DisplayName` already exists in the
   target profile THE SYSTEM SHALL skip execution and return a
   `WindowsFirewallActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `WindowsFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 2: Remove a Service from a Zone

**User Story:** As a maintenance coordinator, I want to remove a named service rule
from a Windows Firewall profile, so that traffic for that service is no longer
permitted.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and a valid profile `zone` THE SYSTEM
   SHALL invoke `powershell.exe -NoProfile -Command "Remove-NetFirewallRule
   -DisplayName 'codecraftai-svc-<service>'"` (scoped to the target profile via
   `-Profile` where supported by the cmdlet) via the command runner.
2. WHEN `zone` is not a valid profile THE SYSTEM SHALL return a
   `WindowsFirewallActionResult` with status `FAILED` and a profile-validation
   `detail` message.
3. WHEN no rule with the computed `DisplayName` exists THE SYSTEM SHALL skip execution
   and return a `WindowsFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `WindowsFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 3: Add a Port to a Zone

**User Story:** As a maintenance coordinator, I want to open a specific port and
protocol in a Windows Firewall profile, so that traffic on that port is permitted.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and valid profile `zone`
   THE SYSTEM SHALL invoke `powershell.exe -NoProfile -Command "New-NetFirewallRule
   -DisplayName 'codecraftai-port-<port>-<protocol>' -Direction Inbound -Action Allow
   -Protocol <protocol> -LocalPort <port> -Profile <zone>"` via the command runner.
2. WHEN `zone` is not a valid profile THE SYSTEM SHALL return a
   `WindowsFirewallActionResult` with status `FAILED` and a profile-validation
   `detail` message.
3. WHEN a rule with the computed `DisplayName` already exists in the target profile
   THE SYSTEM SHALL skip execution and return a `WindowsFirewallActionResult` with
   status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `WindowsFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 4: Remove a Port from a Zone

**User Story:** As a maintenance coordinator, I want to close a specific port and
protocol in a Windows Firewall profile, so that traffic on that port is no longer
permitted.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and valid profile `zone`
   THE SYSTEM SHALL invoke `powershell.exe -NoProfile -Command "Remove-NetFirewallRule
   -DisplayName 'codecraftai-port-<port>-<protocol>'"` via the command runner.
2. WHEN `zone` is not a valid profile THE SYSTEM SHALL return a
   `WindowsFirewallActionResult` with status `FAILED` and a profile-validation
   `detail` message.
3. WHEN no rule with the computed `DisplayName` exists THE SYSTEM SHALL skip
   execution and return a `WindowsFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `WindowsFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 5: Add a Rich Rule to a Zone

**User Story:** As a maintenance coordinator, I want to add a PowerShell splatted-
parameters rule, so that complex traffic filtering logic that cannot be expressed as a
simple service or port can be enforced.

#### Acceptance Criteria

1. WHEN the coordinator provides a rule string in PowerShell splatted-parameters form
   (e.g., `"-DisplayName 'my-rule' -Direction Inbound -Action Allow -Protocol TCP
   -LocalPort 8080 -RemoteAddress 10.0.0.0/8"`) and a valid profile `zone` THE SYSTEM
   SHALL invoke `powershell.exe -NoProfile -Command "New-NetFirewallRule <rule>
   -Profile <zone>"` via the command runner.
2. WHEN the rule string does not contain a `-DisplayName '<name>'` clause THE SYSTEM
   SHALL return a `WindowsFirewallActionResult` with status `FAILED` and a `detail`
   explaining that `-DisplayName` is required for idempotency.
3. WHEN `zone` is not a valid profile THE SYSTEM SHALL return a
   `WindowsFirewallActionResult` with status `FAILED` and a profile-validation
   `detail` message.
4. WHEN a rule with the derived `DisplayName` already exists in the target profile
   THE SYSTEM SHALL skip execution and return a `WindowsFirewallActionResult` with
   status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `WindowsFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 6: Remove a Rich Rule from a Zone

**User Story:** As a maintenance coordinator, I want to remove a rule previously added
with `add_rich_rule`, so that a previously applied complex traffic rule is revoked.

#### Acceptance Criteria

1. WHEN the coordinator provides a rule string with a `-DisplayName '<name>'` clause
   and a valid profile `zone` THE SYSTEM SHALL parse the display name, then invoke
   `powershell.exe -NoProfile -Command "Remove-NetFirewallRule -DisplayName '<name>'"`
   via the command runner.
2. WHEN the rule string does not contain a `-DisplayName '<name>'` clause THE SYSTEM
   SHALL return a `WindowsFirewallActionResult` with status `FAILED`.
3. WHEN `zone` is not a valid profile THE SYSTEM SHALL return a
   `WindowsFirewallActionResult` with status `FAILED` and a profile-validation
   `detail` message.
4. WHEN no rule with the parsed `DisplayName` exists THE SYSTEM SHALL skip execution
   and return a `WindowsFirewallActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `WindowsFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 7: Set the Default Zone

**User Story:** As a maintenance coordinator, I want a uniform `set_default_zone` API
so that mixed-OS orchestration is not forced to special-case Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_default_zone(zone)` where `zone` is one of
   `Domain`, `Private`, `Public`, or `Any` THE SYSTEM SHALL NOT execute any command
   and SHALL return a `WindowsFirewallActionResult` with status `SKIPPED` and a
   `detail` noting that Windows Firewall has no switchable default profile concept —
   profiles activate based on network category.
2. WHEN `zone` is not a valid profile THE SYSTEM SHALL return a
   `WindowsFirewallActionResult` with status `FAILED` and a profile-validation
   `detail` message.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsFirewallActionResult` with status `DRY_RUN` regardless of the
   `zone` value.
4. WHEN the coordinator relies on the return value to drive subsequent steps THE
   SYSTEM SHALL consistently return either `SKIPPED` or `FAILED` (never `SUCCESS`)
   for this method, so behavior is deterministic.

---

### Requirement 8: Reload the Firewall

**User Story:** As a maintenance coordinator, I want a uniform `reload` API so that
mixed-OS orchestration can call it unconditionally.

#### Acceptance Criteria

1. WHEN the coordinator requests a firewall reload THE SYSTEM SHALL NOT execute any
   command and SHALL return a `WindowsFirewallActionResult` with status `SUCCESS` and
   a `detail` of `"Windows Firewall applies rules immediately; no reload required"`.
2. WHEN dry-run mode is active THE SYSTEM SHALL return a `WindowsFirewallActionResult`
   with status `DRY_RUN`.
3. WHEN the coordinator relies on the return value to drive subsequent steps THE
   SYSTEM SHALL consistently return `SUCCESS` or `DRY_RUN` for this method, so
   behavior is deterministic.

---

### Requirement 9: Query Zone State

**User Story:** As a maintenance coordinator, I want to query the current rules in a
Windows Firewall profile including its services, ports, and custom rules, so that I
can make conditional decisions about what changes are needed without taking any
action.

#### Acceptance Criteria

1. WHEN the coordinator provides a valid profile `zone` THE SYSTEM SHALL invoke
   `powershell.exe -NoProfile -Command "Get-NetFirewallRule | Where-Object { $_.Profile
   -match '<zone>' -or $_.Profile -eq 'Any' } | ForEach-Object { ... } |
   ConvertTo-Json -Compress -Depth 5"` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON output into a
   `WindowsZoneInfo` record populated with the zone name, services (rules whose
   `DisplayName` matches `codecraftai-svc-*`), ports (rules whose `DisplayName` matches
   `codecraftai-port-*`), and rich_rules (all other rules, rendered back to splatted-
   parameters form).
3. WHEN `zone` is not a valid profile THE SYSTEM SHALL raise a
   `WindowsFirewallQueryError` explaining the valid profile names.
4. WHEN the command fails for any other reason THE SYSTEM SHALL raise a
   `WindowsFirewallQueryError` with the exit code and captured stderr.

---

### Requirement 10: Query All Zones

**User Story:** As a maintenance coordinator, I want to retrieve a summary of all
three Windows Firewall profiles, so that I can audit the overall firewall
configuration.

#### Acceptance Criteria

1. WHEN the coordinator requests all zones THE SYSTEM SHALL invoke `query_zone` for
   each of `Domain`, `Private`, and `Public` and return the three resulting
   `WindowsZoneInfo` records in that order.
2. WHEN the per-zone query succeeds for every profile THE SYSTEM SHALL mark
   `is_default=True` on the record whose profile name equals the current
   `Get-NetConnectionProfile` category (as reported by `query_state`).
3. WHEN any per-zone query fails THE SYSTEM SHALL raise a
   `WindowsFirewallQueryError` with the exit code and captured stderr.

---

### Requirement 11: Query Windows Firewall State

**User Story:** As a maintenance coordinator, I want to query whether Windows
Firewall is enabled on all profiles, so that I can verify the firewall is active
before applying rule changes.

#### Acceptance Criteria

1. WHEN the coordinator requests the firewall state THE SYSTEM SHALL invoke
   `powershell.exe -NoProfile -Command "Get-NetFirewallProfile | ConvertTo-Json"`
   and `powershell.exe -NoProfile -Command "Get-NetConnectionProfile |
   ConvertTo-Json"` via the command runner without elevation.
2. WHEN `Get-NetFirewallProfile` reports `Enabled=True` for all of `Domain`,
   `Private`, and `Public` THE SYSTEM SHALL return a `WindowsFirewallState` with
   `running=True`.
3. WHEN `Get-NetFirewallProfile` reports `Enabled=False` for at least one of the
   three profiles THE SYSTEM SHALL return a `WindowsFirewallState` with
   `running=False`.
4. WHEN `running=True` THE SYSTEM SHALL set `default_zone` to the `NetworkCategory`
   of the currently active connection profile (`Domain`, `Private`, or `Public`);
   when `running=False` THE SYSTEM SHALL set `default_zone=""`.
5. WHEN `Get-NetFirewallProfile` exits non-zero THE SYSTEM SHALL return a
   `WindowsFirewallState` with `running=False` and `default_zone=""` per the
   contract's firewall-not-running rule.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all firewall write commands to
run with Administrator privileges, so that rule changes succeed on a standard Windows
system where the process does not run elevated.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (add/remove
   service, port, rich rule, set default zone, reload).
2. WHEN performing read-only operations (query zone, list all zones, get state) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of config.
3. WHEN the process is already running elevated THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner to surface the resulting access-denied
   error as a `WindowsFirewallActionResult` with `status=FAILED` for writes or as a
   `WindowsFirewallQueryError` for reads.

# Requirements

## Introduction

The precision-time-management feature provides the
`automojo-sysman-windows` package with the ability to query and
configure the host's NTP synchronisation surface on Windows.  This
spec implements the precision-time-management contract defined at
`automojo-sysman-common/specs/contracts/precision-time-management/contract.md`
for Windows using the Windows Time Service (`W32Time`) plus its
user-space utilities `w32tm.exe`, `net.exe`, and `sc.exe`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* time sources should be active or
*whether* clock sync should be on — it provides the capability to
*manipulate the precision-time surface* reliably, idempotently, and
with full auditability.  All system commands flow through the
`automojo-interfaces` command-runner abstraction.  Persistent
configuration lives under
`HKLM\SYSTEM\CurrentControlSet\Services\W32Time` in the registry;
`w32tm /config /update` is the supported public surface for
applying changes, and that is what this manager calls.

This feature is parallel to — and does not duplicate — any other
feature.  Timezone management lives under
`localization-management`; this feature owns clock synchronisation
only.  Service lifecycle is performed via `net start` / `net stop`
plus `sc.exe config <service> start=` for boot-time posture; the
manager keeps the verb surface to native Win32 utilities so a host
without PowerShell still answers.

---

## Requirements

### Requirement 1: Query Overall Time Status

**User Story:** As a maintenance coordinator, I want to read the
host's overall sync status, so that reporting and idempotency
workflows can branch on whether the W32Time service is running,
which upstream is currently selected, and how recently a sync
occurred.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_time_status()` THE SYSTEM SHALL
   dispatch `w32tm /query /status /verbose` through the command
   runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the
   ``Key: Value`` payload and populate `WindowsTimeStatus.Source`,
   `Stratum`, `Last Successful Sync Time`, and `Phase Offset` into
   the return record.
3. WHEN the command exits non-zero AND stderr matches a known
   "service not started" signature (``0x80070426``,
   ``service has not been started``,
   ``service is not running``,
   ``could not connect to``) THE SYSTEM SHALL return a
   `WindowsTimeStatus(enabled=False, synchronized=False, ...)`
   rather than raise.
4. WHEN the command fails for any other reason THE SYSTEM SHALL
   raise a `WindowsPrecisionTimeQueryError` carrying the failing
   command, exit code, and stderr.
5. THE SYSTEM SHALL set `synchronized=True` only when both
   `last_sync` is populated (i.e. not ``unspecified``) AND
   `stratum` is a positive integer.

---

### Requirement 2: Enumerate Configured Time Sources

**User Story:** As a maintenance coordinator, I want to enumerate
the host's configured upstream peers so that idempotency guards on
`add_time_source` / `remove_time_source` / `set_time_servers` can
reason about current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_time_sources()` THE SYSTEM SHALL
   dispatch `w32tm /query /peers` without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL split the payload on
   blank-line stanza separators and parse each stanza's `Peer:`,
   `State:`, `Stratum:`, and `Last Successful Sync Time:` fields
   into one `WindowsTimeSourceInfo` per peer.
3. THE SYSTEM SHALL classify the `State:` token onto the contract
   vocabulary: ``Active`` → `selected`, ``Pending`` /
   ``Candidate`` → `candidate`,
   ``Rejected`` / ``no valid samples`` → `unreachable`.
4. WHEN the command fails for an inactive-service signature THE
   SYSTEM SHALL return the empty list rather than raise.

---

### Requirement 3: Add a Time Source

**User Story:** As a maintenance coordinator, I want to add an
upstream NTP source so that the host gains another peer.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_time_source(server)` THE SYSTEM
   SHALL probe the current peer list, append `server`, and
   dispatch
   `w32tm /config /manualpeerlist:"<rendered>"
   /syncfromflags:manual /update` followed by
   `net stop w32time` and `net start w32time`.
2. WHEN the supplied server is already in the current list THE
   SYSTEM SHALL skip execution and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `status=DRY_RUN`.
4. WHEN the runner returns non-zero on the `/config /update` step
   OR the `net start` step THE SYSTEM SHALL return
   `status=FAILED` with the captured exit code, stdout, stderr,
   and command.

---

### Requirement 4: Remove a Time Source

**User Story:** As a maintenance coordinator, I want to remove an
upstream NTP source so that the host stops considering it.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_time_source(server)` THE
   SYSTEM SHALL recompute the peer list with `server` removed and
   dispatch the same `/config /update` plus service-bounce
   sequence as `add_time_source`.
2. WHEN the supplied server is not in the current list THE SYSTEM
   SHALL skip execution and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 5: Replace the Time-Source List

**User Story:** As a maintenance coordinator, I want to replace the
host's full upstream list in a single transaction.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_time_servers(servers)` THE
   SYSTEM SHALL render the supplied list space-joined and dispatch
   the `/config /update` plus service-bounce sequence.
2. WHEN the existing list (under sorted comparison) already equals
   the desired list THE SYSTEM SHALL skip execution and return
   `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 6: Force a Synchronisation

**User Story:** As a maintenance coordinator, I want to force the
host to attempt an immediate sync.

#### Acceptance Criteria

1. WHEN the coordinator calls `force_sync_now()` THE SYSTEM SHALL
   dispatch `w32tm /resync /force` through the command runner
   with `elevated=True` when configured.
2. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.
3. `force_sync_now` MUST NOT short-circuit on idempotency.

---

### Requirement 7: Read Drift

**User Story:** As a maintenance coordinator, I want to read the
host's current drift so that reporting workflows can capture
offset without hand-parsing tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_drift()` THE SYSTEM SHALL first
   call `get_time_status()` to learn the current peer, then
   dispatch
   `w32tm /stripchart /computer:<server> /samples:1 /dataonly`
   without elevation.
2. WHEN no peer is configured THE SYSTEM SHALL return a
   `WindowsTimeDrift(offset_seconds=0.0, frequency_ppm=None,
   jitter_seconds=None, stratum=<status.stratum>)` rather than
   raise.
3. WHEN `/stripchart` fails THE SYSTEM SHALL raise a
   `WindowsPrecisionTimeQueryError`.
4. THE SYSTEM SHALL set `frequency_ppm` and `jitter_seconds` to
   `None` — Windows does not expose those values through `w32tm`.

---

### Requirement 8: Enable Sync

**User Story:** As a maintenance coordinator, I want to enable the
W32Time service so that the host begins keeping its clock
synchronised.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_sync()` THE SYSTEM SHALL
   first dispatch
   `sc config <service> start= auto` (with `<service>` from
   `config.service_name`, default `"w32time"`) and then
   `net start <service>`, both with `elevated=True` when
   configured.
2. WHEN the service is already running THE SYSTEM SHALL skip
   execution and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.
4. WHEN the `sc config` step fails THE SYSTEM SHALL return
   `status=FAILED` and SHALL NOT proceed to `net start`.

---

### Requirement 9: Disable Sync

**User Story:** As a maintenance coordinator, I want to disable the
W32Time service so that the host stops adjusting its clock from
upstream.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_sync()` THE SYSTEM SHALL
   first dispatch `net stop <service>` and then
   `sc config <service> start= disabled`, both with
   `elevated=True` when configured.
2. WHEN the service is already stopped THE SYSTEM SHALL skip
   execution and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.
4. WHEN `net stop` fails THE SYSTEM SHALL return `status=FAILED`
   and SHALL NOT proceed to `sc config`.

---

### Requirement 10: Service Selector

**User Story:** As a maintenance coordinator, I want the manager to
target a non-default service unit when needed so that air-gapped
or specialised builds can override the unit name.

#### Acceptance Criteria

1. WHEN `config.service_name` is set THE SYSTEM SHALL substitute
   the supplied name everywhere `net.exe` and `sc.exe` are invoked.
2. WHEN `config.service_name` is unset THE SYSTEM SHALL default to
   `"w32time"`.

---

### Requirement 11: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all
precision-time write operations to run with administrator
privilege so that mutations against the registry-backed
configuration and the W32Time service lifecycle succeed on a
standard system where the process does not run elevated.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write
   (`add_time_source`, `remove_time_source`, `set_time_servers`,
   `force_sync_now`, `enable_sync`, `disable_sync`).
2. WHEN performing read-only operations (`get_time_status`,
   `get_time_sources`, `get_drift`, the
   `w32tm /query /peers` peer probe, the `sc query` running
   probe) THE SYSTEM SHALL NOT pass `elevated=True`, regardless
   of configuration.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL
   default to no elevation and allow the runner to surface the
   resulting permission error as a
   `WindowsPrecisionTimeActionResult` with `status=FAILED` for
   writes or as a `WindowsPrecisionTimeQueryError` for reads.

---

## Out of Scope

- PTP / IEEE 1588 — out of scope; W32Time on Windows server
  builds supports a high-accuracy posture but PTP-specific tuning
  is not part of this Wave 2.4 batch.
- NTP authentication keys.
- Per-source PPS hardware configuration.
- Timezone selection (lives under `localization-management`).
- Hardware clock (`hwclock`) management — the Windows analog is
  not in scope here.
- PowerShell-cmdlet alternates such as `Get-TimeZone` /
  `Set-Date`; this feature stays on `w32tm` / `net` / `sc` so
  hosts without PowerShell still answer.

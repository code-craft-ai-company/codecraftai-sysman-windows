# Requirements

## Introduction

The remote-log-forwarding feature provides the
`automojo-sysman-windows` package with the ability to ship
Windows Event Log records off-host through Windows Event
Forwarding (WEF) subscriptions managed via `wecutil.exe`.  It is
the cross-OS counterpart to the local log-shaping surface owned
by `3-log-management` and the RHEL-canonical
`3-remote-log-forwarding` feature.

WEF is the only Microsoft-supported native mechanism for
shipping Event Log records between Windows hosts.  WEF carries
events between Windows hosts only — it does NOT speak the
syslog wire protocol.  Operators who need to ship to a
non-Windows collector (rsyslog, syslog-ng, Splunk, Elastic) MUST
deploy a third-party shipper such as Winlogbeat or NXLog
alongside this module.  The design document discusses this gap
explicitly.

All system commands (`wecutil.exe cs|ds|es|ss|gs`,
`Write-EventLog`, `tee` via PowerShell, `Test-Path`) are
dispatched through the `automojo-interfaces` `ICommandRunner`
abstraction.  All mutating operations honour dry-run mode and
config-driven elevation.

The reference implementation for shape, idempotency, dry-run, and
result mapping lives in
`automojo-sysman-linux-redhat/specs/3-remote-log-forwarding/`.

---

## Requirements

### Requirement 1: Add a Forwarder

**User Story:** As a maintenance coordinator, I want to declare a
WEF subscription so that Event Log records ship to a remote
Windows collector.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_forwarder(spec)` THE SYSTEM
   SHALL render a WEF subscription XML document for `(host,
   port, query)` and write it to
   `<wec_subscription_dir>\<id>.xml` via PowerShell
   `Set-Content`.
2. WHEN the existing subscription XML already matches the
   rendered desired XML byte-for-byte THE SYSTEM SHALL return
   `SKIPPED`.
3. WHEN the dispatch succeeds THE SYSTEM SHALL invoke
   `wecutil.exe cs <id>.xml` to register the subscription with
   the Windows Event Collector service; failures MUST surface
   as `FAILED` on the mutating verb.
4. THE rendered XML SHALL include the `Description`,
   `Enabled`, `Uri`, `Query`, `ReadExistingEvents`,
   `TransportName` (`HTTP` for port 5985, `HTTPS` for 5986),
   `LogFile`, and `ContentFormat` elements that
   `wecutil cs` validates.

---

### Requirement 2: Remove a Forwarder

**User Story:** As a maintenance coordinator, I want to delete a
WEF subscription.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_forwarder(id)` THE
   SYSTEM SHALL dispatch `wecutil.exe ds <id>` through the
   runner.
2. WHEN the subscription is not registered THE SYSTEM SHALL
   return `SKIPPED` without invoking `wecutil.exe ds`.
3. WHEN the dispatch succeeds AND the on-disk XML still
   exists THE SYSTEM SHALL delete the on-disk subscription XML
   so that `list_forwarders` no longer reports the entry.

---

### Requirement 3: List Forwarders

**User Story:** As a maintenance coordinator, I want to enumerate
the WEF subscriptions registered on this host.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_forwarders()` THE SYSTEM
   SHALL dispatch `wecutil.exe es` through the runner without
   elevation and parse the listing line-by-line.
2. THE SYSTEM SHALL include only subscription names that are
   registered with the Windows Event Collector service.
3. WHEN reading each subscription's properties THE SYSTEM
   SHALL dispatch `wecutil.exe gs <id>` and parse `Enabled`,
   `Uri`, `TransportName`, and `Query` from the formatted
   output.
4. WHEN `wecutil.exe es` exits non-zero with a non-empty
   stderr THE SYSTEM SHALL raise `WindowsForwarderQueryError`.

---

### Requirement 4: Enable / Disable a Forwarder

**User Story:** As a maintenance coordinator, I want to suspend
and resume a registered WEF subscription.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_forwarder(id)` THE
   SYSTEM SHALL dispatch
   `wecutil.exe ss <id> /e:true` through the runner.
2. WHEN the coordinator calls `disable_forwarder(id)` THE
   SYSTEM SHALL dispatch
   `wecutil.exe ss <id> /e:false` through the runner.
3. WHEN the requested state already matches the registered
   state THE SYSTEM SHALL return `SKIPPED` without invoking
   `wecutil.exe ss`.

---

### Requirement 5: Test a Forwarder

**User Story:** As a maintenance coordinator, I want to write a
known event into the Application channel so that the
configured forwarder can be observed end-to-end on the
collector.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_forwarder(id)` THE SYSTEM
   SHALL dispatch a PowerShell scriptblock that calls
   `Write-EventLog -LogName Application -Source automojo-test
   -EventId 4242 -Message 'forwarder-test-<timestamp>'`.
2. WHEN the `automojo-test` event source is not registered the
   scriptblock SHALL register it via `New-EventLog -LogName
   Application -Source automojo-test` before writing.
3. THE SYSTEM SHALL return a `WindowsForwarderTestResult` with
   `forwarder_id` populated and `test_message_id` set to the
   timestamp portion of the dispatched message.
4. THE SYSTEM SHALL NOT raise on dispatch failure; the result's
   `status` reports `FAILED` and `stderr` carries the runner's
   stderr.
5. THE SYSTEM SHALL run the verb unconditionally — there is no
   idempotency guard for `test_forwarder`.

---

### Requirement 6: Get Forwarder Status

**User Story:** As a maintenance coordinator, I want to query a
WEF subscription's current state so that I can correlate it
with the collector's records.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_forwarder_status(id)` THE
   SYSTEM SHALL dispatch `wecutil.exe gs <id>` through the
   runner without elevation and parse `Enabled` from the
   output.
2. WHEN the subscription is not registered THE SYSTEM SHALL
   return a status record whose `enabled` is `False` and whose
   `last_event_time`, `error_count`, and `bytes_sent` are
   `None`.
3. WHEN the status output reports
   `LastError`, `EventSources`, or `RuntimeStatus` blocks the
   parser SHALL extract `last_event_time`, `error_count`, and
   `bytes_sent` when those fields are present; otherwise
   ``None`` is returned.
4. THE SYSTEM SHALL NOT elevate the status probe.

---

### Requirement 7: Subscription XML Generation

**User Story:** As a maintenance coordinator, I want the
rendered subscription XML to validate against the
``wecutil cs`` schema.

#### Acceptance Criteria

1. THE SYSTEM SHALL render a `<Subscription>` root element with
   the `xmlns="http://schemas.microsoft.com/2006/03/windows/events/subscription"`
   attribute.
2. THE SYSTEM SHALL render `SubscriptionId`, `SubscriptionType`,
   `Description`, `Enabled`, `Uri`, `ConfigurationMode`,
   `Query`, `ReadExistingEvents`, `TransportName`, `LogFile`,
   and `ContentFormat` child elements.
3. WHEN `protocol` is `https` THE SYSTEM SHALL render
   `<TransportName>HTTPS</TransportName>`.
4. WHEN `protocol` is `http` THE SYSTEM SHALL render
   `<TransportName>HTTP</TransportName>`.

---

### Requirement 8: Documented Limitations

**User Story:** As a maintenance coordinator, I want the
documentation to enumerate the gaps in the WEF native mechanism.

#### Acceptance Criteria

1. THE `design.md` SHALL document that WEF carries events
   between Windows hosts only — it does NOT speak the syslog
   wire protocol.
2. THE `design.md` SHALL recommend Winlogbeat or NXLog for
   syslog-protocol forwarding.
3. THE `design.md` SHALL note that this manager implements the
   collector-side configuration in source-initiated mode; the
   client-side WinRM listener configuration is out of scope.

---

### Requirement 9: Dry-Run, Elevation, and Error Reporting

**User Story:** As a maintenance coordinator, I want every write
verb to honour dry-run and to elevate predictably.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   `wecutil.exe cs|ds|ss` and SHALL NOT write the
   subscription XML.
2. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner on every write dispatch.
3. WHEN the runner returns non-zero on a write THE SYSTEM SHALL
   return `WindowsForwarderActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)` without raising.
4. THE SYSTEM SHALL never elevate read verbs (`list_forwarders`,
   `get_forwarder_status`, `test_forwarder`).

---

### Requirement 10: Idempotency

**User Story:** As a maintenance coordinator, I want every write
verb to be safe to invoke repeatedly.

#### Acceptance Criteria

1. `add_forwarder` MUST `SKIP` when the registered subscription
   plus on-disk XML already match the rendered desired XML.
2. `remove_forwarder` MUST `SKIP` when the subscription is not
   registered.
3. `enable_forwarder` MUST `SKIP` when the subscription is
   already enabled.
4. `disable_forwarder` MUST `SKIP` when the subscription is
   already disabled.
5. `test_forwarder` MUST run unconditionally — there is no
   idempotency guard.

# Requirements

## Introduction

The mail-transport-management feature provides the
`codecraftai-sysman-windows` package with the ability to interact with
a mail transport agent (MTA) on Windows hosts.  **Windows has no
built-in MTA equivalent.**  After the deprecation of the IIS SMTP
feature on consumer Windows SKUs, Microsoft's recommendation for
outbound mail is a third-party relay (hMailServer, MailEnable) or an
external SMTP gateway.  This spec implements the
mail-transport-management contract defined at
`codecraftai-sysman-common/specs/contracts/mail-transport-management/contract.md`
for Windows as a **best-effort** pilot that documents the gap
prominently and surfaces stable detail strings so callers can
discriminate "no MTA" from "real failure".

The pilot covers three flavours via the
`WindowsMtaKind` enum:

| Kind             | Coverage                                              |
|------------------|-------------------------------------------------------|
| `NONE` (default) | Reads return synthetic empty shapes; writes return    |
|                  | FAILED with `detail="windows_no_built_in_mta"`.       |
| `IIS_SMTP`       | Best-effort.  Reads via `Get-WmiObject IIsSmtpServer`;|
|                  | `Send-MailMessage` for delivery probes; queue-dir    |
|                  | file count for queue status; `Restart-Service`        |
|                  | SMTPSVC for flush.  Aliases not supported -> FAILED.  |
| `THIRD_PARTY`    | Placeholder.  Writes return FAILED with               |
|                  | `detail="windows_third_party_mta_not_implemented"`.   |

### Production guidance (callers MUST read)

**Pilot is best-effort on Windows; full enterprise MTA management is
out of scope.**  Operators who require production MTA capability
SHOULD run their MTA on Linux (Ubuntu / RHEL) or macOS where the
Postfix command suite is available natively.  Windows hosts that
need outbound mail should be configured to relay through a
Linux / macOS MTA rather than running a local server.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  All
external commands (`powershell.exe`) are executed exclusively
through the `codecraftai-interfaces` command-runner abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are
made inside this module.

---

## Requirements

### Requirement 1: Read MTA Configuration

**User Story:** As a maintenance coordinator, I want to read the
live MTA configuration on Windows so that I can decide whether the
host is mail-capable before attempting writes.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_mta_config()` AND
   `mta_kind=NONE` THE SYSTEM SHALL return a synthetic
   `WindowsMtaConfig` with every named field `None` and `detail`
   set to ``"Windows has no built-in MTA configured"``.
2. WHEN `mta_kind=THIRD_PARTY` THE SYSTEM SHALL return a synthetic
   `WindowsMtaConfig` with a descriptive `detail` documenting the
   not-implemented placeholder.
3. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL invoke a PowerShell
   pipeline that calls
   ``Get-WmiObject -Class IIsSmtpServerSetting -Namespace
   'root/MicrosoftIISv2'`` and emits a JSON object with
   `FullyQualifiedDomainName`, `SmartHost`, `MaxMessageSize`, and
   `ServerBindings`.
4. WHEN the IIS SMTP probe exits 0 THE SYSTEM SHALL parse the JSON
   into a `WindowsMtaConfig`: `myhostname` from
   `FullyQualifiedDomainName`, `relayhost` from `SmartHost`,
   `message_size_limit` AND `mailbox_size_limit` from
   `MaxMessageSize`, `extras["ServerBindings"]` from
   `ServerBindings`.
5. WHEN the IIS SMTP probe exits non-zero THE SYSTEM SHALL return
   a synthetic `WindowsMtaConfig` with `detail` set to a stderr
   summary; SHALL NOT raise.
6. `get_mta_config` MUST NOT pass `elevated=True`.

---

### Requirement 2: Apply MTA Configuration

**User Story:** As a maintenance coordinator, I want
`set_mta_config` to either apply directives via the configured MTA
or surface a clear FAILED result so that callers can discriminate
"unsupported" from "broken".

#### Acceptance Criteria

1. WHEN `mta_kind=NONE` THE SYSTEM SHALL return
   `status=FAILED` with `stderr="windows_no_built_in_mta"`,
   `command=None`, and SHALL NOT invoke the runner.
2. WHEN `mta_kind=THIRD_PARTY` THE SYSTEM SHALL return
   `status=FAILED` with
   `stderr="windows_third_party_mta_not_implemented"`.
3. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL build a PowerShell
   pipeline that fetches the WMI server object and assigns each
   spec field that maps to a known IIS SMTP property
   (`myhostname` -> `FullyQualifiedDomainName`, `relayhost` ->
   `SmartHost`, `message_size_limit` / `mailbox_size_limit` ->
   `MaxMessageSize`).
4. WHEN at least one spec field maps THE SYSTEM SHALL emit a
   trailing ``$s.Put_()`` to commit.
5. WHEN `dry_run=True` AND `mta_kind=IIS_SMTP` THE SYSTEM SHALL
   return `status=DRY_RUN` without invoking the runner.
6. WHEN the runner exits 0 THE SYSTEM SHALL return
   `status=SUCCESS`.  WHEN it exits non-zero, `status=FAILED`
   with `exit_code` and `stderr` populated.
7. The pilot SHALL NOT perform a per-property idempotency probe
   for `mta_kind=IIS_SMTP` (the WMI surface does not offer cheap
   per-property comparisons).  The verb dispatches
   unconditionally.
8. WHEN `elevated=True` AND `mta_kind=IIS_SMTP` THE SYSTEM SHALL
   pass `elevated=True` to the runner.

---

### Requirement 3: Add an Alias

**User Story:** As a maintenance coordinator, I want `add_alias` to
return a stable FAILED detail when aliases are not supported, so
that downstream automation can categorise the unsupported
operation.

#### Acceptance Criteria

1. WHEN `mta_kind=NONE` THE SYSTEM SHALL return
   `status=FAILED` with `stderr="windows_no_built_in_mta"`.
2. WHEN `mta_kind=THIRD_PARTY` THE SYSTEM SHALL return
   `status=FAILED` with
   `stderr="windows_third_party_mta_not_implemented"`.
3. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL return
   `status=FAILED` with
   `stderr="windows_iis_smtp_aliases_unsupported"`.  IIS SMTP has
   no native aliases concept.
4. `add_alias` SHALL NOT invoke the runner on any kind.

---

### Requirement 4: Remove an Alias

**User Story:** As a maintenance coordinator, I want `remove_alias`
to return the same stable FAILED surface as `add_alias`.

#### Acceptance Criteria

1. WHEN `mta_kind=NONE` THE SYSTEM SHALL return FAILED with
   `stderr="windows_no_built_in_mta"`.
2. WHEN `mta_kind=THIRD_PARTY` THE SYSTEM SHALL return FAILED
   with `stderr="windows_third_party_mta_not_implemented"`.
3. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL return FAILED with
   `stderr="windows_iis_smtp_aliases_unsupported"`.
4. `remove_alias` SHALL NOT invoke the runner on any kind.

---

### Requirement 5: List Aliases

**User Story:** As a maintenance coordinator, I want
`list_aliases()` to return an empty list on every Windows MTA
flavour, so that read-side enumeration has stable behaviour.

#### Acceptance Criteria

1. WHEN `list_aliases()` is called THE SYSTEM SHALL return an
   empty list regardless of `mta_kind`.
2. `list_aliases()` SHALL NOT invoke the runner.
3. `list_aliases()` MUST NOT pass `elevated=True`.

---

### Requirement 6: Probe Mail Delivery

**User Story:** As a maintenance coordinator, I want to send a
probe message via an installed Windows MTA, so that I can verify
delivery on hosts that have one configured.

#### Acceptance Criteria

1. WHEN `mta_kind=NONE` THE SYSTEM SHALL return FAILED with
   `stderr="windows_no_built_in_mta"`.
2. WHEN `mta_kind=THIRD_PARTY` THE SYSTEM SHALL return FAILED
   with `stderr="windows_third_party_mta_not_implemented"`.
3. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL invoke
   ``powershell.exe -NoProfile -NonInteractive -Command
   "Send-MailMessage -SmtpServer '<host>' -To '<to>' -From
   '<from>' -Subject '<subject>' -Body '...'"`` via the runner.
4. The script SHALL escape single quotes in addresses / subjects
   by doubling them (PowerShell single-quote escape).
5. WHEN `dry_run=True` AND `mta_kind=IIS_SMTP` THE SYSTEM SHALL
   return `status=DRY_RUN` without invoking the runner.
6. WHEN the runner exits 0 THE SYSTEM SHALL return
   `status=SUCCESS`.  WHEN non-zero, `status=FAILED`.
7. `test_mail_delivery` MUST NOT skip — it is operational.

---

### Requirement 7: Read Queue Status

**User Story:** As a maintenance coordinator, I want to read the
local queue status, so that I can detect stuck messages on hosts
running an MTA.

#### Acceptance Criteria

1. WHEN `mta_kind=NONE` OR `mta_kind=THIRD_PARTY` THE SYSTEM SHALL
   return a zeroed `WindowsMtaQueueStatus` and SHALL NOT invoke the
   runner.
2. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL invoke a PowerShell
   pipeline that calls ``Get-ChildItem -LiteralPath
   '<iis_smtp_queue_dir>' -File`` and emits a JSON object with
   ``Count`` and ``Sum`` properties (file count and aggregate
   length in bytes).
3. WHEN the runner exits 0 THE SYSTEM SHALL parse the JSON;
   ``Count`` populates `pending_count`, ``Sum`` populates
   `total_size_bytes`.  `deferred_count` stays at 0 — the IIS SMTP
   queue directory does not partition active vs deferred cheaply.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return a zeroed
   `WindowsMtaQueueStatus` (no raise — best-effort).
5. `get_queue_status` MUST NOT pass `elevated=True`.

---

### Requirement 8: Flush the Queue

**User Story:** As a maintenance coordinator, I want to attempt
immediate redelivery of queued messages on a Windows MTA host.

#### Acceptance Criteria

1. WHEN `mta_kind=NONE` OR `mta_kind=THIRD_PARTY` THE SYSTEM SHALL
   return FAILED with the appropriate stable detail string.
2. WHEN `mta_kind=IIS_SMTP` THE SYSTEM SHALL invoke a PowerShell
   pipeline that calls ``Restart-Service -Name '<service>' -Force``
   against the configured `iis_smtp_service_name` (default
   `"SMTPSVC"`).
3. WHEN `dry_run=True` AND `mta_kind=IIS_SMTP` THE SYSTEM SHALL
   return `status=DRY_RUN`.
4. WHEN the runner exits 0 THE SYSTEM SHALL return
   `status=SUCCESS`.  WHEN non-zero, `status=FAILED`.
5. `flush_queue` MUST NOT skip — it is operational.

---

### Requirement 9: Stable Detail Strings

**User Story:** As a maintenance coordinator, I want every Windows
MTA "unsupported" path to surface a documented, stable detail
string, so that policy decisions can be expressed declaratively.

#### Acceptance Criteria

1. The detail string for `mta_kind=NONE` failures SHALL be
   ``"windows_no_built_in_mta"`` exactly.
2. The detail string for `mta_kind=THIRD_PARTY` failures SHALL be
   ``"windows_third_party_mta_not_implemented"`` exactly.
3. The detail string for IIS-SMTP-but-aliases-unsupported failures
   SHALL be ``"windows_iis_smtp_aliases_unsupported"`` exactly.
4. These strings SHALL be exported as module-level constants
   (`_DETAIL_NO_MTA`, `_DETAIL_THIRD_PARTY_NOT_IMPL`,
   `_DETAIL_IIS_SMTP_NO_ALIASES`) so that callers can match on
   them.

---

### Requirement 10: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
that mutates IIS SMTP state to elevate, so that operations succeed
under UAC.

#### Acceptance Criteria

1. WHEN `elevated=True` AND `mta_kind=IIS_SMTP` THE SYSTEM SHALL
   pass `elevated=True` for `set_mta_config`,
   `test_mail_delivery`, and `flush_queue`.
2. WHEN performing reads (`get_mta_config`, `list_aliases`,
   `get_queue_status`) THE SYSTEM SHALL pass `elevated=False`
   regardless of the config.
3. WHEN `mta_kind=NONE` OR `mta_kind=THIRD_PARTY` THE SYSTEM SHALL
   NOT invoke the runner — elevation is moot.
4. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False`
   on every path.

---

### Requirement 11: Dry-Run

**User Story:** As a maintenance coordinator, I want to preview
every IIS SMTP mutation without dispatching, so that diff-vs-state
reports can be generated.

#### Acceptance Criteria

1. WHEN `dry_run=True` AND `mta_kind=IIS_SMTP` THE SYSTEM SHALL
   return `status=DRY_RUN` for every mutating verb without
   invoking the runner.
2. WHEN `dry_run=True` AND `mta_kind=NONE` / `THIRD_PARTY` THE
   SYSTEM SHALL still return FAILED with the unsupported detail
   string (the dry-run gate is downstream of the kind gate).
3. The DRY_RUN result SHALL include the would-have-been argv on
   the `command` field.

---

### Requirement 12: Documented Caveats

**User Story:** As a maintenance coordinator integrating this
package, I want the Windows MTA gap documented in code, so that
nobody mis-uses it as a production MTA manager.

#### Acceptance Criteria

1. The module docstring SHALL state that "Pilot is best-effort on
   Windows; full enterprise MTA management is out of scope.
   Operators who require production MTA capability SHOULD run
   their MTA on Linux or macOS."
2. The `WindowsMtaKind` enum SHALL document each value with
   guidance on when to choose it.
3. The `WindowsMtaManagerConfig` SHALL document
   `iis_smtp_queue_dir` defaulting to
   ``"%SystemRoot%\\System32\\inetsrv\\mailroot\\Queue"``.

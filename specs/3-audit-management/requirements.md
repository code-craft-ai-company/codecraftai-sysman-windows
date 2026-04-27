# Requirements

## Introduction

The audit management feature provides the
`automojo-sysman-windows` package with the ability to enable,
disable, query, and rule-manage the Windows auditing subsystem.
This spec implements the audit-management contract defined at
`automojo-sysman-common/specs/contracts/audit-management/contract.md`
for Windows using `auditpol.exe` for advanced-audit policy
management, `wevtutil.exe` for event-log channel toggling, and
`Get-WinEvent` (PowerShell) for event-stream filtering.

A maintenance coordinator running in an external package drives
all operations by calling into this feature's public verbs.  The
feature does not decide *which* subcategories belong on the host
— it provides the capability to install, remove, and observe them
reliably, idempotently, and with full auditability.  Every system
command is executed exclusively through the
`automojo-interfaces` command-runner abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls
are made inside the module.

The abstract `rule_id` on Windows maps to the advanced-audit
**subcategory name** (e.g., `"Logon"`, `"Object Access"`,
`"Process Creation"`).  The structured `WindowsAuditRuleSpec`
captures the subcategory plus a pair of `(success, failure)`
booleans that drive `auditpol /set /success:enable|disable
/failure:enable|disable`.

---

## Requirements

### Requirement 1: Add a Rule

**User Story:** As a maintenance coordinator, I want to enable a
specific Windows auditing subcategory, so that the audit
subsystem records (success, failure) events for that area.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_rule(spec)` THE SYSTEM SHALL
   dispatch ``auditpol /set /subcategory:<spec.subcategory>
   /success:<enable|disable> /failure:<enable|disable>`` through
   the runner.
2. WHEN ``auditpol /get`` already reports the subcategory's
   inclusion setting equal to the desired ``(success, failure)``
   tuple THE SYSTEM SHALL return ``status=SKIPPED``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return
   ``status=DRY_RUN`` without invoking the runner.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED`` with ``exit_code`` / ``stdout`` / ``stderr``
   / ``command`` populated.
5. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True``
   to the runner for the ``auditpol /set`` invocation.

---

### Requirement 2: Remove a Rule

**User Story:** As a maintenance coordinator, I want to remove a
Windows auditing subcategory's coverage, so that events of that
type are no longer recorded.

#### Acceptance Criteria

1. WHEN the coordinator calls ``remove_rule(rule_id)`` THE SYSTEM
   SHALL dispatch ``auditpol /set /subcategory:<rule_id>
   /success:disable /failure:disable``.
2. WHEN ``auditpol /get`` already reports the subcategory's
   inclusion setting as ``"No Auditing"`` THE SYSTEM SHALL
   return ``status=SKIPPED``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return
   ``status=DRY_RUN``.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.

---

### Requirement 3: List Rules

**User Story:** As a maintenance coordinator, I want to enumerate
the audit-policy subcategories, so that I can compare current
state against my desired baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_rules()`` THE SYSTEM SHALL
   dispatch ``auditpol /get /category:* /r`` through the runner
   without elevation.
2. WHEN the runner exits zero THE SYSTEM SHALL parse the CSV
   stdout into one ``WindowsAuditRuleInfo`` per subcategory,
   translating ``Inclusion Setting`` into the ``success`` /
   ``failure`` booleans.
3. WHEN the runner exits non-zero with non-empty stderr THE
   SYSTEM SHALL raise ``WindowsAuditQueryError``.
4. WHEN the CSV header lacks the ``Subcategory`` or ``Inclusion
   Setting`` columns THE SYSTEM SHALL return an empty list rather
   than raise (the host's auditpol build is unrecognised).

---

### Requirement 4: Enable Auditing

**User Story:** As a maintenance coordinator, I want to enable
the Windows event-log channel that hosts audit records, so that
the records are queryable.

#### Acceptance Criteria

1. WHEN the coordinator calls ``enable_auditing()`` THE SYSTEM
   SHALL dispatch ``wevtutil sl <default_log_channel> /e:true``.
2. WHEN ``wevtutil gl <channel>`` already reports ``enabled:
   true`` THE SYSTEM SHALL return ``status=SKIPPED``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return
   ``status=DRY_RUN``.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.
5. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True``
   to the runner for the ``wevtutil sl`` invocation.

---

### Requirement 5: Disable Auditing (Destructive)

**User Story:** As a maintenance coordinator, I want a destructive
disable verb for break-glass scenarios, but I want it gated
behind an explicit opt-in so I do not blow away my audit policy
by accident.

#### Acceptance Criteria

1. WHEN the coordinator calls ``disable_auditing()`` and
   ``config.allow_destructive`` is False THE SYSTEM SHALL return
   ``status=FAILED`` with ``message`` describing the opt-in
   requirement.  THE SYSTEM SHALL NOT invoke the runner.
2. WHEN the coordinator calls ``disable_auditing()`` and
   ``config.allow_destructive`` is True THE SYSTEM SHALL
   dispatch ``auditpol /clear /y`` through the runner.
3. WHEN ``dry_run=True`` AND ``allow_destructive`` is True THE
   SYSTEM SHALL return ``status=DRY_RUN``.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED`` with ``stdout`` / ``stderr`` / ``exit_code``.

---

### Requirement 6: Query Audit Status

**User Story:** As a maintenance coordinator, I want to inspect
the audit subsystem's status, so that my reports describe the
current policy.

#### Acceptance Criteria

1. WHEN the coordinator calls ``get_audit_status()`` THE SYSTEM
   SHALL dispatch ``auditpol /get /category:* /r`` and parse the
   CSV.
2. WHEN the CSV is parsed THE SYSTEM SHALL build a
   ``WindowsAuditStatus`` whose ``enabled`` is True iff at least
   one subcategory's inclusion setting is non-empty (i.e., not
   ``"No Auditing"``); ``running`` is always True (the EventLog
   service is always-running on healthy systems);
   ``rule_count`` is the number of subcategories observed; and
   ``subcategory_counts`` is a map of ``<setting> ->
   <count>``.
3. WHEN the runner exits non-zero with non-empty stderr THE
   SYSTEM SHALL raise ``WindowsAuditQueryError``.

---

### Requirement 7: Search Events

**User Story:** As a maintenance coordinator, I want to filter
audit events by ID, level, or time bound, so that post-hoc
analyses can target specific incidents.

#### Acceptance Criteria

1. WHEN the coordinator calls ``search_events(query)`` and
   ``query.raw_filter`` is empty THE SYSTEM SHALL build a
   PowerShell ``Get-WinEvent -LogName <channel>
   -FilterHashtable @{LogName='<channel>';
   Id=<event_id>; Level=<level>; StartTime=<since>;
   EndTime=<until>}`` pipeline.
2. WHEN ``query.raw_filter`` is non-empty THE SYSTEM SHALL emit
   the supplied tokens verbatim after the ``Get-WinEvent
   -LogName <channel>`` prefix.
3. WHEN ``query.limit`` is set THE SYSTEM SHALL append
   ``-MaxEvents <limit>`` to the pipeline.
4. WHEN the runner exits zero THE SYSTEM SHALL parse the
   ``ConvertTo-Json`` output into one ``WindowsAuditEvent`` per
   record, capping at ``query.limit`` when set, and applying
   ``query.keyword`` as a client-side substring filter against
   the message body.
5. WHEN the runner exits non-zero with non-empty stderr THE
   SYSTEM SHALL raise ``WindowsAuditQueryError``.
6. WHEN ``search_events`` is invoked THE SYSTEM SHALL pass
   ``elevated=True`` to the runner regardless of
   ``config.elevated``, because the Windows Security event log
   requires Administrator to read.  This is a documented
   exception to the contract's "reads do not elevate" rule.

---

### Requirement 8: Idempotency Across Verbs

**User Story:** As a maintenance coordinator, I want every write
verb to be safely re-runnable.

#### Acceptance Criteria

1. WHEN ``add_rule`` is called with a spec whose
   ``(success, failure)`` tuple already matches the current
   inclusion setting THE SYSTEM SHALL return ``status=SKIPPED``.
2. WHEN ``remove_rule`` is called and the subcategory is
   already ``"No Auditing"`` THE SYSTEM SHALL return
   ``status=SKIPPED``.
3. WHEN ``enable_auditing`` is called and the channel is
   already enabled THE SYSTEM SHALL return ``status=SKIPPED``.
4. ``disable_auditing`` is operational; it MUST NOT skip on the
   already-empty case (calling ``auditpol /clear`` against an
   already-clear policy is documented as a no-op by Microsoft).

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want write
operations to run with administrator privileges.

#### Acceptance Criteria

1. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True``
   to the runner for every write step.
2. WHEN performing the ``auditpol /get`` and ``wevtutil gl``
   reads THE SYSTEM SHALL NOT pass ``elevated=True`` even when
   the config opts in.
3. WHEN performing ``search_events`` THE SYSTEM SHALL pass
   ``elevated=True`` regardless of ``config.elevated`` (see
   Requirement 7.6).

---

### Requirement 10: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write
verb to honour dry-run mode.

#### Acceptance Criteria

1. WHEN ``dry_run=True`` THE SYSTEM SHALL return
   ``status=DRY_RUN`` for every write verb without invoking the
   runner.
2. WHEN ``dry_run=False`` THE SYSTEM SHALL execute the normal
   code path regardless of other configuration.
3. WHEN ``dry_run=True`` AND ``allow_destructive=False`` THE
   SYSTEM SHALL still return ``status=FAILED`` for
   ``disable_auditing`` (the destructive guard is evaluated
   before the dry-run guard).

---

### Requirement 11: Immutable-Mode Surfacing

**User Story:** As a maintenance coordinator, I want a clear
signal when the audit subsystem rejects a write, so that my
pipeline does not silently appear to succeed.

#### Acceptance Criteria

1. Windows does not expose an immutable audit mode.  The
   ``locked`` field of ``WindowsAuditStatus`` is always False
   for contract conformance.
2. WHEN a write fails because the runner lacks
   Administrator privileges THE SYSTEM SHALL surface the
   failure via the runner's non-zero exit and return
   ``status=FAILED`` with the captured stderr.

---

### Requirement 12: Observability and Result Shape

**User Story:** As a maintenance coordinator, I want every
action result to carry the same fields, so that downstream
reporting can treat them uniformly.

#### Acceptance Criteria

1. WHEN any write verb returns THE SYSTEM SHALL populate
   ``status``, ``command``, and (when applicable) ``rule_id``
   / ``rule_key`` on the result.
2. WHEN any write verb returns ``SUCCESS`` or ``FAILED`` THE
   SYSTEM SHALL populate ``exit_code``, ``stdout``, and
   ``stderr`` from the runner's ``RunResult``.
3. WHEN any write verb returns ``SKIPPED`` THE SYSTEM SHALL
   populate ``message`` with a human-readable explanation.

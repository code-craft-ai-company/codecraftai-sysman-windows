# Design

## Overview

The audit-management feature is implemented as a single module,
`ccai.sysman.windows.auditmanagement`, that exposes a
`WindowsAuditManager` class.  This spec implements the
audit-management contract defined at
`codecraftai-sysman-common/specs/contracts/audit-management/contract.md`
for Windows using `auditpol.exe` for advanced-audit policy,
`wevtutil.exe` for event-log channel toggling, and `Get-WinEvent`
(PowerShell) for event-stream filtering.

All system commands go through the `ICommandRunner` interface.
No `subprocess`, no direct `open`, and no direct `pathlib` calls
are made inside the module.

The feature works identically whether commands execute locally
(via `LocalCommandRunner`) or remotely over WinRM/SSH (via
`SSHCommandRunner`).  When using a remote runner, the
coordinator can track reliability metrics and apply execution
aspects (persistence, retry, timeout) without any changes to the
`WindowsAuditManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/remote execution, reliability tracking, and
aspect configuration.

---

## Cross-OS Audit-Model Concept Mapping

The three Wave-2 OSes diverge sharply in how their audit
subsystems are addressed.  The table below makes the cross-OS
divergence explicit so that the same contract can be honoured by
three very different toolchains.

| Concept | Linux (Ubuntu / RHEL) | macOS (BSM) | Windows |
|---|---|---|---|
| Tool name | `auditctl` / `augenrules` / `ausearch` | `audit` / `praudit` / `auditreduce` | **`auditpol` / `wevtutil` / `Get-WinEvent`** |
| Rule shape | Line-based; one argv body per rule (`-w /etc/passwd -p wa -k passwd_changes`) | Class-based; comma-separated audit-class abbreviations in `/etc/security/audit_control` | **Subcategory-based; `(subcategory, success, failure)` tuple per policy entry** |
| Persistence | Drop-in file under `/etc/audit/rules.d/`, reloaded with `augenrules --load` | Edit `/etc/security/audit_control`, then `audit -s` to reload | **`auditpol /set` is itself the persistent write; `auditpol /backup` exports for archival** |
| Identity | `-k <key>` token | The class abbreviation itself | **The subcategory name (e.g., `"Logon"`)** |
| Status | `auditctl -s` (multi-line key/value) | `audit -s` (textual blob) | **`auditpol /get /category:* /r` (CSV)** |
| Search | `ausearch -k <key> -m <type> -ts <ts>` | `praudit -l <log>` text grep | **`Get-WinEvent -LogName <channel> -FilterHashtable @{...}`** |

Windows sits in the **subcategory-based** column.  Each
subcategory carries a single inclusion-setting label
(`"No Auditing"`, `"Success"`, `"Failure"`, `"Success and
Failure"`) which the manager translates into the
`(success, failure)` tuple in `WindowsAuditRuleSpec`.  There is
no per-path or per-syscall granularity comparable to
`auditctl -w` or per-class flags comparable to macOS — instead,
SACLs on individual securable objects extend the per-subcategory
policy.  SACL management is **out of scope** for this feature
(see `acl-management`).

The recognised inclusion settings:

| CSV value | (success, failure) |
|-----------|---------------------|
| `No Auditing` | `(False, False)` |
| `Success` | `(True, False)` |
| `Failure` | `(False, True)` |
| `Success and Failure` | `(True, True)` |

Anything else is treated as `(False, False)` so that the
idempotency probe never over-claims coverage.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner                                      │
│  - Configures aspects (optional)                            │
│  - Injects runner + WindowsAuditManagerConfig               │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsAuditManager                              │
│  ccai.sysman.windows.auditmanagement                    │
│                                                             │
│  add_rule()             remove_rule()                       │
│  list_rules()           enable_auditing()                   │
│  disable_auditing()     get_audit_status()                  │
│  search_events()                                            │
└───────────┬─────────────────────────────────────────────────┘
            │ delegates to
            ▼
┌──────────────────────────┐
│   ICommandRunner         │
│   run(args, elevated)    │
│   → RunResult            │
└──────────┬───────────────┘
           │
           ▼
  ┌──────────────────────────────────┐
  │   LocalCommandRunner   or        │
  │   WinRM/SSHCommandRunner         │
  └─────────────┬────────────────────┘
                │ optional
                ▼
  ┌──────────────────────────────────┐
  │   PersistenceAspect /            │
  │   RetryAspect / TimeoutAspect    │
  └──────────────────────────────────┘
```

---

## Components

### `WindowsAuditManager`

**Location:**
`source/packages/ccai/sysman/windows/auditmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_rule` | `(spec: WindowsAuditRuleSpec) → WindowsAuditActionResult` | `auditpol /set` against the subcategory. |
| `remove_rule` | `(rule_id: str) → WindowsAuditActionResult` | `auditpol /set ... /success:disable /failure:disable`. |
| `list_rules` | `() → list[WindowsAuditRuleInfo]` | Parse `auditpol /get /category:* /r`. |
| `enable_auditing` | `() → WindowsAuditActionResult` | `wevtutil sl <channel> /e:true`. |
| `disable_auditing` | `() → WindowsAuditActionResult` | `auditpol /clear /y` (destructive; gated by `allow_destructive`). |
| `get_audit_status` | `() → WindowsAuditStatus` | Roll up the CSV into counts. |
| `search_events` | `(query: WindowsAuditEventQuery) → list[WindowsAuditEvent]` | `Get-WinEvent -FilterHashtable`. |

### `WindowsAuditManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit. |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True`. |
| `powershell_executable` | `str` | `"powershell.exe"` | PowerShell host. |
| `default_log_channel` | `str` | `"Security"` | Default channel for `enable_auditing` and `search_events`. |
| `allow_destructive` | `bool` | `False` | Master switch for `disable_auditing`. |

---

## Data Models

### Enum

```python
class WindowsAuditActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

### `WindowsAuditRuleSpec`

| Field | Type | Description |
|---|---|---|
| `subcategory` | `str` | Advanced-audit subcategory name (the abstract `rule_id`). |
| `success` | `bool` | True → `/success:enable`. |
| `failure` | `bool` | True → `/failure:enable`. |

### `WindowsAuditRuleInfo`

| Field | Type | Description |
|---|---|---|
| `rule_id` | `str` | Subcategory name. |
| `subcategory` | `str` | Mirrors `rule_id`. |
| `category` | `str` | Parent category (e.g. `"Logon/Logoff"`). |
| `success` | `bool` | True when inclusion setting includes Success. |
| `failure` | `bool` | True when inclusion setting includes Failure. |
| `expression` | `str` | Raw inclusion-setting text. |
| `subsystem` | `str` | Always `"subcategory"`. |
| `source` | `str` | Always `"auditpol"`. |

### `WindowsAuditStatus`

| Field | Type | Description |
|---|---|---|
| `enabled` | `bool` | True when at least one subcategory is non-empty. |
| `running` | `bool` | Always True (the EventLog service is always-running). |
| `pid` | `int \| None` | Always `None`. |
| `failure_mode` | `str \| None` | Always `None`. |
| `rate_limit` | `int \| None` | Always `None`. |
| `backlog_limit` | `int \| None` | Always `None`. |
| `backlog` | `int \| None` | Always `None`. |
| `lost_events` | `int \| None` | Always `None`. |
| `locked` | `bool` | Always False (no immutable mode). |
| `rule_count` | `int` | Subcategories observed. |
| `subcategory_counts` | `dict` | Map of inclusion setting → count. |
| `raw` | `str` | Raw CSV stdout. |

### `WindowsAuditEventQuery`

| Field | Type | Description |
|---|---|---|
| `channel` | `str \| None` | Event log channel; defaults to `default_log_channel`. |
| `event_id` | `int \| None` | Filter by `Id`. |
| `level` | `int \| None` | Filter by `Level`. |
| `principal` | `str \| None` | Reserved. |
| `keyword` | `str \| None` | Substring filter applied client-side. |
| `since` | `str \| None` | `StartTime` value. |
| `until` | `str \| None` | `EndTime` value. |
| `raw_filter` | `str \| None` | Verbatim filter expression. |
| `limit` | `int \| None` | Cap on parsed records. |

### `WindowsAuditEvent`

| Field | Type | Description |
|---|---|---|
| `timestamp` | `str` | `TimeCreated` field. |
| `event_id` | `int \| None` | `Id` field. |
| `category` | `str \| None` | `LogName` field. |
| `severity` | `str \| None` | `LevelDisplayName` field. |
| `message` | `str` | Truncated message body. |
| `raw` | `str` | Full message body. |

### `WindowsAuditActionResult`

Inherits from `FeatureActionResult` and adds `rule_id`,
`rule_key`, `message`.

### Exceptions

| Exception | Raised When |
|---|---|
| `WindowsAuditQueryError` | A read fails unexpectedly. |
| `WindowsAuditImmutableError` | Reserved for cross-OS conformance; never raised by the manager itself (Windows has no immutable mode). |

---

## Verb Behaviors

### `add_rule(spec)`

1. Render argv via `_build_auditpol_set`.
2. **Dry-run guard:** when `config.dry_run` is True, return
   `DRY_RUN`.
3. **Idempotency guard:** call `_safe_runtime_rules()` and probe
   `_spec_already_in_effect`.  When the existing
   `(success, failure)` tuple matches, return `SKIPPED`.
4. Dispatch the runtime invocation with
   `elevated=config.elevated`.
5. Map exit code 0 → `SUCCESS`, non-zero → `FAILED`.

### `remove_rule(rule_id)`

Built on `add_rule`'s shape with both flags set to False:
1. Render argv via `_build_auditpol_set` against
   `WindowsAuditRuleSpec(subcategory=rule_id, success=False,
   failure=False)`.
2. Dry-run guard.
3. Idempotency guard returns `SKIPPED` when the subcategory is
   already `"No Auditing"`.
4. Dispatch.

### `list_rules()`

1. Dispatch `auditpol /get /category:* /r` without elevation.
2. Raise `WindowsAuditQueryError` on non-zero exit with
   non-empty stderr.
3. Parse via `_parse_auditpol_csv`.

### `enable_auditing()`

1. Build argv `["wevtutil", "sl", <channel>, "/e:true"]`.
2. Dry-run guard.
3. Probe channel state via `_channel_already_enabled` (which
   dispatches `wevtutil gl <channel>` and looks for
   `enabled: true`).  Return `SKIPPED` when already enabled.
4. Dispatch with `elevated=config.elevated`.

### `disable_auditing()`

1. Build argv `["auditpol", "/clear", "/y"]`.
2. **Destructive guard:** when `config.allow_destructive` is
   False, return `FAILED` with the descriptive message
   immediately, without invoking the runner or evaluating the
   dry-run guard.  This is intentional — we want a hard,
   visible rejection on accidentally-shared configs.
3. Dry-run guard.
4. Dispatch with `elevated=config.elevated`.

### `get_audit_status()`

1. Dispatch `auditpol /get /category:* /r`.
2. Raise on non-zero exit + non-empty stderr.
3. Parse via `_parse_auditpol_csv` and roll the rows up into
   `subcategory_counts`.  Mark `enabled` True when any
   subcategory's inclusion setting is non-empty.

### `search_events(query)`

1. Build the PowerShell scriptblock via
   `_build_get_winevent_script`.  When `query.raw_filter` is
   set, the manager emits a string of the form
   `Get-WinEvent -LogName '<channel>' <raw_filter> |
   ConvertTo-Json -Compress -Depth 5`.  Otherwise the manager
   emits `-FilterHashtable` with the structured fields.
2. Build the PowerShell argv via `_build_powershell_argv`.
3. Dispatch with **`elevated=True`** regardless of
   `config.elevated`.  This is the single documented exception
   to the contract's "reads do not elevate" rule: the Windows
   Security event log requires Administrator to read.
4. Parse the JSON output via `_parse_winevent_json`.  Apply
   `query.keyword` as a client-side substring filter against
   the message body.  Cap at `query.limit` when set.

---

## Idempotency Model

| Verb | Probe | Skip condition |
|---|---|---|
| `add_rule` | `auditpol /get /category:* /r` | The subcategory's `(success, failure)` tuple already matches the spec. |
| `remove_rule` | `auditpol /get /category:* /r` | The subcategory is already `"No Auditing"`. |
| `enable_auditing` | `wevtutil gl <channel>` | Channel already reports `enabled: true`. |
| `disable_auditing` | None (operational) | Never skips. |

Probes always run unelevated.  Probe failures fall through to
attempting the operation rather than masking it as a no-op.

---

## Persistence Model

`auditpol /set` is itself a persistent write — the policy is
stored in the local Group Policy hive and survives reboot.
There is no separate "drop-in" or "rules.d" model on Windows.
This is documented in the audit-model concept-mapping table.

---

## Elevation Policy

| Operation | `elevated` |
|---|---|
| `auditpol /set` | `config.elevated` |
| `auditpol /clear /y` | `config.elevated` |
| `wevtutil sl <channel>` | `config.elevated` |
| `auditpol /get` | `False` |
| `wevtutil gl <channel>` | `False` |
| `Get-WinEvent` (PowerShell) | **`True` always** |

The last row is the contract's documented exception: reading
the Windows Security event log requires Administrator
privileges, so `search_events` elevates regardless of
`config.elevated`.

---

## Parsers

### `_parse_auditpol_csv`

`auditpol /r` emits a CSV with a header row plus one row per
subcategory.  The header columns vary across Windows builds, but
the canonical schema is:

```
Machine Name,Policy Target,Subcategory,Subcategory GUID,
Inclusion Setting,Exclusion Setting
```

Some builds prepend a `Category` column.  The parser builds a
column-name → index map from the header and routes every row
through that map, which makes it tolerant of column order
changes and absent columns.  When the header lacks the
`Subcategory` or `Inclusion Setting` columns, the parser
returns an empty list rather than raising — the host's auditpol
build is unrecognised and the caller gets a typed empty result.

### `_split_csv_line`

A small, in-process CSV splitter that honours double-quoted
fields.  We do not pull in the standard library's `csv` module
to keep the import surface flat and to avoid odd behaviour
around the BOM that Windows tooling sometimes emits.

### `_normalise_inclusion_setting`

Translates the inclusion-setting text into a
`(success, failure)` boolean tuple via the lookup table above.
Unknown values default to `(False, False)`.

### `_parse_winevent_json`

`Get-WinEvent | ConvertTo-Json` emits either a single object
(scalar pipeline result) or a JSON array.  The parser uses
`json.loads` and normalises both shapes via the
`isinstance(decoded, dict)` / `isinstance(decoded, list)`
fall-through.  Each record is mapped into a `WindowsAuditEvent`
with field-by-field `isinstance` guards so corrupt rows surface
as `None` fields rather than raising.

---

## Renderers

### `_build_auditpol_set`

Emits `["auditpol", "/set", "/subcategory:<sub>",
"/success:<enable|disable>", "/failure:<enable|disable>"]`.
The boolean → token mapping is owned by
`_bool_to_auditpol_flag`.

### `_build_get_winevent_script`

Emits a PowerShell scriptblock that pipes `Get-WinEvent` into
`ConvertTo-Json -Compress -Depth 5`.  When `query.raw_filter`
is set the verbatim tokens are interpolated; otherwise the
filter hashtable is built from the structured fields.

---

## Failure Mapping

| Runner outcome | Mapped status |
|---|---|
| Zero exit code | `SUCCESS` |
| Non-zero exit code | `FAILED` |
| Pre-flight skip | `SKIPPED` |
| Dry-run | `DRY_RUN` |
| Destructive guard rejects `disable_auditing` | `FAILED` (with descriptive `message`) |

`WindowsAuditQueryError` carries `command`, `exit_code`, and
`stderr` for read-path debuggability.

---

## Testing Strategy

Tests use a `_MockCommandRunner` that records every `run` call
and returns pre-seeded `RunResult` values keyed by argv tuple.
Coverage targets:

1. Each write verb dispatches the canonical argv.
2. Each write verb honours the idempotency guard.
3. Each write verb honours dry-run.
4. `disable_auditing` rejects when `allow_destructive=False` and
   dispatches when True.
5. `list_rules` parses the CSV.
6. `get_audit_status` rolls up counts and flips `enabled`
   correctly.
7. `search_events` builds the PowerShell scriptblock and
   passes `elevated=True`.
8. Reads (auditpol-backed) never elevate; writes elevate.
9. Failed runner exits surface as `FAILED` results with stderr
   intact.
10. Pure-function tests cover `_build_auditpol_set`,
    `_normalise_inclusion_setting`, `_parse_auditpol_csv`,
    `_build_get_winevent_script`, and `_parse_winevent_json`.

---

## Spec Deviations

- **`IFileOperator` collaborator.** The cross-OS contract names
  an `IFileOperator` for rule-file I/O.  Windows audit policy is
  not stored in plain text files; the manager has no file I/O
  to delegate.

- **Subcategory-based addressing.** The contract's abstract
  `AuditRuleSpec.expression` field carries the rule body as a
  string.  Windows's subcategory-based model collapses
  `expression` onto the inclusion setting text (e.g.,
  ``"Success and Failure"``).  Downstream cross-OS callers must
  be aware of this divergence.

- **`reload_rules` verb.** `auditpol /set` is itself the
  persistent write; there is no reload step to surface as a
  separate verb.

- **Destructive `disable_auditing`.** This is a Windows-specific
  surfacing of the destructive guard: `auditpol /clear` is the
  closest Windows analogue to "disable the audit subsystem"
  (the EventLog service itself is always-running).  Callers
  MUST set `allow_destructive=True` to opt in.

- **Elevated reads in `search_events`.** Windows breaks the
  contract's "reads do not elevate" rule for the Security event
  log because the log itself is access-controlled.  The
  contract carves this out in §"Method-level invariants
  (reads)" item 1.

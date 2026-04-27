# Requirements

## Introduction

The maintenance-reporting feature provides the `automojo-sysman-windows` package with
a structured, serialisable record of a maintenance session.  This spec implements the
maintenance-reporting contract defined at
`automojo-sysman-common/specs/contracts/maintenance-reporting/contract.md` for
Windows using pure Python accumulation of action records and the `IFileOperator`
abstraction (via `WindowsLocalFileOperator` / `WindowsSSHFileOperator`) for the
single I/O egress point — `emit_report(path, format)`.

A maintenance coordinator running in an external package drives the reporter by
calling `begin_session`, recording one or more `WindowsActionRecord` instances
through `add_action` / `record_query` / `record_error`, calling `end_session`, and
finally emitting a JSON, YAML, or Markdown file through the file operator.  The
reporter module itself never opens a file handle outside of `IFileOperator`; every
on-disk artefact goes through that abstraction so dry-run semantics and aspect
composition apply uniformly.  A `_clock` callable is accepted for deterministic
tests.  The default output directory on Windows is
`%ProgramData%\Automojo\Maintenance\<session_id>\`, which is ACL-writable by local
Administrators without requiring UAC on directories the install step has already
provisioned.

---

## Requirements

### Requirement 1: Begin a Session

**User Story:** As a maintenance coordinator, I want to start a new reporting session
with a run identifier, host, and optional description, so that every subsequent action
recorded in this process is associated with a specific maintenance run.

#### Acceptance Criteria

1. WHEN the coordinator calls `begin_session(run_id, host, description)` THE SYSTEM
   SHALL initialise a fresh `WindowsMaintenanceSession` with the supplied `run_id`,
   `host`, and `description`, `started_at` set to the clock's current UTC time,
   `finished_at=None`, `overall_status=None`, and an empty `actions` list.
2. WHEN `begin_session` is called THE SYSTEM SHALL NOT touch the file operator.
3. WHEN `begin_session` is called twice on the same reporter instance THE SYSTEM SHALL
   overwrite the previous session, starting a new accumulation.
4. WHEN the `_clock` injection point is replaced THE SYSTEM SHALL use the injected
   callable to obtain `started_at`.

---

### Requirement 2: Record an Action

**User Story:** As a maintenance coordinator, I want to append an action record to the
session in insertion order, so that the report reflects the exact order of operations.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_action(record)` or `record_query(record)` THE
   SYSTEM SHALL append the `WindowsActionRecord` to the session's `actions` list
   without sorting or de-duplication.
2. WHEN the supplied `WindowsActionRecord` has `timestamp=None` THE SYSTEM SHALL
   assign `datetime.utcnow()` via the injected `_clock` at add-time.
3. WHEN the supplied `WindowsActionRecord` has a non-`None` `timestamp` THE SYSTEM
   SHALL preserve it verbatim.
4. WHEN `add_action` or `record_query` is called THE SYSTEM SHALL NOT touch the file
   operator.
5. WHEN `record_query` is called THE SYSTEM SHALL produce the same list mutation as
   `add_action` — the two verbs are identical from the state-machine perspective and
   exist for call-site readability only.

---

### Requirement 3: Record an Error

**User Story:** As a maintenance coordinator, I want a shorthand to record a failure
without constructing a full `WindowsActionRecord`, so that error-handling call sites
stay terse.

#### Acceptance Criteria

1. WHEN the coordinator calls `record_error(feature, operation, error)` THE SYSTEM
   SHALL construct a `WindowsActionRecord` with `feature=feature`,
   `operation=operation`, `status=WindowsActionStatus.FAILED`, `error=error`,
   `timestamp` set by the clock, and append it to the session's `actions` list.
2. WHEN `record_error` is called without an active session THE SYSTEM SHALL raise a
   `RuntimeError` with a descriptive message.
3. WHEN `record_error` is called THE SYSTEM SHALL NOT touch the file operator.

---

### Requirement 4: End a Session

**User Story:** As a maintenance coordinator, I want to finalise the session so that
the overall status reflects all recorded actions and the finish time is stamped.

#### Acceptance Criteria

1. WHEN the coordinator calls `end_session()` THE SYSTEM SHALL set
   `session.finished_at` to the current UTC time via the clock.
2. WHEN any action has `status=FAILED` THE SYSTEM SHALL set
   `session.overall_status=FAILED`.
3. WHEN no action has `status=FAILED` THE SYSTEM SHALL set
   `session.overall_status=SUCCESS`, even when every action is `SKIPPED` or `DRY_RUN`.
4. WHEN `end_session` is called multiple times THE SYSTEM SHALL re-evaluate both
   `finished_at` and `overall_status` on each call.
5. WHEN `end_session` is called THE SYSTEM SHALL NOT touch the file operator.

---

### Requirement 5: Read-Only Queries

**User Story:** As a maintenance coordinator, I want read-only helpers for summary
counts and action filtering, so that I can drive conditional logic after a session
without re-parsing the report.

#### Acceptance Criteria

1. WHEN the coordinator calls `summary_counts()` THE SYSTEM SHALL return a
   `WindowsReportSummary` with `total` equal to `len(actions)` and fields
   `succeeded` / `failed` / `skipped` / `dry_run` equal to the count of
   `WindowsActionStatus.SUCCESS` / `FAILED` / `SKIPPED` / `DRY_RUN` records.
2. WHEN the coordinator calls `actions_by_status(*statuses)` THE SYSTEM SHALL return a
   list of `WindowsActionRecord` whose `status` appears in `statuses`, in the original
   insertion order.
3. WHEN the coordinator calls `actions_by_feature(feature)` THE SYSTEM SHALL return a
   list of `WindowsActionRecord` whose `feature` equals `feature`, in the original
   insertion order.
4. WHEN any of these read helpers is called THE SYSTEM SHALL NOT touch the file
   operator.
5. WHEN the `actions` list is empty THE SYSTEM SHALL return `WindowsReportSummary(0,
   0, 0, 0, 0)` and empty lists for the two filters.

---

### Requirement 6: Serialise to String

**User Story:** As a maintenance coordinator, I want to convert the session to a JSON
or human-readable text string in memory, so that I can log or display the report
without touching disk.

#### Acceptance Criteria

1. WHEN the coordinator calls `to_json(indent)` THE SYSTEM SHALL produce a JSON string
   containing `run_id`, `host`, `description`, `started_at` as ISO-8601, `finished_at`
   as ISO-8601 or `null`, `overall_status` as its enum value or `null`, `summary`
   (inlined `WindowsReportSummary` as a dict), and an `actions` list.
2. WHEN `indent` is supplied to `to_json` THE SYSTEM SHALL forward it to `json.dumps`.
3. WHEN an action's `timestamp` is populated THE SYSTEM SHALL serialise it as ISO-8601
   UTC.
4. WHEN an action's `duration_seconds` is `None` THE SYSTEM SHALL serialise it as
   `null`.
5. WHEN `to_json` is called twice with the same state and indent THE SYSTEM SHALL
   return byte-identical strings.
6. WHEN the coordinator calls `to_text()` THE SYSTEM SHALL return a plain-text block
   containing `run_id`, `host`, `description`, `started_at`, `finished_at`,
   `overall_status`, the summary line, and one line per action in insertion order.
7. WHEN an action has a non-empty `error` THE SYSTEM SHALL render the error on the
   next line indented four spaces with the prefix `Error: `.

---

### Requirement 7: Emit the Report to a File

**User Story:** As a maintenance coordinator, I want to persist the serialised report
to disk through the file operator, so that downstream tooling (log aggregators, CI
artefacts) can consume it.

#### Acceptance Criteria

1. WHEN the coordinator calls `emit_report(path, format)` with `format` equal to
   `WindowsReportFormat.JSON` / `TEXT` / `YAML` / `MARKDOWN` THE SYSTEM SHALL
   serialise the session in the requested format and SHALL invoke
   `file_operator.write_text(path, content)`.
2. WHEN `format=WindowsReportFormat.YAML` THE SYSTEM SHALL serialise via the `yaml`
   module (PyYAML) using `safe_dump` with `default_flow_style=False`.
3. WHEN `format=WindowsReportFormat.MARKDOWN` THE SYSTEM SHALL produce a Markdown
   document with an H1 title, a metadata table, a summary table, and a per-action
   section with status icons.
4. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke the file operator and SHALL
   return `WindowsReportWriteResult(success=True, path=path, dry_run=True)`.
5. WHEN the file operator raises an exception THE SYSTEM SHALL catch it and return
   `WindowsReportWriteResult(success=False, path=path, error=str(exc))` without
   propagating.
6. WHEN the path argument is empty or `None` THE SYSTEM SHALL return
   `WindowsReportWriteResult(success=False, error="path must be a non-empty string")`
   without invoking the file operator.
7. WHEN `format` is not a member of `WindowsReportFormat` THE SYSTEM SHALL return
   `WindowsReportWriteResult(success=False, error="unsupported format: <name>")`
   without invoking the file operator.
8. WHEN `path` contains forward slashes or mixed separators THE SYSTEM SHALL pass the
   path through to the file operator without normalising — `IFileOperator` owns
   path-normalisation semantics.

---

### Requirement 8: Default Session Path

**User Story:** As a maintenance coordinator, I want a conventional output directory
on Windows for emitted reports, so that system administrators can find reports at a
well-known location.

#### Acceptance Criteria

1. WHEN the coordinator calls `default_session_path(session_id)` THE SYSTEM SHALL
   return the string
   `<ProgramData>\Automojo\Maintenance\<session_id>\`, where `<ProgramData>` is
   resolved from `os.environ["ProgramData"]` (falling back to
   `C:\ProgramData` when the variable is unset).
2. WHEN the caller supplies `session_id=""` or `session_id=None` THE SYSTEM SHALL
   raise `ValueError` with a descriptive message.
3. WHEN the caller wants to override the base THE SYSTEM SHALL accept a
   `WindowsMaintenanceReportConfig.output_base` field and join it with `session_id`
   via `os.path.join`.
4. WHEN the resulting path is passed to `emit_report` and the file operator requires
   elevation THE SYSTEM SHALL rely on the file operator's caller-supplied `elevated`
   policy; the reporter itself never embeds elevation policy.
5. WHEN the resolved path uses backslashes THE SYSTEM SHALL preserve them verbatim
   in the return value.

---

### Requirement 9: Clock Injection

**User Story:** As a test author, I want to inject a deterministic clock into the
reporter and the `WindowsActionRecord` constructor, so that tests are deterministic.

#### Acceptance Criteria

1. WHEN the reporter is constructed with a `_clock` callable THE SYSTEM SHALL use that
   callable for `started_at`, `finished_at`, and default action timestamps.
2. WHEN `WindowsActionRecord.__post_init__` observes `timestamp=None` AND a `_clock`
   parameter is supplied THE SYSTEM SHALL use that clock.
3. WHEN no `_clock` is supplied THE SYSTEM SHALL default to `datetime.utcnow`.
4. WHEN the injected `_clock` returns a naive datetime THE SYSTEM SHALL treat it as
   UTC and serialise with `Z` suffix.

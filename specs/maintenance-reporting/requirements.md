# Requirements

## Introduction

The maintenance reporting feature provides the `automojo-sysman-linux-windows` package
with the ability to produce structured, human-readable reports summarising the outcomes
of a maintenance run.  Reports capture the actions taken, their statuses, any errors
encountered, timing information, and system context gathered at the start of the run.

A maintenance coordinator builds up a report incrementally during a run by appending
action records, then finalises and serialises the report at the end.  The feature does
not decide *what* to report — it provides the data structures and serialisation
primitives needed to record, query, and emit maintenance outcomes in a consistent,
machine-readable and human-readable form.  No command execution or file system access is
performed directly; all output is either returned as a string or written through the
`automojo-interfaces` file-operator abstraction.

---

## Requirements

### Requirement 1: Create a Maintenance Report

**User Story:** As a maintenance coordinator, I want to create a report object at the
start of a maintenance run, so that I have a single container in which to accumulate
action results throughout the run.

#### Acceptance Criteria

1. WHEN the coordinator creates a `MaintenanceReport` THE SYSTEM SHALL record the current
   UTC timestamp as `started_at`.
2. WHEN the coordinator provides a `run_id` THE SYSTEM SHALL store it on the report.
3. WHEN the coordinator provides a `host` string THE SYSTEM SHALL store it on the report.
4. WHEN the coordinator provides a `description` string THE SYSTEM SHALL store it on the
   report.
5. WHEN the report is created the `actions` list SHALL be empty and `finished_at` SHALL
   be `None`.

---

### Requirement 2: Record an Action Result

**User Story:** As a maintenance coordinator, I want to append the result of each
maintenance action to the report, so that the final report contains a complete audit
trail of everything that was attempted.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_action` with an `ActionRecord` THE SYSTEM SHALL
   append it to the report's `actions` list in the order received.
2. WHEN an `ActionRecord` is created it SHALL carry: `feature` (the subsystem name),
   `operation` (the method or task name), `status` (success/failed/skipped/dry_run),
   `detail` (optional human-readable description), `error` (optional error message or
   stderr), `duration_seconds` (optional float), and `timestamp` (UTC datetime of the
   action).
3. WHEN the coordinator does not supply `timestamp` THE SYSTEM SHALL default it to the
   current UTC time.

---

### Requirement 3: Finalise the Report

**User Story:** As a maintenance coordinator, I want to finalise the report at the end
of the maintenance run, so that the report captures total elapsed time and an overall
outcome.

#### Acceptance Criteria

1. WHEN the coordinator calls `finalise` THE SYSTEM SHALL record the current UTC
   timestamp as `finished_at`.
2. WHEN all actions have status `success` or `skipped` or `dry_run` THE SYSTEM SHALL set
   the report's `overall_status` to `SUCCESS`.
3. WHEN any action has status `failed` THE SYSTEM SHALL set `overall_status` to `FAILED`.
4. WHEN the report has no actions THE SYSTEM SHALL set `overall_status` to `SUCCESS`.
5. WHEN `finalise` is called more than once THE SYSTEM SHALL update `finished_at` and
   recalculate `overall_status` based on the current action list.

---

### Requirement 4: Query Report Summary Counts

**User Story:** As a maintenance coordinator, I want to retrieve a count of actions by
status, so that I can quickly determine how many items succeeded, failed, were skipped,
or were simulated.

#### Acceptance Criteria

1. WHEN the coordinator calls `summary_counts` on a finalised or in-progress report THE
   SYSTEM SHALL return a `ReportSummary` record containing counts for `total`,
   `succeeded`, `failed`, `skipped`, and `dry_run`.
2. WHEN there are no actions THE SYSTEM SHALL return zero for all counts.

---

### Requirement 5: Serialise the Report to JSON

**User Story:** As a maintenance coordinator, I want to serialise the completed report
to a JSON string, so that it can be stored, transmitted, or ingested by downstream
tooling.

#### Acceptance Criteria

1. WHEN the coordinator calls `to_json` the report SHALL be serialised to a valid JSON
   string with all fields present.
2. WHEN `indent` is specified THE SYSTEM SHALL pass it to the JSON serialiser for
   human-readable formatting.
3. WHEN `started_at` or `finished_at` are present they SHALL be serialised as ISO 8601
   strings.
4. WHEN the report has not been finalised `finished_at` SHALL be serialised as `null`.
5. WHEN `duration_seconds` is `None` on an `ActionRecord` it SHALL be serialised as
   `null`.

---

### Requirement 6: Serialise the Report to a Human-Readable Text Summary

**User Story:** As a maintenance coordinator, I want to generate a plain-text summary of
the report, so that it can be logged to the console or written to a plain-text log file
for human review.

#### Acceptance Criteria

1. WHEN the coordinator calls `to_text` THE SYSTEM SHALL return a formatted string
   containing: the run ID, host, description, start time, finish time (or `"in progress"`
   if not finalised), overall status, summary counts, and a table of action records.
2. WHEN an `ActionRecord` has `error` set THE SYSTEM SHALL include the error text
   indented below the action line.
3. WHEN the report has no actions THE SYSTEM SHALL include a line indicating no actions
   were recorded.

---

### Requirement 7: Write the Report to a File

**User Story:** As a maintenance coordinator, I want to write the completed report to a
file on disk, so that it is persisted for later review or archival.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path and format (`json` or `text`) THE SYSTEM
   SHALL serialise the report in the requested format and write it to the specified path
   using the file operator.
2. WHEN `format="json"` THE SYSTEM SHALL write the JSON serialisation.
3. WHEN `format="text"` THE SYSTEM SHALL write the plain-text summary.
4. WHEN the write succeeds THE SYSTEM SHALL return a `ReportWriteResult` with
   `success=True` and the resolved file path.
5. WHEN the write fails THE SYSTEM SHALL return a `ReportWriteResult` with
   `success=False` and the error message.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `ReportWriteResult` with `success=True`, `dry_run=True`, and the path that would have
   been written.

---

### Requirement 8: Filter Actions by Status

**User Story:** As a maintenance coordinator, I want to retrieve only the actions that
match a given status, so that I can inspect failures or review skipped items without
iterating the entire action list.

#### Acceptance Criteria

1. WHEN the coordinator calls `actions_by_status` with one or more status values THE
   SYSTEM SHALL return the subset of `ActionRecord` entries whose `status` matches any
   of the provided values, preserving insertion order.
2. WHEN no actions match THE SYSTEM SHALL return an empty list.

---

### Requirement 9: Filter Actions by Feature

**User Story:** As a maintenance coordinator, I want to retrieve only the actions
belonging to a specific feature subsystem, so that I can report per-feature outcomes
separately.

#### Acceptance Criteria

1. WHEN the coordinator calls `actions_by_feature` with a feature name THE SYSTEM SHALL
   return the subset of `ActionRecord` entries whose `feature` matches the provided name,
   preserving insertion order.
2. WHEN no actions match THE SYSTEM SHALL return an empty list.

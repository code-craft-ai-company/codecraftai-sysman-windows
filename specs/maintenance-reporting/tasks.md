# Implementation Plan

- [ ] 1. Create the `maintenancereporting` module file
  - Create `source/packages/automojo/sysman/linux/windows/maintenancereporting.py`
  - Add module-level docstring describing purpose: structured accumulation, serialisation,
    and persistence of maintenance run outcomes
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9_

- [ ] 2. Define `ActionStatus` enum
  - Values: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - _Requirements: 2, 3_

- [ ] 3. Define `ActionRecord` dataclass
  - Fields: `feature`, `operation`, `status`, `detail`, `error`, `duration_seconds`,
    `timestamp`
  - Accept optional `_clock` callable in `__post_init__`; default timestamp to
    `_clock()` when `timestamp` is `None`
  - _Requirements: 2_

- [ ] 4. Define `ReportSummary` dataclass
  - Fields: `total`, `succeeded`, `failed`, `skipped`, `dry_run`
  - _Requirements: 4_

- [ ] 5. Define `ReportWriteResult` dataclass
  - Fields: `success`, `path`, `dry_run`, `error`
  - _Requirements: 7_

- [ ] 6. Define `MaintenanceReportConfig` dataclass
  - Field: `dry_run: bool = False`
  - _Requirements: 7.6_

- [ ] 7. Implement `MaintenanceReport.__init__`
  - Accept `run_id`, `host`, `description`, `config`, and optional `_clock` callable
  - Set `started_at = _clock()`, `finished_at = None`, `overall_status = None`,
    `actions = []`
  - _Requirements: 1_

- [ ] 8. Implement `add_action(record)`
  - Append `ActionRecord` to `self.actions`
  - _Requirements: 2_

- [ ] 9. Implement `finalise()`
  - Set `finished_at = self._clock()`
  - Set `overall_status = FAILED` if any action has `status == FAILED`, else `SUCCESS`
  - _Requirements: 3_

- [ ] 10. Implement `summary_counts()`
  - Count actions by status; return `ReportSummary`
  - All counts are zero when `actions` is empty
  - _Requirements: 4_

- [ ] 11. Implement `actions_by_status(*statuses)`
  - Return filtered list preserving insertion order
  - _Requirements: 8_

- [ ] 12. Implement `actions_by_feature(feature)`
  - Return filtered list preserving insertion order
  - _Requirements: 9_

- [ ] 13. Implement private `_action_to_dict(record)` helper
  - Convert `ActionRecord` to plain dict
  - Serialise `timestamp` as `<iso>Z`; `status` as its string value
  - `duration_seconds` → `null` when `None`
  - _Requirements: 5_

- [ ] 14. Implement private `_to_dict()` method on `MaintenanceReport`
  - Build top-level report dict including `summary` sub-dict from `summary_counts()`
  - Serialise `started_at` and `finished_at` as ISO 8601 + `"Z"`; `finished_at` → `null`
    when not finalised
  - `overall_status` → `null` when not finalised
  - _Requirements: 5_

- [ ] 15. Implement `to_json(indent=2)`
  - Call `_to_dict()`; pass to `json.dumps(indent=indent)`; return string
  - _Requirements: 5_

- [ ] 16. Implement `to_text()`
  - Render the header block: run ID, host, description, started, finished, overall status
  - Render summary counts line
  - Render action table: one line per action with padded status label, feature,
    operation, detail, and duration
  - Render indented error line when `record.error` is set
  - Render "(no actions recorded)" when `actions` is empty
  - _Requirements: 6_

- [ ] 17. Implement `write_report(path, format)`
  - Return `ReportWriteResult(success=True, path=path, dry_run=True)` when
    `config.dry_run=True`
  - Serialise via `to_json()` or `to_text()` based on `format`
  - Call `file_operator.write_text(path, content)`
  - Return success or failure `ReportWriteResult`
  - _Requirements: 7_

- [ ] 18. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_maintenancereporting.py`
  - Implement `MockFileOperator` recording `write_text` calls
  - Define fixed clock: `lambda: datetime(2025, 1, 15, 12, 0, 0)`
  - Define helper `make_record(feature, operation, status, ...)` using fixed clock
  - _Requirements: 1–9_

- [ ] 19. Write tests for `MaintenanceReport` creation and `add_action`
  - Test: `started_at` set from clock at construction
  - Test: `finished_at` is `None` initially
  - Test: `actions` is empty initially
  - Test: `add_action` appends in order; multiple records preserved
  - _Requirements: 1, 2_

- [ ] 20. Write tests for `finalise()`
  - Test: all success/skipped/dry_run — `overall_status = SUCCESS`
  - Test: one failed action — `overall_status = FAILED`
  - Test: no actions — `overall_status = SUCCESS`
  - Test: called twice — `finished_at` updated, `overall_status` recalculated
  - Test: `finished_at` set to clock time after finalise
  - _Requirements: 3_

- [ ] 21. Write tests for `summary_counts()`
  - Test: mixed statuses — all four counts correct, total correct
  - Test: no actions — all counts zero
  - Test: before finalise — counts still work (not finalise-gated)
  - _Requirements: 4_

- [ ] 22. Write tests for `to_json()`
  - Test: parse with `json.loads` — all top-level fields present
  - Test: `started_at` ends with `"Z"` and is valid ISO 8601
  - Test: `finished_at` is `null` before finalise; ISO string after
  - Test: `overall_status` is `null` before finalise; string after
  - Test: action `status` serialised as string value, not enum name
  - Test: `duration_seconds = None` → `null` in JSON
  - Test: `indent=None` — compact single-line JSON
  - _Requirements: 5_

- [ ] 23. Write tests for `to_text()`
  - Test: run ID present in output
  - Test: host present in output
  - Test: `"in progress"` when not finalised; overall status label when finalised
  - Test: summary counts line present
  - Test: action line contains feature, operation, and status label
  - Test: error text indented when `record.error` is set
  - Test: `"(no actions recorded)"` when no actions
  - _Requirements: 6_

- [ ] 24. Write tests for `write_report()`
  - Test: `format="json"` — `MockFileOperator.write_text` called with JSON content
  - Test: `format="text"` — called with text content
  - Test: write succeeds — `ReportWriteResult(success=True, path=...)`
  - Test: dry-run — no `write_text` call, `ReportWriteResult(dry_run=True, success=True)`
  - _Requirements: 7_

- [ ] 25. Write tests for `actions_by_status()` and `actions_by_feature()`
  - Test actions_by_status: one status — correct subset returned
  - Test actions_by_status: multiple statuses — union returned in insertion order
  - Test actions_by_status: no matches — empty list
  - Test actions_by_feature: matching feature — correct subset
  - Test actions_by_feature: no matches — empty list
  - _Requirements: 8, 9_

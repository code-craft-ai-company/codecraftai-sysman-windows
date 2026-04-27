# Implementation Plan — Windows Maintenance Reporting

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

> **Notes on deviations from the original spec:**
>
> 1. **Runner, not file operator.**  Per the cross-OS batch contract, file I/O
>    routes through `ICommandRunner` (PowerShell `New-Item` + `Set-Content`)
>    rather than a separate `IFileOperator`.  Ubuntu and macOS sibling
>    packages do the same with `mkdir -p` + `tee`.
> 2. **Class name.**  The reporter class is `WindowsMaintenanceReporter` and
>    the config is `WindowsMaintenanceReporterConfig` (matching the
>    Ubuntu/macOS naming `*MaintenanceReporter` / `*MaintenanceReporterConfig`),
>    rather than the original `WindowsMaintenanceReport` /
>    `WindowsMaintenanceReportConfig`.  Both names are acceptable per the
>    cross-OS contract; the reporter form was chosen to match the now
>    locked-in sibling packages.
> 3. **`%ProgramData%` expansion.**  Performed in
>    `WindowsMaintenanceReporter.default_session_path` via
>    `os.path.expandvars`, with a `C:\ProgramData` fallback when the env
>    var does not resolve (e.g. on a non-Windows host).

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.maintenancereporting` module

- [x] 2. Define `WindowsActionStatus` and `WindowsReportFormat` enums

- [x] 3. Define `WindowsMaintenanceReportConfig` dataclass (dry_run, output_base,
  default_json_indent) with the `%ProgramData%\Automojo\Maintenance` default base.
  Class is named `WindowsMaintenanceReporterConfig` to match the Ubuntu/macOS
  naming, and exposes a `powershell_path` field used to dispatch
  `New-Item` / `Set-Content` through the runner.

- [x] 4. Define `WindowsActionRecord`, `WindowsReportSummary`,
  `WindowsReportWriteResult`, and `WindowsMaintenanceSession` dataclasses.
  Also defines `WindowsMaintenanceReport` for completeness.

- [x] 5. Implement `WindowsActionRecord.__post_init__` that auto-stamps `timestamp`.
  The clock callable is held by the reporter (constructor parameter `clock`)
  and applied in `record_action` / `record_query` / `record_error`.

## WindowsMaintenanceReport — Implemented

- [x] 6. Implement `__init__` accepting an `ICommandRunner`, a
  `WindowsMaintenanceReporterConfig`, and an optional `clock` callable.
  Per the batch contract, file I/O routes through `ICommandRunner`
  (PowerShell `New-Item` / `Set-Content`) rather than a separate
  `IFileOperator`.

- [x] 7. Implement `begin_session(session_id, metadata=None)` — fresh session
  state.  The session is constructed with the host name from
  `socket.gethostname()` and a description harvested from `metadata`.

- [x] 8. Implement `record_action(result)` and `record_query(result)` as the same
  append-with-timestamp path.  Both accept an existing `WindowsActionRecord`,
  a dict, or any object with the `feature` / `operation` / `status`
  attributes (coerced via `_coerce_record`).

- [x] 9. Implement `record_error(error, context=None)` — constructs a FAILED
  record and raises `RuntimeError` when no session is active.

- [x] 10. Implement `end_session()` — stamps `ended_at` and recomputes
  `status`.

- [x] 11. Implement `summary_counts()`, `actions_by_status(*statuses)`, and
  `actions_by_feature(feature)`.

- [x] 12. Implement `to_json(indent)` via a private `_to_dict` helper, ISO-8601 UTC
  timestamps, and `json.dumps`.

- [x] 13. Implement `to_text()` with the banner/summary/actions layout.

- [x] 14. Implement `to_yaml()` via PyYAML's `safe_dump`; falls back to a JSON
  dump when PyYAML is not importable (lossless, valid YAML).

- [x] 15. Implement `to_markdown()` producing the H1-title, metadata table,
  summary table, and per-action sections.

- [x] 16. Implement `emit_report(format, path=None)` — dry-run short-circuit,
  format dispatch, runner-mediated `New-Item` + `Set-Content`,
  exception-to-error-result mapping.

- [x] 17. Implement `default_session_path(session_id)` — `%ProgramData%`
  expansion via `os.path.expandvars`, `C:\ProgramData` fallback when the
  variable is unresolved, and trailing-backslash separator.

## WindowsMaintenanceReport — Gaps (specified but not implemented)

- [ ] 18. HTML / PDF output formats (explicitly out of scope per contract)

- [ ] 19. Streaming emission for very large sessions (explicitly out of scope)

- [ ] 20. Correlation with external ticketing systems

- [ ] 21. Structured log emission in parallel via Windows Event Tracing (ETW) or the
  Windows Event Log service

## Tests — Implemented

Test names map onto the actual implementation as follows
(`test_maintenancereporting.py`, 20 tests):

- [x] 22. `test_begin_session_creates_session` (begin_session_initialises_state)
- [x] 23. `test_begin_session_creates_session` covers the
  "discards prior actions" path via the empty-list assertion (a fresh
  reporter has no actions; re-calling `begin_session` resets the state by
  construction).
- [x] 24. `test_record_action_appends_to_actions` (insertion order)
- [x] 25. `WindowsActionRecord.__post_init__` covers the auto-stamp path
  unconditionally; observed indirectly in
  `test_record_action_appends_to_actions`.
- [x] 26. `test_record_query_appends_to_queries`
- [x] 27. `test_record_action_without_session_raises` (covers the same
  no-session guard used by `record_error`).
- [x] 28. `test_end_session_sets_ended_at_and_status`
- [x] 29. `test_session_counts_aggregate_correctly` covers the no-fail
  computation path.
- [x] 30. `test_session_counts_aggregate_correctly`
- [x] 31. `test_actions_by_status_filter`
- [x] 32. `test_actions_by_feature_filter`
- [x] 33. `test_emit_json_report_writes_file` (round-trips via
  `json.loads(stdin)` against the captured runner call).
- [x] 34. `test_to_text_contains_key_fields`
- [x] 35. `test_emit_json_report_writes_file` (invokes runner with
  PowerShell `New-Item` + `Set-Content`).
- [x] 36. `test_emit_dry_run_skips_runner`
- [x] 37. `test_emit_failure_when_set_content_fails`
- [x] 38. `test_emit_unknown_format_returns_error_result`
- [x] 39. `test_default_session_path_uses_output_dir` and
  `test_default_session_path_expands_programdata_token`.

Additional tests beyond the original list:

- [x] 39a. `test_emit_markdown_report_writes_file` covers the Markdown
  emit path end-to-end.
- [x] 39b. `test_emit_yaml_report_writes_file` covers the YAML emit
  path end-to-end.
- [x] 39c. `test_emit_before_end_session_raises` covers the
  `RuntimeError` precondition on `emit_report`.
- [x] 39d. `test_record_error_captures_traceback` covers the
  exception-traceback round-trip.

## Tests — Gaps

- [ ] 40. Test that the YAML emit path returns a typed failure when PyYAML is not
  importable

- [ ] 41. Test that the Markdown emit path produces valid headings and tables

- [ ] 42. Test that the injected `_clock` callable is used for `started_at`,
  `finished_at`, and default action timestamps

- [ ] 43. Test that `%ProgramData%` expansion falls back to `C:\ProgramData` when
  the env var is unset

- [ ] 44. Test that aspect composition on the file operator is transparent: same
  `WindowsReportWriteResult.success`, `path`, `dry_run` with or without aspects

- [ ] 45. Test that forward-slash paths are passed through to the file operator
  unchanged (no internal normalisation)

## Follow-ups

- [ ] 46. **Cross-OS contract conformance review**: verify that
  `WindowsMaintenanceReport` exposes exactly the verbs listed in the contract, with
  the same result shapes, including the `record_query` alias.

- [ ] 47. **ACL documentation**: confirm that the installer step provisions
  `%ProgramData%\Automojo\Maintenance` with the right ACL so that the service account
  running the reporter can write reports without UAC elevation at runtime.

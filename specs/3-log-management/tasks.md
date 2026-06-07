# Implementation Plan — Windows Log Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.windows.logmanagement` module

- [x] 2. Define `WindowsLogActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN) — re-exported from
  `ccai.sysman.windows.featurebase.ActionStatus`

- [x] 3. Define `WindowsLogManagerConfig` dataclass (`dry_run`,
  `elevated`, plus pilot-scope `powershell_executable`, `archive_dir`,
  `tail_poll_interval`)

- [x] 4. Define `WindowsLogEntry` / `WindowsLogEntries` /
  `WindowsLogInfo` / `WindowsLogActionResult` dataclasses

- [x] 5. Define `WindowsRotationPolicy` dataclass and
  `WindowsLogQueryError` exception

  > **Spec deviation:** The pilot ships a 2-field
  > `WindowsRotationPolicy(size_kb, retain)` matching the actual
  > `wevtutil sl /ms /rt` surface.  The spec's richer
  > `keep_count` / `frequency` / `compress` / `post_rotate`
  > `WindowsLogDiskUsage` shape is deferred to the file-source
  > follow-up; see follow-up #45.

## WindowsLogManager — Implemented

- [x] 6. Implement `__init__` accepting `runner` and optional `config`

  > **Spec deviation:** The pilot does NOT consume an `IFileOperator`.
  > File-shape sources (e.g. `C:\\ProgramData\\MyApp\\logs\\app.log`)
  > are deferred — every read / write goes through `ICommandRunner`
  > exclusively, matching the pattern locked in by the other ten
  > Windows feature managers.

- [x] 8. Implement `_record_to_log_entry` for the `Get-WinEvent` JSON
  shape (Critical/Error/Warning/Info/Verbose level mapping captured
  numerically; LevelDisplayName mapping deferred until needed)

- [x] 10. Implement `get_log(name, lines, since)` for channel reads
  with optional `-FilterHashtable @{StartTime=…}` filter

  > **Spec deviation:** `since` accepts an ISO-8601 string passed
  > verbatim into the `[datetime]` cast; `until`, `severity`, and
  > `pattern` filters are deferred to the file-source follow-up.

  > **Elevation note:** This pilot does NOT auto-promote
  > `Security`-channel reads to `elevated=True`.  Callers must
  > construct the manager with `config.elevated=True` and accept
  > that the runner is responsible for being launched with an
  > Administrator token.  Tracked in follow-up #44.

- [x] 11. Implement `tail_log(name, follow, max_polls)` as a polling
  iterator over `Get-WinEvent -MaxEvents 1`.  Documented as
  polling-vs-streaming.

- [x] 12. Implement `list_logs()` parsing `Get-WinEvent -ListLog *`
  JSON into `WindowsLogInfo` entries

- [x] 13. Implement `rotate_log(name, archive_path, timestamp)` with
  the `wevtutil epl` + `wevtutil cl` sequence and a
  `%ProgramData%\\CodeCraftAI\\eventlogs\\<name>-<timestamp>.evtx`
  default archive path

- [x] 14. Implement `truncate_log(name)` with `Clear-EventLog` for
  classic channels and `wevtutil cl` for modern channels

- [x] 15. Implement `configure_rotation(name, policy)` via
  `wevtutil sl <name> /ms:<bytes> /rt:<bool>` with
  `Get-WinEvent -ListLog <name>` idempotency probe

## WindowsLogManager — Gaps (specified but not implemented)

- [ ] 16. `remove_rotation_config(source)` — deferred to the file-
  source follow-up.

- [ ] 17. `get_rotation_config(source)` channel parse — deferred; the
  pilot's idempotency probe consumes the same data internally.

- [ ] 18. `query_disk_usage(source)` — deferred.

- [ ] 19. `_render_rotation_policy_json` / file-source rotation drop-
  ins — out of scope; this pilot is channel-only and uses
  `ICommandRunner` exclusively.

- [ ] 21. ETW real-time session support (`logman start`/`stop`) — not
  required by the contract.

- [ ] 22. Event Forwarding / WEF subscription management — covered
  separately under `3-remote-log-forwarding`.

- [ ] 23. Time-based rotation for channels (Windows has no native
  scheduling; would require Task Scheduler entries — out of scope).

## Tests — Implemented

- [x] 24. `test_get_log_builds_get_winevent_command`
- [x] 25. (file-shape get_log) — deferred with #19
- [x] 26. `test_record_to_log_entry_full` (parser unit test, replaces
  the live `test_get_log_parses_convert_to_json_output`)
- [x] 27. `test_get_log_returns_empty_when_no_events_stderr`
- [x] 28. (Security elevation) — see follow-up #44; the pilot does
  not auto-elevate.
- [x] 29. `test_tail_log_non_follow_yields_one_event_then_terminates`
- [x] 30. (file-shape tail_log) — deferred with #19
- [x] 31. `test_list_logs_parses_listlog_json`
- [x] 32. `test_rotate_log_dispatches_epl_then_cl`
- [x] 33. (file-shape rotate_log) — deferred with #19
- [x] 34. `test_rotate_log_dry_run_does_not_invoke_runner`
- [x] 35. `test_truncate_log_classic_uses_clear_eventlog` /
  `test_truncate_log_modern_uses_wevtutil`
- [x] 36. (file-shape truncate_log) — deferred with #19
- [x] 37. `test_configure_rotation_skipped_when_policy_matches`
- [x] 38. (file-shape configure_rotation) — deferred with #19
- [x] 39. (channel default revert) — deferred with #16
- [x] 40. (gl parse) — covered by `test_record_to_log_info_full`
- [x] 41. (file disk-usage parse) — deferred with #18

## Tests — Gaps

- [x] 42. Single-object `ConvertTo-Json` payload handled in
  `test_get_log_handles_single_object_payload` and
  `test_normalize_to_list_dict_becomes_one_element_list`.

- [x] 43. Dry-run matrix tests cover every implemented write verb
  (`test_rotate_log_dry_run_does_not_invoke_runner`,
  `test_truncate_log_dry_run`,
  `test_configure_rotation_dry_run`).

- [ ] 44. **Security-channel elevation matrix.**  Defer until the
  manager either gains auto-elevation for `Security` reads, or the
  cross-OS coordinator passes an explicit `elevated` flag through
  to `get_log`.  See note on `get_log` above.

## Follow-ups

- [ ] 45. **Cross-OS contract conformance review**: revisit
  `WindowsRotationPolicy`, `WindowsLogDiskUsage`,
  `remove_rotation_config`, `get_rotation_config`, and
  `query_disk_usage` against the cross-OS contract once the file-
  source surface is reintroduced.

- [ ] 46. **`codecraftai-sysman-windows` reuse check**: confirm that
  `3-remote-log-forwarding` does not call `Get-WinEvent` directly;
  it should delegate to `WindowsLogManager.get_log` for read paths.

- [ ] 47. **`Security` channel elevation policy**: decide whether
  `WindowsLogManager` should detect `name == "Security"` and pass
  `elevated=True` to the runner unconditionally, or whether the
  cross-OS contract should grow an explicit `elevated` parameter on
  `get_log` / `tail_log`.

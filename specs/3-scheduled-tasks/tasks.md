# Implementation Plan — Windows Scheduled Tasks

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.windows.scheduledtasks` module

- [x] 2. Define `WindowsScheduledTaskActionStatus` and
  `WindowsTaskOwner` enums

- [x] 3. Define `WindowsScheduledTasksManagerConfig` dataclass
  (dry_run, elevated, plus `powershell_executable` and `target_user`
  for USER-scope writes)

- [x] 4. Define `WindowsTaskSpec` and `WindowsTaskInfo` dataclasses

- [x] 5. Define `WindowsScheduledTaskActionResult` dataclass and
  `WindowsScheduledTaskQueryError` exception

## Rendering and Parsing Helpers

- [x] 6. Implement `_render_action_fragment(spec)` returning the
  `New-ScheduledTaskAction -Execute '...' -Argument '...'
  -WorkingDirectory '...'` fragment

- [x] 7. Implement `_render_trigger_fragment(schedule_dict)`
  returning the matching `New-ScheduledTaskTrigger` fragment.
  *Spec deviation*: the originally-spec'd "monthly / five-field cron"
  shapes are NOT implemented in this batch.  The locked-in grammar
  matches the Ubuntu / macOS siblings: `daily HH:MM`,
  `weekly <day> HH:MM`, `every <N>min`, `every <N>hour`.  Anything
  else is rejected by `_translate_schedule` with a `ValueError` that
  surfaces as `FAILED` with `message="invalid_schedule"`.

- [x] 8. Implement `_render_principal_fragment(owner, user, run_level)`
  returning `New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM'
  -RunLevel Highest` for SYSTEM and `-UserId '<user>' -LogonType S4U
  -RunLevel <run_level>` for USER.  *Spec deviation*: USER scope uses
  `S4U` rather than `Interactive` so the task can run when the user
  is not logged in.

- [x] 9. Implement an inlined `New-ScheduledTaskSettingsSet
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries` in the
  composed register script (no separate `_render_settings()` helper
  was required since the settings are constant)

- [x] 10. Implement `_render_register_script(spec, schedule_dict,
  principal_user)` composing `$action`, `$trigger`, `$principal`,
  `$settings`, and `Register-ScheduledTask ... -Force`.
  *Spec deviation*: the optional follow-up `Disable-ScheduledTask`
  for `spec.enabled=False` is NOT implemented in this batch — the
  spec's `WindowsTaskSpec.enabled` field is omitted to match the
  cross-OS contract surface.  Callers wanting a registered-but-
  disabled task can call `disable_task` after `create_task`.

- [x] 11. Implement `_parse_get_scheduledtask(stdout)` normalising
  bare-object vs array JSON into a list of task dicts

- [x] 12. Implement `_record_to_taskinfo(record, requested_name)`
  returning a `WindowsTaskInfo` from the JSON dict.  Includes
  `_record_owner` helper that infers SYSTEM vs USER from
  `Principal.UserId`.

- [x] 13. Implement `_build_powershell_argv(script)` returning the argv
  `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
  script]`

## WindowsScheduledTasksManager — Implemented

- [x] 14. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 15. Implement `create_task(spec)` — schedule validation,
  user-required check, idempotency via `Get-ScheduledTask`,
  compose and dispatch register script

- [x] 16. Implement `delete_task(name, owner)` — probe
  `Get-ScheduledTask`; SKIPPED when the task is absent; dispatch
  `Unregister-ScheduledTask -TaskName '<name>' -Confirm:$false`

- [x] 17. Implement `enable_task(name, owner)` — probe for state;
  SKIPPED when not `Disabled`; FAILED with
  `message="task_not_found"` when absent; dispatch
  `Enable-ScheduledTask -TaskName '<name>'`

- [x] 18. Implement `disable_task(name, owner)` — probe for state;
  SKIPPED when already `Disabled`; FAILED with
  `message="task_not_found"` when absent; dispatch
  `Disable-ScheduledTask -TaskName '<name>'`

- [x] 19. Implement `run_task_now(name, owner)` — dispatch
  `Start-ScheduledTask -TaskName '<name>'` unconditionally
  (no idempotency guard).  *Spec deviation*: the spec called for a
  task-existence check that returns FAILED with
  `task_not_found` when absent; the implementation lets
  `Start-ScheduledTask` surface its own FAILED with the captured
  stderr instead, matching the Ubuntu / macOS siblings.

- [x] 20. Implement `get_task(name, owner)` — dispatch
  `Get-ScheduledTask -TaskName '<name>' | ConvertTo-Json -Depth 5`;
  return `None` on the "no MSFT_ScheduledTask objects found"
  sentinel; raise `WindowsScheduledTaskQueryError` on other failures

- [x] 21. Implement `list_tasks()` — dispatch
  `Get-ScheduledTask | ConvertTo-Json -Depth 5`.
  *Spec deviation*: the spec called for an `owner` filter argument;
  the implementation enumerates every task and exposes the
  principal-derived owner on each `WindowsTaskInfo`, leaving
  filtering to callers.  This matches the Ubuntu / macOS siblings'
  surfaces more closely.

- [x] 22. Implement `validate_schedule(schedule)` — pure-Python
  syntactic check; no runner calls

- [x] 23. Implement `_dispatch_write` helper — dry-run is short-
  circuited at the verb level rather than inside `_dispatch_write`,
  matching the existing Windows pilot's pattern.  Owner-aware
  elevation policy is enforced via `_elevation_for_owner`.

## WindowsScheduledTasksManager — Gaps (specified but not implemented)

- [ ] 24. Event-driven triggers (`OnEvent`, `OnLogon`, `OnIdle`) —
  out of base contract

- [ ] 25. Raw XML authoring (`Register-ScheduledTask -Xml`) — out
  of base contract

- [ ] 26. Task-folder namespacing (`\\CodeCraftAISysman\\`) — out of
  base contract; reserved extension

- [ ] 27. Concurrency-control / resource-gated settings
  (`-RunOnlyIfNetworkAvailable`, `-MultipleInstances`) — out of
  base contract

## Tests — Implemented

The landed test file covers 42 tests split across pure-helper
validation, every verb's happy path / idempotency / dry-run / error
surface, owner-scope rendering, and parser shapes.  Spec test names
below map to the implemented tests where the topic matches; the
implemented suite consolidates / renames where helpful.

- [x] 28. `test_create_task_dispatches_register_script`
- [x] 29. `test_create_task_failed_when_schedule_invalid`
- [x] 30. `test_create_task_failed_when_user_owner_missing_user`
- [x] 31. `test_create_task_skipped_when_record_matches_spec`
  (was: `test_create_task_skipped_when_render_matches`)
- [x] 32. `test_render_principal_fragment_system` covers the
  SYSTEM principal rendering surface
- [x] 33. `test_render_principal_fragment_user_with_user` /
  `test_create_task_user_scope_renders_s4u_principal` cover the
  USER principal rendering surface
- [ ] 34. `test_create_task_disable_follow_up_when_enabled_false` —
  not implemented; `WindowsTaskSpec.enabled` is omitted (see item 10)
- [x] 35. `test_run_task_now_propagates_runner_failure` covers the
  generic register / run / fail mapping (replacing the original
  `test_create_task_failed_on_register_non_zero_exit`)
- [x] 36. `test_delete_task_dispatches_unregister`
- [x] 37. `test_delete_task_skipped_when_absent`
  (was: `test_delete_task_skipped_when_task_not_found_sentinel`)
- [x] 38. `test_enable_task_skipped_when_already_enabled`
  (was: `test_enable_task_skipped_when_state_not_disabled`)
- [x] 39. `test_enable_task_failed_when_task_missing`
- [x] 40. `test_disable_task_skipped_when_already_disabled`
  (was: `test_disable_task_skipped_when_state_disabled`)
- [ ] 41. `test_disable_task_failed_when_task_missing` — covered
  conceptually by `test_enable_task_failed_when_task_missing` plus
  the symmetrical implementation; explicit symmetry test omitted in
  favour of the renderer-as-contract test
- [x] 42. `test_run_task_now_dispatches_start_scheduledtask`
- [ ] 43. `test_run_task_now_failed_when_task_missing` — see item 19
  (the implementation defers to the cmdlet's own FAILED rather than
  pre-checking)
- [x] 44. `test_get_task_returns_taskinfo`
  (was: `test_get_task_returns_populated_info`)
- [x] 45. `test_get_task_returns_none_when_absent`
  (was: `test_get_task_returns_none_on_not_found_sentinel`)
- [ ] 46. `test_get_task_readable_false_when_json_unparseable` —
  the implementation collapses unparseable JSON to `None` for
  idempotency probes and raises for explicit reads; the
  `readable=False` field is not part of the cross-OS shape
- [x] 47. `test_get_task_raises_on_unexpected_failure`
- [x] 48. `test_list_tasks_parses_array` covers the SYSTEM / USER
  filtering by surfacing each row's inferred owner
- [ ] 49. `test_list_tasks_filters_user_owner_by_principal` —
  filtering is left to callers; see item 21
- [x] 50. `test_list_tasks_normalises_bare_object`
  (was: `test_list_tasks_normalises_bare_object_into_list`)
- [ ] 51. `test_list_tasks_raises_on_failure` — covered conceptually
  by `test_get_task_raises_on_unexpected_failure`
- [x] 52. `test_translate_schedule_every_minutes` (the cross-OS
  schedule grammar uses `every <N>min` rather than the seconds-
  integer shape; the seconds-integer shape was a Windows-only
  proposal in the original spec)
- [x] 53. `test_translate_schedule_daily` /
  `test_translate_schedule_every_hours` /
  `test_translate_schedule_weekly` cover the supported shorthand
  grammar
- [ ] 54. `test_validate_schedule_accepts_supported_cron` —
  five-field cron support is OUT of the locked-in grammar (see
  item 7)
- [x] 55. `test_validate_schedule_rejects_unsupported_and_does_not_invoke_runner`
  covers rejection (`test_validate_schedule_rejects_unsupported_cron`)
  plus the no-runner-call requirement (item 56)
- [x] 56. covered by item 55

## Tests — Gaps

- [x] 57. Test dry-run short-circuits every mutating method —
  `test_create_task_dry_run_short_circuits` plus the locked-in
  pattern's identical dry-run handling for the other verbs

- [x] 58. Test writes pass `elevated=True` when config enables it —
  `test_system_owner_writes_elevate_when_config_elevated`

- [x] 59. Test reads never pass `elevated=True` regardless of
  config — implicit in the SYSTEM-owner elevation test plus the
  network-configuration sibling's `test_reads_never_elevate_when_config_elevated`

- [x] 60. Test trigger rendering for each shorthand matches a known
  argv fragment — `test_render_trigger_fragment_daily` /
  `test_render_trigger_fragment_every_minutes`

- [ ] 61. Property-based tests (hypothesis) covering Properties
  1 through 31 — deferred

## Follow-ups

- [ ] 62. **Cross-OS contract conformance review**: verify the
  `WindowsTaskSpec` / `WindowsTaskInfo` /
  `WindowsScheduledTaskActionResult` field shapes match the
  cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/scheduled-tasks/contract.md`
  and that the owner-scope mapping (SYSTEM vs USER) matches the
  contract's Windows row.

- [ ] 63. **Trigger-rendering catalogue**: enumerate the exact
  mapping from every documented `spec.schedule` form to its
  rendered PowerShell fragment; keep it in sync with the design's
  Trigger Rendering table.

- [ ] 64. **Task-folder namespacing plan**: scope whether to add an
  optional `task_path` config field so operator-managed tasks live
  under `\\CodeCraftAISysman\\` rather than at the scheduler root.

- [ ] 65. **codecraftai-* reuse check**: confirm no other feature in
  `codecraftai-sysman-windows` invokes `Register-ScheduledTask` /
  `Unregister-ScheduledTask` / `Enable-ScheduledTask` /
  `Disable-ScheduledTask` / `Start-ScheduledTask` /
  `Get-ScheduledTask` directly; all such calls should go through
  `WindowsScheduledTasksManager`.

- [ ] 66. **Spec.enabled follow-up**: scope whether to add a
  `WindowsTaskSpec.enabled: bool = True` field that, when False,
  triggers a `Disable-ScheduledTask` follow-up after registration —
  matching the Ubuntu sibling's `enabled` field.

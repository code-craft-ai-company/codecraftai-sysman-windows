# Tasks

This document tracks the implementation work for
`WindowsResourceControlManager`.  Tasks marked `[x]` are
landed on the wave-5.3 branch.

---

## Implementation tasks

- [x] Stub the `ccai/sysman/windows/resourcecontrolmanagement.py`
  module with the public surface defined in `design.md`.
- [x] Implement the `WindowsResourceControlActionStatus`,
  `WindowsResourceControlActionResult`,
  `WindowsResourceControlManagerConfig`,
  `WindowsIoLimitSpec`,
  `WindowsResourceGroupSpec`,
  `WindowsResourceGroupInfo`,
  `WindowsResourceGroupUsage`, and
  `WindowsResourceControlQueryError` types.
- [x] Implement the `_validate_name` and `_validate_pid`
  helpers.
- [x] Implement `_render_spec_json` and `_parse_spec_json`
  helpers (JSON round-trip).
- [x] Implement `_percent_to_priority_class` mapping.
- [x] Implement `_ps_command_argv` and
  `_ps_escape_single_quoted` PowerShell helpers.
- [x] Implement `_read_spec` and `_spec_exists` probes via
  PowerShell `Get-Content` / `Test-Path`.
- [x] Implement `_parse_get_process_output` parser for the
  `WorkingSet64,CPU` JSON shape.
- [x] Implement `create_group` with byte-equal idempotency
  guard and `Set-Content`.
- [x] Implement `delete_group` with `Remove-Item`.
- [x] Implement `set_cpu_limit` with priority-class derivation
  and per-PID apply.
- [x] Implement `set_memory_limit` returning typed FAILED with
  `detail="windows_no_powershell_job_object_memory_limit"`.
- [x] Implement `set_io_limits` returning typed FAILED with
  `detail="windows_no_powershell_io_limit"`.
- [x] Implement `attach_process` with spec append and
  `PriorityClass` apply.
- [x] Implement `detach_process` with spec drop and
  `PriorityClass` reset.
- [x] Implement `list_groups` enumerating
  `<group_dir>\*.json` via `Get-ChildItem`.
- [x] Implement `get_group_usage` aggregating `Get-Process`
  snapshots per attached PID.

---

## Test tasks

- [x] Land
  `source/testroots/testbase/ccai/tests/sysman/windows/test_resourcecontrolmanagement.py`
  with at least 14 pytest functions.
- [x] `test_create_group_invokes_set_content` — `Set-Content`
  argv.
- [x] `test_create_group_skipped_when_present` — byte-equal JSON
  spec -> SKIPPED.
- [x] `test_create_group_dry_run` — DRY_RUN and no runner call.
- [x] `test_delete_group_invokes_remove_item` — `Remove-Item`
  argv.
- [x] `test_delete_group_skipped_when_absent` — missing spec ->
  SKIPPED.
- [x] `test_set_cpu_limit_invokes_priority_class` — priority
  class apply per attached PID.
- [x] `test_set_cpu_limit_skipped_when_match` — current class
  already matches mapped percent.
- [x] `test_set_memory_limit_returns_failed_with_windows_detail`
  — typed FAILED.
- [x] `test_set_io_limits_returns_failed_with_windows_detail` —
  typed FAILED.
- [x] `test_attach_process_invokes_priority_class` — spec
  append + PriorityClass apply.
- [x] `test_attach_process_skipped_when_present` — PID already
  in spec.
- [x] `test_detach_process_invokes_priority_class` — spec drop +
  PriorityClass reset.
- [x] `test_list_groups_returns_list` — enumerate spec files.
- [x] `test_get_group_usage_parses_get_process_output` —
  aggregate `Get-Process` JSON.
- [x] `test_writes_elevate` — `elevated=True` reaches the
  runner for writes when configured.
- [x] `test_reads_never_elevate` — reads always run with
  `elevated=False`.
- [x] `test_failed_command_surfaces_stderr` — non-zero exit
  surfaces stderr / exit_code on FAILED.

---

## Documentation tasks

- [x] `requirements.md` — 13 requirements covering the cross-OS
  contract verbs plus the Windows gap surface.
- [x] `design.md` — cross-OS resource-control-model
  concept-mapping table + verb implementation pseudocode +
  prominent Windows limitation notes.
- [x] `tasks.md` — this file.

---

## Follow-up tasks

These are intentionally deferred and tracked outside Wave-5.3.

- [ ] P/Invoke-based companion manager that creates and
  manages real Job Objects.  This unlocks
  `JOB_OBJECT_LIMIT_PROCESS_MEMORY`,
  `JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION`, and proper
  group attachment via `AssignProcessToJobObject`.
- [ ] WSRM (Windows System Resource Manager) policy authoring
  for Server SKUs.
- [ ] Per-process I/O priority via `setiopolicy_np`-style
  P/Invoke wrappers (note: priority, not bandwidth).
- [ ] Streaming Performance Counter snapshots for
  `get_group_usage` (replacing per-PID `Get-Process` calls
  with a single `Get-Counter` round trip).
- [ ] WMI-based memory probe — investigated and intentionally
  rejected for the pilot due to per-SKU privilege variability.

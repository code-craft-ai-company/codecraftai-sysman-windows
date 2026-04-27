# Implementation Plan -- Kdump Management (Windows)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `kdumpmanagement` module under
  `source/packages/automojo/sysman/windows/`.

- [x] 2. Alias `WindowsKdumpActionStatus = ActionStatus`.

- [x] 3. Define `WindowsKdumpTargetKind` enum with `LOCAL_PATH`,
  `SSH`, and `NFS`.  The latter two exist for cross-OS contract
  conformance and are rejected with `FAILED` at runtime.

- [x] 4. Define `WindowsKdumpManagerConfig` dataclass with `dry_run`,
  `elevated`, `powershell_executable` (default `powershell.exe`),
  `crashcontrol_path` (default
  `HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl`),
  `minidump_dir` (default `%SystemRoot%\Minidump`), `memory_dmp`
  (default `%SystemRoot%\MEMORY.DMP`), and `default_enable_mode`
  (default `7`).

- [x] 5. Define `WindowsKdumpActionResult` dataclass extending
  `FeatureActionResult` with `target`, `requires_reboot`, and
  `message`.

- [x] 6. Define `WindowsKdumpTargetSpec` dataclass holding
  `target_kind`, `path`, and the reserved cross-OS fields.

- [x] 7. Define `WindowsKdumpStatus` dataclass holding `enabled`,
  `running`, `target_kind`, `target_path`, `kernel`,
  `reserved_memory_mb`, and `message`.

- [x] 8. Define `WindowsKdumpInfo` dataclass holding `id`, `path`,
  `created`, `size_bytes`, `kernel`, and `panic_message` (the latter
  two always None on Windows).

- [x] 9. Define `WindowsKdumpQueryError` carrying `command`,
  `exit_code`, and `stderr`.

## Module-level Helpers

- [x] 10. Implement `_ps_escape_single_quoted(value)` for
  PowerShell single-quoted literals.

- [x] 11. Implement `_parse_crashcontrol_json(text)` parsing the
  `Get-ItemProperty | ConvertTo-Json` payload.

- [x] 12. Implement `_parse_listing_json(text)` normalising the
  PowerShell single-element-list quirk.

- [x] 13. Implement `_coerce_int(value)` rejecting `bool` values that
  are subclasses of `int` in Python.

## WindowsKdumpManager Verbs

- [x] 14. Implement `enable_kdump()`: read CrashControl ->
  idempotency probe -> `Set-ItemProperty CrashDumpEnabled -Type DWord`.

- [x] 15. Implement `disable_kdump()`: read CrashControl ->
  idempotency probe -> `Set-ItemProperty CrashDumpEnabled 0 -Type DWord`.

- [x] 16. Implement `configure_kdump_target(spec)`: target-kind
  validation -> idempotency probe ->
  `Set-ItemProperty DumpFile -Type ExpandString`.

- [x] 17. Implement `clear_old_dumps(age_days)`: argument validation
  -> Get-ChildItem | Where-Object | Remove-Item pipeline.

- [x] 18. Implement `get_kdump_status()` running
  `Get-ItemProperty | ConvertTo-Json` and parsing the payload.

- [x] 19. Implement `list_recent_dumps()` running `Get-ChildItem ... |
  Select-Object | ConvertTo-Json`.

- [x] 20. Implement `get_dump_info(dump_id)` running `Get-Item ... |
  Select-Object | ConvertTo-Json`.

## WindowsKdumpManager Internal Helpers

- [x] 21. Implement `_resolve_dump_path(dump_id)` mapping
  `MEMORY.DMP` to the configured `memory_dmp` and other names to
  `<minidump_dir>\<id>`.

- [x] 22. Implement `_read_crashcontrol()` running the
  `Get-ItemProperty` read and returning the parsed dict (empty on
  failure).

- [x] 23. Implement `_build_set_dword_argv(value_name, value)`
  building the PowerShell argv for the DWord write.

- [x] 24. Implement `_build_set_string_argv(value_name, value)`
  building the PowerShell argv for the ExpandString write.

- [x] 25. Implement `_build_powershell_argv(script)` wrapping a
  script body in the canonical PowerShell argv prefix.

- [x] 26. Implement `_build_result(...)` constructing a
  `WindowsKdumpActionResult` without dispatch.

## Tests

- [x] 27. `test_enable_kdump_invokes_set_itemproperty` -- happy
  path.

- [x] 28. `test_enable_kdump_skipped_when_enabled` -- idempotency.

- [x] 29. `test_enable_kdump_dry_run_short_circuits` -- dry-run.

- [x] 30. `test_disable_kdump_invokes_set_itemproperty` -- happy
  path.

- [x] 31. `test_configure_kdump_target_invokes_set_itemproperty` --
  happy path.

- [x] 32. `test_configure_kdump_target_rejects_ssh` -- typed FAILED
  for SSH.

- [x] 33. `test_configure_kdump_target_skipped_when_already_set` --
  idempotency.

- [x] 34. `test_get_kdump_status_parses_get_itemproperty_output` --
  parser.

- [x] 35. `test_get_kdump_status_disabled_when_value_is_zero` --
  enabled-bit logic.

- [x] 36. `test_list_recent_dumps_parses_directory_listing` --
  Get-ChildItem JSON parser.

- [x] 37. `test_clear_old_dumps_invokes_find_with_mtime` -- pipeline
  shape.

- [x] 38. `test_get_dump_info_parses_get_item_output` -- single-item
  parser.

- [x] 39. `test_writes_elevate` -- elevation policy.

- [x] 40. `test_reads_never_elevate` -- elevation policy.

- [x] 41. `test_writes_set_requires_reboot` -- reboot advisory.

- [x] 42. `test_failed_command_surfaces_stderr` -- error surface.

- [x] 43. `test_list_recent_dumps_raises_on_unexpected_failure` --
  typed error.

## Follow-ups

- [ ] 44. Wire WER (Windows Error Reporting) integration so user-mode
  crash dumps land in a separate query surface.  The current manager
  scopes itself to kernel-mode dumps via CrashControl.

- [ ] 45. Add a `.dmp` file metadata extractor that reads the
  `MINIDUMP_HEADER` to surface the bug-check code without invoking
  windbg.  Today the manager surfaces only filesystem metadata.

- [ ] 46. Add a `set_pagefile_minimum` helper for hosts where the
  pagefile is too small to capture a complete dump.  Today the
  pagefile is treated as out-of-scope and operators are expected to
  size it via a separate playbook.

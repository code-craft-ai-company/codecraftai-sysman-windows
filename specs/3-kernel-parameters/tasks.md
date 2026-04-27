# Implementation Plan — Windows Kernel Parameters

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.kernelparameters` module

- [x] 2. Define `WindowsKernelParameterActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN)  *(aliased from the shared
  `featurebase.ActionStatus`)*

- [x] 3. Define `WindowsKernelParameterManagerConfig` dataclass
  (dry_run, elevated, powershell_executable, parameter_map)
  *(spec called for `persist_dir`; this pilot ships with
  `parameter_map` instead because Windows has no persistence directory
  analogue.  See Spec deviations.)*

- [x] 4. Define `WindowsParameterValue` dataclass (name, value,
  is_default, registry_path, registry_key)

- [x] 5. Define `WindowsKernelParameterActionResult` dataclass with
  contract-mandated fields including `requires_reboot`,
  `registry_path`, `registry_key`, `current_value`, `applied_value`,
  `message`

- [x] 6. Define `WindowsKernelParameterQueryError` exception

- [x] 7. Define internal `WindowsKernelParameterMappingEntry` dataclass
  (registry_path, value_name, registry_type, requires_reboot)

## Mapping Table and Helpers

- [x] 8. Build the bundled `_DEFAULT_PARAMETER_MAP` constant with
  documented sample entries (tcp_keepalive_time,
  tcp_keepalive_interval, tcp_max_syn_backlog, ip_forward,
  somaxconn, tcp_syncookies, disable_ipv6, plus a
  no-Windows-equivalent sentinel for vm.swappiness)

- [x] 9. Implement `get_mapping_table()` returning a copy

- [x] 10. Implement implicit `_lookup` via `self._parameter_map.get()`

- [x] 11. Implement `_format_value_for_powershell` (double-quotes
  embedded single quotes, leaves DWord/QWord as bare integers)

- [x] 12. Implement registry-aware ``$ErrorActionPreference = 'Stop'``
  preface on every cmdlet so non-zero exit conditions reach the
  runner consistently  *(replaces the `; exit $LASTEXITCODE`
  approach the spec originally called for)*

- [x] 13. Implement `_build_powershell_argv(script)` returning
  `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
  script]`

## WindowsKernelParameterManager — Implemented

- [x] 14. Implement `__init__` accepting runner and optional config

- [x] 15. Implement `get_parameter(name)` — lookup, dispatch
  `Get-ItemProperty` read, map "property not found" / "path not
  found" to `None`, other non-zero to
  `WindowsKernelParameterQueryError`

- [x] 16. Implement `list_parameters()` — iterate mapping table,
  read each, omit missing values

- [x] 17. Implement `set_parameter(name, value, persistent)` —
  reject unmapped names, idempotency via `get_parameter`, dispatch
  `Set-ItemProperty` with `-Type`, propagate `requires_reboot`

- [x] 18. Implement `reload_parameters()` — no-op returning `SUCCESS`
  / `DRY_RUN` with the documented message

- [x] 19. Implement `remove_persisted_parameter(name)` — reject
  unmapped names, idempotency, dispatch `Remove-ItemProperty`,
  propagate `requires_reboot`

- [x] 20. Implement `_dispatch`-style helper short-circuiting on
  dry_run and populating argv into `command`

## WindowsKernelParameterManager — Gaps

- [ ] 21. Driver-specific IOCTLs for immediate-apply — NOT implemented;
  out of scope

- [ ] 22. Group Policy integration — NOT implemented; separate feature

- [ ] 23. Boot-loader / `bcdedit` kernel parameters — NOT implemented;
  separate feature

- [ ] 24. Mass-apply from a policy document — NOT implemented; out of
  scope

## Tests — Implemented

- [x] 25. `test_get_parameter_dispatches_get_item_property`
- [x] 26. `test_get_parameter_unmapped_returns_none`
- [x] 27. `test_get_parameter_property_not_found_returns_none`
- [x] 28. `test_get_parameter_unexpected_failure_raises`
- [x] 29. `test_set_parameter_unmapped_rejected`
- [x] 30. `test_set_parameter_skipped_when_value_matches`
- [x] 31. `test_set_parameter_dispatches_set_item_property_with_dword`
- [x] 32. `test_set_parameter_propagates_requires_reboot`
- [x] 33. `test_list_parameters_walks_mapping_and_omits_missing`
- [x] 34. `test_set_parameter_runner_failure_becomes_failed_result`
- [x] 35. `test_reload_parameters_returns_success_noop`
- [x] 36. `test_remove_persisted_unmapped_rejected`
- [x] 37. `test_remove_persisted_skipped_when_value_absent`
- [x] 38. `test_remove_persisted_dispatches_remove_item_property`

## Tests — Additional coverage in this batch

- [x] 39. `test_powershell_argv_includes_noprofile` — argv shape

- [x] 40. `test_set_parameter_no_windows_equivalent_rejected` —
  sentinel-None entries

- [x] 41. `test_set_parameter_persistent_false_records_advisory_message`
  — Windows persistence semantics

- [x] 42. `test_set_parameter_dry_run_short_circuits` — dry-run for
  set_parameter

- [x] 43. `test_writes_elevate_when_configured` — elevation honoured
  for writes

- [x] 44. `test_reads_never_elevate` — elevation suppressed for reads

- [x] 45. `test_get_mapping_table_returns_copy` — defensive copy
  contract

- [x] 46. `test_custom_parameter_map_overrides_bundled` — config
  override

## Tests — Gaps

- [ ] 47. Property-based tests (hypothesis) covering Properties 1
  through 31

## Follow-ups

- [ ] 48. **Cross-OS contract conformance review**: verify the data
  classes match the cross-OS contract exactly, and that every result
  honors `requires_reboot` per the table.

- [ ] 49. **Mapping table expansion**: document a process for adding
  new entries (rigorous review required — registry mistakes can
  break the OS); this is intentionally out of scope for initial
  implementation.

- [ ] 50. **Reload documentation**: add a user-guide note explaining
  why `reload_parameters()` is a no-op on Windows for cross-team
  education.

## Spec deviations from this implementation

- The spec called for a `persist_dir` config field; Windows has no
  persistence-directory analogue (registry writes are inherently
  persistent), so the field was replaced with `parameter_map` for
  caller-supplied override of the bundled mapping table.

- The bundled mapping table includes a sentinel `None`-paths entry for
  `vm.swappiness` (no Windows equivalent).  The spec did not call this
  out explicitly; we kept it because the rejection message is more
  helpful than treating the parameter as silently unmapped.

- `set_parameter(persistent=False)` is honoured advisorily: the write
  always touches the registry (Windows has no runtime-only surface).
  The result message documents this so callers do not assume the call
  was a no-op.

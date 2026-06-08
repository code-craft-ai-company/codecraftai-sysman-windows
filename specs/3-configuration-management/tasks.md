# Implementation Plan — Windows Configuration Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.windows.configurationmanagement` module

- [x] 2. Define `WindowsConfigActionStatus` enum (aliased from the
  shared `featurebase.ActionStatus`)

- [x] 3. Define `WindowsConfigManagerConfig` dataclass (`dry_run`,
  `elevated`, `powershell_executable`, `backup_suffix_format`,
  `backup_extension`, `registry_backup_extension`)

- [x] 4. Define `WindowsConfigActionResult` and
  `WindowsSettingValue` dataclasses
  *(spec also called for `WindowsConfigDiffResult`,
  `WindowsConfigValidationResult`, `WindowsConfigFileInfo` — those are
  deferred; see Spec deviations.)*

- [x] 5. Define `WindowsConfigQueryError` exception
  *(spec also called for `WindowsConfigFileNotFoundError`,
  `WindowsConfigTemplateError`,
  `WindowsConfigValidatorNotFoundError`,
  `WindowsConfigSettingNotFoundError` — deferred to a future batch
  when validators / templates are implemented.)*

## ValidatorRegistry

- [ ] 6. Define `WINDOWS_VALIDATORS` module-level dict with
  certificate, groupPolicy, sfc, testPath, xml, json entries  *(not
  in this batch — validators are a separate workstream)*

- [ ] 7. Implement `register_validator(id, builder)` runtime hook
  *(deferred)*

## WindowsConfigurationManager — Implemented

- [x] 8. Implement `__init__` accepting `runner` and optional
  `config`  *(no `file_operator` parameter — every operation routes
  through the runner.)*

- [x] 9. Implement `_classify_source(source)` returning `"registry"`
  or `"file"` based on hive prefix

- [x] 10. Implement `_infer_reg_type(value)` returning the correct
  `Set-ItemProperty -Type <type>` flag

- [x] 11. Implement registry-list parsing via
  `Get-Item.Property | Get-ItemProperty | ConvertTo-Json`

- [ ] 12. Implement `_parse_icacls(text)` producing ACE tuples  *(not
  in this batch — set_permissions is deferred.)*

- [x] 13. Implement `read_setting(source, key)` with registry +
  plain-file dispatch

- [x] 14. Implement `write_setting(source, key, value)` with registry +
  plain-file dispatch and idempotency guard

- [x] 15. Implement `remove_setting(source, key)` with registry +
  plain-file dispatch and idempotency guard

- [x] 16. Implement `list_settings(source)` with registry enumeration
  and plain-file parse

- [ ] 17. Implement `write(source, content, mode, owner, group)` with
  registry `reg import` temp-file sequence and plain-file content
  write plus optional ACL follow-up  *(deferred)*

- [ ] 18. Implement `diff(source, desired_content)` for registry
  (via `reg export`) and plain-file paths  *(deferred)*

- [ ] 19. Implement `validate(source, validator_id)` dispatching via
  the registry  *(deferred)*

- [x] 20. Implement `backup_configuration(source)` with registry
  `reg export` and file `Copy-Item` paths

- [x] 21. Implement `restore_configuration(backup_path, target_path)`
  routing by target path pattern to `reg import` or `Copy-Item`
  with byte-match idempotency on file restores

- [ ] 22. Implement `set_permissions(source, mode)` with file ACL and
  registry ACL paths  *(deferred)*

- [ ] 23. Implement `set_ownership(source, user, group)` with file
  and registry paths  *(deferred)*

- [x] 24. Implement `reload_configuration(service)` via
  `Restart-Service` with the `"none"` sentinel returning SKIPPED

## WindowsConfigurationManager — Gaps (specified but not implemented)

- [ ] 25. Group Policy Object (GPO) authoring beyond the `gpupdate`
  validator — intentionally out of scope

- [ ] 26. Registry permissions via SDDL strings (today only
  `Set-Acl`-style PowerShell is exposed)

- [ ] 27. Template rendering helper (`write_template`) using
  `string.Template`

## Tests — Implemented

- [x] 28. `test_read_setting_registry_runs_get_item_property`
- [x] 29. `test_read_setting_file_parses_value`
- [x] 30. `test_read_setting_registry_missing_key_raises`
- [x] 31. `test_read_setting_file_missing_raises`
- [x] 32. `test_write_setting_registry_dword_inferred`
- [x] 33. `test_write_setting_file_rewrites_content`
- [x] 34. `test_write_setting_registry_skipped_when_value_matches`
- [x] 35. `test_write_setting_registry_dry_run_no_runner_call`
- [x] 36. `test_remove_setting_registry_runs_remove_item_property`
- [x] 37. `test_remove_setting_file_rewrites_without_key`
- [x] 38. `test_list_settings_registry_parses_json`
- [x] 39. `test_list_settings_file_parses_kv`
- [ ] 40. `test_write_registry_uses_reg_import_temp_file_sequence`
  *(`write(source, content, ...)` whole-source helper not in this
  batch.)*
- [ ] 41. `test_diff_registry_uses_reg_export`  *(diff verb deferred.)*
- [ ] 42. `test_validate_unknown_id_raises_validator_not_found`
  *(validators deferred.)*
- [ ] 43. `test_validate_returns_valid_based_on_exit_code`
  *(validators deferred.)*
- [x] 44. `test_backup_registry_runs_reg_export`
- [x] 45. `test_backup_file_runs_copy_item`
- [x] 46. `test_restore_registry_runs_reg_import`
- [x] 47. `test_restore_file_skipped_when_byte_match`  *(file
  restore byte-match idempotency.)*
- [ ] 48. `test_set_ownership_registry_uses_powershell_setowner`
  *(set_ownership deferred.)*
- [x] 49. `test_reload_configuration_runs_restart_service`
- [x] 50. `test_reload_configuration_none_returns_skipped`

## Tests — Additional coverage in this batch

- [x] 51. `test_write_setting_registry_string_inferred` — type
  inference for plain strings

- [x] 52. `test_write_setting_registry_multistring_inferred` — type
  inference for list values

- [x] 53. `test_write_setting_registry_hklm_elevates` — HKLM writes
  always elevate

- [x] 54. `test_write_setting_registry_hkcu_does_not_elevate` — HKCU
  writes never elevate

- [x] 55. `test_remove_setting_registry_skipped_when_absent` —
  remove idempotency

- [x] 56. `test_routing_registry_vs_file` — explicit dispatch
  routing test

- [x] 57. `test_infer_reg_type_table` — table-driven type inference
  test (bool, int, str, expand-string, list, bytes)

- [x] 58. `test_reads_never_elevate` — read elevation suppression

- [x] 59. `test_powershell_argv_includes_noprofile` — argv shape

- [x] 60. `test_write_setting_file_skipped_when_already_set` —
  file-routing idempotency

## Tests — Gaps

- [ ] 61. Hypothesis property-based tests covering quoting / escape
  edge cases on registry and file paths

## Follow-ups

- [ ] 62. **Cross-OS contract conformance review**: verify that the
  shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/configuration-management/contract.md`
  and that the validator registry is consistent with the Windows row.

- [ ] 63. **codecraftai-sysman-windows reuse check**: confirm that
  every other feature which touches the registry or `%ProgramData%`
  delegates to `WindowsConfigurationManager` rather than invoking
  PowerShell cmdlets directly.

- [ ] 64. Implement the deferred verbs (`write`, `diff`, `validate`,
  `set_permissions`, `set_ownership`) and their tests in a follow-up
  batch.

## Spec deviations from this implementation

- The spec called the config `WindowsConfigurationManagerConfig`; the
  pilot uses `WindowsConfigManagerConfig` for parity with the spec
  documents that abbreviated to `WindowsConfig*` elsewhere.

- The pilot's per-setting verbs (`read_setting`, `write_setting`,
  `remove_setting`, `list_settings`) plus `reload_configuration`,
  `backup_configuration`, and `restore_configuration` are implemented.
  The whole-file `write`/`diff`/`validate`/`set_permissions`/
  `set_ownership` verbs are deferred to a follow-up batch.

- `reg.exe` (used by `backup_configuration` / `restore_configuration`
  on registry sources) requires the colon-less hive form
  (`HKLM\\foo`) while PowerShell registry cmdlets use the colon-
  prefixed form (`HKLM:\\foo`).  The pilot centralises this conversion
  in the `_to_reg_exe_path` helper, which is documented in the module
  docstring under "Path-syntax fragility".

- The shell-style `KEY=VALUE` parser shares semantics with the Ubuntu
  sibling: lines without an identifier-shaped key are silently
  skipped, comments (`#`) and blank lines are preserved verbatim on
  rewrite.  INI / JSON / XML support is out of scope for this batch.

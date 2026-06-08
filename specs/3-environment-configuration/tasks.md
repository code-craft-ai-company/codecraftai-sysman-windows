# Implementation Plan — Windows Environment Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.windows.environmentconfiguration` module

- [x] 2. Define `WindowsEnvActionStatus` and `WindowsEnvScope` enums

- [x] 3. Define `WindowsEnvironmentConfigurationManagerConfig`
  dataclass (`dry_run`, `elevated`, `powershell_executable`,
  `current_user`)
  *(spec called the class `WindowsEnvironmentConfiguratorConfig`; the
  pilot uses the longer `Manager` form for consistency with the other
  Windows feature configs.)*

- [x] 4. Define `WindowsEnvValue` and `WindowsEnvActionResult`
  dataclasses

- [x] 5. Define `WindowsEnvQueryError` exception
  *(spec called this `WindowsEnvConfigError`; renamed for symmetry with
  the other `<Feature>QueryError` exceptions in this package.)*

## WindowsEnvironmentConfigurationManager — Implemented

- [x] 6. Implement `__init__` accepting `runner` and optional `config`
  *(no `file_operator` parameter — every operation is registry-backed
  through the runner.)*

- [x] 7. Implement `_resolve_user_sid(user)` via PowerShell
  NTAccount-to-SID translation with empty-output and non-SID guard

- [x] 8. Implement `_is_current_user(user)` comparing on the
  configured `current_user` (case-insensitive bare-account match,
  domain-prefix tolerant)

- [x] 9. Implement `_parse_json_dict(...)` dropping PowerShell
  pseudo-keys (`PSPath`, `IsReadOnly`, `Count`, etc.)

- [x] 10. Implement `set_system_env(name, value)` via
  `SetEnvironmentVariable('Machine')` with idempotency guard

- [x] 11. Implement `unset_system_env(name)` via
  `SetEnvironmentVariable(..., $null, 'Machine')` with idempotency
  guard

- [x] 12. Implement `get_system_env(name)` via
  `GetEnvironmentVariable('Machine')`

- [x] 13. Implement `list_system_env()` via
  `GetEnvironmentVariables('Machine') | ConvertTo-Json`

- [x] 14. Implement `set_user_env(user, name, value)` with
  unknown-user `FAILED`, current-vs-other dispatch, and same-user
  elevation suppression

- [x] 15. Implement `unset_user_env(user, name)` with unknown-user
  `FAILED`, current-vs-other dispatch, and idempotency

- [ ] 16. Implement `get_user_env(user, name)` with
  `WindowsEnvQueryError` on unknown user and current-vs-other
  dispatch  *(deferred — not in this batch)*

- [ ] 17. Implement `list_user_env(user)` with unknown-user raise,
  current-vs-other dispatch, and empty-key handling  *(deferred — not
  in this batch)*

## WindowsEnvironmentConfigurationManager — Gaps (specified but not implemented)

- [ ] 18. `WM_SETTINGCHANGE` broadcast after writes — relies on the
  .NET `SetEnvironmentVariable` API which already broadcasts; no
  explicit follow-up implemented

- [ ] 19. Per-process environment block manipulation (beyond
  user/machine scopes) — out of scope

- [ ] 20. Group Policy-managed environment variables (ADMX) —
  intentionally out of scope

## Tests — Implemented

- [x] 21. `test_set_system_env_dispatches_setenvironmentvariable_machine`
- [x] 22. `test_set_system_env_skipped_when_value_matches`
- [x] 23. `test_set_system_env_dry_run_short_circuits`
- [x] 24. `test_set_system_env_elevates_when_configured`
- [x] 25. `test_unset_system_env_skipped_when_absent`
- [x] 26. `test_unset_system_env_dispatches_null_value`
- [x] 27. `test_get_system_env_returns_none_when_empty`
- [x] 28. `test_list_system_env_filters_pseudo_keys`
- [x] 29. `test_get_system_env_returns_value`
- [x] 30. `test_list_system_env_raises_on_runner_failure`
- [x] 31. `test_set_user_env_unknown_user_returns_failed`
- [x] 32. `test_set_user_env_current_user_uses_user_scope_api`
- [x] 33. `test_set_user_env_other_user_uses_per_sid_registry`
- [x] 34. `test_set_user_env_current_user_does_not_elevate`
- [x] 35. `test_set_user_env_other_user_elevates_when_configured`
- [x] 36. `test_unset_user_env_other_user_uses_remove_item_property`
- [x] 37. `test_unset_user_env_current_user_dispatches_null_value`
- [x] 38. `test_list_system_env_parses_json_dict`
- [x] 39. `test_current_user_matched_with_domain_prefix`
- [x] 40. `test_reads_never_elevate`
- [x] 41. `test_powershell_argv_includes_noprofile`

## Tests — Gaps

- [ ] 42. Per-user `get_user_env` / `list_user_env` tests (those verbs
  are not yet implemented)

- [ ] 43. Hypothesis property-based tests covering quoting / escape
  edge cases

## Follow-ups

- [ ] 44. **Cross-OS contract conformance review**: verify that the
  shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`
  and that the scope-mapping rows are consistent.

- [ ] 45. **codecraftai-sysman-windows reuse check**: confirm that
  features which need environment variables delegate to
  `WindowsEnvironmentConfigurationManager` rather than invoking
  PowerShell environment APIs directly.

- [ ] 46. Implement `get_user_env` / `list_user_env` for full feature
  parity with the system-scope read surface.

## Spec deviations from this implementation

- The spec called the manager class
  `WindowsEnvironmentConfigurator` and the config
  `WindowsEnvironmentConfiguratorConfig`.  The pilot uses
  `WindowsEnvironmentConfigurationManager` /
  `WindowsEnvironmentConfigurationManagerConfig` for symmetry with
  the other Windows feature managers.

- "Current user" detection is delegated to a configured
  `current_user` field on the config rather than probing
  `[Environment]::UserName` at runtime.  This keeps the manager
  deterministic under remote execution where the runner identity may
  differ from the host's interactive user.  When `current_user` is
  not configured, every user-scope target is treated as "other user"
  and routed through the per-SID registry path with elevation.

- Per-SID writes use the explicit `Registry::HKEY_USERS\\<SID>`
  provider path (NOT `HKU:`) because the `HKU` PSDrive is not always
  available.  This is documented in the module docstring.

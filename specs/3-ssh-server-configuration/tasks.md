# Implementation Plan — Windows SSH-Server-Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.windows.sshserverconfiguration`
  module

- [x] 2. Define `WindowsSshActionStatus` alias of the shared
  `ActionStatus` enum (SUCCESS / FAILED / SKIPPED / DRY_RUN)

- [x] 3. Define `WindowsSshServerConfigurationManagerConfig`
  dataclass (`dry_run`, `elevated`, `sshd_config_path`,
  `authorized_keys_path_template`, `admin_authorized_keys_path`,
  `service_name`, `powershell_executable`)

- [x] 4. Define `WindowsSshActionResult` dataclass (adds `keyword`,
  `path`, `user`, `message` fields over `FeatureActionResult`)

- [x] 5. Define `WindowsSshSettingValue`, `WindowsSshMatchBlock`,
  and `WindowsSshAuthorizedKey` query dataclasses

- [x] 6. Define `WindowsSshQueryError` exception (reserved for
  future read paths)

## WindowsSshServerConfigurationManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 8. Implement `get_sshd_setting(keyword)` — `Get-Content -Raw`
  on `%ProgramData%\ssh\sshd_config`, parse, return first top-
  level match or `None`

- [x] 9. Implement `list_sshd_settings()` — `Get-Content` + parse
  + return top-level dict

- [x] 10. Implement `set_sshd_setting(keyword, value)` — read,
  parse, upsert top setting, render, idempotency-skip on byte-
  equal, dispatch `Set-Content`

- [x] 11. Implement `remove_sshd_setting(keyword)` — read, parse,
  drop top-level occurrences, render, idempotency-skip when absent
  or content-equal, dispatch `Set-Content`

- [x] 12. Implement `add_match_block(criteria, settings)` — read,
  parse, upsert block (whitespace-collapsed criteria match),
  render, dispatch `Set-Content`

- [x] 13. Implement `remove_match_block(criteria)` — read, parse,
  drop matching block, render, idempotency-skip, dispatch
  `Set-Content`

- [x] 14. Implement `list_match_blocks()` — `Get-Content` + parse
  + return blocks in declaration order

- [x] 15. Implement `reload_sshd()` — dispatch PowerShell argv
  containing `Restart-Service -Name 'sshd'`

- [x] 16. Implement `test_sshd_config()` — dispatch `sshd.exe -t`
  (no PowerShell wrapper)

- [x] 17. Implement `list_authorized_keys(user, *, is_admin=False)`
  — choose between `authorized_keys_path_template.format(user=...)`
  and `admin_authorized_keys_path` based on `is_admin`, `Get-
  Content`, parse with options + comment + fingerprint

- [x] 18. Implement `add_authorized_key(user, key, *,
  is_admin=False)` — parse the supplied line (FAILED on parse
  failure), idempotency-skip by fingerprint, dispatch
  `Set-Content` to the per-user OR admin path

- [x] 19. Implement `remove_authorized_key(user, fingerprint, *,
  is_admin=False)` — drop every line whose computed fingerprint
  matches, dispatch `Set-Content` to the per-user OR admin path

- [x] 20. Implement helper `_parse_sshd_config(content)` — returns
  `(top_settings, match_blocks)`

- [x] 21. Implement helper `_render_sshd_config(top_settings,
  match_blocks)` — deterministic rendering, four-space-indented
  Match-block bodies

- [x] 22. Implement helper `_parse_authorized_keys(content)` and
  `_parse_authorized_key_line(line)` — recognise common OpenSSH
  key types, capture options prefix and trailing comment

- [x] 23. Implement helper `_compute_fingerprint(key_data)` —
  SHA-256 of base64-decoded body, base64-encoded, padding stripped

- [x] 24. Implement helper `_ps_escape_single_quoted(value)` —
  doubles embedded single quotes for PowerShell single-quoted
  literals

## WindowsSshServerConfigurationManager — Gaps

- [ ] 25. Comment-line preservation across mutation.  Tracked as a
  shared follow-up across all OS implementations.

- [ ] 26. Pre-write `sshd.exe -t` validation with rollback.
  Same follow-up as the Linux/macOS siblings.

- [ ] 27. Per-user authorized-key writes without elevation.  Currently
  honours `config.elevated` uniformly.

- [ ] 28. ACL repair after write.  Windows OpenSSH host-key files
  require strict ACLs; the same is true of
  `administrators_authorized_keys`.  The pilot does NOT run
  `icacls` after writing; a follow-up will mirror Microsoft's
  ``Repair-AuthorizedKeyPermission`` / ``Repair-SshdHostKeyPermission``
  cmdlets through the runner.

- [ ] 29. UNC-path support.  Domain joined hosts may want to write
  to `\\server\share\sshd_config` for centralised policy.  Not
  required by the pilot.

- [ ] 30. ``Get-Service`` precondition: skip `Restart-Service` if
  `sshd` is not installed (instead of FAILED).  Currently surfaces
  the runner's exit code.

## Tests — Implemented

- [x] 31. `test_get_sshd_setting_parses_value`
- [x] 32. `test_list_sshd_settings_parses_top_level`
- [x] 33. `test_list_match_blocks_parses_blocks`
- [x] 34. `test_list_authorized_keys_parses_user_keys`
- [x] 35. `test_list_authorized_keys_uses_admin_path_when_is_admin_true`
- [x] 36. `test_set_sshd_setting_writes_when_absent`
- [x] 37. `test_set_sshd_setting_skipped_when_already_set`
- [x] 38. `test_set_sshd_setting_replaces_existing_value`
- [x] 39. `test_dry_run_short_circuits_set_sshd_setting`
- [x] 40. `test_remove_sshd_setting_skipped_when_absent`
- [x] 41. `test_remove_sshd_setting_rewrites_file`
- [x] 42. `test_add_match_block_appends_to_config`
- [x] 43. `test_remove_match_block_skipped_when_absent`
- [x] 44. `test_remove_match_block_rewrites_file`
- [x] 45. `test_reload_sshd_invokes_restart_service`
- [x] 46. `test_test_sshd_config_invokes_sshd_dash_t`
- [x] 47. `test_add_authorized_key_writes_to_per_user_path`
- [x] 48. `test_add_authorized_key_writes_to_admin_path_when_is_admin_true`
- [x] 49. `test_add_authorized_key_skipped_when_already_present`
- [x] 50. `test_add_authorized_key_failed_on_parse_error`
- [x] 51. `test_remove_authorized_key_finds_by_fingerprint`
- [x] 52. `test_remove_authorized_key_skipped_when_fingerprint_absent`
- [x] 53. `test_writes_pass_elevated_true`
- [x] 54. `test_reads_never_elevate`
- [x] 55. `test_failed_command_returns_failed`
- [x] 56. `test_parser_handles_match_block_followed_by_eof`

## Tests — Gaps

- [ ] 57. Property-based hypothesis coverage.

- [ ] 58. Integration test on a real Windows host (Server / 10 / 11)
  that exercises `Restart-Service sshd` and validates the reload
  picks up `sshd_config` changes.

## Spec Deviations / Revisions Needed

- [ ] 59. **Path syntax**: the contract column for Windows uses
  `%ProgramData%\ssh\sshd_config`; this implementation uses the
  expanded literal `C:\ProgramData\ssh\sshd_config` as the default
  for `sshd_config_path`.  Either form works at runtime
  (PowerShell expands env vars in `Get-Content` arguments), but
  the literal form makes the dispatched argv reproducible across
  hosts where `%ProgramData%` resolves to a non-default value.
  Callers that need env-var expansion can override via the config.

- [ ] 60. **`is_admin` keyword**: the cross-OS contract's
  `add_authorized_key` / `remove_authorized_key` /
  `list_authorized_keys` signatures take `(user, ...)` and do
  NOT include `is_admin`.  This Windows implementation ADDS the
  keyword as a Windows-specific extension because the
  admin-shared file is a Windows-only concept.  The contract
  text (Out-of-Scope §) already documents that
  `authorized_keys`-command-restriction policies beyond the
  `options` field are extensions; a tracked follow-up will
  promote `is_admin` to a documented contract extension for
  Windows.

- [ ] 61. **Type naming**: the contract uses `SshdSetting` /
  `SshdMatchBlock` / `AuthorizedKey` (with OS prefixes).  This
  module uses `WindowsSshSettingValue` / `WindowsSshMatchBlock` /
  `WindowsSshAuthorizedKey`.  The `SettingValue` suffix tracks
  the prior wave-1 naming used by `configurationmanagement.py`.

## Follow-ups

- [ ] 62. **Cross-OS contract conformance review**.

- [ ] 63. **Shared `featurebase` upstreaming**: vendored from RHEL.

- [ ] 64. **ACL repair**: integrate Microsoft's
  ``OpenSSHUtils`` PowerShell module to run
  `Repair-AuthorizedKeyPermission` after writing
  `administrators_authorized_keys` and per-user files.

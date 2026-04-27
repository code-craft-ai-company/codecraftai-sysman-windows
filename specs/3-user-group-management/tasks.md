# Implementation Plan — Windows User-Group Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.usergroupmanagement`
  module

- [x] 2. Define `WindowsUserGroupActionStatus` enum (SUCCESS / FAILED
  / SKIPPED / DRY_RUN)

- [x] 3. Define `WindowsUserGroupManagerConfig` dataclass (dry_run,
  elevated, powershell_executable).  No `profile_root` field — the
  pilot does not delete the user profile directory; that work is
  tracked as a follow-up (`delete_user(name, remove_home=True)`).

- [x] 4. Define `WindowsUserGroupActionResult` dataclass with
  sanitised `command` field

- [x] 5. Define `WindowsUserSpec`, `WindowsUserChanges`,
  `WindowsUserInfo`, `WindowsGroupSpec`, `WindowsGroupInfo` dataclasses
  (the spec named the modify spec `WindowsUserModSpec`; the
  implementation uses `WindowsUserChanges` to match the macOS / Ubuntu
  peers' verb).  `WindowsPrincipalKind` enum added for parity with the
  macOS peer.

- [x] 6. Define `WindowsUserGroupQueryError` exception.
  `WindowsUserNotFoundError` / `WindowsGroupNotFoundError` were folded
  into `modify_user` / `add_to_group` / `remove_from_group` returning
  `FAILED` results with `message="user does not exist"` /
  `"group does not exist"` to match the macOS peer's contract.  A
  follow-up may reintroduce the typed exceptions if the cross-OS
  contract requires them.

## WindowsUserGroupManager — Implemented

- [x] 7. Implement `__init__` accepting runner + config (inherited
  from `BaseFeatureManager`)

- [x] 8. Implement `create_user(spec)` — `New-LocalUser` with
  SecureString password embedded inside the scriptblock; idempotent
  via `Get-LocalUser`.  Spec deviation: the pilot embeds the plaintext
  password as a single-quoted string inside the
  `ConvertTo-SecureString -AsPlainText -Force '<pw>'` literal and
  redacts that literal in the recorded argv.  Stdin-piping (the
  spec's preferred approach) was deferred because the runner contract
  does not currently expose stdin redaction in the recorded `command`
  field; a follow-up may rework this once stdin redaction lands.

- [x] 9. Implement `delete_user(name)` — `Remove-LocalUser`;
  idempotent via `Get-LocalUser`.  `remove_home=True` deferred (the
  pilot does not remove the user profile directory; follow-up
  tracked).

- [x] 10. Implement `modify_user(name, changes)` — `Rename-LocalUser`
  for renames + `Set-LocalUser` for scalar attributes +
  `Add-LocalGroupMember` / `Remove-LocalGroupMember` for group
  reconciliation; per-attribute idempotency

- [x] 11. Implement `get_user(name)` — `Get-LocalUser |
  ConvertTo-Json` + `Get-LocalGroup` enumeration with per-group
  `Get-LocalGroupMember` probe to compute group memberships

- [x] 12. Implement `list_users()` — `Get-LocalUser | ConvertTo-Json`
  array parser

- [x] 13. Implement `set_password(name, password)` — PowerShell
  script with embedded `ConvertTo-SecureString -AsPlainText -Force` +
  `Set-LocalUser -Password`; recorded argv has plaintext replaced
  with `<redacted>`

- [x] 14. Implement `lock_account` / `unlock_account` —
  `Disable-LocalUser` / `Enable-LocalUser`; idempotent via the
  `Enabled` flag from `Get-LocalUser`

- [x] 15. Implement `create_group(spec)` — `New-LocalGroup`;
  idempotent via `Get-LocalGroup`

- [x] 16. Implement `delete_group(name)` — `Remove-LocalGroup`;
  idempotent via `Get-LocalGroup`

- [x] 17. Implement `get_group(name)` — `Get-LocalGroup` + per-group
  `Get-LocalGroupMember`, strips domain prefix for short names

- [x] 18. Implement `list_groups()` — `Get-LocalGroup |
  ConvertTo-Json` array parser with `members=[]`

- [x] 19. Implement `add_to_group` / `remove_from_group` —
  `Add-LocalGroupMember` / `Remove-LocalGroupMember`; returns FAILED
  with documenting message when the group is missing; idempotent via
  `Get-LocalGroupMember` membership probe

- [x] 20. Implement `_dispatch` helper — dry-run short-circuit, argv
  sanitisation (script-level password redaction), runner-result
  mapping, elevation decision

## WindowsUserGroupManager — Gaps (specified but not implemented)

- [ ] 21. Domain-account support (Active Directory) — out of this
  contract's scope; would require `ActiveDirectory` module

- [ ] 22. UAC policy management via `Registry::` provider edits

- [ ] 23. Password-complexity policy management (`secedit`)

- [ ] 24. `delete_user(name, remove_home=True)` — profile-directory
  removal via `Remove-Item -Recurse -Force` against the resolved
  profile root.  Deferred from this batch.

- [ ] 25. Stdin-piped password channel (with stdin-redaction in the
  recorded result) once the runner contract exposes that hook;
  current implementation embeds the plaintext password inside the
  scriptblock and redacts it via regex.

## Tests — Implemented

- [x] 26. `test_create_user_constructs_new_localuser_script`
- [x] 27. `test_create_user_password_redacted_in_recorded_command`
- [x] 28. `test_create_user_skipped_when_present`
- [x] 29. `test_create_user_dry_run_short_circuits`
- [x] 30. `test_delete_user_invokes_remove_localuser`
- [x] 31. `test_delete_user_skipped_when_absent`
- [x] 32. `test_modify_user_emits_only_changed_attributes`
- [x] 33. `test_modify_user_skipped_when_noop`
- [x] 34. `test_modify_user_failed_when_user_missing`
- [x] 35. `test_modify_user_password_redacted_in_recorded_command`
- [x] 36. `test_set_password_dispatches_set_localuser`
- [x] 37. `test_lock_account_skipped_when_already_disabled`
- [x] 38. `test_lock_account_invokes_disable_localuser`
- [x] 39. `test_unlock_account_skipped_when_already_enabled`
- [x] 40. `test_create_group_constructs_new_localgroup_script`
- [x] 41. `test_create_group_skipped_when_present`
- [x] 42. `test_delete_group_skipped_when_absent`
- [x] 43. `test_add_to_group_skipped_when_already_member`
- [x] 44. `test_add_to_group_invokes_add_localgroupmember`
- [x] 45. `test_remove_from_group_skipped_when_not_member`
- [x] 46. `test_add_to_group_failed_when_group_missing`
- [x] 47. `test_list_users_parses_array_form`
- [x] 48. `test_list_groups_parses_array_form`
- [x] 49. `test_get_localgroupmember_parser_strips_domain_prefix`
- [x] 50. `test_parse_get_localuser_handles_bare_object`
- [x] 51. `test_parse_get_localgroup_handles_array_form`
- [x] 52. `test_sanitise_script_passwords_replaces_every_match`
- [x] 53. `test_writes_pass_elevated_when_configured`
- [x] 54. `test_reads_never_elevate`
- [x] 55. `test_failed_write_returns_failed_with_stderr`
- [x] 56. `test_powershell_argv_construction_includes_noprofile`
- [x] 57. `test_list_users_query_error_on_failure`

## Tests — Gaps

- [ ] 58. Persistence-aspect end-to-end test asserting that no
  plaintext password is ever written to the audit store

- [ ] 59. Test proving the `ObjectNotFound`-classifier handles both
  English and localised error messages (requires message-code parsing
  rather than text matching)

- [ ] 60. `delete_user(name, remove_home=True)` profile-removal
  coverage once the verb is implemented

## Follow-ups

- [ ] 61. **Cross-OS contract conformance review**: verify this
  implementation's `WindowsUserGroupActionResult` field shapes match
  the cross-OS contract at
  `automojo-sysman-common/specs/contracts/user-group-management/contract.md`.

- [ ] 62. **automojo-* reuse check**: confirm that no other feature in
  `automojo-sysman-windows` invokes `New-LocalUser` /
  `Add-LocalGroupMember` directly; all such calls should go through
  `WindowsUserGroupManager`.

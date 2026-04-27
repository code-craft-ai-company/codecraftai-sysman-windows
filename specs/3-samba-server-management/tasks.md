# Implementation Plan — Windows Samba Server Management

All tasks are unchecked — new feature.

## Module and Models

- [ ] 1. Create `source/packages/automojo/sysman/windows/sambaservermanagement.py`
- [ ] 2. Define module-level constants (PowerShell argv pieces,
  delegation messages).
- [ ] 3. Define `WindowsSambaActionStatus` alias of `ActionStatus`.
- [ ] 4. Define `WindowsSambaManagerConfig` dataclass extending
  `FeatureConfig` with `powershell_executable`,
  `delegate_user_mgmt`.
- [ ] 5. Define `WindowsSambaActionResult` dataclass extending
  `FeatureActionResult` with `share_name`, `username`, `message`.
- [ ] 6. Define `WindowsSambaShareSpec`, `WindowsSambaShareInfo`,
  `WindowsSambaShareAcl`, `WindowsSambaUserInfo` dataclasses.
- [ ] 7. Define `WindowsSambaQueryError`.

## Helpers

- [ ] 8. Implement `_build_powershell_argv(script)`.
- [ ] 9. Implement `_ps_quote(value)`.
- [ ] 10. Implement `_build_new_smb_share_script(spec)`.
- [ ] 11. Implement `_build_remove_smb_share_script(name)`.
- [ ] 12. Implement `_build_grant_smb_share_access_script(name,
  account, access_right)`.
- [ ] 13. Implement `_build_revoke_smb_share_access_script(name,
  account)`.
- [ ] 14. Implement `_parse_shares_json(text)` returning
  `list[WindowsSambaShareInfo]`.
- [ ] 15. Implement `_parse_share_access_json(text)` returning a
  ``dict[<account>, <access-right>]`` per share.
- [ ] 16. Implement `_users_from_access_records(records)` returning
  `list[WindowsSambaUserInfo]`.

## Manager — lifecycle

- [ ] 17. Implement `WindowsSambaServerManager` class extending
  `BaseFeatureManager`.
- [ ] 18. Implement `__init__(runner, config=None)`.

## Manager — write verbs

- [ ] 19. Implement `add_share(spec)` per design.md.
- [ ] 20. Implement `remove_share(name)` per design.md.
- [ ] 21. Implement `set_share_permissions(name, acl)` per design.md.
- [ ] 22. Implement `reload_config()` returning no-op SUCCESS.
- [ ] 23. Implement `add_samba_user(name, password)` returning
  delegated FAILED.
- [ ] 24. Implement `remove_samba_user(name)` returning delegated
  FAILED.

## Manager — read verbs

- [ ] 25. Implement `list_shares()` per design.md.
- [ ] 26. Implement `get_share(name)` per design.md.
- [ ] 27. Implement `list_samba_users()` per design.md.

## Tests

- [ ] 28. Create `test_sambaservermanagement.py`.
- [ ] 29. Add `test_add_share_invokes_new_smbshare`.
- [ ] 30. Add `test_add_share_skipped_when_present`.
- [ ] 31. Add `test_add_share_dry_run`.
- [ ] 32. Add `test_remove_share_invokes_remove_smbshare`.
- [ ] 33. Add `test_list_shares_parses_output`.
- [ ] 34. Add `test_get_share_returns_correct_record`.
- [ ] 35. Add `test_set_share_permissions_dispatches_grant_and_revoke`.
- [ ] 36. Add `test_reload_config_invokes_no_op_success`.
- [ ] 37. Add `test_add_samba_user_returns_failed_with_delegation_message`.
- [ ] 38. Add `test_remove_samba_user_returns_failed_with_delegation_message`.
- [ ] 39. Add `test_list_samba_users_parses_get_smbshareaccess_output`.
- [ ] 40. Add `test_writes_elevate`.
- [ ] 41. Add `test_reads_never_elevate`.
- [ ] 42. Add `test_failed_command_surfaces_stderr`.

## Follow-ups

- [ ] 43. **Domain-aware ACL.** When the host is domain-joined,
  resolve plain account names to ``DOMAIN\\account`` automatically.
- [ ] 44. **Local-user wiring.** Allow
  ``delegate_user_mgmt=False`` to dispatch ``New-LocalUser`` /
  ``Remove-LocalUser`` (with the password on stdin).
- [ ] 45. **Cluster-aware shares.** Emit ``New-SmbShare
  -ScopeName`` for cluster nodes.

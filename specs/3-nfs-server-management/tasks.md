# Implementation Plan — Windows NFS Server Management

All tasks are unchecked — new feature.

## Module and Models

- [ ] 1. Create
  ``source/packages/ccai/sysman/windows/nfsservermanagement.py``.
- [ ] 2. Define module-level constants (default PowerShell argv,
  default service name, default options).
- [ ] 3. Define ``WindowsNfsActionStatus`` alias of ``ActionStatus``.
- [ ] 4. Define ``WindowsNfsManagerConfig`` dataclass extending
  ``FeatureConfig`` with ``powershell_executable``,
  ``requires_nfs_server_role``, ``service_name``.
- [ ] 5. Define ``WindowsNfsActionResult`` extending
  ``FeatureActionResult`` with ``path``, ``share_name``,
  ``message``.
- [ ] 6. Define ``WindowsNfsClientSpec``, ``WindowsNfsExportSpec``,
  ``WindowsNfsExportInfo``, ``WindowsNfsExportStatus`` dataclasses.
- [ ] 7. Define ``WindowsNfsQueryError`` with ``command`` /
  ``exit_code`` / ``stderr`` / ``os.linesep``-joined message.

## Helpers — PowerShell scriptblock builders

- [ ] 8. Implement ``_ps_quote(value)`` that doubles embedded
  single quotes per PowerShell single-quoted-string grammar.
- [ ] 9. Implement ``_build_new_nfs_share_script(spec)`` rendering
  the canonical ``New-NfsShare`` cmdlet invocation.
- [ ] 10. Implement ``_build_remove_nfs_share_script(share_name)``
  rendering ``Remove-NfsShare -Name '<n>' -Force``.
- [ ] 11. Implement ``_build_set_nfs_share_script(share_name,
  options)`` translating the supplied options list into
  ``Set-NfsShare`` named parameters.
- [ ] 12. Implement ``_build_get_nfs_share_script(name)`` and
  ``_build_list_nfs_shares_script()``.

## Helpers — JSON parsers

- [ ] 13. Implement ``_parse_shares_json(text) →
  list[WindowsNfsExportInfo]``.
- [ ] 14. Implement ``_parse_share_status_json(text) → dict``
  used by ``get_export_status``.
- [ ] 15. Implement ``_lookup_share_by_path(shares, path)`` that
  returns the matching ``WindowsNfsExportInfo`` or ``None``.

## Manager — lifecycle

- [ ] 16. Implement ``WindowsNfsServerManager`` class extending
  ``BaseFeatureManager``.
- [ ] 17. Implement ``__init__(runner, config=None)``.

## Manager — write verbs

- [ ] 18. Implement ``add_export(spec)`` per design.md.
  - _Requirements: 1, 7.1, 11.1_
- [ ] 19. Implement ``remove_export(path)`` per design.md.
  - _Requirements: 2, 11.2_
- [ ] 20. Implement ``reload_exports()`` per design.md.
  - _Requirements: 4, 11.4_
- [ ] 21. Implement ``set_export_options(path, options)`` per
  design.md.
  - _Requirements: 6, 7.2, 11.3_

## Manager — read verbs

- [ ] 22. Implement ``list_exports()`` per design.md.
  - _Requirements: 3_
- [ ] 23. Implement ``get_export_status(path)`` per design.md.
  - _Requirements: 5_

## Manager — internal helpers

- [ ] 24. Implement ``_share_exists_with_same_path(name, path)``
  used by ``add_export`` for idempotency.
- [ ] 25. Implement ``_lookup_share_by_path_via_runner(path)`` used
  by ``remove_export`` / ``set_export_options`` /
  ``get_export_status``.
- [ ] 26. Implement ``_build_powershell_argv(script)``.
- [ ] 27. Implement ``_build_result(...)`` and
  ``_wrap_runner_result(...)``.

## Tests

- [ ] 28. Create
  ``source/testroots/testbase/ccai/tests/sysman/windows/test_nfsservermanagement.py``.
- [ ] 29. Add ``test_add_export_invokes_powershell``.
- [ ] 30. Add ``test_add_export_skipped_when_present``.
- [ ] 31. Add ``test_add_export_dry_run``.
- [ ] 32. Add ``test_remove_export_invokes_powershell``.
- [ ] 33. Add ``test_remove_export_skipped_when_absent``.
- [ ] 34. Add ``test_list_exports_parses_json_output``.
- [ ] 35. Add ``test_reload_exports_invokes_powershell``.
- [ ] 36. Add ``test_get_export_status_parses_json_output``.
- [ ] 37. Add ``test_set_export_options_invokes_powershell``.
- [ ] 38. Add ``test_writes_elevate``.
- [ ] 39. Add ``test_reads_never_elevate``.
- [ ] 40. Add ``test_failed_command_surfaces_stderr``.
- [ ] 41. Add ``test_share_name_required_for_writes``.
- [ ] 42. Add ``test_get_export_status_returns_inactive_for_missing_path``.
- [ ] 43. Add ``test_reload_exports_uses_restart_service``.

## Follow-ups

- [ ] 44. **Per-client permissions.** Add
  ``grant_share_permission(name, client, permission)`` verb wrapping
  ``Grant-NfsSharePermission``.
- [ ] 45. **AD-integrated auth.** Surface ``-Authentication krb5`` /
  ``krb5p`` via ``set_export_options``.
- [ ] 46. **Multi-share-per-path.** Add
  ``remove_export_by_name(name)`` for setups where multiple shares
  share a path.

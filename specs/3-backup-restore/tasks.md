# Tasks

The following implementation tasks are derived from the requirements and
design documents.

---

## Module Skeleton

- [ ] T1. Create `source/packages/ccai/sysman/windows/backuprestore.py`
      with the module docstring describing the verb-to-tool mapping, the
      SKU constraint notes, and the schedule-delegation note.
- [ ] T2. Add the module-level constants for the PowerShell argv shape
      (`_POWERSHELL`, `_PS_NOPROFILE`, `_PS_NONINTERACTIVE`,
      `_PS_COMMAND`), the backend tool names (`_WBADMIN`, `_ROBOCOPY`,
      `_RESTIC`), and the default job directory path
      (`_DEFAULT_JOB_DIR = r"C:\ProgramData\CodeCraftAI\backup\jobs"`).
- [ ] T3. Add the `WindowsBackupActionStatus = ActionStatus` re-export.

---

## Data Models

- [ ] T4. Define the `WindowsBackupKind` enum with values `WBADMIN`,
      `ROBOCOPY`, `RESTIC`.
- [ ] T5. Define the `WindowsBackupManagerConfig` dataclass extending
      `FeatureConfig`, with fields
      `default_kind: WindowsBackupKind = WBADMIN`,
      `job_dir: str = _DEFAULT_JOB_DIR`,
      `powershell_executable: str = _POWERSHELL`.
- [ ] T6. Define the `WindowsBackupActionResult` dataclass extending
      `FeatureActionResult`, adding `job_id: Optional[str]` and
      `target_path: Optional[str]`.
- [ ] T7. Define the `WindowsBackupJobSpec` dataclass with the standard
      fields. Add the alias `WindowsBackupSpec = WindowsBackupJobSpec`.
- [ ] T8. Define the `WindowsBackupInfo`, `WindowsBackupVerifyResult`,
      and `WindowsBackupJobStatus` dataclasses per the contract.
- [ ] T9. Define the `WindowsBackupQueryError`,
      `WindowsBackupNotFoundError`, and `WindowsBackupVerifyError`
      exception classes.

---

## Module Helpers

- [ ] T10. Implement `_serialise_spec(spec)` returning a JSON string
      with the passphrase redacted.
- [ ] T11. Implement `_deserialise_spec(text)`.
- [ ] T12. Implement `_redact_passphrase(spec)`.
- [ ] T13. Implement `_build_powershell_argv(*, script,
      powershell_executable)` returning the canonical PowerShell argv.
- [ ] T14. Implement `_build_set_content_script(*, path, body)`,
      `_build_remove_item_script(*, path)`,
      `_build_get_content_script(*, path)`,
      `_build_get_childitem_script(*, path)`.
- [ ] T15. Implement `_build_wbadmin_script(*, spec, mode, target,
      version_id)` for the `wbadmin start backup` /
      `wbadmin start recovery` / `wbadmin get items` /
      `wbadmin get versions` modes.
- [ ] T16. Implement `_build_robocopy_script(*, spec, mode, target)`.
- [ ] T17. Implement `_build_restic_script(*, spec, mode, target)`
      returning the script and a `bool` indicating whether the runner
      stdin must carry the passphrase.
- [ ] T18. Implement `_job_path(*, job_dir, job_id)`.

---

## Manager Class

- [ ] T19. Implement `WindowsBackupRestoreManager.__init__`.
- [ ] T20. Implement `create_backup_job(spec)` per Requirement 1.
- [ ] T21. Implement `delete_backup_job(job_id)` per Requirement 2.
- [ ] T22. Implement `run_backup_now(job_id)` per Requirement 3.
- [ ] T23. Implement `list_backups()` per Requirement 4. When any
      persisted spec is `WBADMIN`, also dispatch
      `wbadmin get versions` with `elevated=True`.
- [ ] T24. Implement `restore_from_backup(job_id, target)` per
      Requirement 5.
- [ ] T25. Implement `verify_backup(job_id)` per Requirement 6.
- [ ] T26. Implement `get_job_status(job_id)`.
- [ ] T27. Implement the helper methods `_typed_config`,
      `_read_spec_text`, `_path_exists`, `_build_result`,
      `_wrap_run_result`.

---

## Tests

- [ ] T28. Create
      `source/testroots/testbase/ccai/tests/sysman/windows/test_backuprestore.py`
      with a `_MockCommandRunner` matching the prior-wave PowerShell
      pattern.
- [ ] T29. `test_create_backup_job_persists_spec` — script body
      contains `Set-Content` and the JSON payload.
- [ ] T30. `test_create_backup_job_skipped_when_present`.
- [ ] T31. `test_create_backup_job_dry_run`.
- [ ] T32. `test_delete_backup_job_invokes_remove`.
- [ ] T33. `test_run_backup_now_dispatches_wbadmin`.
- [ ] T34. `test_run_backup_now_dispatches_robocopy`.
- [ ] T35. `test_run_backup_now_dispatches_restic`.
- [ ] T36. `test_list_backups_parses_get_childitem_output`.
- [ ] T37. `test_restore_from_backup_invokes_wbadmin`.
- [ ] T38. `test_restore_from_backup_invokes_robocopy`.
- [ ] T39. `test_verify_backup_invokes_wbadmin_get_items`.
- [ ] T40. `test_verify_backup_invokes_restic_check`.
- [ ] T41. `test_writes_elevate`.
- [ ] T42. `test_reads_never_elevate` (for spec reads).
- [ ] T43. `test_failed_command_surfaces_stderr`.
- [ ] T44. `test_passphrase_redacted_when_restic_encrypted`.
- [ ] T45. `test_wbadmin_dispatch_for_wbadmin_kind` — canonical
      `wbadmin start backup` argv-shape pinning.

---

## Documentation

- [ ] T46. Update the package-level `Agents.md` entry for
      `codecraftai-sysman-windows` to point at the
      `specs/3-backup-restore/` directory.
- [ ] T47. Note in the manager's module docstring that schedule
      wiring is delegated to `scheduled-tasks`, that the
      `encryption_passphrase` field is redacted from on-disk spec
      files, and that `wbadmin` availability depends on the host SKU.

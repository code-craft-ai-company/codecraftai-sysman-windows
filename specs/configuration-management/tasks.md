# Implementation Plan

- [ ] 1. Create the `configurationmanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/configurationmanagement.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10_

- [ ] 2. Define status enum and result dataclasses
  - Implement `ConfigActionStatus` enum with values: `SUCCESS`, `FAILED`, `SKIPPED`,
    `DRY_RUN`
  - Implement `ConfigActionResult` dataclass with fields: `status`, `path`, `backup_path`,
    `diff`, `exit_code`, `stdout`, `stderr`, `command`
  - Implement `ConfigDiffResult` dataclass with fields: `path`, `has_diff`, `diff`
  - Implement `ConfigValidationResult` dataclass with fields: `path`, `validator_id`,
    `valid`, `exit_code`, `stdout`, `stderr`
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9_

- [ ] 3. Define typed exceptions
  - Implement `ConfigFileNotFoundError` with `path` attribute
  - Implement `ConfigTemplateError` with `missing_variable` attribute
  - Implement `ConfigValidatorNotFoundError` with `validator_id` attribute
  - _Requirements: 2, 3, 4, 6, 7, 8_

- [ ] 4. Implement `ConfigurationManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - _Requirements: 1.7, 2.5, 3.5, 4.5, 7.6, 8.8, 9.6, 10_

- [ ] 5. Build `ValidatorRegistry`
  - Define module-level `VALIDATORS` dict mapping identifier strings to command builder
    callables for: `nginx`, `sshd`, `httpd`, `named`, `rsyslog`, `sudoers`
  - Implement `ConfigurationManager.register_validator(identifier, builder)` class method
    for runtime extension
  - _Requirements: 6_

- [ ] 6. Implement `ConfigurationManager.__init__` and private `_read_file()`
  - Accept `ICommandRunner` and `ConfigurationManagerConfig` as constructor arguments
  - Implement `_read_file(path) -> str` using `cat <path>` via runner; return empty string
    when cat exits non-zero (file not found)
  - _Requirements: 1, 5_

- [ ] 7. Implement private `_compute_diff()`
  - Accept `path`, `current_content`, and `desired_content`
  - Use `difflib.unified_diff` in pure Python to compute unified diff string
  - Return `ConfigDiffResult` with `has_diff` set based on whether diff is non-empty
  - _Requirements: 5_

- [ ] 8. Implement private `_write_file()`
  - Accept `path`, `content`, optional `mode`, optional `owner`, optional `group`
  - Read current file content via `_read_file()`; return `SKIPPED` if content identical
  - Write via `tee <path>` with content as stdin; pass `elevated` flag
  - After successful write: apply `chmod` if `mode` provided; apply `chown` if
    `owner` or `group` provided
  - Return appropriate `ConfigActionResult`
  - _Requirements: 1_

- [ ] 9. Implement `write()`
  - Compute diff in dry-run mode and return `DRY_RUN` result with diff included
  - Delegate to `_write_file()` for all other cases
  - _Requirements: 1_

- [ ] 10. Implement `write_template()`
  - Accept template as string or file path; read from disk via runner if path provided
  - Render using `string.Template.safe_substitute`; detect missing keys and raise
    `ConfigTemplateError` before calling `_write_file()`
  - Honour `dry_run` flag: return `DRY_RUN` result with rendered content
  - Delegate to `_write_file()` for the actual write
  - _Requirements: 2_

- [ ] 11. Implement `diff()`
  - Call `_read_file()` for current content (empty string if file absent)
  - Delegate to `_compute_diff()` and return `ConfigDiffResult`
  - _Requirements: 5_

- [ ] 12. Implement `backup()`
  - Check file exists via `stat`; raise `ConfigFileNotFoundError` if not
  - Generate backup path: `<filepath>.bak.<YYYYMMDDTHHMMSS>` from `datetime.utcnow()`
  - Execute `cp -p <src> <dst>` via runner with `elevated` flag
  - Return `ConfigActionResult` with `backup_path` populated
  - Honour `dry_run` flag; return `DRY_RUN` with computed backup path
  - _Requirements: 3_

- [ ] 13. Implement `restore()`
  - Check backup file exists via `stat`; raise `ConfigFileNotFoundError` if not
  - Derive original path by stripping `.bak.<timestamp>` suffix
  - Execute `cp -p <backup> <original>` via runner with `elevated` flag
  - Return `ConfigActionResult` with restored path
  - Honour `dry_run` flag
  - _Requirements: 4_

- [ ] 14. Implement `validate()`
  - Look up `validator_id` in `VALIDATORS`; raise `ConfigValidatorNotFoundError` if absent
  - Build command from registry callable and path
  - Execute via runner without elevation (read-only)
  - Map exit code 0 to `valid=True`; non-zero to `valid=False`
  - Return `ConfigValidationResult`
  - _Requirements: 6_

- [ ] 15. Implement `set_permissions()`
  - Check file exists; raise `ConfigFileNotFoundError` if not
  - Read current mode via `stat -c "%a" <path>`; return `SKIPPED` if already matches
  - Execute `chmod <mode> <path>` via runner with `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 7_

- [ ] 16. Implement `set_ownership()`
  - Check file exists; raise `ConfigFileNotFoundError` if not
  - Read current owner/group via `stat -c "%U %G" <path>`; return `SKIPPED` if matches
  - Build `chown` argument from user and/or group (handle user-only, group-only, both cases)
  - Execute via runner with `elevated` flag; honour `dry_run` flag
  - _Requirements: 8_

- [ ] 17. Implement `set_ini_key()`
  - Read current file content via `_read_file()`
  - Parse with `configparser`; create section if absent
  - Set key to value; serialise back to string
  - Compute diff; return `SKIPPED` if no change
  - Write via `tee` with `elevated` flag; honour `dry_run` flag with diff in result
  - _Requirements: 9_

- [ ] 18. Implement `remove_ini_key()`
  - Read current file content via `_read_file()`
  - Parse with `configparser`; return `SKIPPED` if key or section absent
  - Remove key; remove section if empty; serialise back to string
  - Write via `tee` with `elevated` flag; honour `dry_run` flag
  - _Requirements: 9_

- [ ] 19. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_configurationmanagement.py`
  - Implement `MockCommandRunner` keyed on command prefix to return configured `RunResult`
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10_

- [ ] 20. Write tests for `diff()` and `_compute_diff()`
  - Test: identical content — `has_diff=False`, empty diff string
  - Test: differing content — `has_diff=True`, non-empty unified diff
  - Test: file absent — treated as empty; all desired lines shown as additions
  - _Requirements: 5_

- [ ] 21. Write tests for `write()`
  - Test: new file — tee command issued with correct content, returns `SUCCESS`
  - Test: existing file, same content — returns `SKIPPED`, no write issued
  - Test: existing file, different content — write issued, returns `SUCCESS`
  - Test: write with mode — chmod called after tee
  - Test: write with owner/group — chown called after tee
  - Test: write failure — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN` with diff, no tee issued
  - _Requirements: 1_

- [ ] 22. Write tests for `write_template()`
  - Test: template string with all variables — rendered and written, returns `SUCCESS`
  - Test: template file path — file read then rendered and written
  - Test: missing variable — raises `ConfigTemplateError` before any write
  - Test: dry-run — returns `DRY_RUN` with rendered content, no write
  - _Requirements: 2_

- [ ] 23. Write tests for `backup()` and `restore()`
  - Test backup: file exists — cp issued with timestamped path, returns `SUCCESS`
    with `backup_path`
  - Test backup: file absent — raises `ConfigFileNotFoundError`
  - Test backup: dry-run — returns `DRY_RUN` with computed backup path
  - Test restore: backup exists — cp to original path, returns `SUCCESS`
  - Test restore: backup absent — raises `ConfigFileNotFoundError`
  - Test restore: dry-run — returns `DRY_RUN`
  - _Requirements: 3, 4_

- [ ] 24. Write tests for `validate()`
  - Test: registered validator, exit 0 — returns `ConfigValidationResult` with `valid=True`
  - Test: registered validator, non-zero exit — returns result with `valid=False` and stderr
  - Test: unknown validator id — raises `ConfigValidatorNotFoundError`
  - Test: validation runs even in dry-run mode
  - _Requirements: 6_

- [ ] 25. Write tests for `set_permissions()` and `set_ownership()`
  - Test chmod: mode differs — chmod issued, returns `SUCCESS`
  - Test chmod: mode already matches — returns `SKIPPED`
  - Test chmod: file absent — raises `ConfigFileNotFoundError`
  - Test chmod: dry-run — returns `DRY_RUN`
  - Test chown: user and group — correct chown arg built
  - Test chown: user only — `chown <user> <path>`
  - Test chown: group only — `chown :<group> <path>`
  - Test chown: already matches — returns `SKIPPED`
  - Test chown: dry-run — returns `DRY_RUN`
  - _Requirements: 7, 8_

- [ ] 26. Write tests for `set_ini_key()` and `remove_ini_key()`
  - Test set: key absent — section and key created, file written
  - Test set: section absent — section created then key added
  - Test set: key already at target value — returns `SKIPPED`
  - Test set: dry-run — returns `DRY_RUN` with diff
  - Test remove: key present — key removed, file written
  - Test remove: section empty after removal — section removed from file
  - Test remove: key absent — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 9_

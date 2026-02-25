# Implementation Plan

- [ ] 1. Create the `securitycompliance` module file
  - Create `source/packages/automojo/sysman/linux/windows/securitycompliance.py`
  - Add module-level docstring: audit and remediate SELinux posture, file permissions,
    SSH daemon configuration, and world-writable/SUID-SGID file presence on Windows Linux
  - _Requirements: 1–10_

- [ ] 2. Define `SELinuxMode` enum
  - Values: `ENFORCING = "enforcing"`, `PERMISSIVE = "permissive"`, `DISABLED = "disabled"`
  - _Requirements: 1, 2_

- [ ] 3. Define `ComplianceSeverity` enum
  - Values: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`
  - _Requirements: 5, 9_

- [ ] 4. Define `SELinuxStatus` dataclass
  - Fields: `mode: SELinuxMode`, `policy: str | None = None`
  - _Requirements: 1_

- [ ] 5. Define `FilePermissionCheck` dataclass
  - Fields: `path`, `expected_mode`, `expected_owner`, `expected_group`
  - _Requirements: 3_

- [ ] 6. Define `FilePermissionResult` dataclass
  - Fields: `path`, `compliant`, `expected_mode`, `expected_owner`, `expected_group`,
    `actual_mode`, `actual_owner`, `actual_group` (all optional), `error` (optional)
  - _Requirements: 3, 4_

- [ ] 7. Define `SSHConfigFinding` dataclass
  - Fields: `setting`, `expected`, `actual` (optional), `compliant`, `severity`
  - _Requirements: 5_

- [ ] 8. Define `ComplianceCheck` dataclass
  - Fields: `check_id`, `description`, `check_type`, `severity`, `expected_value`
    (optional), `file_check` (optional), `setting_key` (optional), `search_path`
    (optional), `allowed_paths` (optional)
  - _Requirements: 9_

- [ ] 9. Define `ComplianceCheckResult` dataclass
  - Fields: `check_id`, `description`, `severity`, `check_type`, `compliant`,
    `finding` (optional), `error` (optional)
  - _Requirements: 9_

- [ ] 10. Define `ComplianceReport` dataclass
  - Fields: `results`, `total`, `compliant`, `non_compliant`, `errored`
  - _Requirements: 9_

- [ ] 11. Define `ComplianceActionResult` dataclass
  - Fields: `success`, `target` (optional), `dry_run` (default `False`),
    `error` (optional)
  - _Requirements: 2, 4, 6_

- [ ] 12. Define `SecurityQueryError` exception class
  - Subclass `Exception`; raised by read-only query methods on command failure
  - _Requirements: 1, 7, 8_

- [ ] 13. Define `SecurityComplianceConfig` dataclass
  - Fields: `dry_run: bool = False`,
    `ssh_config_path: str = "/etc/ssh/sshd_config"`
  - _Requirements: 1–10_

- [ ] 14. Define module-level `_SSH_BASELINE` and `_SSH_BASELINE_SEVERITY` constants
  - `_SSH_BASELINE`: `dict[str, str]` with the 9 default settings listed in the design
  - `_SSH_BASELINE_SEVERITY`: `dict[str, ComplianceSeverity]` with per-setting severity
  - _Requirements: 5_

- [ ] 15. Implement `SecurityComplianceManager.__init__`
  - Accept `file_operator: IFileOperator`, `command_runner: ICommandRunner`,
    `config: SecurityComplianceConfig`
  - Store all three as instance attributes
  - _Requirements: 1–10_

- [ ] 16. Implement `check_selinux() → SELinuxStatus`
  - Run `["getenforce"]` with `elevated=True`; raise `SecurityQueryError` on non-zero exit
  - Map stdout to `SELinuxMode`; raise `SecurityQueryError` on unrecognised value
  - Run `["sestatus"]`; parse for policy name (tolerates failure gracefully, sets
    `policy=None`)
  - Return `SELinuxStatus`
  - _Requirements: 1_

- [ ] 17. Implement `set_selinux_mode(mode: SELinuxMode) → ComplianceActionResult`
  - Return failure result immediately for `SELinuxMode.DISABLED` without running any command
  - Return dry-run result when `config.dry_run=True`
  - Run `["setenforce", "1"]` or `["setenforce", "0"]` with `elevated=True`
  - Return success or failure `ComplianceActionResult`
  - _Requirements: 2, 10_

- [ ] 18. Implement `check_file_permissions(checks) → list[FilePermissionResult]`
  - For each check: run `["stat", "-c", "%a %U %G", path]` with `elevated=True`
  - Parse `"mode owner group"` from stdout; compare against expected values
  - Handle absent-file and command-failure cases returning non-compliant results
  - Return empty list when `checks` is empty
  - _Requirements: 3, 10_

- [ ] 19. Implement `remediate_file_permissions(results) → list[ComplianceActionResult]`
  - Skip compliant results and results with `error` set
  - Return dry-run results when `config.dry_run=True` for each eligible result
  - Run `["chmod", expected_mode, path]` when mode differs, with `elevated=True`
  - Run `["chown", f"{expected_owner}:{expected_group}", path]` when owner/group differs,
    with `elevated=True`
  - Collect `ComplianceActionResult` per path
  - _Requirements: 4, 10_

- [ ] 20. Implement private `_parse_ssh_config(content: str) → dict[str, str]`
  - Split on newlines; skip blank lines and lines starting with `#` after stripping
  - Split each active line on whitespace with `maxsplit=1`; store in dict
  - Return mapping of setting name to value
  - _Requirements: 5_

- [ ] 21. Implement `check_ssh_config(baseline=None, baseline_severity=None) → list[SSHConfigFinding]`
  - Read `config.ssh_config_path` via `file_operator.read_text(..., elevated=True)`; raise
    `SecurityQueryError` on failure
  - Use `_SSH_BASELINE` and `_SSH_BASELINE_SEVERITY` when baseline is `None`
  - Parse file with `_parse_ssh_config`; compare each baseline key to parsed values
  - Return list of `SSHConfigFinding` objects
  - _Requirements: 5, 10_

- [ ] 22. Implement private `_apply_setting_to_lines(lines: list[str], key: str, value: str) → list[str]`
  - Scan for first line matching `^\s*#?\s*<key>\s+`; replace it; return updated list
  - Append `"<key> <value>"` when no match found
  - _Requirements: 6_

- [ ] 23. Implement `apply_ssh_hardening(settings: dict[str, str]) → ComplianceActionResult`
  - Return dry-run result when `config.dry_run=True`
  - Read current `sshd_config` content
  - Apply each setting via `_apply_setting_to_lines`
  - Write updated content via `file_operator.write_text(..., elevated=True)`
  - Run `["systemctl", "restart", "sshd"]` with `elevated=True`
  - Return success or failure `ComplianceActionResult`
  - _Requirements: 6, 10_

- [ ] 24. Implement private `_run_find(args: list[str]) → list[str]`
  - Run command with `elevated=True`; raise `SecurityQueryError` on non-zero exit
  - Return non-empty stripped lines from stdout
  - _Requirements: 7, 8_

- [ ] 25. Implement `find_world_writable_files(search_paths: list[str]) → list[str]`
  - For each path: call `_run_find(["find", path, "-xdev", "-type", "f", "-perm", "-0002"])`
  - Collect all results; sort and deduplicate; return
  - _Requirements: 7, 10_

- [ ] 26. Implement `find_suid_sgid_files(search_paths: list[str]) → list[str]`
  - For each path: call `_run_find(["find", path, "-xdev", "(", "-perm", "-4000",
    "-o", "-perm", "-2000", ")", "-type", "f"])`
  - Collect all results; sort and deduplicate; return
  - _Requirements: 8, 10_

- [ ] 27. Implement private `_run_compliance_check(check: ComplianceCheck) → ComplianceCheckResult`
  - Dispatch on `check.check_type`; call appropriate method; derive `compliant` and
    `finding` string; return `ComplianceCheckResult`
  - Catch all exceptions; return result with `error` set and `compliant=False`
  - _Requirements: 9_

- [ ] 28. Implement `run_compliance_checks(checks) → ComplianceReport`
  - Call `_run_compliance_check` for each check in order
  - Count `compliant`, `non_compliant` (compliant=False, error=None), `errored`
    (error set) from results
  - Return `ComplianceReport`
  - _Requirements: 9_

- [ ] 29. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_securitycompliance.py`
  - Implement `MockCommandRunner` with per-prefix `(exit_code, stdout, stderr)` config
    and call recording
  - Implement `MockFileOperator` with configurable `read_text` return and `write_text`
    call recording
  - Define helpers: `make_file_check(...)`, `make_compliance_check(type, ...)`
  - _Requirements: 1–10_

- [ ] 30. Write tests for `check_selinux`
  - Test: `getenforce` returns `"Enforcing"` → `mode=ENFORCING`
  - Test: `"Permissive"` → `mode=PERMISSIVE`; `"Disabled"` → `mode=DISABLED`
  - Test: `sestatus` output parsed for policy name; stored in `SELinuxStatus.policy`
  - Test: `getenforce` non-zero exit → `SecurityQueryError` raised
  - Test: `sestatus` failure → `policy=None`, no exception
  - _Requirements: 1_

- [ ] 31. Write tests for `set_selinux_mode`
  - Test: `ENFORCING` → `setenforce 1` called with `elevated=True`
  - Test: `PERMISSIVE` → `setenforce 0` called with `elevated=True`
  - Test: `DISABLED` → no command called, `success=False` result returned
  - Test: command failure → `success=False`, error text in result
  - Test: dry-run → zero commands, `dry_run=True` result
  - _Requirements: 2_

- [ ] 32. Write tests for `check_file_permissions`
  - Test: `stat` returns matching mode/owner/group → `compliant=True`
  - Test: `stat` returns differing mode → `compliant=False`, actual values populated
  - Test: `stat` returns "No such file" → `compliant=False`, error set
  - Test: empty input list → empty result list
  - Test: multiple checks → one result per check in order
  - _Requirements: 3_

- [ ] 33. Write tests for `remediate_file_permissions`
  - Test: compliant result skipped (no commands run)
  - Test: result with `error` set skipped (no commands run)
  - Test: mode mismatch → `chmod` called with correct args and `elevated=True`
  - Test: owner/group mismatch → `chown` called with correct args
  - Test: both mode and ownership mismatch → both `chmod` and `chown` called
  - Test: command failure → `success=False` in result
  - Test: dry-run → zero commands, `dry_run=True` results for each eligible path
  - _Requirements: 4_

- [ ] 34. Write tests for `check_ssh_config`
  - Test: active setting matching baseline → `compliant=True`
  - Test: active setting differing from baseline → `compliant=False`, `actual` set
  - Test: setting absent → `compliant=False`, `actual=None`
  - Test: setting commented out → treated as absent (`compliant=False`)
  - Test: custom baseline used when provided
  - Test: `SecurityQueryError` raised when `read_text` fails
  - _Requirements: 5_

- [ ] 35. Write tests for `apply_ssh_hardening`
  - Test: active setting replaced in-place (correct key, new value in written content)
  - Test: commented-out setting replaced (first comment-match replaced)
  - Test: absent setting appended to end of file
  - Test: `systemctl restart sshd` called with `elevated=True` after write
  - Test: write failure → `success=False`, no `systemctl` call
  - Test: dry-run → zero `write_text` calls, zero commands, `dry_run=True`
  - _Requirements: 6_

- [ ] 36. Write tests for `find_world_writable_files`
  - Test: single path with results → sorted list returned
  - Test: multiple paths → results merged, sorted, deduplicated
  - Test: no results → empty list
  - Test: `find` non-zero exit → `SecurityQueryError` raised
  - _Requirements: 7_

- [ ] 37. Write tests for `find_suid_sgid_files`
  - Test: single path with results → sorted list returned
  - Test: multiple paths → merged, sorted, deduplicated
  - Test: no results → empty list
  - Test: `find` non-zero exit → `SecurityQueryError` raised
  - _Requirements: 8_

- [ ] 38. Write tests for `run_compliance_checks`
  - Test: `selinux_mode` check — compliant when mode matches; finding set when it differs
  - Test: `file_permission` check — delegates to `check_file_permissions`; compliant flag propagated
  - Test: `ssh_setting` check — delegates to `check_ssh_config`; compliant flag propagated
  - Test: `no_world_writable` check — compliant when find returns empty; finding with count when not
  - Test: `no_unexpected_suid` check — compliant when all found paths are in `allowed_paths`
  - Test: exception during check → result has `error` set, `compliant=False`
  - Test: summary counts correct for mixed results
  - _Requirements: 9_

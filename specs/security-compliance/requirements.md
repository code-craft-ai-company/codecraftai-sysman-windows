# Requirements

## Introduction

The security compliance feature provides the `automojo-sysman-linux-windows` package with
the ability to audit and remediate common security posture concerns on Windows Linux
systems.  It covers five areas: SELinux mode management, file permission auditing and
remediation, SSH daemon configuration hardening, discovery of world-writable and
SUID/SGID files, and an extensible compliance check runner that produces a structured
`ComplianceReport`.

A maintenance coordinator uses this feature to query the current security state, compare
it against a desired baseline, apply remediations, and obtain a machine-readable report
of findings.  All file reads and writes are performed through the
`automojo-interfaces` file-operator abstraction.  All commands (`getenforce`, `sestatus`,
`setenforce`, `stat`, `chmod`, `chown`, `find`, `systemctl`) are executed through the
`automojo-interfaces` command-runner abstraction.  No shell is invoked directly, and all
write or mutating operations respect dry-run mode.

---

## Requirements

### Requirement 1: Query SELinux Status

**User Story:** As a maintenance coordinator, I want to query the current SELinux
enforcement mode and active policy, so that I can determine whether the system meets
the required SELinux posture before applying further changes.

#### Acceptance Criteria

1. WHEN the coordinator calls `check_selinux` THE SYSTEM SHALL run `getenforce` to
   determine the current runtime mode.
2. WHEN `getenforce` returns `"Enforcing"` THE SYSTEM SHALL set `mode=SELinuxMode.ENFORCING`
   on the returned `SELinuxStatus`.
3. WHEN `getenforce` returns `"Permissive"` THE SYSTEM SHALL set
   `mode=SELinuxMode.PERMISSIVE`.
4. WHEN `getenforce` returns `"Disabled"` THE SYSTEM SHALL set `mode=SELinuxMode.DISABLED`.
5. WHEN `sestatus` is available THE SYSTEM SHALL also run it and extract the active policy
   name (e.g. `"targeted"`) from the `SELinuxfs mount` or `Policy from config file` line
   and store it in `SELinuxStatus.policy`.
6. WHEN either command fails THE SYSTEM SHALL raise `SecurityQueryError` with the
   captured error text.

---

### Requirement 2: Set SELinux Mode

**User Story:** As a maintenance coordinator, I want to set the runtime SELinux mode to
enforcing or permissive, so that I can enforce or relax policy without rebooting.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_selinux_mode` with `SELinuxMode.ENFORCING` THE SYSTEM
   SHALL run `setenforce 1` with elevated privileges.
2. WHEN the coordinator calls `set_selinux_mode` with `SELinuxMode.PERMISSIVE` THE SYSTEM
   SHALL run `setenforce 0` with elevated privileges.
3. WHEN the coordinator calls `set_selinux_mode` with `SELinuxMode.DISABLED` THE SYSTEM
   SHALL return a `ComplianceActionResult` with `success=False` and an error message
   explaining that disabling SELinux requires a reboot and configuration file change, and
   SHALL NOT run any command.
4. WHEN the command succeeds THE SYSTEM SHALL return a `ComplianceActionResult` with
   `success=True`.
5. WHEN the command fails THE SYSTEM SHALL return a `ComplianceActionResult` with
   `success=False` and the captured error text.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT run any command and SHALL return a
   `ComplianceActionResult` with `success=True` and `dry_run=True`.

---

### Requirement 3: Check File Permissions

**User Story:** As a maintenance coordinator, I want to audit a list of files against
expected permission modes, owners, and groups, so that I can identify files whose
permissions have drifted from the secure baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `check_file_permissions` with a list of
   `FilePermissionCheck` objects THE SYSTEM SHALL run `stat -c "%a %U %G" <path>` for
   each path and compare the result against `expected_mode`, `expected_owner`, and
   `expected_group`.
2. WHEN all three values match THE SYSTEM SHALL return a `FilePermissionResult` with
   `compliant=True` for that path.
3. WHEN any value differs THE SYSTEM SHALL return a `FilePermissionResult` with
   `compliant=False` and the actual values populated.
4. WHEN the file does not exist THE SYSTEM SHALL return a `FilePermissionResult` with
   `compliant=False` and `error` set to indicate the file was not found.
5. WHEN `stat` fails for a reason other than absence THE SYSTEM SHALL return a
   `FilePermissionResult` with `compliant=False` and the error text captured.
6. WHEN the coordinator provides an empty list THE SYSTEM SHALL return an empty list.

---

### Requirement 4: Remediate File Permissions

**User Story:** As a maintenance coordinator, I want to remediate non-compliant file
permissions discovered by `check_file_permissions`, so that files are returned to their
expected secure state.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate_file_permissions` with a list of
   `FilePermissionResult` objects THE SYSTEM SHALL skip any result where
   `compliant=True` or `error` is set (file absent or unreadable).
2. WHEN a result has a non-matching mode THE SYSTEM SHALL run
   `chmod <expected_mode> <path>` with elevated privileges.
3. WHEN a result has a non-matching owner or group THE SYSTEM SHALL run
   `chown <expected_owner>:<expected_group> <path>` with elevated privileges.
4. WHEN both mode and ownership are non-matching THE SYSTEM SHALL run both `chmod` and
   `chown`.
5. WHEN all commands for a path succeed THE SYSTEM SHALL include a
   `ComplianceActionResult` with `success=True` and the path as `target`.
6. WHEN any command for a path fails THE SYSTEM SHALL include a
   `ComplianceActionResult` with `success=False`, the path as `target`, and the error
   text.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT run any command and SHALL return a
   `ComplianceActionResult` with `success=True`, `dry_run=True`, and `target` set for
   each non-compliant, non-errored result.

---

### Requirement 5: Check SSH Configuration

**User Story:** As a maintenance coordinator, I want to audit the SSH daemon
configuration against a hardening baseline, so that I can identify settings that deviate
from the expected secure values.

#### Acceptance Criteria

1. WHEN the coordinator calls `check_ssh_config` THE SYSTEM SHALL read
   `/etc/ssh/sshd_config` (or the path in `config.ssh_config_path`) using the file
   operator.
2. WHEN the coordinator provides a `baseline` mapping of `{setting: expected_value}` THE
   SYSTEM SHALL check each key against the file.
3. WHEN the coordinator does not provide a `baseline` THE SYSTEM SHALL use the built-in
   default hardening baseline (see design for the default set).
4. WHEN a setting is found in the file with the expected value THE SYSTEM SHALL return an
   `SSHConfigFinding` with `compliant=True`.
5. WHEN a setting is found with a different value THE SYSTEM SHALL return an
   `SSHConfigFinding` with `compliant=False` and `actual` populated.
6. WHEN a setting is not present in the file (or is commented out) THE SYSTEM SHALL
   return an `SSHConfigFinding` with `compliant=False` and `actual=None`.
7. WHEN the file cannot be read THE SYSTEM SHALL raise `SecurityQueryError`.

---

### Requirement 6: Apply SSH Hardening

**User Story:** As a maintenance coordinator, I want to write hardened SSH daemon
settings to the configuration file and restart the daemon, so that SSH is secured
according to the desired baseline without a full configuration file replacement.

#### Acceptance Criteria

1. WHEN the coordinator calls `apply_ssh_hardening` with a `settings` mapping THE SYSTEM
   SHALL read the current `sshd_config` file content.
2. WHEN a setting key already exists in the file (even if commented out) THE SYSTEM SHALL
   replace that line (or the last commented-out occurrence) with the new `key value` line.
3. WHEN a setting key is not present THE SYSTEM SHALL append it at the end of the file.
4. WHEN all modifications are collected THE SYSTEM SHALL write the updated content back
   to `sshd_config` using the file operator with elevated privileges.
5. WHEN the write succeeds THE SYSTEM SHALL run `systemctl restart sshd` with elevated
   privileges.
6. WHEN the write or restart succeeds THE SYSTEM SHALL return a `ComplianceActionResult`
   with `success=True`.
7. WHEN any step fails THE SYSTEM SHALL return a `ComplianceActionResult` with
   `success=False` and the error text.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file or run any command and
   SHALL return a `ComplianceActionResult` with `success=True` and `dry_run=True`.

---

### Requirement 7: Find World-Writable Files

**User Story:** As a maintenance coordinator, I want to find world-writable files under
specified paths, so that I can identify files that any user can modify, which may
represent a security risk.

#### Acceptance Criteria

1. WHEN the coordinator calls `find_world_writable_files` with a list of search paths
   THE SYSTEM SHALL run `find <path> -xdev -type f -perm -0002` for each path with
   elevated privileges.
2. WHEN files are found THE SYSTEM SHALL return their absolute paths as a sorted list of
   strings with duplicates removed.
3. WHEN no files are found THE SYSTEM SHALL return an empty list.
4. WHEN any `find` command fails THE SYSTEM SHALL raise `SecurityQueryError` with the
   captured error text.

---

### Requirement 8: Find SUID and SGID Files

**User Story:** As a maintenance coordinator, I want to find files with the SUID or SGID
bit set under specified paths, so that I can audit binaries that run with elevated
privileges.

#### Acceptance Criteria

1. WHEN the coordinator calls `find_suid_sgid_files` with a list of search paths THE
   SYSTEM SHALL run `find <path> -xdev \( -perm -4000 -o -perm -2000 \) -type f` for
   each path with elevated privileges.
2. WHEN files are found THE SYSTEM SHALL return their absolute paths as a sorted list of
   strings with duplicates removed.
3. WHEN no files are found THE SYSTEM SHALL return an empty list.
4. WHEN any `find` command fails THE SYSTEM SHALL raise `SecurityQueryError` with the
   captured error text.

---

### Requirement 9: Run a Compliance Check Suite

**User Story:** As a maintenance coordinator, I want to run a defined set of compliance
checks and receive a structured report, so that I can assess the system's overall
security posture in a single operation.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_compliance_checks` with a list of `ComplianceCheck`
   objects THE SYSTEM SHALL execute each check in order and collect a
   `ComplianceCheckResult` for each.
2. WHEN a check of `type="selinux_mode"` is executed THE SYSTEM SHALL call
   `check_selinux` and compare the returned mode against `check.expected_value`.
3. WHEN a check of `type="file_permission"` is executed THE SYSTEM SHALL call
   `check_file_permissions` with the single `FilePermissionCheck` embedded in the
   `ComplianceCheck`.
4. WHEN a check of `type="ssh_setting"` is executed THE SYSTEM SHALL call
   `check_ssh_config` with a single-entry baseline and return the first `SSHConfigFinding`
   as the result.
5. WHEN a check of `type="no_world_writable"` is executed THE SYSTEM SHALL call
   `find_world_writable_files` on `check.search_path`; the check is compliant when the
   returned list is empty.
6. WHEN a check of `type="no_unexpected_suid"` is executed THE SYSTEM SHALL call
   `find_suid_sgid_files` on `check.search_path`; the check is compliant when the
   returned list is a subset of `check.allowed_paths`.
7. WHEN a check raises an exception THE SYSTEM SHALL record the exception message in
   `ComplianceCheckResult.error` and mark the check as non-compliant.
8. WHEN all checks are evaluated THE SYSTEM SHALL return a `ComplianceReport` containing
   the list of results and summary counts (`total`, `compliant`, `non_compliant`,
   `errored`).

---

### Requirement 10: Elevation

**User Story:** As a maintenance coordinator, I want all mutating commands and file
writes to use elevated privileges, so that the feature can operate on system-owned files
without manual sudo escalation.

#### Acceptance Criteria

1. WHEN running `setenforce`, `chmod`, `chown`, `find`, or `systemctl restart sshd` THE
   SYSTEM SHALL pass `elevated=True` to the command runner.
2. WHEN writing `sshd_config` THE SYSTEM SHALL pass `elevated=True` to the file operator.
3. WHEN reading `sshd_config` or `stat` output THE SYSTEM SHALL pass `elevated=True` to
   the command runner and file operator respectively.
4. WHEN dry-run mode is active elevation parameters SHALL still be specified but no
   elevated operation SHALL be performed.

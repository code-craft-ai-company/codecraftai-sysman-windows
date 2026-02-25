# Requirements

## Introduction

The configuration management feature provides the `automojo-sysman-linux-windows` package
with the ability to write, update, backup, restore, diff, and validate configuration files
on Windows Linux systems.  It supports common on-disk formats used by system software:
INI/key-value, JSON, YAML, and plain text rendered from templates.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* files to manage
— it provides the capability to *do it* reliably, idempotently, and with full auditability.
All file system mutations are executed through the `automojo-interfaces` command-runner
abstraction so that dry-run simulation, auditing, and test mocking apply uniformly across
all operations.

---

## Requirements

### Requirement 1: Write a Configuration File from Content

**User Story:** As a maintenance coordinator, I want to write a configuration file to a
specified path with specific content, so that a system service has the exact configuration
it requires.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path and content string THE SYSTEM SHALL write the
   content to the path via the command runner.
2. WHEN the file already exists and its content is identical to the supplied content THE
   SYSTEM SHALL skip the write and return a `ConfigActionResult` with status `SKIPPED`.
3. WHEN the file does not exist or its content differs THE SYSTEM SHALL write the new
   content and return a `ConfigActionResult` with status `SUCCESS`.
4. WHEN the coordinator specifies file permissions THE SYSTEM SHALL apply the given
   permission mode to the file after writing.
5. WHEN the coordinator specifies file ownership (user and/or group) THE SYSTEM SHALL apply
   the given ownership to the file after writing.
6. WHEN the write operation fails THE SYSTEM SHALL return a `ConfigActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `ConfigActionResult` with status `DRY_RUN` including the diff of what would change.

---

### Requirement 2: Write a Configuration File from a Template

**User Story:** As a maintenance coordinator, I want to render a configuration file from a
template and a set of variables, so that configuration can be generated dynamically for
each target system without hard-coded values.

#### Acceptance Criteria

1. WHEN the coordinator provides a template string and a variables dictionary THE SYSTEM
   SHALL render the template using Python's `string.Template` substitution.
2. WHEN the coordinator provides a template file path THE SYSTEM SHALL read the template
   file from disk and render it with the supplied variables dictionary.
3. WHEN a required template variable is missing from the dictionary THE SYSTEM SHALL raise
   a `ConfigTemplateError` with the name of the missing variable before writing any file.
4. WHEN the template renders successfully THE SYSTEM SHALL write the result to the target
   path following all criteria in Requirement 1.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `ConfigActionResult` with status `DRY_RUN` including the rendered content that would
   have been written.

---

### Requirement 3: Backup a Configuration File

**User Story:** As a maintenance coordinator, I want to create a timestamped backup of a
configuration file before modifying it, so that the original configuration can be restored
if the change causes problems.

#### Acceptance Criteria

1. WHEN the coordinator requests a backup of a file path THE SYSTEM SHALL copy the file to
   `<filepath>.bak.<ISO8601-timestamp>` via the command runner.
2. WHEN the source file does not exist THE SYSTEM SHALL raise a `ConfigFileNotFoundError`
   before issuing any command.
3. WHEN the backup succeeds THE SYSTEM SHALL return a `ConfigActionResult` with status
   `SUCCESS` and the full path of the backup file in the result.
4. WHEN the backup fails THE SYSTEM SHALL return a `ConfigActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT copy any file and SHALL return a
   `ConfigActionResult` with status `DRY_RUN` and the backup path that would have been
   created.

---

### Requirement 4: Restore a Configuration File from Backup

**User Story:** As a maintenance coordinator, I want to restore a configuration file from
a specified backup path, so that a system can be returned to a known-good state after a
failed maintenance operation.

#### Acceptance Criteria

1. WHEN the coordinator provides a backup file path THE SYSTEM SHALL copy the backup file
   to the original path (with the `.bak.<timestamp>` suffix stripped) via the command
   runner.
2. WHEN the backup file path does not exist THE SYSTEM SHALL raise a
   `ConfigFileNotFoundError` before issuing any command.
3. WHEN the restore succeeds THE SYSTEM SHALL return a `ConfigActionResult` with status
   `SUCCESS` and the path of the restored file.
4. WHEN the restore fails THE SYSTEM SHALL return a `ConfigActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT copy any file and SHALL return a
   `ConfigActionResult` with status `DRY_RUN`.

---

### Requirement 5: Diff Current vs. Desired Configuration

**User Story:** As a maintenance coordinator, I want to compare the current content of a
configuration file on disk with a desired content string, so that I can determine whether
a write is necessary before committing any changes.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path and a desired content string THE SYSTEM SHALL
   read the current file content via the command runner and compute a unified diff.
2. WHEN the file does not exist THE SYSTEM SHALL treat the current content as empty and
   produce a diff showing all lines as additions.
3. WHEN the file content matches the desired content exactly THE SYSTEM SHALL return a
   `ConfigDiffResult` with `has_diff=False` and an empty diff string.
4. WHEN the file content differs from the desired content THE SYSTEM SHALL return a
   `ConfigDiffResult` with `has_diff=True` and the unified diff string.

---

### Requirement 6: Validate Configuration File Syntax

**User Story:** As a maintenance coordinator, I want to validate the syntax of a
configuration file using the relevant tool for its service type, so that a broken
configuration is detected before it is applied and causes a service outage.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path and a validator identifier THE SYSTEM SHALL
   invoke the corresponding validation command via the command runner.
2. WHEN the validator exits with code 0 THE SYSTEM SHALL return a `ConfigValidationResult`
   with `valid=True` and the captured stdout.
3. WHEN the validator exits with a non-zero code THE SYSTEM SHALL return a
   `ConfigValidationResult` with `valid=False`, the exit code, and the captured stderr.
4. WHEN the validator identifier is not registered THE SYSTEM SHALL raise a
   `ConfigValidatorNotFoundError`.
5. WHEN dry-run mode is active THE SYSTEM SHALL still execute the validator, as validation
   is read-only and safe to run at any time.

#### Built-in Validators

| Identifier | Command |
|---|---|
| `nginx` | `nginx -t -c <filepath>` |
| `sshd` | `sshd -t -f <filepath>` |
| `httpd` | `httpd -t -f <filepath>` |
| `named` | `named-checkconf <filepath>` |
| `rsyslog` | `rsyslogd -N1 -f <filepath>` |
| `sudoers` | `visudo -c -f <filepath>` |

---

### Requirement 7: Set File Permissions

**User Story:** As a maintenance coordinator, I want to set the permission mode on a
configuration file independently of writing its content, so that file permissions can be
corrected during a maintenance pass without modifying file content.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path and an octal permission mode THE SYSTEM SHALL
   invoke `chmod <mode> <filepath>` via the command runner.
2. WHEN the file does not exist THE SYSTEM SHALL raise a `ConfigFileNotFoundError`.
3. WHEN the current permission mode already matches the requested mode THE SYSTEM SHALL skip
   execution and return a `ConfigActionResult` with status `SKIPPED`.
4. WHEN the chmod command succeeds THE SYSTEM SHALL return a `ConfigActionResult` with
   status `SUCCESS`.
5. WHEN the chmod command fails THE SYSTEM SHALL return a `ConfigActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ConfigActionResult` with status `DRY_RUN`.

---

### Requirement 8: Set File Ownership

**User Story:** As a maintenance coordinator, I want to set the owner and group of a
configuration file independently of writing its content, so that ownership can be corrected
during a maintenance pass without modifying file content.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path and a user and/or group THE SYSTEM SHALL
   invoke `chown <user>:<group> <filepath>` via the command runner.
2. WHEN only a user is provided THE SYSTEM SHALL invoke `chown <user> <filepath>`.
3. WHEN only a group is provided THE SYSTEM SHALL invoke `chown :<group> <filepath>`.
4. WHEN the file does not exist THE SYSTEM SHALL raise a `ConfigFileNotFoundError`.
5. WHEN the current ownership already matches the requested ownership THE SYSTEM SHALL skip
   execution and return a `ConfigActionResult` with status `SKIPPED`.
6. WHEN the chown command succeeds THE SYSTEM SHALL return a `ConfigActionResult` with
   status `SUCCESS`.
7. WHEN the chown command fails THE SYSTEM SHALL return a `ConfigActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `ConfigActionResult` with status `DRY_RUN`.

---

### Requirement 9: Manage INI-style Key-Value Entries

**User Story:** As a maintenance coordinator, I want to set, update, or remove a specific
key-value entry within an INI-style configuration file without replacing the entire file,
so that targeted configuration changes can be made while preserving all other settings.

#### Acceptance Criteria

1. WHEN the coordinator provides a file path, section, key, and value THE SYSTEM SHALL read
   the file, update the specified key in the specified section, and write the result back
   via the command runner.
2. WHEN the section does not exist THE SYSTEM SHALL create it before adding the key.
3. WHEN the key already holds the target value THE SYSTEM SHALL skip the write and return a
   `ConfigActionResult` with status `SKIPPED`.
4. WHEN the coordinator requests removal of a key THE SYSTEM SHALL remove the key from the
   section and write the result back, removing the section if it becomes empty.
5. WHEN the write succeeds THE SYSTEM SHALL return a `ConfigActionResult` with status
   `SUCCESS`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `ConfigActionResult` with status `DRY_RUN` with the diff of what would change.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all configuration file write commands
to run with the privileges required to modify system configuration files, so that writes to
protected paths such as `/etc/` succeed on a standard system where the process does not run
as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (write, backup, restore, chmod, chown,
   INI key set/remove).
2. WHEN performing read-only operations (diff, validate, read current permissions) THE
   SYSTEM SHALL NOT use elevation.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.

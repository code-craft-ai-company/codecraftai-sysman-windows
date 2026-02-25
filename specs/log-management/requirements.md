# Requirements

## Introduction

The log management feature provides the `automojo-sysman-linux-windows` package with the
ability to inspect and manage logs on a Windows Linux system.  This covers querying the
`systemd` journal via `journalctl`, reading and rotating traditional log files, managing
`logrotate` configuration drop-ins, and vacuuming old journal data to reclaim disk space.

A maintenance coordinator drives all operations by calling into this feature's public
interfaces.  The feature does not decide *which* logs to inspect or rotate — it provides
the primitives to do so reliably and with full auditability.  All commands are executed
through the `automojo-interfaces` command-runner abstraction and all file operations go
through the file-operator abstraction, enabling dry-run simulation, auditing, and test
mocking without requiring a live system.

---

## Requirements

### Requirement 1: Query Journal Entries

**User Story:** As a maintenance coordinator, I want to query the systemd journal for log
entries matching a set of filters, so that I can inspect recent system events or diagnose
problems as part of a maintenance workflow.

#### Acceptance Criteria

1. WHEN the coordinator provides query parameters THE SYSTEM SHALL invoke
   `journalctl` with the appropriate flags via the command runner and return a
   `JournalQueryResult` containing the raw output lines.
2. WHEN `unit` is specified THE SYSTEM SHALL add `--unit=<unit>` to the command.
3. WHEN `since` is specified THE SYSTEM SHALL add `--since=<since>` to the command.
4. WHEN `until` is specified THE SYSTEM SHALL add `--until=<until>` to the command.
5. WHEN `priority` is specified THE SYSTEM SHALL add `--priority=<priority>` to the
   command (accepts `emerg`, `alert`, `crit`, `err`, `warning`, `notice`, `info`,
   `debug`).
6. WHEN `lines` is specified THE SYSTEM SHALL add `--lines=<n>` to the command.
7. WHEN `grep` is specified THE SYSTEM SHALL add `--grep=<pattern>` to the command.
8. WHEN `output_format` is specified THE SYSTEM SHALL add `--output=<format>` to the
   command (accepts `short`, `json`, `cat`, `verbose`).
9. WHEN the command exits with code 0 THE SYSTEM SHALL return a `JournalQueryResult`
   with `success=True` and the captured stdout split into a list of lines.
10. WHEN the command exits non-zero THE SYSTEM SHALL return a `JournalQueryResult` with
    `success=False` and the captured stderr.

---

### Requirement 2: Query Journal Disk Usage

**User Story:** As a maintenance coordinator, I want to query the total disk space used
by the systemd journal, so that I can decide whether vacuuming is needed.

#### Acceptance Criteria

1. WHEN the coordinator requests journal disk usage THE SYSTEM SHALL invoke
   `journalctl --disk-usage` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL parse the output and return a
   `JournalDiskUsage` record containing the human-readable size string and the size in
   bytes.
3. WHEN the command fails THE SYSTEM SHALL raise a `LogManagementError` with the exit
   code and captured stderr.

---

### Requirement 3: Vacuum Journal by Size

**User Story:** As a maintenance coordinator, I want to limit the total disk space used
by the systemd journal to a maximum size, so that journal data does not consume excessive
storage.

#### Acceptance Criteria

1. WHEN the coordinator provides a maximum size string (e.g. `"500M"`, `"2G"`) THE
   SYSTEM SHALL invoke `journalctl --vacuum-size=<size>` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `LogActionResult` with status
   `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `LogActionResult` with status
   `FAILED`, the exit code, and captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `LogActionResult` with status `DRY_RUN`.

---

### Requirement 4: Vacuum Journal by Time

**User Story:** As a maintenance coordinator, I want to remove journal entries older than
a specified retention period, so that historical data beyond the retention window does
not consume storage.

#### Acceptance Criteria

1. WHEN the coordinator provides a retention period string (e.g. `"30d"`, `"2weeks"`)
   THE SYSTEM SHALL invoke `journalctl --vacuum-time=<period>` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `LogActionResult` with status
   `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `LogActionResult` with status
   `FAILED`, the exit code, and captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `LogActionResult` with status `DRY_RUN`.

---

### Requirement 5: Read a Log File

**User Story:** As a maintenance coordinator, I want to read the last N lines of a
traditional log file, so that I can inspect recent entries without transferring the
entire file.

#### Acceptance Criteria

1. WHEN the coordinator provides an absolute file path and a line count THE SYSTEM SHALL
   invoke `tail --lines=<n> <path>` via the command runner and return the output lines.
2. WHEN the file does not exist THE SYSTEM SHALL return a `LogFileResult` with
   `success=False` and a descriptive error message.
3. WHEN the command succeeds THE SYSTEM SHALL return a `LogFileResult` with `success=True`
   and the captured lines.
4. WHEN the command fails for any other reason THE SYSTEM SHALL return a `LogFileResult`
   with `success=False` and the captured stderr.

---

### Requirement 6: Search a Log File

**User Story:** As a maintenance coordinator, I want to search a traditional log file for
lines matching a pattern, so that I can find specific events without reading the entire
file.

#### Acceptance Criteria

1. WHEN the coordinator provides an absolute file path and a search pattern THE SYSTEM
   SHALL invoke `grep --extended-regexp <pattern> <path>` via the command runner.
2. WHEN `context_lines` is specified THE SYSTEM SHALL add `--context=<n>` to the grep
   command.
3. WHEN `max_matches` is specified THE SYSTEM SHALL add `--max-count=<n>` to the grep
   command.
4. WHEN the command exits with code 0 (matches found) THE SYSTEM SHALL return a
   `LogFileResult` with `success=True` and the captured lines.
5. WHEN grep exits with code 1 (no matches) THE SYSTEM SHALL return a `LogFileResult`
   with `success=True` and an empty lines list.
6. WHEN grep exits with code 2 (error) THE SYSTEM SHALL return a `LogFileResult` with
   `success=False` and the captured stderr.

---

### Requirement 7: Rotate a Log File

**User Story:** As a maintenance coordinator, I want to trigger an immediate logrotate
run for a specific configuration file, so that logs are rotated as part of the
maintenance window without waiting for the scheduled cron job.

#### Acceptance Criteria

1. WHEN the coordinator provides a logrotate configuration file path THE SYSTEM SHALL
   invoke `logrotate --force <config_path>` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `LogActionResult` with status
   `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `LogActionResult` with status
   `FAILED`, the exit code, and captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL invoke `logrotate --debug <config_path>`
   instead and return a `LogActionResult` with status `DRY_RUN` and the debug output in
   `detail`.

---

### Requirement 8: Write a logrotate Drop-In Configuration

**User Story:** As a maintenance coordinator, I want to write a logrotate configuration
block for a specific log path to a drop-in file under `/etc/logrotate.d/`, so that the
log is rotated according to the desired policy on subsequent logrotate runs.

#### Acceptance Criteria

1. WHEN the coordinator provides a drop-in name and a `LogrotateConfig` object THE
   SYSTEM SHALL write the rendered configuration block to
   `/etc/logrotate.d/<name>`.
2. WHEN the file already exists with identical content THE SYSTEM SHALL skip the write
   and return a `LogActionResult` with status `SKIPPED`.
3. WHEN the file does not exist or the content differs THE SYSTEM SHALL write the file
   and return a `LogActionResult` with status `SUCCESS`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the file and SHALL return a
   `LogActionResult` with status `DRY_RUN`.
5. WHEN elevation is configured THE SYSTEM SHALL pass `elevated=True` to the file
   operator.

---

### Requirement 9: Remove a logrotate Drop-In Configuration

**User Story:** As a maintenance coordinator, I want to remove a logrotate configuration
drop-in file, so that a log path is no longer rotated by the system logrotate job.

#### Acceptance Criteria

1. WHEN the coordinator provides a drop-in name THE SYSTEM SHALL delete
   `/etc/logrotate.d/<name>` using the file operator.
2. WHEN the file does not exist THE SYSTEM SHALL return a `LogActionResult` with status
   `SKIPPED`.
3. WHEN the deletion succeeds THE SYSTEM SHALL return a `LogActionResult` with status
   `SUCCESS`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT delete any file and SHALL return a
   `LogActionResult` with status `DRY_RUN`.

---

### Requirement 10: Query logrotate Drop-In State

**User Story:** As a maintenance coordinator, I want to check whether a logrotate drop-in
configuration exists and retrieve its content, so that I can make conditional decisions
about whether to write or update it.

#### Acceptance Criteria

1. WHEN the coordinator provides a drop-in name THE SYSTEM SHALL check whether
   `/etc/logrotate.d/<name>` exists using the file operator.
2. WHEN the file exists THE SYSTEM SHALL return a `LogrotateDropInState` with
   `exists=True` and the file content.
3. WHEN the file does not exist THE SYSTEM SHALL return a `LogrotateDropInState` with
   `exists=False` and `content=None`.

---

### Requirement 11: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want journal vacuum commands and
logrotate file writes to run with the privileges required by those tools, so that
operations succeed on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass
   `elevated=True` to the command runner for `journalctl --vacuum-size`,
   `journalctl --vacuum-time`, and `logrotate --force`.
2. WHEN performing read-only operations (journal query, disk usage, log file reads,
   logrotate drop-in queries) THE SYSTEM SHALL NOT use elevation.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the command runner to surface the resulting permission error.

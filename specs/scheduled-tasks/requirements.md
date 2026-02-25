# Requirements

## Introduction

The scheduled tasks feature provides the `automojo-sysman-linux-windows` package with the
ability to manage recurring and one-time task schedules on Windows Linux systems.  Two
scheduling mechanisms are supported: **cron jobs** (entries written to drop-in files
under `/etc/cron.d/`) and **systemd timers** (`.timer` + `.service` unit-file pairs
managed under `/etc/systemd/system/`).

A maintenance coordinator uses this feature to install, remove, enable, disable, list,
and query scheduled tasks without needing to know the exact file paths or `systemctl`
commands involved.  All file writes are performed through the `automojo-interfaces`
file-operator abstraction.  All runtime commands (`systemctl`) are executed through the
`automojo-interfaces` command-runner abstraction.  The feature does not invoke a shell
directly, and all write operations respect dry-run mode.

---

## Requirements

### Requirement 1: Add a Cron Job

**User Story:** As a maintenance coordinator, I want to install a new cron job by
providing a name, schedule, command, and user, so that a recurring task runs on the
system without me manually editing crontab files.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_cron_job` with a valid `CronJobEntry` THE SYSTEM SHALL
   write a cron drop-in file to `<cron_dir>/<entry.name>` containing the cron expression,
   user, and command.
2. WHEN `entry.comment` is provided THE SYSTEM SHALL include it as a `#`-prefixed line
   above the cron entry in the file.
3. WHEN the file is written successfully THE SYSTEM SHALL return a `ScheduledTaskResult`
   with `success=True` and `name=entry.name`.
4. WHEN the write fails THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=False` and the error message.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `ScheduledTaskResult` with `success=True`, `dry_run=True`, and `name=entry.name`.
6. WHEN a cron drop-in file with the same name already exists THE SYSTEM SHALL
   overwrite it with the new entry.

---

### Requirement 2: Remove a Cron Job

**User Story:** As a maintenance coordinator, I want to remove a cron job by name, so
that a previously installed recurring task no longer runs.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_cron_job` with a name that matches an existing
   drop-in file THE SYSTEM SHALL delete the file and return a `ScheduledTaskResult` with
   `success=True`.
2. WHEN no file matching the name exists THE SYSTEM SHALL return a `ScheduledTaskResult`
   with `success=False` and an error message indicating the job was not found.
3. WHEN the deletion fails for a reason other than absence THE SYSTEM SHALL return a
   `ScheduledTaskResult` with `success=False` and the error message.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT delete any file and SHALL return a
   `ScheduledTaskResult` with `success=True`, `dry_run=True`, and `name=name`.

---

### Requirement 3: List Cron Jobs

**User Story:** As a maintenance coordinator, I want to list all cron jobs managed by
this feature, so that I can audit which recurring tasks are installed on the system.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_cron_jobs` THE SYSTEM SHALL return a list of
   `ScheduledTaskInfo` objects, one per file present in `<cron_dir>`.
2. WHEN `cron_dir` contains no files THE SYSTEM SHALL return an empty list.
3. WHEN a file cannot be read THE SYSTEM SHALL skip it and include it in the returned
   list with `readable=False` and no schedule or command fields populated.

---

### Requirement 4: Validate a Cron Expression

**User Story:** As a maintenance coordinator, I want to validate a cron expression before
installing it, so that I can detect syntax errors without writing a file to the system.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate_cron_expression` with a standard five-field cron
   expression (`minute hour dom month dow`) THE SYSTEM SHALL return `True` when the
   expression is syntactically valid.
2. WHEN the expression contains an invalid field (out-of-range value, unrecognised
   special character) THE SYSTEM SHALL return `False`.
3. WHEN the expression contains `@reboot`, `@daily`, `@weekly`, `@monthly`, or `@yearly`
   THE SYSTEM SHALL return `True`.
4. WHEN the expression has fewer or more than five fields (and is not a `@` shorthand)
   THE SYSTEM SHALL return `False`.
5. WHEN validation is requested THE SYSTEM SHALL NOT execute any external command or
   write any file.

---

### Requirement 5: Create a Systemd Timer

**User Story:** As a maintenance coordinator, I want to install a systemd timer by
providing a name, calendar expression, and the command to run, so that the task is
managed by systemd's scheduling infrastructure.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_systemd_timer` with a valid `SystemdTimerEntry`
   THE SYSTEM SHALL write a `.service` unit file to
   `<unit_dir>/<entry.name>.service` and a `.timer` unit file to
   `<unit_dir>/<entry.name>.timer`.
2. WHEN both files are written successfully THE SYSTEM SHALL run
   `systemctl daemon-reload` and then `systemctl enable --now <entry.name>.timer`.
3. WHEN the coordinator sets `entry.enabled=False` THE SYSTEM SHALL write the unit files
   and run `systemctl daemon-reload` but SHALL NOT run `systemctl enable`.
4. WHEN the operation succeeds THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=True` and `name=entry.name`.
5. WHEN any file write or command fails THE SYSTEM SHALL return a `ScheduledTaskResult`
   with `success=False` and the first error message encountered.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any files or run any commands
   and SHALL return a `ScheduledTaskResult` with `success=True`, `dry_run=True`, and
   `name=entry.name`.
7. WHEN a unit file with the same name already exists THE SYSTEM SHALL overwrite it.

---

### Requirement 6: Remove a Systemd Timer

**User Story:** As a maintenance coordinator, I want to remove a systemd timer by name
so that its associated service and timer units are fully uninstalled from the system.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_systemd_timer` with a timer name THE SYSTEM SHALL
   run `systemctl disable --now <name>.timer` (suppressing errors if the unit is already
   inactive), then delete `<unit_dir>/<name>.timer` and `<unit_dir>/<name>.service`, then
   run `systemctl daemon-reload`.
2. WHEN neither unit file exists THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=False` and an error indicating the timer was not found.
3. WHEN all steps succeed THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=True`.
4. WHEN any step fails THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=False` and the error message.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT run any commands or delete any files
   and SHALL return a `ScheduledTaskResult` with `success=True`, `dry_run=True`, and
   `name=name`.

---

### Requirement 7: Enable or Disable a Systemd Timer

**User Story:** As a maintenance coordinator, I want to enable or disable an installed
systemd timer without removing it, so that I can temporarily suspend or resume a
scheduled task.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_systemd_timer_enabled` with `enabled=True` THE SYSTEM
   SHALL run `systemctl enable --now <name>.timer`.
2. WHEN the coordinator calls `set_systemd_timer_enabled` with `enabled=False` THE SYSTEM
   SHALL run `systemctl disable --now <name>.timer`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=True`.
4. WHEN the command fails THE SYSTEM SHALL return a `ScheduledTaskResult` with
   `success=False` and the error message.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT run any command and SHALL return a
   `ScheduledTaskResult` with `success=True`, `dry_run=True`, and `name=name`.

---

### Requirement 8: List Systemd Timers

**User Story:** As a maintenance coordinator, I want to list all systemd timers managed
by this feature, so that I can audit which timers are installed and whether they are
currently enabled.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_systemd_timers` THE SYSTEM SHALL run
   `systemctl list-timers --all --no-legend --no-pager` and parse the output into a list
   of `ScheduledTaskInfo` objects.
2. WHEN the command returns no timer entries THE SYSTEM SHALL return an empty list.
3. WHEN the command fails THE SYSTEM SHALL raise `ScheduledTaskQueryError` with the
   captured error text.

---

### Requirement 9: Query a Specific Scheduled Task

**User Story:** As a maintenance coordinator, I want to query the current state of a
specific scheduled task by name and type, so that I can determine whether it is
installed, active, and correctly configured.

#### Acceptance Criteria

1. WHEN the coordinator calls `query_task` with `task_type="cron"` and a name THE SYSTEM
   SHALL look for the file `<cron_dir>/<name>` and return a `ScheduledTaskInfo` populated
   from the file contents when found.
2. WHEN the coordinator calls `query_task` with `task_type="systemd"` and a name THE
   SYSTEM SHALL run `systemctl show <name>.timer` and return a `ScheduledTaskInfo`
   populated from the command output.
3. WHEN the task is not found (file absent for cron, unit not found for systemd) THE
   SYSTEM SHALL return `None`.
4. WHEN the query command or file read fails for a reason other than absence THE SYSTEM
   SHALL raise `ScheduledTaskQueryError`.

---

### Requirement 10: Elevation

**User Story:** As a maintenance coordinator, I want all file writes and systemctl
commands to use elevated privileges, so that the feature can manage system-level cron
and timer units without manual sudo escalation.

#### Acceptance Criteria

1. WHEN writing any cron or unit file THE SYSTEM SHALL pass `elevated=True` to the file
   operator.
2. WHEN deleting any cron or unit file THE SYSTEM SHALL use an elevated delete operation.
3. WHEN running any `systemctl` command THE SYSTEM SHALL pass `elevated=True` to the
   command runner.
4. WHEN dry-run mode is active elevation parameters SHALL still be set but no actual
   elevated operation SHALL be performed.

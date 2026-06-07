# Requirements

## Introduction

The backup-restore feature provides the `codecraftai-sysman-windows` package
with the ability to define, persist, run, list, restore, and verify backup
jobs on Windows hosts. Windows offers two first-party backup tools whose
availability depends on the SKU: `wbadmin` (the Windows Server Backup CLI,
shipping with the "Windows Server Backup" optional feature on Windows
Server, and a limited subset on Windows 10/11 Pro), and `Robocopy` (the
mirroring tool that ships with every supported Windows release). For
operators that prefer a deduplicating archive, `restic` is also supported
when installed.

A maintenance coordinator uses this feature to capture known-good snapshots
of specified paths before destructive or risky changes, to restore from
those snapshots on demand, and to verify the integrity of an archive. All
system commands are executed through the `codecraftai-interfaces` command-
runner abstraction, dispatched through a small PowerShell wrapper because
Windows tooling is canonically driven from PowerShell. All mutating
operations respect dry-run mode.

This feature does not perform Volume Shadow Copy (VSS) snapshot management
beyond what `wbadmin` itself orchestrates; VSS snapshot rotation is the
responsibility of the `storage-management` feature. Application-consistent
database dumps remain the responsibility of application-specific tooling.
Schedule wiring is delegated to the `scheduled-tasks` feature; this
feature persists the `schedule` field on each spec but does not itself
register Task Scheduler triggers.

---

## Requirements

### Requirement 1: Persist a Backup Job Specification

**User Story:** As a maintenance coordinator, I want to declare a named
backup job from a declarative spec so that subsequent runs can dispatch the
correct backend tool without re-supplying its parameters.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `WindowsBackupJobSpec` THE SYSTEM SHALL
   serialise the spec to JSON and write it to
   `%ProgramData%\CodeCraftAI\backup\jobs\<job_id>.json` via PowerShell
   `Set-Content`.
2. THE SYSTEM SHALL pass `elevated=True` to the runner so PowerShell
   executes with Administrator privileges (required to write under
   `%ProgramData%`).
3. WHEN a job with the same `job_id` already exists on disk AND the
   persisted JSON is byte-equivalent to the proposed JSON, THE SYSTEM
   SHALL return `WindowsBackupActionStatus.SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any runner
   command and SHALL return `DRY_RUN`.
5. WHEN `Set-Content` exits non-zero THE SYSTEM SHALL return `FAILED`
   with the runner's exit code, captured stdout, captured stderr, and
   argv.

---

### Requirement 2: Delete a Persisted Backup Job

**User Story:** As a maintenance coordinator, I want to delete a previously-
defined backup job so that the host stops carrying its declarative
footprint.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` THE SYSTEM SHALL invoke
   PowerShell `Remove-Item -Path <spec_path>` through the runner with
   `elevated=True`.
2. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `WindowsBackupActionStatus.SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke `Remove-Item`.
4. WHEN `Remove-Item` exits non-zero THE SYSTEM SHALL return `FAILED`
   with the captured stderr.

---

### Requirement 3: Run a Backup On Demand

**User Story:** As a maintenance coordinator, I want to run a previously-
defined job on demand so that a fresh archive is captured before an
immediate risky change.

#### Acceptance Criteria

1. WHEN the spec's `kind` is `WBADMIN` THE SYSTEM SHALL invoke
   `wbadmin start backup -backupTarget:<destination>
   -include:<source_paths> -allCritical -quiet` through the runner with
   `elevated=True`.
2. WHEN the spec's `kind` is `ROBOCOPY` THE SYSTEM SHALL invoke
   PowerShell `Robocopy <source> <destination> /MIR /COPYALL /R:1 /W:1`
   per source path.
3. WHEN the spec's `kind` is `RESTIC` THE SYSTEM SHALL invoke
   `restic.exe --repo <destination> --password-file=- backup
   <source_paths>` and forward the encryption passphrase via runner
   stdin (`--password-file=-` is the Windows-flavoured equivalent of
   `--password-file=/dev/stdin`).
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend.
5. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `FAILED` with `detail="job not found"`.
6. WHEN the backend exits zero THE SYSTEM SHALL return `SUCCESS`.
7. WHEN the backend exits non-zero THE SYSTEM SHALL return `FAILED`
   with the captured stderr available on the result.

---

### Requirement 4: List Persisted Backup Jobs

**User Story:** As a maintenance coordinator, I want to enumerate every
persisted backup job (and, for `WBADMIN` jobs, every backup version
`wbadmin` has on the destination volume).

#### Acceptance Criteria

1. WHEN the coordinator requests the list THE SYSTEM SHALL invoke
   PowerShell `Get-ChildItem -Path <job_dir> -Filter '*.json'` through
   the runner with `elevated=False`.
2. THE SYSTEM SHALL parse the JSON output into one `WindowsBackupInfo`
   per spec.
3. WHEN any persisted spec carries `kind=WBADMIN` THE SYSTEM SHALL ALSO
   invoke `wbadmin get versions -backupTarget:<destination>` to harvest
   the live `wbadmin` backup-version list. Live versions SHALL NOT
   override persisted spec rows; they SHALL be appended.
4. WHEN the job directory does not exist THE SYSTEM SHALL return only
   the live `wbadmin` rows (when present) or an empty list.

---

### Requirement 5: Restore from a Backup

**User Story:** As a maintenance coordinator, I want to restore a
previously-taken backup into a target path so that a prior state can be
recovered.

#### Acceptance Criteria

1. WHEN the spec's `kind` is `WBADMIN` THE SYSTEM SHALL invoke
   `wbadmin start recovery -version:<version_id> -itemType:File
   -items:<target> -recoveryTarget:<target> -quiet` through the runner
   with `elevated=True`. The `<version_id>` is the most-recent version
   reported by `wbadmin get versions` unless the caller pins one.
2. WHEN the spec's `kind` is `ROBOCOPY` THE SYSTEM SHALL invoke
   PowerShell `Robocopy <destination> <target> /MIR /COPYALL /R:1 /W:1`
   (reverse direction).
3. WHEN the spec's `kind` is `RESTIC` THE SYSTEM SHALL invoke
   `restic.exe --repo <destination> --password-file=- restore latest
   --target <target>` and forward the passphrase via runner stdin.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend.
5. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `FAILED` with `detail="job not found"`.

---

### Requirement 6: Verify a Backup

**User Story:** As a maintenance coordinator, I want to verify that an
archive is intact so that corrupted archives can be detected before being
relied on.

#### Acceptance Criteria

1. WHEN the spec's `kind` is `WBADMIN` THE SYSTEM SHALL invoke
   `wbadmin get items -version:<version_id>
   -backupTarget:<destination>` (best-effort: the cmdlet enumerates the
   items in the named version and a non-zero exit indicates a corrupt
   or missing version).
2. WHEN the spec's `kind` is `RESTIC` THE SYSTEM SHALL invoke
   `restic.exe --repo <destination> --password-file=- check`.
3. WHEN the spec's `kind` is `ROBOCOPY` THE SYSTEM SHALL invoke
   PowerShell `Robocopy <source> <destination> /L /MIR /R:0 /W:0` as a
   best-effort verification (the `/L` flag makes Robocopy list-only
   and a zero "extra files" count is a proxy for an intact mirror).
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend.

---

### Requirement 7: Encryption Passphrase Safety

**User Story:** As a maintenance coordinator, I want any passphrase used
to encrypt or decrypt a `RESTIC` repository to never appear on argv.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER include `encryption_passphrase` as a
   positional or named argv token on any `restic.exe` command.
2. THE SYSTEM SHALL deliver the passphrase via runner stdin and pass
   `--password-file=-` on argv.
3. THE SYSTEM SHALL exclude the passphrase from the recorded `command`
   field on every returned `WindowsBackupActionResult`.
4. THE SYSTEM SHALL exclude the passphrase from any persisted on-disk
   spec by replacing the field with the sentinel string
   `"***REDACTED***"` before writing.

---

### Requirement 8: Backend Dispatch via `WindowsBackupKind`

**User Story:** As a maintenance coordinator, I want to choose between
three backends (`WBADMIN`, `ROBOCOPY`, `RESTIC`) per job so that each
operator-defined job uses the right tool for its target paths.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept a `kind` field on every
   `WindowsBackupJobSpec` of type `WindowsBackupKind`.
2. WHEN `kind=WBADMIN` THE SYSTEM SHALL route every backend operation
   to `wbadmin`.
3. WHEN `kind=ROBOCOPY` THE SYSTEM SHALL route every backend operation
   to `Robocopy` (via PowerShell).
4. WHEN `kind=RESTIC` THE SYSTEM SHALL route every backend operation to
   `restic.exe`.
5. WHEN `kind` is none of the above THE SYSTEM SHALL return `FAILED`
   with `detail` describing the unsupported kind.

---

### Requirement 9: SKU and `wbadmin` Availability

**User Story:** As a maintenance coordinator, I want the `WBADMIN`
backend to surface SKU-related failures (`wbadmin` not installed, only
limited subset available on client SKUs) verbatim so that operators can
diagnose without spelunking the Event Log.

#### Acceptance Criteria

1. THE SYSTEM SHALL document that `wbadmin start backup` requires the
   "Windows Server Backup" feature on Windows Server, or a Windows
   10/11 Pro / Enterprise SKU. Home SKUs do not ship `wbadmin`.
2. THE SYSTEM SHALL surface a `wbadmin` non-zero exit verbatim on the
   returned `WindowsBackupActionResult`'s `stderr` field.
3. THE SYSTEM SHALL document that the `Restore-Computer` PowerShell
   cmdlet is also limited to specific SKUs and is not used by this
   feature; restores go through `wbadmin start recovery`.

---

### Requirement 10: Schedule Field is Persisted but Not Wired

**User Story:** As a maintenance coordinator, I want the `schedule`
field on a backup spec to be persisted alongside the job so that the
`scheduled-tasks` feature can wire it up later via Task Scheduler.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `schedule` field THE SYSTEM SHALL
   serialise it into the persisted JSON.
2. THE SYSTEM SHALL NOT itself call `Register-ScheduledTask`,
   `schtasks.exe`, or any other scheduling primitive.
3. THE SYSTEM SHALL document this division of responsibility.

---

### Requirement 11: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want every mutating
backup operation to run with the privileges required by the underlying
tools.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to every runner invocation in
   `create_backup_job`, `delete_backup_job`, `run_backup_now`,
   `restore_from_backup`, and `verify_backup`.
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL pass `elevated=False`.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for read verbs except
   where `wbadmin get versions` requires it (wbadmin's catalogue reads
   are admin-only on most SKUs; the manager honours this by passing
   `elevated=True` for the `wbadmin get versions` call inside
   `list_backups`).

---

### Requirement 12: Result Shape and Error Surface

**User Story:** As a maintenance coordinator, I want every action to
return a typed result so that audit-report generation can enumerate
every action with a predictable shape.

#### Acceptance Criteria

1. THE SYSTEM SHALL return a `WindowsBackupActionResult` from every
   write verb, carrying `status`, `detail`, `exit_code`, `stdout`,
   `stderr`, `command`, `job_id`, and `target_path`.
2. THE SYSTEM SHALL raise `WindowsBackupQueryError` from `list_backups`
   only when the underlying read fails unexpectedly.
3. THE SYSTEM SHALL never include the encryption passphrase or any
   other secret material on the `command` field.

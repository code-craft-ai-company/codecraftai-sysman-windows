# Design

## Overview

The Windows backup-restore feature is implemented as a single module,
`ccai.sysman.windows.backuprestore`, exposing a
`WindowsBackupRestoreManager` class and its associated dataclasses and
enum. The manager wraps three backend tools — `wbadmin` (Windows Server
Backup, default), PowerShell `Robocopy`, and `restic.exe` — behind a
single verb surface defined by the cross-OS contract at
`codecraftai-sysman-common/specs/contracts/backup-restore/contract.md`.

Windows offers two first-party backup tools whose availability depends on
the SKU: `wbadmin` (the Windows Server Backup CLI; ships with the "Windows
Server Backup" optional feature on Windows Server, and a limited subset on
Windows 10/11 Pro/Enterprise) and `Robocopy` (the mirroring tool that
ships with every supported Windows release). Both are operator-driven
rather than service-managed, in contrast to Time Machine on macOS. For
operators that prefer a deduplicating archive, `restic.exe` is also
supported when installed.

All system commands are dispatched through a small PowerShell wrapper —
`["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
"<script>"]` — because Windows tooling is canonically driven from
PowerShell. Even `wbadmin.exe` and `restic.exe` are invoked via PowerShell
so that the same argv shape covers every dispatch and so that the
`-NoProfile` / `-NonInteractive` flags neutralise `$PROFILE` mutations
and prevent PowerShell from blocking on interactive input.

The feature is strictly scoped to **file-tree backup and restore**. It
does not perform Volume Shadow Copy (VSS) snapshot management beyond what
`wbadmin` itself orchestrates; VSS rotation belongs to the
`storage-management` feature. Database-consistent dumps remain the
responsibility of application-specific tooling. Schedule wiring is
delegated to the `scheduled-tasks` feature.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH or WinRM (via
`SSHCommandRunner` / a future `WinRMCommandRunner`). Aspect composition
is transparent to the manager.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                      │
│  - Configures aspects (optional)                           │
│  - Injects runner into WindowsBackupRestoreManager         │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           WindowsBackupRestoreManager                       │
│  ccai.sysman.windows.backuprestore                      │
│                                                             │
│  create_backup_job()      run_backup_now()                  │
│  delete_backup_job()      restore_from_backup()             │
│  list_backups()           verify_backup()                   │
│                          get_job_status()                   │
└──────────┬──────────────────────────────────────────────────┘
           │ delegates to
           ▼
┌────────────────────────────┐
│      ICommandRunner        │
│  run(args, elevated, ...)  │
│  → RunResult               │
└────────────────────────────┘
```

The manager NEVER constructs a subprocess directly and NEVER reaches into
`pathlib`, `os.makedirs`, or `os.remove`.

---

## PowerShell Argv Shape

Every PowerShell scriptblock is dispatched as:

```
["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "<script>"]
```

The `-NoProfile` flag prevents `$PROFILE` mutations from changing cmdlet
behaviour; `-NonInteractive` prevents PowerShell from blocking on input
prompts. The script body is constructed by string concatenation; argv
arguments to cmdlets are quoted with single quotes (PowerShell's literal
form) so embedded characters do not break out of the literal.

Native executables (`wbadmin.exe`, `restic.exe`) are also invoked via
PowerShell rather than directly, so the same argv shape covers every
dispatch. This keeps the manager's command-construction code uniform.

---

## Verb / Tool Mapping

| Verb                  | WBADMIN backend                                                     | ROBOCOPY backend                                                | RESTIC backend                                                   |
|-----------------------|---------------------------------------------------------------------|-----------------------------------------------------------------|------------------------------------------------------------------|
| `create_backup_job`   | `Set-Content -Path <job-path>`                                      | `Set-Content -Path <job-path>`                                  | `Set-Content -Path <job-path>`                                   |
| `delete_backup_job`   | `Remove-Item -Path <job-path>`                                      | `Remove-Item -Path <job-path>`                                  | `Remove-Item -Path <job-path>`                                   |
| `run_backup_now`      | `wbadmin start backup -backupTarget:<dst> -include:<paths> -allCritical -quiet` | `Robocopy <src> <dst> /MIR /COPYALL /R:1 /W:1`         | `restic --repo <dst> --password-file=- backup`                   |
| `list_backups`        | `Get-ChildItem` + `wbadmin get versions -backupTarget:<dst>`        | `Get-ChildItem`                                                 | `Get-ChildItem`                                                  |
| `restore_from_backup` | `wbadmin start recovery -version:<id> -itemType:File -items:<target> -recoveryTarget:<target> -quiet` | `Robocopy <dst> <target> /MIR /COPYALL /R:1 /W:1` | `restic restore latest --target <target>`                        |
| `verify_backup`       | `wbadmin get items -version:<id> -backupTarget:<dst>`               | `Robocopy <src> <dst> /L /MIR /R:0 /W:0`                        | `restic check`                                                   |

---

## Backend Dispatch

The dispatch decision is made in three places: `run_backup_now`,
`restore_from_backup`, and `verify_backup`. Each verb resolves the spec
from disk (via PowerShell `Get-Content`), parses the persisted JSON, and
chooses the backend by the spec's `kind` field.

For `WBADMIN`:

* `run_backup_now` → `wbadmin start backup -backupTarget:<dst>
  -include:<paths> -allCritical -quiet`. The `-quiet` flag suppresses
  `wbadmin`'s interactive prompt; the `-allCritical` flag includes
  every system-critical volume (system, boot, recovery).
* `restore_from_backup` → `wbadmin start recovery -version:<id>
  -itemType:File -items:<target> -recoveryTarget:<target> -quiet`. The
  `<id>` is a `wbadmin`-format version identifier (e.g.
  `04/24/2026-12:00`).
* `verify_backup` → `wbadmin get items -version:<id>`. The cmdlet
  enumerates the items in a version; a non-zero exit indicates a
  corrupt or missing version.

For `ROBOCOPY`:

* Robocopy uses non-zero exit codes to communicate status (0 = no
  files copied, 1 = files copied, 2 = extra files detected, ...).
  Exit codes 0–7 indicate success; 8+ indicate failure. The manager
  treats any exit ≤ 7 as `SUCCESS`.

For `RESTIC`:

* Identical to the Ubuntu / macOS managers, except the
  passphrase-on-stdin flag is `--password-file=-` rather than
  `--password-file=/dev/stdin` (Windows has no `/dev/stdin`).

---

## Encryption Passphrase Handling

Identical to the Ubuntu / macOS managers: only the `RESTIC` backend
supports encryption; the passphrase is delivered via the runner's stdin
channel and the argv carries `--password-file=-`. The persisted on-disk
spec replaces the passphrase with the sentinel `"***REDACTED***"`.

`wbadmin` does not natively encrypt at-rest archives; it relies on
volume-level encryption (BitLocker on the destination volume). This
feature does NOT manage BitLocker; the operator pre-encrypts the
destination volume.

---

## Job Persistence

Backup-job specs are persisted as JSON files at
`%ProgramData%\CodeCraftAI\backup\jobs\<job_id>.json`. `%ProgramData%`
expands to `C:\ProgramData` on every modern Windows install; the manager
hard-codes this path on the assumption that the operator has not moved
`%ProgramData%`. The default `job_dir` config field is
`r"C:\ProgramData\CodeCraftAI\backup\jobs"` (raw string to avoid escape
issues with the backslashes).

Idempotency for `create_backup_job` is implemented by reading the
existing spec via PowerShell `Get-Content` and comparing its content to
the proposed serialisation. The comparison is byte-for-byte after
normalising trailing whitespace (PowerShell `Set-Content` may add a
trailing newline that the manager strips before comparing).

The job directory itself is NOT pre-created by the manager; the
operator's installation manifest is responsible for `New-Item -Path
<job-dir> -ItemType Directory` at host provisioning time.

---

## Module Layout

```
backuprestore.py
├── module-level constants
│   ├── _POWERSHELL, _PS_NOPROFILE, _PS_NONINTERACTIVE, _PS_COMMAND
│   └── _DEFAULT_JOB_DIR (r"C:\ProgramData\CodeCraftAI\backup\jobs")
├── enums
│   └── WindowsBackupKind (WBADMIN, ROBOCOPY, RESTIC)
├── data classes
│   ├── WindowsBackupManagerConfig (extends FeatureConfig)
│   ├── WindowsBackupActionResult  (extends FeatureActionResult)
│   ├── WindowsBackupJobSpec       (alias WindowsBackupSpec)
│   ├── WindowsBackupInfo
│   ├── WindowsBackupVerifyResult
│   └── WindowsBackupJobStatus
├── exceptions
│   ├── WindowsBackupQueryError
│   ├── WindowsBackupNotFoundError
│   └── WindowsBackupVerifyError
├── manager
│   └── WindowsBackupRestoreManager
└── module helpers
    ├── _serialise_spec
    ├── _deserialise_spec
    ├── _redact_passphrase
    ├── _build_powershell_argv
    ├── _build_set_content_script
    ├── _build_remove_item_script
    ├── _build_get_content_script
    ├── _build_get_childitem_script
    ├── _build_wbadmin_argv
    ├── _build_robocopy_argv
    ├── _build_restic_argv
    └── _job_path
```

---

## Backup-Kind Mapping

| Abstract `BackupKind` | Windows `WindowsBackupKind` | Notes                                                 |
|-----------------------|-----------------------------|-------------------------------------------------------|
| `ARCHIVE`             | not directly supported      | Use `RESTIC` or wrap `Compress-Archive`.              |
| `MIRROR`              | `ROBOCOPY`                  | `Robocopy /MIR` mirror.                               |
| (deduplicating archive) | `RESTIC`                  | Restic deduplicating snapshot store.                  |
| `TIME_MACHINE`        | not supported               | macOS-only.                                           |
| `SYSTEM_IMAGE`        | `WBADMIN`                   | Windows Server Backup system image.                   |

---

## Idempotency Decision Table

| Verb                  | Idempotent? | Skip condition                                         |
|-----------------------|-------------|--------------------------------------------------------|
| `create_backup_job`   | yes         | spec on disk byte-equivalent to proposed JSON          |
| `delete_backup_job`   | yes         | spec file absent                                       |
| `run_backup_now`      | no          | always dispatches                                      |
| `restore_from_backup` | no          | always dispatches                                      |
| `verify_backup`       | no          | always dispatches                                      |
| `list_backups`        | n/a         | read verb                                              |
| `get_job_status`      | n/a         | read verb                                              |

---

## Elevation Decision Table

| Verb                  | `elevated=` argument forwarded                          |
|-----------------------|---------------------------------------------------------|
| `create_backup_job`   | `self._config.elevated`                                 |
| `delete_backup_job`   | `self._config.elevated`                                 |
| `run_backup_now`      | `self._config.elevated`                                 |
| `restore_from_backup` | `self._config.elevated`                                 |
| `verify_backup`       | `self._config.elevated`                                 |
| `list_backups`        | `False` for spec reads; `self._config.elevated` for `wbadmin get versions` |
| `get_job_status`      | always `False`                                          |

The `wbadmin get versions` cmdlet requires Administrator privileges on
most SKUs; the manager honours this by passing `elevated=True` for that
specific call inside `list_backups` regardless of the config value, when
the manager has been told to enumerate `WBADMIN` rows.

(Implementation note: in practice, the contract permits passing
`elevated=True` for reads when the OS requires it; this decision-table
row documents the deviation.)

---

## SKU and Tooling Constraints

`wbadmin` availability:

* Windows Server (any supported edition): full `wbadmin` available
  after installing the "Windows Server Backup" optional feature.
* Windows 10 / 11 Pro / Enterprise: limited subset of `wbadmin`
  available; some flags (e.g. `-allCritical`) may behave differently.
* Windows 10 / 11 Home: `wbadmin` is NOT shipped.

`Robocopy` availability: every supported Windows release ships
`Robocopy.exe`.

`restic.exe` availability: requires the operator to install `restic`
manually (e.g. via `choco install restic` or the GitHub release).

The manager does NOT pre-validate any of these constraints. A missing
binary surfaces as a `CommandNotFoundException` from PowerShell, which
the manager passes through verbatim on the returned
`WindowsBackupActionResult`'s `stderr` field.

---

## Error Surface

`WindowsBackupQueryError` is raised only by `list_backups` and
`get_job_status`, and only when the underlying read fails unexpectedly.

`WindowsBackupNotFoundError` is reserved for the case where a `job_id`
or `backup_id` is supplied to `restore_from_backup`, `verify_backup`,
or `get_job_status` and the spec does not exist on disk.

`WindowsBackupVerifyError` is reserved for structural errors during
verification.

---

## Test Strategy

The test suite at
`source/testroots/testbase/ccai/tests/sysman/windows/test_backuprestore.py`
covers, at minimum, the following surface:

* Command construction: one test per write verb per backend.
* Idempotency: `create_backup_job` returns `SKIPPED` on matching
  state; `delete_backup_job` returns `SKIPPED` when absent.
* Dry-run: every write verb returns `DRY_RUN`.
* `list_backups` parsing.
* `wbadmin` dispatch: explicit test that `wbadmin start backup` is
  the script body for a `WBADMIN` job.
* Credential safety: a `RESTIC` job with a passphrase produces a
  script body that does NOT contain the passphrase.
* Elevation: writes pass `elevated=True` and reads do not.
* Failure: the runner's stderr surfaces unchanged on `FAILED`
  results.

The tests use a `_MockCommandRunner` that records every `run` call
and returns pre-seeded `RunResult` values, matching the script-key
matching pattern used by `test_dnsservermanagement.py`.

---

## Cross-OS Concept Mapping

| Concept                | Windows                                          | Ubuntu                                    | macOS                                    |
|------------------------|--------------------------------------------------|-------------------------------------------|------------------------------------------|
| Default backup tool    | `wbadmin` (Server) / Robocopy (client)           | `rsync` (selected via enum)               | Time Machine via `tmutil`                |
| Job persistence        | `%ProgramData%\CodeCraftAI\backup\jobs\`            | `/var/lib/ccai/backups/jobs/`         | `/var/db/ccai/backups/jobs/`         |
| Repository layout      | `wbadmin` catalogue on a backup volume           | tool-specific                             | TM volume + `Backups.backupdb`           |
| Encryption native?     | none (relies on BitLocker on volume)             | only for `restic`                         | Volume-level (FileVault on destination)  |
| Snapshot model         | block-level VHDX                                 | none                                      | block-level (HFS+/APFS)                  |
| Schedule wiring        | partly built in via Task Scheduler               | delegated to `scheduled-tasks`            | partly built in via `tmutil` cadence     |

The dominant divergence on Windows is the SKU dependency: a job that
works on Windows Server may fail on Windows 10 Home because `wbadmin`
is missing. The manager surfaces SKU-related failures verbatim rather
than pre-flighting; the `scheduled-tasks` feature is responsible for
host-capability detection.

---

## Aspect Composition

* `RetryAspect` may transparently flip a `FAILED` result to `SUCCESS`.
* `PersistenceAspect` MUST NOT log the stdin payload from a `RESTIC`
  run.
* `TimeoutAspect` may abort a long `wbadmin` run; `wbadmin` is not
  reliably interruptible, so retries should be configured carefully.

---

## Out of Scope

* VSS snapshot rotation (delegated to `storage-management`).
* Database-consistent dumps.
* BitLocker key escrow.
* `Restore-Computer` system-state restore (limited SKUs; not supported
  by this feature).
* Cross-host replication beyond what `wbadmin` itself supports.
* Bare-metal recovery ISO generation.

# Requirements

## Introduction

The kdump-management feature provides the `automojo-sysman-windows`
package with the ability to manage Windows kernel crash dumps through
the cross-OS contract defined at
`automojo-sysman-common/specs/contracts/kdump-management/contract.md`.

Windows' native kernel-crash-dump model is fundamentally different
from Linux's kdump:

* There is no separate capture-kernel and no pre-reserved memory.
  The kernel writes a dump from its own context using the pagefile
  on the system volume.
* Configuration lives in the registry at
  `HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl`. The two
  primary values are:
  * `CrashDumpEnabled` -- integer mode selector
    (0=Disabled, 1=Complete, 2=Kernel, 3=Small/Mini, 7=Automatic).
  * `DumpFile` -- target path (default `%SystemRoot%\MEMORY.DMP`).
* Kernel reads `CrashControl` at boot. **Every write to a CrashControl
  value requires a reboot for the new value to take effect.**
* Minidumps land in `%SystemRoot%\Minidump\`; the
  Memory.DMP file lands at the path stored in `DumpFile`.

This spec is the Windows realisation of the cross-OS contract:

* The mutating verbs (`enable_kdump`, `disable_kdump`,
  `configure_kdump_target`) operate on the registry and mark
  `requires_reboot=True` on every successful write.
* `configure_kdump_target` only honours `LOCAL_PATH` targets;
  `SSH` and `NFS` produce a typed `FAILED` result with stderr token
  `unsupported_target_kind`.
* The reads operate over registry / filesystem and never elevate.

A maintenance coordinator running in an external package drives all
operations through the public interface. Every command is dispatched
through the `automojo-interfaces` command-runner abstraction.

---

## Requirements

### Requirement 1: Enable Crash Dumps

**User Story:** As a maintenance coordinator, I want to enable
kernel crash dump capture on Windows so that the next bugcheck
produces a usable Memory.DMP.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_kdump()` THE SYSTEM SHALL
   write the configured `default_enable_mode` integer (default `7` --
   Automatic Memory Dump) to
   `HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl\CrashDumpEnabled`
   via `Set-ItemProperty -Type DWord` with elevated privileges.
2. WHEN the registry already carries the desired mode THE SYSTEM
   SHALL return a `WindowsKdumpActionResult` with status `SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL return `SUCCESS` with
   `requires_reboot=True`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN` with `requires_reboot=True`
   carried onto the result for advisory purposes.
5. WHEN the write fails THE SYSTEM SHALL return `FAILED` with
   `exit_code`, `stderr`, and `command` populated.

---

### Requirement 2: Disable Crash Dumps

**User Story:** As a maintenance coordinator, I want to disable
kernel crash dump capture so the system does not consume disk space
on dump files.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_kdump()` THE SYSTEM SHALL
   write `0` to `CrashControl\CrashDumpEnabled` via
   `Set-ItemProperty -Type DWord` with elevated privileges.
2. WHEN the registry already carries `0` THE SYSTEM SHALL return
   `SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL return `SUCCESS` with
   `requires_reboot=True`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 3: Configure Dump Target

**User Story:** As a maintenance coordinator, I want to point Windows
at a non-default dump path so that captured dumps land on a volume
with sufficient free space.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `configure_kdump_target(spec=WindowsKdumpTargetSpec(...))` AND
   `target_kind=LOCAL_PATH` AND `path` is non-empty THE SYSTEM SHALL
   write `path` to `CrashControl\DumpFile` via
   `Set-ItemProperty -Type ExpandString` with elevated privileges.
2. WHEN `target_kind=SSH` THE SYSTEM SHALL return `FAILED` with
   `stderr="unsupported_target_kind"` and SHALL NOT execute any
   command.
3. WHEN `target_kind=NFS` THE SYSTEM SHALL return `FAILED` with
   `stderr="unsupported_target_kind"` and SHALL NOT execute any
   command.
4. WHEN `target_kind=LOCAL_PATH` AND `path` is None or empty THE
   SYSTEM SHALL return `FAILED` with
   `stderr="invalid_target_spec"`.
5. WHEN the registry already carries a `DumpFile` value matching the
   requested path THE SYSTEM SHALL return `SKIPPED`.
6. WHEN the write succeeds THE SYSTEM SHALL return `SUCCESS` with
   `requires_reboot=True` and `target=spec.path`.

---

### Requirement 4: Query Status

**User Story:** As a maintenance coordinator, I want to know whether
kernel crash dumps are enabled and which target is configured so
that I can verify a host's compliance with the declared baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_kdump_status()` THE SYSTEM SHALL
   invoke `Get-ItemProperty -Path '<crashcontrol>' | ConvertTo-Json
   -Compress -Depth 5` via the runner without elevation.
2. THE SYSTEM SHALL parse the JSON payload and return a
   `WindowsKdumpStatus` carrying `enabled` (True iff `CrashDumpEnabled
   != 0`), `running` (mirror of `enabled`), `target_kind` (`LOCAL_PATH`
   when `DumpFile` is non-empty, otherwise `None`), `target_path`
   (the `DumpFile` value or `None`), `kernel=None`,
   `reserved_memory_mb=None`, and a `message` describing any unknown
   `CrashDumpEnabled` mode.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for the status read.
4. WHEN the read fails THE SYSTEM SHALL return a status with
   `enabled=False`, `running=False`, and `target_*=None` rather than
   raise.

---

### Requirement 5: List Recent Dumps

**User Story:** As a maintenance coordinator, I want to enumerate
recent kernel-dump artifacts so that I can audit how often a host
is bugchecking.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_recent_dumps()` THE SYSTEM SHALL
   invoke `Get-ChildItem -File` against
   `<minidump_dir>\*.dmp` and the configured `memory_dmp` path,
   project `Name`, `FullName`, `LastWriteTime`, and `Length`, sort by
   `LastWriteTime` descending, and convert to JSON for parsing.
2. THE SYSTEM SHALL return one `WindowsKdumpInfo` per entry, freshest
   first, with `id`, `path`, `created`, and `size_bytes` populated.
3. WHEN the cmdlet fails for any reason THE SYSTEM SHALL raise
   `WindowsKdumpQueryError`.
4. WHEN the cmdlet succeeds but the JSON payload is empty or
   `null`-equivalent THE SYSTEM SHALL return an empty list.

---

### Requirement 6: Clear Old Dumps

**User Story:** As a maintenance coordinator, I want to purge
kernel-dump artifacts older than a threshold so that the dump
directory does not grow without bound.

#### Acceptance Criteria

1. WHEN the coordinator calls `clear_old_dumps(age_days=N)` THE
   SYSTEM SHALL invoke a PowerShell pipeline that builds a cutoff
   from `(Get-Date).AddDays(-N)`, enumerates the dump paths, filters
   `LastWriteTime -lt $cutoff`, and pipes to `Remove-Item -Force`.
2. THE SYSTEM SHALL execute with elevated privileges.
3. WHEN `age_days < 0` THE SYSTEM SHALL return `FAILED` with
   `message="age_days must be >= 0"` and SHALL NOT execute any
   command.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.
5. THE SYSTEM SHALL return `SUCCESS` even when no files matched the
   filter.

---

### Requirement 7: Get Dump Info

**User Story:** As a maintenance coordinator, I want to surface
filesystem metadata for a single dump file so that I can attach
context to a maintenance ticket.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_dump_info(dump_id=ID)` THE SYSTEM
   SHALL resolve `<minidump_dir>\<ID>` first; when `ID` matches the
   basename of `memory_dmp` (case-insensitive), THE SYSTEM SHALL use
   the `memory_dmp` path instead.
2. THE SYSTEM SHALL invoke `Get-Item -LiteralPath '<resolved>'`
   piped to `Select-Object Name,FullName,LastWriteTime,Length |
   ConvertTo-Json -Compress -Depth 5` via the runner without
   elevation.
3. THE SYSTEM SHALL return a `WindowsKdumpInfo` carrying `id`,
   `path`, `created`, `size_bytes`, `kernel=None`, and
   `panic_message=None`. The kernel and panic_message fields are
   intentionally None on Windows because no in-Python parser exists
   for `.dmp` files; surface metadata only.
4. WHEN the cmdlet fails THE SYSTEM SHALL raise
   `WindowsKdumpQueryError`.

---

### Requirement 8: Reboot Required Advisory

**User Story:** As a maintenance coordinator, I want CrashControl
writes to be flagged as requiring a reboot so that orchestration
code knows when to schedule one.

#### Acceptance Criteria

1. WHEN any of `enable_kdump`, `disable_kdump`,
   `configure_kdump_target` returns `SUCCESS` THE SYSTEM SHALL set
   `requires_reboot=True` on the result.
2. WHEN any of those verbs returns `DRY_RUN` THE SYSTEM SHALL also
   set `requires_reboot=True` (advisory: tells the planner to
   schedule a reboot if the dry-run is committed).
3. WHEN any of those verbs returns `SKIPPED` THE SYSTEM SHALL set
   `requires_reboot=False` (no write occurred; no reboot needed).
4. `clear_old_dumps` SHALL always set `requires_reboot=False`.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes to
elevate when configured but reads to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (`enable_kdump`, `disable_kdump`, `configure_kdump_target`,
   `clear_old_dumps`) THE SYSTEM SHALL pass `elevated=True` to the
   runner for the mutating call.
2. WHEN `config.elevated=False` THE SYSTEM SHALL never pass
   `elevated=True`.
3. WHEN the verb is a read THE SYSTEM SHALL never pass
   `elevated=True`, regardless of the config.
4. THE SYSTEM SHALL NOT pass `elevated=True` for the idempotency
   probe that reads the CrashControl key.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that exercises the planning logic without touching the host.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` AND any write verb is invoked THE
   SYSTEM SHALL NOT call the runner for the mutating step.
2. THE SYSTEM SHALL still return a `WindowsKdumpActionResult` with
   status `DRY_RUN` and the `command` field populated with the argv
   that would have been executed.
3. THE SYSTEM SHALL NEVER write to the registry in dry-run mode.
4. Read verbs SHALL execute unchanged in dry-run mode -- reads do
   not mutate.

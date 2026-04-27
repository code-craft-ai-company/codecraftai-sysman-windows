# Requirements

## Introduction

The filesystem-quotas feature provides the
`automojo-sysman-windows` package with the ability to manage
disk quotas on Windows systems for NTFS volumes through
`fsutil quota` — the volume-level user-quota subsystem built
into NTFS.  It covers per-user block quotas with idempotent
enable / disable / set semantics and read-only query verbs for
current usage and limits.

A maintenance coordinator uses this feature to declare and
enforce per-user disk-usage policy on a volume, to report the
current posture of that volume, and to confirm whether quota
tracking and enforcement are currently active.  Every system
command (`fsutil quota track`, `fsutil quota enforce`, `fsutil
quota disable`, `fsutil quota modify`, `fsutil quota query`) is
dispatched through the `automojo-interfaces` `ICommandRunner`
abstraction so the manager works identically under local and
WinRM / SSH execution.  All mutating operations honour dry-run.

## fsutil vs FSRM (load-bearing scope decision)

Windows has TWO separate quota subsystems:

1. **`fsutil quota`** — a built-in NTFS feature that tracks
   per-user space usage on a per-volume basis.  Available on
   every Windows installation with NTFS volumes.  Supports
   per-user quotas only — NO per-group, NO per-directory.
2. **FSRM `dirquota`** — File Server Resource Manager,
   available only on Windows Server, optional install. Provides
   per-directory quotas with templates, notification actions,
   and per-group restrictions.  FSRM is genuinely powerful but
   is a server feature, requires installation, and has a
   completely different cmdlet surface.

This pilot uses `fsutil quota` ONLY, because it is universally
available.  Group quotas (which `fsutil quota` does not support
at all) return `FAILED` with `detail="ntfs_group_quota_unsupported"`.
FSRM integration is a documented follow-up.

## Major Limitation: No Group Quota Support

`fsutil quota` is fundamentally per-user.  NTFS volume-level
quotas address Windows security identifiers (SIDs), and Windows
does not have a concept of a "primary group" the way POSIX
does.  Calling `set_group_quota`, `get_group_quota`, or
listing group-target quotas returns FAILED with
`detail="ntfs_group_quota_unsupported"`.

This feature does NOT manage volume mount points, drive-letter
assignments, or `mountvol` configuration — those are owned by
`filesystem-management`.  Project quotas, FSRM templates, and
FSRM notification actions are out of scope.

---

## Requirements

### Requirement 1: Enable Quota Enforcement

**User Story:** As a maintenance coordinator, I want to enable
quota tracking and enforcement on a volume, so that subsequent
`fsutil quota modify` calls take effect against active
accounting.

#### Acceptance Criteria

1. WHEN the coordinator enables quotas on a volume THE SYSTEM
   SHALL invoke `fsutil quota track <volume>` (begin
   accounting) followed by `fsutil quota enforce <volume>`
   (begin enforcement).
2. WHEN `fsutil quota query <volume>` already reports the
   volume as enforcing THE SYSTEM SHALL return `SKIPPED`
   without dispatching either command.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke any
   runner command and SHALL return
   `WindowsQuotaActionResult(status=DRY_RUN)` with the
   would-have-been argv recorded on `command`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN any of the dispatched commands exits non-zero THE
   SYSTEM SHALL return `FAILED` with the runner's exit code,
   captured stdout, captured stderr, and the failing argv.

---

### Requirement 2: Disable Quota Enforcement

**User Story:** As a maintenance coordinator, I want to disable
quota enforcement on a volume.

#### Acceptance Criteria

1. WHEN the coordinator disables quotas THE SYSTEM SHALL invoke
   `fsutil quota disable <volume>`.
2. WHEN `fsutil quota query <volume>` already reports the
   volume as having quotas disabled THE SYSTEM SHALL return
   `SKIPPED` without dispatching the command.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `fsutil quota disable` and SHALL return `DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN `fsutil quota disable` exits non-zero THE SYSTEM SHALL
   return `FAILED` with the captured exit code, stdout, stderr,
   and argv.

---

### Requirement 3: Set User Quota

**User Story:** As a maintenance coordinator, I want to set
soft / hard byte limits for a specific user on a volume.

#### Acceptance Criteria

1. WHEN the coordinator sets a user quota THE SYSTEM SHALL
   invoke `fsutil quota modify <volume> <hard_bytes>
   <soft_bytes> <user-or-SID>`.
2. WHEN `fsutil quota query <volume>` already reports the
   requested `<soft_bytes>` and `<hard_bytes>` for `<user>` THE
   SYSTEM SHALL return `SKIPPED` without dispatching the
   command.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `fsutil quota modify` and SHALL return `DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN `fsutil quota modify` exits non-zero THE SYSTEM SHALL
   return `FAILED` with the captured exit code, stdout, stderr,
   and argv.

---

### Requirement 4: Set Group Quota Returns FAILED

**User Story:** As a maintenance coordinator, I want
`set_group_quota` to refuse with a clear error so that the
NTFS-level limitation is surfaced as a typed FAILED result
rather than silently succeeding or producing a kernel error.

#### Acceptance Criteria

1. WHEN the coordinator invokes `set_group_quota` THE SYSTEM
   SHALL return `WindowsQuotaActionResult(status=FAILED,
   detail="ntfs_group_quota_unsupported", ...)` without
   invoking any runner command.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL still return
   `FAILED` with `detail="ntfs_group_quota_unsupported"` because
   the operation is unsupported by NTFS rather than skipped by
   policy.

---

### Requirement 5: Query a User's Current Quota

**User Story:** As a maintenance coordinator, I want to read
the current usage and limits for a specific user on a volume.

#### Acceptance Criteria

1. WHEN the coordinator queries a user quota THE SYSTEM SHALL
   invoke `fsutil quota query <volume>` (without elevation) and
   parse the per-user block matching `<user>` into a
   `WindowsQuotaInfo` carrying `used_bytes`, `soft_bytes`,
   `hard_bytes`, and `grace_period_seconds`.
2. WHEN no block matches `<user>` in the parsed output THE
   SYSTEM SHALL return `None`.
3. WHEN `fsutil quota query` exits non-zero THE SYSTEM SHALL
   raise `WindowsQuotaQueryError`.

---

### Requirement 6: Query a Group's Current Quota Returns FAILED

**User Story:** Group queries must surface the NTFS limitation
the same way as the writes.

#### Acceptance Criteria

1. WHEN the coordinator invokes `get_group_quota` THE SYSTEM
   SHALL raise `WindowsQuotaQueryError` with a message that
   carries `"ntfs_group_quota_unsupported"`.
2. THE SYSTEM SHALL NOT invoke any runner command.

---

### Requirement 7: List All Quotas on a Volume

**User Story:** As a maintenance coordinator, I want to
enumerate every per-user quota on a volume.

#### Acceptance Criteria

1. WHEN the coordinator lists quotas THE SYSTEM SHALL invoke
   `fsutil quota query <volume>` (without elevation) and parse
   the output into a `list[WindowsQuotaInfo]` covering every
   per-user block.
2. THE SYSTEM SHALL emit only USER-kind entries; group-kind
   entries are not produced because NTFS volume quotas have no
   group surface.
3. WHEN the parsed output contains no per-user blocks THE
   SYSTEM SHALL return an empty list.
4. WHEN `fsutil quota query` exits non-zero THE SYSTEM SHALL
   raise `WindowsQuotaQueryError`.

---

### Requirement 8: Query Quota Enforcement Status

**User Story:** As a maintenance coordinator, I want to query
whether quota tracking and enforcement are currently active on
a volume.

#### Acceptance Criteria

1. WHEN the coordinator queries the enforcement status THE
   SYSTEM SHALL invoke `fsutil quota query <volume>` (without
   elevation) and parse the header lines (`File System Type
   Status: ...`, `Logging Status: ...`, `Enforce Limit Status:
   ...`) into a `WindowsQuotaStatus` carrying `enabled`,
   `enforcing`, `user_quotas_active`, `group_quotas_active`
   (always False), and `filesystem_type` (always "ntfs" when
   the query succeeded).
2. WHEN `fsutil quota query` exits non-zero THE SYSTEM SHALL
   raise `WindowsQuotaQueryError`.

---

### Requirement 9: Dry-Run, Elevation, and Error Reporting

**User Story:** Standard dry-run / elevation / failure
semantics across every write verb.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command and SHALL return
   `WindowsQuotaActionResult(status=DRY_RUN)` with the
   would-have-been argv recorded on `command`. Note the
   exception in Requirement 4 — `set_group_quota` returns
   FAILED even under dry-run because the operation is
   unsupported.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch every
   write command with `elevated=True`.  Reads MUST NEVER pass
   `elevated=True` to the runner, regardless of
   `config.elevated`.
3. WHEN the runner returns non-zero from a write command THE
   SYSTEM SHALL return `WindowsQuotaActionResult(status=FAILED,
   exit_code=<code>, stdout=<captured>, stderr=<captured>,
   command=<argv>)`.

---

### Requirement 10: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeated
invocations of the same write verb to be no-ops when the
volume already matches the requested state.

#### Acceptance Criteria

1. WHEN `enable_quotas` is invoked on a volume already
   enforcing THE SYSTEM SHALL return `SKIPPED`.
2. WHEN `disable_quotas` is invoked on a volume with quotas
   already disabled THE SYSTEM SHALL return `SKIPPED`.
3. WHEN `set_user_quota` is invoked with values that already
   match the user's current record THE SYSTEM SHALL return
   `SKIPPED`.

---

### Requirement 11: PowerShell-vs-fsutil Argv Shape

**User Story:** As an operator, I want every command dispatched
through the runner to carry a clearly-formed argv that can be
audited and replayed.

#### Acceptance Criteria

1. THE SYSTEM SHALL dispatch `fsutil` directly (e.g.
   `["fsutil", "quota", "track", "C:"]`) rather than wrapping
   it in a PowerShell scriptblock.  `fsutil.exe` is a native
   Windows binary that accepts argv directly without requiring
   PowerShell mediation.
2. The volume token SHALL be passed verbatim as supplied by the
   caller (e.g. `"C:"`).  The manager SHALL NOT normalise or
   coerce drive-letter tokens.

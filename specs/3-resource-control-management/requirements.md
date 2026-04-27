# Requirements

## Introduction

The resource-control-management feature provides the
`automojo-sysman-windows` package with the ability to manage
named, idempotent, auditable resource ceilings for groups of
processes on Windows systems.  The implementation rides on
**logical group records** (one JSON spec per group at
`%ProgramData%\Automojo\rcgroups\<name>.json`) plus
**per-PID PowerShell adjustments**
(`(Get-Process).PriorityClass = ...` and `.ProcessorAffinity =
...`) — Windows kernel-level Job Objects expose
`JOB_OBJECT_LIMIT_PROCESS_MEMORY` / `LIMIT_JOB_TIME` /
`LIMIT_PRIORITY_CLASS` ceilings, but PowerShell does not surface
a clean cmdlet for creating them.  The pilot therefore takes the
"logical group" path: the group spec is the source of truth, and
its declared ceilings are applied to every attached PID
best-effort via `Get-Process`.

This package satisfies the cross-OS
[`resource-control-management` contract](../../../automojo-sysman-common/specs/contracts/resource-control-management/contract.md)
**with documented limitations** — several verbs return
`status=FAILED` with a typed `detail`/`message` token when the
underlying tooling does not exist via PowerShell on Windows.
Those gaps are deliberate and prominent (see Requirement 13
below).

The Windows manager covers:

- Creating and deleting "logical" resource groups represented
  as JSON files at `<group_dir>\<name>.json`.
- Setting per-group CPU "limit" by adjusting every attached
  PID's `PriorityClass` (best-effort approximation; Windows has
  no native CPU-percent throttle for arbitrary process groups).
- Best-effort attach / detach by adding the PID to the spec
  file and applying the recorded `PriorityClass` /
  `ProcessorAffinity` to the running process.
- Listing all groups by enumerating `<group_dir>`.
- Aggregating per-PID `Get-Process | Measure-Object`
  snapshots into a per-group usage summary.

All system commands are dispatched through the injected
`ICommandRunner` as PowerShell scriptblocks
(`powershell.exe -NoProfile -NonInteractive -Command "..."`).

This feature complements — and does not duplicate — the Windows
service-lifecycle surface owned by `3-service-management`.

---

## Requirements

### Requirement 1: Create a Resource Group

**User Story:** As a maintenance coordinator, I want to declare
a named resource group with optional initial ceilings, so that
subsequent attach / set-* calls have a target.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_group(spec)` with
   `spec.name == "batch"` THE SYSTEM SHALL render a JSON spec
   document carrying every populated field on the
   `WindowsResourceGroupSpec` and write it to
   `<group_dir>\batch.json` via PowerShell `Set-Content`.
2. WHEN the on-disk JSON spec already matches the rendered
   document byte-for-byte THE SYSTEM SHALL return
   `status=SKIPPED` without invoking PowerShell.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` carrying the planned `Set-Content` argv.

---

### Requirement 2: Delete a Resource Group

**User Story:** As a maintenance coordinator, I want to remove
a previously created resource group's spec file.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_group(name)` THE SYSTEM
   SHALL invoke PowerShell `Remove-Item -LiteralPath
   <group_dir>\<name>.json -Force`.
2. WHEN the spec file does not exist THE SYSTEM SHALL return
   `status=SKIPPED` without invoking the runner.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 3: Set CPU Limit (best-effort)

**User Story:** As a maintenance coordinator, I want to
approximate a per-group CPU ceiling on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_cpu_limit(name, percent)`
   THE SYSTEM SHALL translate the requested percent to a
   `System.Diagnostics.ProcessPriorityClass` value:
   - `percent <= 25` -> `Idle`
   - `25 < percent <= 50` -> `BelowNormal`
   - `50 < percent <= 75` -> `Normal`
   - `75 < percent < 100` -> `AboveNormal`
   - `percent >= 100` -> `High`
2. THE SYSTEM SHALL invoke PowerShell `(Get-Process -Id
   <pid>).PriorityClass = '<class>'` for every PID listed in
   the spec's `attached_pids`, in a single composite command.
3. WHEN the spec's existing `cpu_percent` field already maps
   to the same priority class THE SYSTEM SHALL return
   `status=SKIPPED`.
4. THE SYSTEM SHALL also persist the new `cpu_percent` value
   into the spec file via `Set-Content`.
5. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

The mapping is INTENTIONALLY coarse — Windows has no native
CPU-percent throttle.  The mapping is documented in the design
note.

---

### Requirement 4: Set Memory Limit (FAILED with windows detail)

**User Story:** As a maintenance coordinator using a cross-OS
configuration, I want `set_memory_limit` to fail loudly on
Windows so my orchestration tooling can detect the gap.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_memory_limit(name, bytes_)`
   on Windows THE SYSTEM SHALL return `status=FAILED` with
   `detail="windows_no_powershell_job_object_memory_limit"` and
   `message="PowerShell exposes no clean Job Object memory limit cmdlet"`
   WITHOUT invoking the runner.
2. THE SYSTEM SHALL persist the requested `memory_bytes` into
   the spec file (recording intent) ONLY when the verb is
   later promoted to a full implementation; the wave-5.3 pilot
   does NOT persist the value.
3. THE SYSTEM SHALL include the would-have-been
   `Get-Process -Id <pid>` argv on `result.command` for audit.

---

### Requirement 5: Set IO Limits (FAILED with windows detail)

**User Story:** As a maintenance coordinator using a cross-OS
configuration, I want `set_io_limits` to fail loudly on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_io_limits(name, spec)` on
   Windows THE SYSTEM SHALL return `status=FAILED` with
   `detail="windows_no_powershell_io_limit"` and
   `message="PowerShell exposes no per-process IO bandwidth limit"`
   WITHOUT invoking the runner.

---

### Requirement 6: Attach Process

**User Story:** As a maintenance coordinator, I want to attach
a running PID to a Windows resource group, applying its
PriorityClass best-effort.

#### Acceptance Criteria

1. WHEN the coordinator calls `attach_process(name, pid)` THE
   SYSTEM SHALL append the PID to the spec's `attached_pids`
   list, write the updated spec file via `Set-Content`, then
   apply the spec's recorded `cpu_percent`-derived
   `PriorityClass` to the PID via `(Get-Process -Id
   <pid>).PriorityClass = '<class>'`.
2. WHEN the PID is already in the spec's `attached_pids` THE
   SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the spec file does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="group_not_found"`.
4. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 7: Detach Process

**User Story:** As a maintenance coordinator, I want to detach
a PID from a Windows resource group, restoring its default
PriorityClass.

#### Acceptance Criteria

1. WHEN the coordinator calls `detach_process(name, pid)` THE
   SYSTEM SHALL remove the PID from the spec's `attached_pids`,
   re-write the spec file, then reset the PID's PriorityClass
   to `Normal` via `(Get-Process -Id <pid>).PriorityClass =
   'Normal'`.
2. WHEN the PID is NOT in the spec's `attached_pids` THE
   SYSTEM SHALL return `status=SKIPPED`.

---

### Requirement 8: List Resource Groups

**User Story:** As a maintenance coordinator, I want to
enumerate every resource group the manager knows about.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_groups()` THE SYSTEM SHALL
   invoke PowerShell `Get-ChildItem -LiteralPath <group_dir>
   -Filter '*.json' | Select-Object -ExpandProperty Name |
   ConvertTo-Json -Compress` and parse the JSON output.
2. FOR each entry THE SYSTEM SHALL `Get-Content` the spec
   file, parse the JSON, and assemble a
   `WindowsResourceGroupInfo` record.
3. THE SYSTEM SHALL return an empty list (not raise) when no
   spec files are present.
4. WHEN any underlying PowerShell call exits non-zero AND its
   stderr is non-empty THE SYSTEM SHALL raise
   `WindowsResourceControlQueryError`.

---

### Requirement 9: Get Group Usage

**User Story:** As a maintenance coordinator, I want a
per-group usage snapshot on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_group_usage(name)` THE
   SYSTEM SHALL read the group's spec file, then invoke
   PowerShell `Get-Process -Id <pid> | Select-Object
   WorkingSet64,CPU | ConvertTo-Json -Compress` for each
   attached PID.
2. THE SYSTEM SHALL aggregate the parsed `WorkingSet64`
   values for `memory_used_bytes`.
3. THE SYSTEM SHALL aggregate the parsed `CPU` (cumulative
   seconds) values for `cpu_usage_seconds`.
4. THE SYSTEM SHALL set `io_read_bytes` and `io_write_bytes`
   to `None` (Windows `Get-Process` does not surface per-PID
   IO byte counters in a portable PowerShell shape).
5. WHEN the group's spec file does not exist THE SYSTEM SHALL
   raise `WindowsResourceControlQueryError` with
   `detail="unknown_group"`.

---

### Requirement 10: Idempotency

**User Story:** As a maintenance coordinator, I want every
write verb to short-circuit when the requested state already
holds.

#### Acceptance Criteria

1. THE SYSTEM SHALL implement the per-verb idempotency rules
   listed in Requirements 1-7 above.
2. THE SYSTEM SHALL NOT invoke `Set-Content` or
   `Get-Process` mutating cmdlets when the idempotency guard
   fires.
3. THE SYSTEM SHALL still record the would-have-been
   PowerShell scriptblock on the returned
   `WindowsResourceControlActionResult.command` field for
   audit.

---

### Requirement 11: Elevation

**User Story:** As a maintenance coordinator, I want every
write verb to elevate when the manager is configured for
elevation, and every read verb to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated == True` THE SYSTEM SHALL pass
   `elevated=True` to the runner for write-verb mutating
   commands.
2. THE SYSTEM SHALL NOT pass `elevated=True` for the
   idempotency-guard `Get-Content` / `Test-Path` probes.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for read verbs
   (`list_groups`, `get_group_usage`).

---

### Requirement 12: Dry-Run, and Error Reporting

**User Story:** As a maintenance coordinator, I want every
mutating operation to respect dry-run and surface failures
structurally.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   PowerShell mutating cmdlets.
2. WHEN the runner returns non-zero THE SYSTEM SHALL return
   `WindowsResourceControlActionResult(status=FAILED,
   exit_code=<code>, stdout=<captured>, stderr=<captured>,
   command=<argv>)`.
3. THE SYSTEM SHALL NOT raise from any write verb on a
   dispatched-command failure.

---

### Requirement 13: Documented Windows Limitations

**User Story:** As a maintenance coordinator running cross-OS
configurations, I want the manager's gaps relative to the
contract documented in one place.

#### Acceptance Criteria

1. THE SYSTEM SHALL surface the following limitations
   prominently in the design note and in the affected verb
   docstrings:
   - `set_memory_limit` returns `FAILED` with
     `detail="windows_no_powershell_job_object_memory_limit"`.
   - `set_io_limits` returns `FAILED` with
     `detail="windows_no_powershell_io_limit"`.
   - `set_cpu_limit` is a best-effort `PriorityClass`
     translation; it is NOT a percent ceiling.  Documented in
     the verb's docstring.
   - `attach_process` records the PID in the spec and applies
     the recorded `PriorityClass`; the PID is NOT a member of
     a Windows Job Object.
   - `get_group_usage.io_read_bytes` /
     `get_group_usage.io_write_bytes` are always `None`;
     `Get-Process` does not surface per-PID I/O byte
     counters in a portable shape.
2. THE SYSTEM SHALL include the gap details in
   `result.message` so callers can branch on them
   programmatically.

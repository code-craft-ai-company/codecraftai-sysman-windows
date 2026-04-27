# Requirements

## Introduction

The repository management feature provides the `automojo-sysman-windows`
package with the ability to manage `winget source` entries on Windows
systems.  This spec implements the repository-management contract
defined at
`automojo-sysman-common/specs/contracts/repository-management/contract.md`
for Windows using `winget.exe`'s source subcommand.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* sources to configure — it provides
the capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through
the `automojo-interfaces` command-runner abstraction.

Windows's `winget source` model differs from apt/dnf and Homebrew:

- Sources are named (e.g., `winget`, `msstore`, `mycorp`).
- Sources have a type (`Microsoft.PreIndexed.Package` for standard
  manifests, or `Microsoft.Rest` for REST sources).
- `winget` does not support enabling/disabling sources without
  removing them.  `enable_repository` / `disable_repository` are
  documented as SKIPPED / FAILED respectively for winget.
- `winget` does not support priority.  `set_priority` returns
  SKIPPED (or raises NotFound).
- `winget` does not support explicit GPG key import — package
  signatures are verified via the source type's built-in mechanism.
  `import_gpg_key` is documented as SKIPPED.
- Writes to machine-scope winget sources require Administrator;
  user-scope sources do not.  The manager honors the `elevated`
  config flag for writes.

Because the contract explicitly anticipates this divergence, the
manager honors the verb set but surfaces gaps loudly.

---

## Requirements

### Requirement 1: Add a Source

**User Story:** As a maintenance coordinator, I want to add a winget
source, so that packages from that source become installable.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_repository(spec)` AND `spec.base_url`
   is set THE SYSTEM SHALL invoke `winget source add --name <spec.name>
   --arg <spec.base_url>` via the command runner.
2. WHEN `spec.channel` is set to a known winget source type
   (`Microsoft.PreIndexed.Package` or `Microsoft.Rest`) THE SYSTEM
   SHALL include `--type <channel>` in the command.
3. WHEN `spec.base_url` is NOT set THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `FAILED` and
   `message="base_url_required"` without invoking the runner.
4. WHEN the source is already present in the output of `winget
   source list` with the same URL THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `FAILED`, the exit
   code, captured stderr, and the `command`.
7. WHEN `spec.gpg_key`, `spec.mirror_list_url`, or `spec.priority`
   is set THE SYSTEM SHALL accept the spec, log a one-time warning
   that the field is not applicable to winget sources, and
   continue.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   command and SHALL return `DRY_RUN`.

---

### Requirement 2: Remove a Source

**User Story:** As a maintenance coordinator, I want to remove a
winget source, so that packages from that source are no longer
installable.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_repository(name)` THE SYSTEM
   SHALL invoke `winget source remove --name <name>` via the
   command runner.
2. WHEN the source is NOT present in the output of `winget source
   list` THE SYSTEM SHALL return a `WindowsRepositoryActionResult`
   with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a result with
   status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a result with
   status `FAILED`, the exit code, captured stderr, and the
   `command`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   command and SHALL return `DRY_RUN`.

---

### Requirement 3: Enable a Source (N/A)

**User Story:** As a maintenance coordinator, I want a uniform
`enable_repository` API on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_repository(name)` AND the
   source is present in the output of `winget source list` THE
   SYSTEM SHALL return a `WindowsRepositoryActionResult` with
   status `SKIPPED` and
   `message="winget_sources_always_enabled"`.
2. WHEN the source is NOT present THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `FAILED` and
   `message="repository_not_found"`.
3. THE SYSTEM SHALL NOT execute any mutating command for
   `enable_repository` — the method is deliberately a no-op so
   cross-OS code can call it unconditionally.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN`.

---

### Requirement 4: Disable a Source

**User Story:** As a maintenance coordinator, I want a uniform
`disable_repository` API on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_repository(name)` THE SYSTEM
   SHALL return a `WindowsRepositoryActionResult` with status
   `FAILED` and `message="winget_no_disable: remove_required"`.
2. THE SYSTEM SHALL NOT execute any command for
   `disable_repository` — winget has no soft-disable concept;
   the only way to stop using a source is to remove it via
   `remove_repository`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN` and the same message.

---

### Requirement 5: List Sources

**User Story:** As a maintenance coordinator, I want to enumerate
every winget source, so that I can audit configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_repositories()` THE SYSTEM SHALL
   invoke `winget source list` via the command runner without
   elevation.
2. WHEN the command exits non-zero THE SYSTEM SHALL raise a
   `WindowsRepositoryQueryError` carrying the exit code and
   captured stderr.
3. WHEN the command succeeds THE SYSTEM SHALL parse the tabular
   stdout (columns `Name`, `Argument`, optional `Type`,
   `Identifier`, `Explicit`) and SHALL return a
   `list[WindowsRepositoryInfo]` with one entry per non-header
   line: `name`=Name column, `display_name`=Name column,
   `enabled=True`, `url`=Argument column, `priority=None`.
4. WHEN stdout contains header rows AND separator rows (lines with
   only dashes and spaces) THE SYSTEM SHALL skip them.
5. WHEN no sources are configured THE SYSTEM SHALL return an empty
   list.

---

### Requirement 6: Get a Single Source

**User Story:** As a maintenance coordinator, I want to resolve a
source by name, so that I can inspect its state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_repository(name)` THE SYSTEM
   SHALL invoke `list_repositories()` internally and return the
   matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.
3. WHEN `list_repositories()` raises THE SYSTEM SHALL propagate the
   `WindowsRepositoryQueryError`.

---

### Requirement 7: Refresh Sources

**User Story:** As a maintenance coordinator, I want to refresh
winget source metadata.

#### Acceptance Criteria

1. WHEN the coordinator calls `refresh_repositories(name=None)` THE
   SYSTEM SHALL invoke `winget source update` via the command
   runner.
2. WHEN the coordinator calls `refresh_repositories(name=<name>)`
   THE SYSTEM SHALL invoke `winget source update --name <name>`
   via the command runner.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a result with
   status `FAILED`, the exit code, and captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN`.
6. THE SYSTEM SHALL NOT evaluate an idempotency guard on
   `refresh_repositories` — refresh is operational.

---

### Requirement 8: Set Priority (N/A)

**User Story:** As a maintenance coordinator, I want a uniform
`set_priority` API on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_priority(name, priority)` AND
   the source does not exist THE SYSTEM SHALL raise a
   `WindowsRepositoryNotFoundError` with the missing name.
2. WHEN the source exists THE SYSTEM SHALL return a
   `WindowsRepositoryActionResult` with status `SKIPPED` and
   `message="winget_has_no_priority_concept"`.
3. THE SYSTEM SHALL NOT execute any command for `set_priority`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN`.

---

### Requirement 9: Import a GPG Key (N/A)

**User Story:** As a maintenance coordinator, I want a uniform
`import_gpg_key` API on Windows.

#### Acceptance Criteria

1. WHEN the coordinator calls `import_gpg_key(key_source)` AND
   `key_source` is a local path AND the file does not exist THE
   SYSTEM SHALL raise `WindowsRepositoryKeyFileNotFoundError` —
   precondition exceptions take precedence.
2. WHEN `key_source` exists (URL or extant local file) THE SYSTEM
   SHALL return a `WindowsRepositoryActionResult` with status
   `SKIPPED` and
   `message="winget_verifies_via_source_type_signing"`.
3. THE SYSTEM SHALL NOT execute any command for `import_gpg_key`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN` (subject to the precondition exception
   rule above).

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want winget source
writes to run with the Administrator token required by
machine-scope sources.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the command runner for `winget
   source add`, `winget source remove`, and `winget source update`
   (the latter writes cached metadata under `%ProgramData%`).
2. WHEN performing read-only operations (`list_repositories`,
   `get_repository`) THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN the system is running as Administrator THE SYSTEM SHALL
   NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL
   default to no elevation and allow the runner to surface the
   resulting permission error as a
   `WindowsRepositoryActionResult` with `status=FAILED`.
5. WHEN user-scope sources are being managed THE SYSTEM SHALL
   allow the coordinator to leave `elevated=False` — the manager
   does not distinguish machine vs user scope at the verb layer;
   it is the coordinator's responsibility.

---

### Requirement 11: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that simulates source changes without mutating the system.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM
   SHALL NOT invoke `winget source add`, `winget source remove`, or
   `winget source update`.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command`
   field of every returned result with the argv that would have
   been executed.
3. WHEN dry-run is active THE SYSTEM SHALL still perform read
   operations (`winget source list` for idempotency guards) to
   evaluate SKIPPED / FAILED status correctly.
4. WHEN dry-run is active THE SYSTEM SHALL still raise
   `WindowsRepositoryNotFoundError` and
   `WindowsRepositoryKeyFileNotFoundError` — precondition
   exceptions take precedence.

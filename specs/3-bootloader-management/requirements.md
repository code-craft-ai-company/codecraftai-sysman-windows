# Requirements

## Introduction

The bootloader-management feature provides the
`codecraftai-sysman-windows` package with the ability to manage the
**OS-level bootloader configuration layer** on Windows.  It
implements the cross-OS contract defined at
`codecraftai-sysman-common/specs/contracts/bootloader-management/contract.md`.

### What this feature is NOT

This feature is **distinct from `boot-management`** (the UEFI
firmware boot-entry feature in `codecraftai-sysman-uefi`).  On Windows
the firmware-level entries are surfaced through
``bcdedit /enum firmware`` and the ``{fwbootmgr}`` BCD global; that
surface is owned by `boot-management`.  This feature addresses the
**OS-level BCD osloader / bootmgr namespaces that sit between the
firmware boot entry and the running kernel**.

| Feature | Layer | bcdedit namespace |
|---------|-------|-------------------|
| `boot-management` (UEFI) | Firmware boot entries | `firmware`, `{fwbootmgr}` (firmware side) |
| `bootloader-management` (this) | OS-level BCD config | `osloader`, `{bootmgr}`, `{<guid>}` |

### Windows BCD model

Windows' OS-level bootloader is the Boot Configuration Data (BCD)
store, manipulated through the `bcdedit` command-line utility.  The
relevant namespaces are:

1. `{bootmgr}` -- the Windows Boot Manager.  Its `default` property
   names the GUID of the active OS loader entry.
2. `osloader` -- the collection of OS loader entries
   (`{<guid>}`) that the boot manager can launch.

BCD identifiers are GUIDs wrapped in braces.  Built-in identifiers
also accept aliases such as `{bootmgr}` and `{default}`.  This
feature normalises every GUID to the canonical brace-wrapped lower-
case form before dispatching to `bcdedit`.

### Implication for the cross-OS contract

* `update_bootloader_config` and `regenerate_config` are SUCCESS
  no-ops because BCD writes take effect immediately and Windows
  does not expose a separate regenerate / apply phase.
* All writes set `requires_reboot=True` because the boot manager
  consults the store only at boot.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interface.  All
system commands (`bcdedit`) are executed through the
`codecraftai-interfaces` command-runner abstraction.  All mutating
operations respect dry-run mode.

---

## Requirements

### Requirement 1: List Boot Entries

**User Story:** As a maintenance coordinator, I want to enumerate
the BCD osloader entries known to the host so that I can verify
what the host is offering at boot time.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_entries()` THE SYSTEM SHALL
   invoke `bcdedit /enum osloader` via the runner without
   elevation.
2. THE SYSTEM SHALL also invoke `bcdedit /enum {fwbootmgr}` and
   merge any non-duplicate entries into the returned list.
3. THE SYSTEM SHALL invoke `bcdedit /enum {bootmgr} /v` and mark
   the entry whose GUID matches the `default` line with
   `is_default=True`.
4. THE SYSTEM SHALL return one `WindowsBootEntryInfo` per parsed
   block carrying `id` (canonical brace-wrapped lower-case GUID),
   `name` (BCD `description`), `kernel` (BCD `path`), and `device`
   (BCD `device`).
5. WHEN the underlying ``bcdedit /enum`` calls fail THE SYSTEM
   SHALL raise `WindowsBootloaderQueryError`.
6. THE SYSTEM SHALL never pass `elevated=True` for any of these
   reads.

---

### Requirement 2: Get Default Entry

**User Story:** As a maintenance coordinator, I want to know which
BCD entry is currently the default so that I can compare it against
the maintenance baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_default_entry()` THE SYSTEM
   SHALL invoke `bcdedit /enum {bootmgr} /v` and parse the
   `default` line for the GUID.
2. THE SYSTEM SHALL look the GUID up in the result of
   `list_entries()` and return the corresponding entry.
3. WHEN no default GUID can be parsed THE SYSTEM SHALL raise
   `WindowsBootloaderQueryError`.
4. WHEN the parsed default GUID does not match any osloader
   entry THE SYSTEM SHALL raise `WindowsBootloaderQueryError`.
5. THE SYSTEM SHALL never pass `elevated=True`.

---

### Requirement 3: Set Default Entry

**User Story:** As a maintenance coordinator, I want to change the
active default boot entry so that the next reboot lands on the
declared OS loader.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_default_entry(entry_id=<guid>)`
   THE SYSTEM SHALL normalise the GUID (accepting both bare and
   brace-wrapped forms) and SHALL invoke
   `bcdedit /default {<guid>}` via the runner with elevated
   privileges.
2. WHEN the supplied GUID is already the default THE SYSTEM SHALL
   return `SKIPPED` and SHALL NOT issue the `bcdedit` call.
3. WHEN the supplied value is not a valid GUID THE SYSTEM SHALL
   return `FAILED` with `stderr` token `invalid_guid` and SHALL
   NOT invoke `bcdedit`.
4. WHEN the `bcdedit` call fails THE SYSTEM SHALL return
   `FAILED` carrying the runner stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.
6. THE SYSTEM SHALL set `requires_reboot=True` on success.

---

### Requirement 4: Add Boot Entry

**User Story:** As a maintenance coordinator, I want to register a
new BCD osloader entry so that operators can offer a custom OS
loader at boot time.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `add_entry(spec=WindowsBootEntrySpec(...))` THE SYSTEM SHALL
   invoke
   ``bcdedit /create [{<guid>}] /d "<name>" /application <application>``
   via the runner with elevated privileges.
2. WHEN ``spec.id`` is `None` THE SYSTEM SHALL omit the GUID from
   the create command and SHALL parse the auto-generated GUID
   from the ``bcdedit /create`` stdout.
3. WHEN ``spec.id`` is a non-GUID, non-alias value THE SYSTEM SHALL
   return `FAILED` with `stderr` token `invalid_guid`.
4. AFTER ``bcdedit /create`` succeeds THE SYSTEM SHALL invoke
   ``bcdedit /set {<guid>} <key> <value>`` for each of: `device`
   (when ``spec.device`` is set), `path` (when ``spec.path`` is
   set), and every key/value pair in ``spec.options``.
5. AFTER all ``/set`` calls succeed THE SYSTEM SHALL invoke
   ``bcdedit /displayorder {<guid>} /addlast`` so the entry
   appears in the boot menu.
6. WHEN any step fails THE SYSTEM SHALL return `FAILED` with the
   appropriate `command`, `exit_code`, and `stderr` populated.
7. WHEN an entry with the same GUID and matching ``description``
   is already present in the BCD store THE SYSTEM SHALL return
   `SKIPPED` and SHALL NOT issue any of the create / set /
   displayorder calls.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 5: Remove Boot Entry

**User Story:** As a maintenance coordinator, I want to remove a
BCD entry so that operators no longer see it at boot time.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_entry(entry_id=<guid>)` THE
   SYSTEM SHALL invoke `bcdedit /delete {<guid>}` via the runner
   with elevated privileges.
2. WHEN no entry with the supplied GUID is present THE SYSTEM
   SHALL return `SKIPPED` and SHALL NOT issue the `bcdedit` call.
3. WHEN the supplied value is not a valid GUID THE SYSTEM SHALL
   return `FAILED` with `stderr` token `invalid_guid`.
4. WHEN the `bcdedit` call fails THE SYSTEM SHALL return
   `FAILED` carrying the runner stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 6: Update Bootloader Config (No-Op)

**User Story:** As a maintenance coordinator, I want a single verb
that exists for cross-OS contract conformance even though Windows
BCD writes are immediate.

#### Acceptance Criteria

1. WHEN the coordinator calls `update_bootloader_config()` THE
   SYSTEM SHALL return `SUCCESS` without invoking the runner.
2. THE RESULT SHALL carry a `message` indicating that BCD writes
   are immediate and that no separate update step exists.
3. THE RESULT SHALL set `requires_reboot=False`.

---

### Requirement 7: Regenerate Config (No-Op)

**User Story:** As a maintenance coordinator, I want a single verb
that exists for cross-OS contract conformance even though Windows
BCD is its own source of truth.

#### Acceptance Criteria

1. WHEN the coordinator calls `regenerate_config()` THE SYSTEM
   SHALL return `SUCCESS` without invoking the runner.
2. THE RESULT SHALL carry a `message` indicating the no-op
   rationale.
3. THE RESULT SHALL set `requires_reboot=False`.

---

### Requirement 8: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeat calls
to the same write verb to be no-ops when the host already matches
the desired state, so that maintenance runs are safe to re-execute.

#### Acceptance Criteria

1. `set_default_entry()` SHALL skip when the supplied GUID is
   already the default.
2. `add_entry()` SHALL skip when an entry with the same GUID and
   matching description is already present.
3. `remove_entry()` SHALL skip when no entry with the supplied
   GUID is present.
4. `update_bootloader_config()` and `regenerate_config()` SHALL
   return SUCCESS unconditionally.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes to
elevate when configured but reads to never elevate, so that the
manager works on hosts that do not run as Administrator.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (`set_default_entry`, `add_entry`, `remove_entry`) THE SYSTEM
   SHALL pass `elevated=True` to the runner for every
   ``bcdedit`` invocation that mutates state.
2. WHEN `config.elevated=False` THE SYSTEM SHALL never pass
   `elevated=True`.
3. WHEN the verb is a read (`list_entries`, `get_default_entry`)
   THE SYSTEM SHALL never pass `elevated=True`, regardless of the
   config.  HKLM-rooted ``bcdedit /enum`` calls typically require
   Administrator on UAC-protected hosts; the runner is
   responsible for surfacing the access-denied error when the
   process lacks elevation.
4. Idempotency probes (the ``bcdedit /enum`` reads) MUST never
   elevate.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run
mode that exercises the planning logic without touching the host.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` AND a write verb is invoked THE
   SYSTEM SHALL NOT call `runner.run` for the mutating step.
2. THE SYSTEM SHALL still return a `WindowsBootloaderActionResult`
   with status `DRY_RUN` and the `command` field populated with
   the argv that would have been executed.
3. Read verbs SHALL execute unchanged in dry-run mode -- reads do
   not mutate.

---

### Requirement 11: Distinction From UEFI Boot-Management

**User Story:** As a maintenance coordinator, I want the
bootloader-management feature to never accidentally touch the UEFI
firmware NVRAM so that the firmware boot entries remain owned by
`codecraftai-sysman-uefi`.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER invoke ``bcdedit /enum firmware``.
2. THE SYSTEM SHALL NEVER invoke ``bcdedit /set {fwbootmgr} ...``;
   the only ``{fwbootmgr}``-touching call is the read-only
   ``bcdedit /enum {fwbootmgr}`` used to merge any
   firmware-side entries the operator has explicitly added (per
   the cross-OS contract).
3. THE SYSTEM SHALL document the distinction in the design.md
   and the module docstring so that downstream readers do not
   confuse the two layers.

---

### Requirement 12: Destructive-Change Safety

**User Story:** As a maintenance coordinator, I want clear signals
when a write changes the BCD store so that I can pair the change
with a backup-and-verify workflow.

#### Acceptance Criteria

1. THE SYSTEM SHALL set `requires_reboot=True` on every successful
   write result.
2. THE SYSTEM SHALL surface the underlying ``bcdedit`` stderr
   verbatim on `FAILED` results so that operators can triage
   misconfigurations.
3. THE SYSTEM SHALL document in the module docstring that BCD
   misconfiguration can render the host unbootable and that
   callers SHOULD pair every write with a ``bcdedit /export``
   backup and SHOULD verify the resulting state through
   ``list_entries`` before rebooting.

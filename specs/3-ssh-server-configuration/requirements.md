# Requirements

## Introduction

The ssh-server-configuration feature provides the
`codecraftai-sysman-windows` package with the ability to manage the
OpenSSH server configuration on Windows hosts: top-level `sshd_config`
keywords, `Match` blocks, configuration validation, daemon restart,
and per-user `authorized_keys` entries.  This spec implements the
ssh-server-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/ssh-server-configuration/contract.md`
for Windows.

A maintenance coordinator running in an external package drives all
operations.  All system commands (`sshd.exe`, PowerShell cmdlets) are
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction.  No `subprocess`, no direct `open`, and no direct
`pathlib` calls are made inside this module.

The Windows OpenSSH server (the `OpenSSH.Server` capability) stores
its configuration at **`%ProgramData%\ssh\sshd_config`** (typically
`C:\ProgramData\ssh\sshd_config`).  The daemon is exposed as the
`sshd` Windows service.

### Admin-vs-Non-Admin Authorized Keys (Windows-specific)

Windows OpenSSH treats users in the `Administrators` group specially.
The stock `sshd_config` ends with the directive:

```
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
```

Two distinct paths are therefore in play:

- **Per-user**: `%UserProfile%\.ssh\authorized_keys` for ordinary
  users (typically `C:\Users\<user>\.ssh\authorized_keys`).
- **Admin shared**: `%ProgramData%\ssh\administrators_authorized_keys`
  for every Administrator-group user (shared across all admins).

The `WindowsSshServerConfigurationManager` exposes both paths through
its config and accepts an `is_admin: bool` keyword on the
authorized-key verbs.  Callers MUST decide whether the supplied user
is an admin because Windows determines admin status from group
membership, not from the username.

---

## Requirements

### Requirement 1: Read a Top-Level Setting

**User Story:** As a maintenance coordinator, I want to read a single
`sshd_config` keyword's value, so that I can inspect declared policy
without parsing the whole file.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_sshd_setting(keyword)` THE SYSTEM
   SHALL read `%ProgramData%\ssh\sshd_config` via PowerShell
   `Get-Content -Path '<path>' -Raw -Encoding utf8` (no elevation)
   and parse it line-by-line.
2. WHEN the keyword exists at the top level THE SYSTEM SHALL return a
   `WindowsSshSettingValue` with `keyword`, `value`, and
   `source_file` populated.
3. WHEN the keyword is absent THE SYSTEM SHALL return `None`.
4. WHEN the source file does not exist THE SYSTEM SHALL return
   `None`.
5. WHEN `get_sshd_setting` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 2: Set a Top-Level Setting

**User Story:** As a maintenance coordinator, I want to set a
`sshd_config` keyword to a value, so that policy is applied
declaratively.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_sshd_setting(keyword, value)` THE
   SYSTEM SHALL read the current file via `Get-Content`, parse,
   mutate the keyword in memory, re-render, and write the result via
   `Set-Content -Path '<path>' -Value $content -NoNewline -Encoding
   utf8` with the rendered content supplied through stdin
   (`[Console]::In.ReadToEnd()`).
2. WHEN the rendered content equals the existing content THE SYSTEM
   SHALL return `status=SKIPPED` without invoking `Set-Content`.
3. WHEN the keyword exists with a different value THE SYSTEM SHALL
   replace it in the rendered content.
4. WHEN the file does not exist THE SYSTEM SHALL create it with a
   single line `keyword value`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED` with
   `exit_code`, `stdout`, `stderr`, and `command` populated.

---

### Requirement 3: Remove a Top-Level Setting

**User Story:** As a maintenance coordinator, I want to remove a
`sshd_config` keyword, so that the daemon falls back to its default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_sshd_setting(keyword)` and the
   file does not exist THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the keyword is not present THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the keyword is present THE SYSTEM SHALL parse, drop every
   top-level occurrence, re-render, and write via `Set-Content`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 4: List Top-Level Settings

**User Story:** As a maintenance coordinator, I want to enumerate every
top-level `sshd_config` keyword, so that I can audit the declared
configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_sshd_settings()` and the file does
   not exist THE SYSTEM SHALL return an empty dict.
2. WHEN the file exists THE SYSTEM SHALL parse and return a `dict[str,
   str]` of every top-level entry.
3. WHEN `list_sshd_settings` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 5: Add a Match Block

**User Story:** As a maintenance coordinator, I want to declare a
`Match <criteria>` block with a set of child settings.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_match_block(criteria, settings)`
   THE SYSTEM SHALL read the current file, parse, upsert the block
   identified by `criteria` (whitespace-collapsed comparison),
   re-render, and write via `Set-Content`.
2. WHEN a block with identical criteria AND identical settings already
   exists THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN a block with the same criteria exists with different settings
   THE SYSTEM SHALL replace the block's settings.
4. WHEN no matching block exists THE SYSTEM SHALL append a new block
   to the end of the file.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 6: Remove a Match Block

**User Story:** As a maintenance coordinator, I want to remove a
`Match` block, so that scoped policy can be retired.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_match_block(criteria)` and no
   block with that criteria exists THE SYSTEM SHALL return
   `status=SKIPPED`.
2. WHEN the file does not exist THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN a matching block exists THE SYSTEM SHALL parse, drop the
   block, re-render, and write via `Set-Content`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 7: List Match Blocks

**User Story:** As a maintenance coordinator, I want to enumerate every
`Match` block, so that scoped policy (including the stock
administrators block) can be audited.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_match_blocks()` and the file does
   not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse every `Match` block
   and return a `list[WindowsSshMatchBlock]` in declaration order.
3. WHEN `list_match_blocks` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 8: Reload sshd

**User Story:** As a maintenance coordinator, I want to restart the
sshd service, so that configuration changes take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_sshd()` THE SYSTEM SHALL invoke
   `powershell.exe -NoProfile -NonInteractive -Command "Restart-
   Service -Name 'sshd'"` via the runner.
2. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the
   runner.
3. WHEN the restart succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the restart fails THE SYSTEM SHALL return `status=FAILED`
   with `exit_code` and `stderr` populated.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.

---

### Requirement 9: Validate sshd Configuration

**User Story:** As a maintenance coordinator, I want to validate the
current `sshd_config`, so that policy errors can be caught before a
restart.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_sshd_config()` THE SYSTEM SHALL
   invoke `sshd.exe -t` via the runner with `elevated=elevated`.
2. WHEN exit code is 0 THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN exit code is non-zero THE SYSTEM SHALL return `status=FAILED`
   with the captured `stderr` populated.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 10: List Authorized Keys

**User Story:** As a maintenance coordinator, I want to enumerate the
keys for either a specific user (per-user file) or for the
Administrators group (shared file), so that I can audit access.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_authorized_keys(user)` and the
   file does not exist THE SYSTEM SHALL return an empty list.
2. WHEN `is_admin=False` THE SYSTEM SHALL read the per-user path
   resolved from `authorized_keys_path_template` (default
   `C:\Users\{user}\.ssh\authorized_keys`).
3. WHEN `is_admin=True` THE SYSTEM SHALL read the
   `admin_authorized_keys_path` (default
   `C:\ProgramData\ssh\administrators_authorized_keys`) and SHALL
   ignore the supplied `user` value for path resolution.
4. THE SYSTEM SHALL parse each non-comment, non-empty line into a
   `WindowsSshAuthorizedKey` with `key_type`, `key_data`, `comment`,
   `fingerprint`, and `options`.
5. WHEN `list_authorized_keys` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 11: Add an Authorized Key

**User Story:** As a maintenance coordinator, I want to add a public
key to either a user's `authorized_keys` or the
administrators-shared file.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_authorized_key(user, key)` THE
   SYSTEM SHALL parse `key` as an OpenSSH `authorized_keys` line.
2. WHEN parsing fails THE SYSTEM SHALL return `status=FAILED` with
   `message="key line failed to parse"` and SHALL NOT invoke the
   runner.
3. WHEN `is_admin=True` THE SYSTEM SHALL write the
   `admin_authorized_keys_path` instead of the per-user path.
4. WHEN the existing authorized-keys file already contains an entry
   with the same SHA-256 fingerprint THE SYSTEM SHALL return
   `status=SKIPPED`.
5. WHEN the key is new THE SYSTEM SHALL append the line to the file
   content and write it via `Set-Content`.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
7. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 12: Remove an Authorized Key

**User Story:** As a maintenance coordinator, I want to remove a
specific key by fingerprint from either a user's `authorized_keys` or
the administrators-shared file.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_authorized_key(user,
   fingerprint)` and the file does not exist THE SYSTEM SHALL return
   `status=SKIPPED`.
2. WHEN no entry has the supplied fingerprint THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN a matching entry exists THE SYSTEM SHALL drop every line
   whose computed fingerprint matches and write the new content via
   `Set-Content`.
4. WHEN `is_admin=True` THE SYSTEM SHALL operate on the
   `admin_authorized_keys_path` instead of the per-user path.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 13: Elevation Policy

**User Story:** As a maintenance coordinator, I want all writes that
target `%ProgramData%\ssh\sshd_config` (or the admin-shared
authorized-keys file) to elevate, so that operations succeed on
standard Windows hosts where the process does not run with
Administrator privileges.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the
   runner for every write verb (`set_sshd_setting`,
   `remove_sshd_setting`, `add_match_block`, `remove_match_block`,
   `add_authorized_key`, `remove_authorized_key`, `reload_sshd`,
   `test_sshd_config`).
2. WHEN performing reads THE SYSTEM SHALL pass `elevated=False`
   regardless of config.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on
   every path.
4. (Pilot follow-up) Per-user `authorized_keys` writes for the
   calling user technically do not require elevation; the pilot
   honours `config.elevated` uniformly and tracks the optimisation
   in `tasks.md`.

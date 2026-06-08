# Requirements

## Introduction

This spec implements the acl-management contract defined at
`codecraftai-sysman-common/specs/contracts/acl-management/contract.md`
for Windows using PowerShell `Get-Acl` / `Set-Acl` for reads and the
combined PowerShell + `icacls.exe` toolchain for writes — the
canonical NTFS DACL surface shipped with every supported Windows
edition.

The ACL management feature provides the `codecraftai-sysman-windows`
package with the ability to query and mutate NTFS Discretionary
Access Control Lists (DACLs) on individual filesystem paths.  A
maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs.  The feature
does not decide which ACL a path should carry — it provides the
capability to apply, observe, and idempotently re-apply DACLs
reliably and with full auditability.  Every system command is
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction; this module does not import `subprocess`, does not call
`open`, and does not consume an `IFileOperator`.  Persistence is
owned by `Set-Acl` and `icacls` themselves (the kernel-level
SECURITY_DESCRIPTOR stored on the NTFS file record); the manager
relies on these tools to write the on-disk state.

The OS-prefixed types in this module are `WindowsAclManager`,
`WindowsAclManagerConfig`, `WindowsAclActionStatus` (alias of the
package-shared `ActionStatus`), `WindowsAclActionResult`,
`WindowsAceInfo`, `WindowsAceSpec`, `WindowsAclInfo`,
`WindowsAcePermission`, `WindowsAceInheritance`, and
`WindowsAclQueryError`.

This feature complements — and does not duplicate — the basic-
permission surface owned by `3-filesystem-management` (the
`icacls /grant` shorthand emulation of POSIX `chmod` mode bits and
the `icacls /setowner` emulation of `chown`).  NTFS DACLs are
conceptually a superset of the classic owner / group / other model
and orthogonal to filesystem flags surfaced by `attrib`; this module
operates exclusively on the explicit ACE list, leaving mode-bit
approximation and ownership writes to their respective sibling
features.

---

## Requirements

### Requirement 1: Query the Full ACL of a Path

**User Story:** As a maintenance coordinator, I want to read the
current Windows DACL of a file or directory, so that compliance
checks and diffs are possible.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_acl(path)` THE SYSTEM SHALL
   invoke a PowerShell scriptblock wrapping
   `Get-Acl -Path '<path>' | Select-Object Owner,Group,Sddl,Access |
   ConvertTo-Json -Compress -Depth 5` through the command runner
   with `elevated=False` and parse the output into a
   `WindowsAclInfo`.
2. THE SYSTEM SHALL populate `WindowsAclInfo.owner` and
   `WindowsAclInfo.group` from the JSON `Owner` / `Group` fields.
3. THE SYSTEM SHALL populate `WindowsAclInfo.sddl` from the JSON
   `Sddl` field — the canonical NTFS round-trip serialisation.
4. THE SYSTEM SHALL populate `WindowsAclInfo.aces` with every entry
   in the JSON `Access` array, in source order, as `WindowsAceInfo`
   records, each carrying the `principal`, `permission`, `rights`,
   `inheritance`, and `is_inherited` fields.
5. THE SYSTEM SHALL accept the JSON `Access` shape both as a list
   (multi-ACE) and as a bare object (single-ACE) — `ConvertTo-Json`
   collapses single-element arrays into the bare object form.
6. THE SYSTEM SHALL populate `WindowsAclInfo.default_aces` with an
   empty list — Windows DACLs have no default-ACL partition.
7. WHEN the PowerShell scriptblock exits non-zero THE SYSTEM SHALL
   raise `WindowsAclQueryError` carrying the failed argv, exit
   code, and captured stderr.
8. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 2: Replace the Full ACL on a Path

**User Story:** As a maintenance coordinator, I want to replace the
entire explicit DACL on a path with a declared specification, so
that access aligns deterministically with the desired posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_acl(path, acl_spec)` THE SYSTEM
   SHALL first invoke `icacls '<path>' /reset` to clear all explicit
   ACEs, then invoke
   `icacls '<path>' /grant:r '<principal>:<rights>'` (or
   `/deny '<principal>:<rights>'`) per ACE in `acl_spec`.
2. WHEN the parsed current ACL is tuple-equal to `acl_spec` (on the
   explicit, non-inherited ACEs) THE SYSTEM SHALL return
   `status=SKIPPED` without invoking either `icacls` step.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner at all and SHALL populate the
   would-have-been argv on `command`.
4. WHEN the runner reports a non-zero exit on either step THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, and `command` populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for every `icacls` step.
6. WHEN a per-ACE write step fails THE SYSTEM SHALL short-circuit
   and return that step's `FAILED` result; subsequent ACEs SHALL
   NOT be dispatched.
7. WHEN `acl_spec` carries a rights token outside the canonical
   `[FileSystemRights]` vocabulary THE SYSTEM SHALL return
   `status=FAILED` with a descriptive `message` BEFORE invoking
   the runner.

---

### Requirement 3: Add a Single ACE

**User Story:** As a maintenance coordinator, I want to add a single
ACE for a named principal with a specific rights set, so that I can
grant access incrementally without reasserting the full ACL.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_ace(path, principal, rights)` THE
   SYSTEM SHALL invoke `icacls '<path>' /grant:r
   '<principal>:<rendered>'` (or `/deny` when the spec's permission
   is `DENY`) where `<rendered>` is the parenthesised rights body
   prefixed by any inheritance flag tokens.
2. WHEN the parsed ACL already contains an explicit (non-inherited)
   ACE with the same principal / permission / rights-set /
   inheritance tuple THE SYSTEM SHALL return `status=SKIPPED`
   without invoking `icacls`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. THE SYSTEM SHALL populate `result.principal` with the spec's
   `principal` value on every returned result.
7. WHEN the spec carries a rights token outside the canonical
   `[FileSystemRights]` vocabulary THE SYSTEM SHALL return
   `status=FAILED` with a descriptive `message` BEFORE invoking the
   runner.

---

### Requirement 4: Remove ACEs by Principal

**User Story:** As a maintenance coordinator, I want to remove every
ACE belonging to a named principal, so that access can be revoked
without enumerating each ACE individually.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_ace(path, principal)` THE
   SYSTEM SHALL invoke `icacls '<path>' /remove '<principal>'` —
   the `/remove` flag accepts only the principal and removes every
   matching grant / deny on that path.
2. WHEN the parsed ACL contains no explicit (non-inherited) ACE
   matching the principal THE SYSTEM SHALL return `status=SKIPPED`
   without invoking `icacls`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 5: Clear All Explicit ACEs

**User Story:** As a maintenance coordinator, I want to drop every
explicit ACE from a path, so that access reverts to the inherited-
only state.

#### Acceptance Criteria

1. WHEN the coordinator calls `clear_acl(path)` THE SYSTEM SHALL
   invoke `icacls '<path>' /reset`.
2. WHEN the parsed ACL contains zero explicit (non-inherited) ACEs
   THE SYSTEM SHALL return `status=SKIPPED` without invoking
   `icacls`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. THE SYSTEM SHALL NOT remove inherited ACEs; `/reset` is documented
   to leave the inherited posture intact.

---

### Requirement 6: Copy ACL Between Paths

**User Story:** As a maintenance coordinator, I want to copy the ACL
from one path onto another, so that I can replicate access posture
between siblings without re-rendering the full spec.

#### Acceptance Criteria

1. WHEN the coordinator calls `copy_acl(src, dst)` THE SYSTEM SHALL
   invoke a single PowerShell pipeline of the form
   `Get-Acl -Path '<src>' | Set-Acl -Path '<dst>'` — the canonical
   NTFS-native form that travels the security descriptor as a
   `[FileSystemSecurity]` object directly between cmdlets.
2. WHEN the parsed source `Sddl` string equals the parsed
   destination `Sddl` string (the canonical NTFS round-trip form)
   THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `Get-Acl` on `src` exits non-zero THE SYSTEM SHALL return
   `status=FAILED` populated with the read step's runner state.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the pipeline; the per-side
   `Get-Acl` probes never elevate.

---

### Requirement 7: Enumerate ACEs for a Path

**User Story:** As a maintenance coordinator, I want to enumerate
every ACE attached to a single path, so that compliance reports can
summarise per-path posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_aces(path)` THE SYSTEM SHALL
   re-use the read path used by `get_acl` and return a flat
   `list[WindowsAceInfo]`.
2. THE SYSTEM SHALL emit ACEs in the source order reported by
   `Get-Acl` (their natural index ordering as serialised by
   `ConvertTo-Json`).
3. WHEN the underlying `Get-Acl` invocation exits non-zero THE
   SYSTEM SHALL raise `WindowsAclQueryError`.
4. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 8: Allow / Deny Polarity

**User Story:** As a maintenance coordinator, I want allow ACEs and
deny ACEs to be expressible, because Windows DACLs support both
polarities and a deny ACE is the only way to express "this principal
can NOT do X regardless of group inheritance".

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `WindowsAcePermission` with values
   `ALLOW` and `DENY`.
2. THE SYSTEM SHALL render `WindowsAcePermission.ALLOW` as the
   `icacls /grant:r` verb and `WindowsAcePermission.DENY` as the
   `icacls /deny` verb in every write argument.
3. THE SYSTEM SHALL parse the JSON `AccessControlType` field of
   `Get-Acl`'s `Access` records into the corresponding enum value,
   accepting both the integer encoding (`0=Allow`, `1=Deny`) and
   the string encoding (`"Allow"` / `"Deny"`) — both shapes are
   emitted by different PowerShell host versions.
4. THE SYSTEM SHALL treat permission as part of the idempotency
   tuple — an `ALLOW` ACE and a `DENY` ACE for the same principal
   and rights set are NOT considered identical.

---

### Requirement 9: Inheritance Support

**User Story:** As a maintenance coordinator, I want directory ACEs
to optionally carry inheritance flags, so that newly-created
children inherit declared access automatically.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `WindowsAceInheritance` with values
   `NONE`, `OBJECT_INHERIT`, `CONTAINER_INHERIT`, and `BOTH`.
2. WHEN a `WindowsAceSpec` carries `inheritance=OBJECT_INHERIT` THE
   SYSTEM SHALL prefix the rights body with `(OI)`.  `CONTAINER_INHERIT`
   prefixes `(CI)`.  `BOTH` prefixes `(OI)(CI)`.
3. WHEN parsing a `Get-Acl` ACE record's `InheritanceFlags` field
   THE SYSTEM SHALL coerce both the integer encoding (`0=None`,
   `1=ContainerInherit`, `2=ObjectInherit`, `3=both`) and the
   string encoding (`"None"`, `"ContainerInherit"`, `"ObjectInherit"`,
   `"ContainerInherit, ObjectInherit"`) into the corresponding
   `WindowsAceInheritance` value.
4. THE SYSTEM SHALL include `inheritance` in the idempotency tuple
   for `add_ace` and `set_acl` comparisons.

---

### Requirement 10: Rights-Token Vocabulary and Validation

**User Story:** As a maintenance coordinator, I want unknown rights
tokens to be rejected at the manager boundary, so that typos return
typed `FAILED` results instead of opaque `icacls` stderr messages.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept `rights` as a `list[str]` of
   `[FileSystemRights]` token strings.
2. THE SYSTEM SHALL accept the canonical `[FileSystemRights]`
   vocabulary documented by
   `System.Security.AccessControl.FileSystemRights`:
   `FullControl`, `Modify`, `ReadAndExecute`, `ListDirectory`,
   `Read`, `Write`, `ReadData`, `WriteData`, `AppendData`,
   `Delete`, `DeleteSubdirectoriesAndFiles`, `ChangePermissions`,
   `TakeOwnership`, `Synchronize`, `ExecuteFile`, `ReadAttributes`,
   `WriteAttributes`, `ReadExtendedAttributes`,
   `WriteExtendedAttributes`, `ReadPermissions`, `Traverse`.
3. WHEN any token in the rights list falls outside the canonical
   vocabulary THE SYSTEM SHALL return `status=FAILED` with a
   `message` enumerating the rejected tokens BEFORE invoking the
   runner.
4. THE SYSTEM SHALL render the canonical short-letter aliases
   (`FullControl`->`F`, `Modify`->`M`, `ReadAndExecute`->`RX`,
   `Read`->`R`, `Write`->`W`) when present.  Long-form tokens are
   rendered verbatim — `icacls` accepts both forms in the
   parenthesised body.

---

### Requirement 11: Elevation, Dry-Run, and Aspect Composition

**User Story:** As a maintenance coordinator, I want every mutating
verb to honour the manager's elevation policy and to be safe under
arbitrary aspect composition on the runner, so that the manager
behaves identically against a `LocalCommandRunner`,
`SSHCommandRunner`, or any wrapped variant.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner on every write step
   (`icacls /reset`, `icacls /grant:r`, `icacls /deny`,
   `icacls /remove`, the `Get-Acl | Set-Acl` pipeline).
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL never pass `elevated=True`.
3. THE SYSTEM SHALL never pass `elevated=True` on any read
   (`get_acl`, `list_aces`, every idempotency probe, the source-
   side and destination-side reads of `copy_acl`).
4. WHEN `dry_run=True` THE SYSTEM SHALL never invoke the runner for
   a mutating verb.
5. THE SYSTEM SHALL hold no state across calls beyond the injected
   config and runner.
6. THE SYSTEM SHALL be safe under any retry / persistence /
   timeout aspect composition on the runner.

---

### Requirement 12: Error Reporting and No Direct Filesystem Access

**User Story:** As an operator, I want failed ACL writes to surface
the underlying tool's error output, and I want guarantees that no
direct filesystem access bypasses the runner-driven dispatch model.

#### Acceptance Criteria

1. WHEN any `icacls` invocation exits non-zero THE SYSTEM SHALL
   return `WindowsAclActionResult` populated with `status=FAILED`,
   `exit_code`, `stdout`, `stderr`, and `command`.
2. WHEN `Get-Acl` exits non-zero on a read verb THE SYSTEM SHALL
   raise `WindowsAclQueryError`.
3. THE SYSTEM SHALL NOT import `subprocess`, `os.system`, or any
   process-spawning module other than indirectly through the
   command runner.
4. THE SYSTEM SHALL NOT call `open()` to read or write any path.
5. THE SYSTEM SHALL NOT consume an `IFileOperator`.

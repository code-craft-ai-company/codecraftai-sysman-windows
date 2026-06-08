# Requirements

## Introduction

The samba-server-management feature provides the
`codecraftai-sysman-windows` package with the ability to manage the
SMB-share surface on Windows hosts.  Windows ships its own SMB
server as part of the base operating system; the canonical
management toolchain is the PowerShell ``SmbShare`` /
``SmbShareAccess`` cmdlet family (``New-SmbShare`` /
``Remove-SmbShare`` / ``Get-SmbShare`` /
``Grant-SmbShareAccess`` / ``Revoke-SmbShareAccess``).  Windows
SMB is **NOT** a Samba binary — there is no ``smb.conf``, no
``smbpasswd`` user database, and no ``pdbedit`` enumeration tool.
Authentication against an SMB share routes through the local
account database (or a domain controller for domain-joined hosts).

This spec implements the cross-OS samba-server-management contract
defined at
``codecraftai-sysman-common/specs/contracts/samba-server-management/contract.md``
for Windows, scoped to native SMB.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  All
PowerShell scriptblocks are dispatched through the
``codecraftai-interfaces`` command-runner abstraction with the
canonical ``powershell.exe -NoProfile -NonInteractive -Command``
argv shape so that ``$PROFILE`` cannot mutate cmdlet behaviour and
PowerShell never blocks waiting for interactive input.

## Native SMB vs Samba distinction

Because Windows does NOT use the Samba project's user database,
``add_samba_user`` and ``remove_samba_user`` are delegated to
``user-group-management`` and return ``status=FAILED`` with an
explanatory ``message`` and ``detail``.  ``list_samba_users``
returns the SMB-share access map (``Get-SmbShareAccess``) as the
closest available analogue — it enumerates which principals have
which permission on each share rather than a flat list of "Samba
users".

This feature complements — and does not duplicate — the
``LanmanServer`` Windows service lifecycle owned by
``3-service-management``, the SMB-port firewall surface owned by
``3-firewall-management``, and the local account surface owned by
``3-user-group-management``.

---

## Requirements

### Requirement 1: Add a Share

**User Story:** As a maintenance coordinator, I want to register
an SMB share via ``New-SmbShare``, so that the declared share set
matches the desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls ``add_share(spec)`` THE SYSTEM SHALL
   dispatch ``New-SmbShare -Name '<name>' -Path '<path>'
   -Description '<comment>' -FullAccess <users>``.
2. WHEN ``Get-SmbShare -Name '<name>' | ConvertTo-Json`` already
   reports a share with matching ``Name`` and ``Path`` THE SYSTEM
   SHALL return ``status=SKIPPED``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.
4. WHEN ``New-SmbShare`` exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.

---

### Requirement 2: Remove a Share

#### Acceptance Criteria

1. WHEN the coordinator calls ``remove_share(name)`` THE SYSTEM
   SHALL dispatch ``Remove-SmbShare -Name '<name>' -Force``.
2. WHEN no share with the given name is registered THE SYSTEM
   SHALL return ``status=SKIPPED``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.

---

### Requirement 3: List Shares

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_shares()`` THE SYSTEM SHALL
   dispatch ``Get-SmbShare | ConvertTo-Json -Compress -Depth 5``
   with ``elevated=False`` and parse the JSON output into
   ``list[WindowsSambaShareInfo]``.
2. WHEN the cmdlet exits non-zero AND ``stderr`` is non-empty THE
   SYSTEM SHALL raise ``WindowsSambaQueryError``.

---

### Requirement 4: Get Share

#### Acceptance Criteria

1. WHEN the coordinator calls ``get_share(name)`` THE SYSTEM SHALL
   dispatch ``Get-SmbShare -Name '<name>' | ConvertTo-Json
   -Compress -Depth 5``.
2. WHEN no share with the given name exists THE SYSTEM SHALL
   return ``None``.
3. WHEN ``get_share`` is invoked THE SYSTEM SHALL pass
   ``elevated=False``.

---

### Requirement 5: Set Share Permissions

#### Acceptance Criteria

1. WHEN the coordinator calls ``set_share_permissions(name, acl)``
   THE SYSTEM SHALL dispatch one ``Grant-SmbShareAccess -Name
   '<name>' -AccountName '<user>' -AccessRight <token> -Force``
   per principal in ``acl.read_users`` / ``acl.write_users`` /
   ``acl.full_access_users`` and one ``Revoke-SmbShareAccess
   -Name '<name>' -AccountName '<user>' -Force`` per principal in
   ``acl.denied_users``.
2. WHEN the existing access map already matches the requested ACL
   (order-insensitive on principals per access right) THE SYSTEM
   SHALL return ``status=SKIPPED``.
3. WHEN no share with the given name is registered THE SYSTEM
   SHALL return ``status=SKIPPED``.
4. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.

---

### Requirement 6: Reload Config (no-op SUCCESS)

**User Story:** Windows commits SMB share changes synchronously,
so there is no per-share reload analogue to ``smbcontrol`` /
``nfsd update``.

#### Acceptance Criteria

1. WHEN the coordinator calls ``reload_config()`` THE SYSTEM SHALL
   return ``status=SUCCESS`` immediately WITHOUT invoking the
   runner.  The recorded ``command`` field SHALL be a sanitised
   placeholder argv documenting the no-op.
2. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   with the same placeholder argv.
3. ``reload_config`` MUST NOT skip — it always reports SUCCESS or
   DRY_RUN.

---

### Requirement 7: Add a Samba User (Delegated)

#### Acceptance Criteria

1. THE SYSTEM SHALL return ``status=FAILED`` with
   ``detail="add_samba_user delegated"`` and ``message`` carrying
   the delegation explanation directing callers to
   ``user-group-management``.
2. THE SYSTEM SHALL NEVER place the password on argv.  The
   recorded ``command`` field SHALL be a sanitised placeholder argv
   that does not contain the password.

---

### Requirement 8: Remove a Samba User (Delegated)

Same shape as Requirement 7.

---

### Requirement 9: List Samba Users

**User Story:** Windows has no flat "Samba user list" analogue;
this verb returns the SMB-share access map (which principals
have which access right on each share) as the closest available
information.

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_samba_users()`` THE SYSTEM
   SHALL dispatch ``Get-SmbShareAccess -Name '*' | ConvertTo-Json
   -Compress -Depth 5`` and parse the result into
   ``list[WindowsSambaUserInfo]`` (one record per unique account
   seen across the access map).
2. WHEN the cmdlet exits non-zero AND ``stderr`` is non-empty THE
   SYSTEM SHALL raise ``WindowsSambaQueryError``.
3. WHEN ``list_samba_users`` is invoked THE SYSTEM SHALL pass
   ``elevated=False``.

---

### Requirement 10: Elevation Policy

#### Acceptance Criteria

1. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True``
   for every write verb that actually dispatches.
2. WHEN performing reads THE SYSTEM SHALL pass ``elevated=False``.

---

### Requirement 11: Dry-Run

#### Acceptance Criteria

1. WHEN ``dry_run=True`` THE SYSTEM SHALL NOT dispatch any
   mutating cmdlet.
2. WHEN ``dry_run=True`` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions
   (``Get-SmbShare`` / ``Get-SmbShareAccess``).
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   with the would-have-been argv on the result.

---

### Requirement 12: Failure Reporting

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return ``status=FAILED`` with ``exit_code``,
   ``stdout``, ``stderr``, and ``command`` populated.
2. THE SYSTEM SHALL NEVER include passphrase material in the
   recorded ``command`` field on any returned result.

---

### Requirement 13: Idempotency Across Verbs

#### Acceptance Criteria

1. ``add_share`` MUST be idempotent on (name, path).
2. ``remove_share`` MUST skip when the share is not registered.
3. ``set_share_permissions`` MUST skip when the existing
   ``Get-SmbShareAccess`` map matches the requested ACL.
4. ``add_samba_user`` and ``remove_samba_user`` MUST surface a
   FAILED result with the delegation message.
5. ``reload_config`` MUST NOT skip — it always reports SUCCESS or
   DRY_RUN (there is no underlying command to dispatch).

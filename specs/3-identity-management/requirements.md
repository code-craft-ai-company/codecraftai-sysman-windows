# Requirements

## Introduction

The identity management feature provides the
`automojo-sysman-windows` package with the ability to join a Windows
host to an Active Directory domain and resolve directory-backed users
and groups against the local SAM and the AD directory.  This spec
implements the cross-OS contract defined at
`automojo-sysman-common/specs/contracts/identity-management/contract.md`
for Windows using PowerShell cmdlets: `Add-Computer` /
`Remove-Computer` for join / unjoin, `Get-WmiObject
Win32_ComputerSystem` for membership query, `Get-ADUser` /
`Get-ADGroup` (RSAT Active Directory module) for directory-backed
principal resolution with a `Get-LocalUser` / `Get-LocalGroup`
fallback when RSAT is not installed, and `klist purge` for Kerberos
ticket cache invalidation.

A maintenance coordinator drives all operations by calling into this
feature's public surface.  The feature does not decide *which* domain
the host should join — it provides the capability to *manipulate
domain membership and inspect directory state* reliably,
idempotently, and with full auditability.  Every system command is
executed exclusively through the `automojo-interfaces` command-runner
abstraction, enabling dry-run simulation, auditing, and test mocking
without requiring a live system.  Administrator passwords land
inside the PowerShell scriptblock as a SecureString but are
sanitised to the literal `<redacted>` token in the argv recorded on
the `WindowsIdentityActionResult` so the password never leaks into
audit captures.

This feature does NOT manage local user accounts (see
`3-user-group-management`), does NOT configure Group Policy / MDM
(out of scope for the cross-OS contract), and does NOT manage
certificate trust anchors (see `3-certificate-trust-management`).

---

## Requirements

### Requirement 1: Join the host to an Active Directory domain

**User Story:** As a maintenance coordinator, I want to join a
Windows host to an Active Directory domain using supplied admin
credentials, so that the host participates in the declared identity
directory.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsRealmJoinSpec` with
   `realm`, `admin_user`, and `admin_password` THE SYSTEM SHALL
   build a PowerShell scriptblock that constructs a
   `PSCredential` from the admin user and a SecureString-wrapped
   password, then invokes `Add-Computer -DomainName <realm>
   -Credential $cred -Force -Restart:$false` (extended with
   `-NewName <spec.computer_name>`, `-OUPath <spec.ou_path>`, and
   `-Server <spec.domain_controller>` when those fields are set)
   and dispatch the canonical PowerShell argv (`powershell.exe
   -NoProfile -NonInteractive -Command <script>`) through the
   runner with `elevated=True` when configured.
2. THE SYSTEM SHALL replace every embedded
   `ConvertTo-SecureString -AsPlainText -Force '<pw>'` in the
   recorded argv with `ConvertTo-SecureString -AsPlainText -Force
   '<redacted>'` so the password never leaks into audit captures.
3. WHEN `Win32_ComputerSystem` already shows the host joined to
   `spec.realm` THE SYSTEM SHALL skip execution and return a
   `WindowsIdentityActionResult` with `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return a `WindowsIdentityActionResult` with
   `status=DRY_RUN` and the would-have-been (sanitised) argv in
   `command`.
5. WHEN `Add-Computer` succeeds THE SYSTEM SHALL return a
   `WindowsIdentityActionResult` with `status=SUCCESS`,
   `realm=spec.realm`, `username=spec.admin_user`, and the captured
   `exit_code`, `stdout`, and `stderr`.
6. WHEN `Add-Computer` fails THE SYSTEM SHALL return a
   `WindowsIdentityActionResult` with `status=FAILED`, the captured
   diagnostics, and the sanitised argv in `command`.

---

### Requirement 2: Unjoin the host from a domain

**User Story:** As a maintenance coordinator, I want to remove a
Windows host from a domain using supplied admin credentials, so
that decommissioned hosts no longer authenticate against the
directory.

#### Acceptance Criteria

1. WHEN the coordinator calls `leave_realm(admin_user,
   admin_password, realm)` THE SYSTEM SHALL build a PowerShell
   scriptblock that constructs a `PSCredential` and invokes
   `Remove-Computer -UnjoinDomainCredential $cred -PassThru -Force
   -Restart:$false` and dispatch it with `elevated=True` when
   configured.
2. THE SYSTEM SHALL sanitise the recorded argv as for `join_realm`.
3. WHEN `Win32_ComputerSystem.PartOfDomain` is False THE SYSTEM
   SHALL return `status=SKIPPED` without invoking
   `Remove-Computer`.
4. WHEN the caller supplies a non-`None` `realm` argument and the
   currently-joined realm differs (case-insensitive) THE SYSTEM
   SHALL return `status=SKIPPED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.
6. WHEN `Remove-Computer` succeeds THE SYSTEM SHALL return
   `status=SUCCESS`; on failure, `status=FAILED` with captured
   diagnostics.

---

### Requirement 3: Query domain-join status

**User Story:** As a maintenance coordinator, I want to know whether
a Windows host is currently joined to a domain, the domain name, and
the host's computer name, so that downstream logic can branch on
join state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_realm_status()` THE SYSTEM SHALL
   dispatch `Get-WmiObject Win32_ComputerSystem | Select-Object
   Domain, PartOfDomain, DomainRole, Name | ConvertTo-Json` through
   the runner without elevation.
2. WHEN `PartOfDomain` is True THE SYSTEM SHALL return
   `WindowsRealmStatus(joined=True)` with `current_realm` set to
   the parsed `Domain` field, `realm_kind=ACTIVE_DIRECTORY`, and
   `computer_name` set to the parsed `Name` field.
3. WHEN `PartOfDomain` is False THE SYSTEM SHALL return
   `WindowsRealmStatus(joined=False)` with `current_realm` set to
   the parsed workgroup name, `realm_kind=WORKGROUP`, and
   `computer_name` populated.
4. WHEN the WMI query exits non-zero or the JSON cannot be parsed
   THE SYSTEM SHALL raise `WindowsIdentityQueryError`.

---

### Requirement 4: List joined realms (single-domain reality)

**User Story:** As a maintenance coordinator that drives a
multi-OS rollout, I want a uniform list-of-realms verb on Windows,
so that my coordinator code does not have to special-case the
platform.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_joined_realms()` THE SYSTEM
   SHALL return a list of zero or one `WindowsRealmInfo` records.
2. WHEN the host is joined to a domain THE SYSTEM SHALL return a
   single-element list whose entry carries the joined realm, the
   `ACTIVE_DIRECTORY` kind, and `is_default=True`.
3. WHEN the host is in a workgroup THE SYSTEM SHALL return an
   empty list.
4. THE SYSTEM SHALL document that Windows hosts are
   single-domain by design — there is no multi-realm surface to
   enumerate.

---

### Requirement 5: Synchronise users from the directory

**User Story:** As a maintenance coordinator, I want to flush the
host's Kerberos ticket cache so the next AD lookup re-acquires a
fresh TGT, so that newly-added or recently-modified directory
accounts become visible without waiting.

#### Acceptance Criteria

1. WHEN the coordinator calls `sync_users()` THE SYSTEM SHALL
   dispatch `klist purge -li 0x3e7` (the Local System LUID)
   through the runner with `elevated=True` when configured.
2. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.
3. THE SYSTEM SHALL NOT apply an idempotency guard.  Every call
   MUST invoke `klist`.
4. WHEN `klist` exits non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured diagnostics; this is
   best-effort because hosts without a system-LUID ticket cache
   may surface errors that are operationally benign.

---

### Requirement 6: Resolve a user

**User Story:** As a maintenance coordinator, I want to resolve a
username through `Get-ADUser` (RSAT) with a `Get-LocalUser`
fallback, so that downstream logic can branch on existence and
the user's UID / GID / home / shell / realm without parsing
PowerShell output itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `resolve_user(username)` THE SYSTEM
   SHALL first dispatch `Get-ADUser -Identity <username> -Properties
   * | Select-Object SamAccountName, DistinguishedName,
   UserPrincipalName, MemberOf, HomeDirectory, uidNumber,
   gidNumber, loginShell | ConvertTo-Json` through the runner
   without elevation.
2. WHEN `Get-ADUser` exits zero THE SYSTEM SHALL parse the JSON
   record into a `WindowsUserResolveResult` with `found=True`,
   populated `uid` (if `uidNumber` is present), `gid` (if
   `gidNumber` is present), `home` (`HomeDirectory`),
   `shell` (`loginShell`), `realm` (extracted from the `DC=`
   components of `DistinguishedName`), and `groups` (parsed CN
   components of `MemberOf`).
3. WHEN `Get-ADUser` exits non-zero AND the stderr looks like an
   RSAT-missing error AND `config.fallback_to_local` is True THE
   SYSTEM SHALL fall back to `Get-LocalUser -Name <username>`
   and return a `WindowsUserResolveResult` with `found=True` and
   `realm=None`.
4. WHEN both attempts fail THE SYSTEM SHALL return a
   `WindowsUserResolveResult` with `found=False`.

---

### Requirement 7: Resolve a group

**User Story:** As a maintenance coordinator, I want to resolve a
groupname through `Get-ADGroup` (RSAT) with a `Get-LocalGroup`
fallback, so that downstream logic can branch on existence, the
group's GID / members / realm without parsing PowerShell output
itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `resolve_group(groupname)` THE
   SYSTEM SHALL first dispatch `Get-ADGroup -Identity <groupname>
   -Properties * | Select-Object SamAccountName,
   DistinguishedName, Members, gidNumber | ConvertTo-Json`
   through the runner without elevation.
2. WHEN `Get-ADGroup` exits zero THE SYSTEM SHALL parse the JSON
   record into a `WindowsGroupResolveResult` with `found=True`,
   populated `gid`, `members` (parsed CN components of `Members`),
   and `realm` extracted from the DN.
3. WHEN `Get-ADGroup` fails and `config.fallback_to_local` is True
   THE SYSTEM SHALL fall back to `Get-LocalGroup -Name <groupname>`
   plus `Get-LocalGroupMember -Group <groupname>` and return a
   `WindowsGroupResolveResult` with `found=True`, `realm=None`,
   and `members` populated from the fallback.
4. WHEN both attempts fail THE SYSTEM SHALL return a
   `WindowsGroupResolveResult` with `found=False`.

---

### Requirement 8: Dry-run, elevation, and credential handling

**User Story:** As a maintenance coordinator, I want every mutating
operation to respect dry-run, run with declared elevation, surface
failures structurally, and route credentials safely.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT contact a
   domain controller and SHALL NOT dispatch any mutating command.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch every
   write verb (`join_realm`, `leave_realm`, `sync_users`) with
   `elevated=True` to the runner.
3. WHEN `config.elevated=True` and the coordinator calls a read
   verb (`get_realm_status`, `list_joined_realms`, `resolve_user`,
   `resolve_group`) THE SYSTEM SHALL still dispatch the underlying
   probe with `elevated=False`.
4. THE SYSTEM SHALL replace every embedded
   `ConvertTo-SecureString -AsPlainText -Force '<pw>'` in the
   recorded argv with `ConvertTo-SecureString -AsPlainText -Force
   '<redacted>'`.
5. WHEN the runner returns a non-zero exit code on a write THE
   SYSTEM SHALL return a `WindowsIdentityActionResult` with
   `status=FAILED` and the captured `exit_code` and `stderr`.

---

### Requirement 9: Realm-kind classification

**User Story:** As a maintenance coordinator, I want directory back-
end kinds reported in a normalised enum, so that platform-agnostic
logic does not have to interpret WMI shapes.

#### Acceptance Criteria

1. WHEN the host is joined to an AD domain THE SYSTEM SHALL report
   `WindowsRealmKind.ACTIVE_DIRECTORY`.
2. WHEN the host is in a workgroup THE SYSTEM SHALL report
   `WindowsRealmKind.WORKGROUP`.
3. WHEN `Win32_ComputerSystem` reports a value that cannot be
   classified THE SYSTEM SHALL report `WindowsRealmKind.UNKNOWN`
   rather than raise.

---

### Requirement 10: Configurable executables

**User Story:** As a test author, I want to inject an alternate
PowerShell executable path so that pwsh-based test rigs can target
PowerShell 7+ without code changes.

#### Acceptance Criteria

1. THE `WindowsIdentityManagerConfig` dataclass SHALL expose a
   `powershell_executable: str = "powershell.exe"` override.
2. EVERY argv built by the manager SHALL use the configured
   executable name as its argv[0].

---

### Requirement 11: RSAT availability and local fallback

**User Story:** As a maintenance coordinator that operates against
hosts without RSAT, I want directory-resolution verbs to fall back
to the local SAM, so that minimal automation images still surface
useful results.

#### Acceptance Criteria

1. THE `WindowsIdentityManagerConfig` dataclass SHALL expose a
   `requires_rsat: bool = True` advisory flag and a
   `fallback_to_local: bool = True` flag.
2. WHEN `fallback_to_local=True` AND `Get-ADUser` /
   `Get-ADGroup` fails THE SYSTEM SHALL try `Get-LocalUser` /
   `Get-LocalGroup` and surface the result with `realm=None`.
3. WHEN `fallback_to_local=False` THE SYSTEM SHALL return
   `found=False` on AD-cmdlet failure rather than fall back.
4. THE SYSTEM SHALL NOT use RSAT-specific cmdlets in the write
   verbs (`Add-Computer` and `Remove-Computer` ship with the
   built-in PowerShell module).

---

### Requirement 12: Query-error surface

**User Story:** As a maintenance coordinator, I want unexpected
read failures to surface as a typed exception rather than silent
empty data, so that broken WMI installations do not look like
"workgroup" hosts.

#### Acceptance Criteria

1. THE `WindowsIdentityQueryError` exception SHALL be raised by
   `get_realm_status` (and, transitively, `list_joined_realms`)
   when the WMI query exits non-zero.
2. THE exception SHALL carry the failing argv, exit code, and
   stderr for diagnostics.
3. `resolve_user` and `resolve_group` SHALL NOT raise; they SHALL
   return a `found=False` result on any failure (and try the local
   fallback first when configured).

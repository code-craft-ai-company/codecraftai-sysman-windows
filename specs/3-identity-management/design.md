# Design

## Overview

The identity management feature is implemented as a single module,
`automojo.sysman.windows.identitymanagement`, that exposes a
`WindowsIdentityManager` class.  This spec implements the cross-OS
identity-management contract defined at
`automojo-sysman-common/specs/contracts/identity-management/contract.md`
for Windows using PowerShell cmdlets: `Add-Computer` /
`Remove-Computer` (built-in `Microsoft.PowerShell.Management`
module) for join / unjoin, `Get-WmiObject Win32_ComputerSystem` for
membership query, `Get-ADUser` / `Get-ADGroup` (RSAT
`ActiveDirectory` module) for directory-backed principal resolution
with a `Get-LocalUser` / `Get-LocalGroup` fallback when RSAT is not
installed, and `klist purge` for Kerberos ticket cache invalidation.

Every PowerShell scriptblock is dispatched as
`["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
"<script>"]` so that `$PROFILE` cannot mutate cmdlet behaviour and
PowerShell never blocks waiting for interactive input on a runner
that has no controlling terminal.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct registry access or
`subprocess` calls are made inside the module.

Administrator passwords for `join_realm` and `leave_realm` land
inside the scriptblock as `ConvertTo-SecureString -AsPlainText
-Force '<pw>'` and are immediately wrapped into a `PSCredential`.
The recorded `command` field on the `WindowsIdentityActionResult`
replaces every embedded plaintext password with the literal
`<redacted>` token using the same regex-based sanitiser established
by `WindowsUserGroupManager`.

The feature works identically whether commands execute locally or
remotely over WinRM / SSH.

**See Also:**
- `3-user-group-management` for local-account operations.
- `3-certificate-trust-management` for the enterprise-CA anchors
  required by LDAPS / Kerberos-over-TLS setups.
- `automojo-sysman-linux-redhat/specs/3-identity-management/` for
  the FreeIPA / SSSD / realmd reference implementation that
  motivated this cross-OS contract.
- `automojo-sysman-linux-ubuntu/specs/3-identity-management/` for
  the Ubuntu sibling implementation using `realm` + `sssctl`.
- `automojo-sysman-macos/specs/3-identity-management/` for the
  macOS sibling implementation using `dsconfigad` + `dscl`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsIdentityManager               │
│  - Supplies WindowsRealmJoinSpec with admin credentials     │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsIdentityManager                           │
│  automojo.sysman.windows.identitymanagement                 │
│                                                             │
│  Writes:                                                    │
│    join_realm()    leave_realm()    sync_users()            │
│  Reads:                                                     │
│    get_realm_status()    list_joined_realms()               │
│    resolve_user()        resolve_group()                    │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsIdentityManager`

**Location:** `source/packages/automojo/sysman/windows/identitymanagement.py`

| Method | Signature | Description |
|---|---|---|
| `join_realm` | `(spec) → WindowsIdentityActionResult` | `Add-Computer -DomainName <r> -Credential $cred ...` |
| `leave_realm` | `(admin_user, admin_password, realm=None) → WindowsIdentityActionResult` | `Remove-Computer -UnjoinDomainCredential $cred ...` |
| `get_realm_status` | `() → WindowsRealmStatus` | `Win32_ComputerSystem` parsed |
| `list_joined_realms` | `() → list[WindowsRealmInfo]` | List of zero or one |
| `sync_users` | `() → WindowsIdentityActionResult` | `klist purge -li 0x3e7` |
| `resolve_user` | `(username) → WindowsUserResolveResult` | `Get-ADUser` with `Get-LocalUser` fallback |
| `resolve_group` | `(groupname) → WindowsGroupResolveResult` | `Get-ADGroup` with `Get-LocalGroup` fallback |

### `WindowsIdentityManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `powershell_executable` | `str` | `"powershell.exe"` | Path / name of the PowerShell binary |
| `requires_rsat` | `bool` | `True` | Advisory flag; reserved for follow-up RSAT-availability probes |
| `fallback_to_local` | `bool` | `True` | When True, AD-cmdlet failures fall back to `Get-LocalUser` / `Get-LocalGroup` |

---

## Data Models

### Outcome / status

```python
class WindowsIdentityActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class WindowsIdentityActionResult(FeatureActionResult):
    realm: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None
```

### Realm models

```python
class WindowsRealmKind(Enum):
    ACTIVE_DIRECTORY = "active_directory"
    WORKGROUP        = "workgroup"
    UNKNOWN          = "unknown"


@dataclass
class WindowsRealmJoinSpec:
    realm: str
    admin_user: str
    admin_password: str
    computer_name: Optional[str] = None
    ou_path: Optional[str] = None
    domain_controller: Optional[str] = None


@dataclass
class WindowsRealmInfo:
    realm: str
    realm_kind: WindowsRealmKind
    is_default: bool = False
    domain_controller: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class WindowsRealmStatus:
    joined: bool
    current_realm: Optional[str] = None
    realm_kind: Optional[WindowsRealmKind] = None
    computer_name: Optional[str] = None
```

### Resolve models

```python
@dataclass
class WindowsUserResolveResult:
    username: str
    uid: Optional[int] = None
    gid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    realm: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    found: bool = False


@dataclass
class WindowsGroupResolveResult:
    groupname: str
    gid: Optional[int] = None
    members: list[str] = field(default_factory=list)
    realm: Optional[str] = None
    found: bool = False
```

### Exceptions

```python
class WindowsIdentityQueryError(Exception): ...
```

`WindowsIdentityQueryError` is raised by `get_realm_status` (and
transitively by `list_joined_realms` and `_is_realm_joined`) when
the WMI query fails.  `resolve_user` / `resolve_group` do NOT raise;
they return a `found=False` result on any failure (and try the local
fallback first when configured).

---

## Realm-Kind Mapping

The cross-OS contract defines four `RealmKind` values:
`ACTIVE_DIRECTORY`, `FREEIPA`, `OPEN_DIRECTORY`, `KERBEROS_ONLY`.
Windows supports only `ACTIVE_DIRECTORY` (via `Add-Computer`):

| Cross-OS `RealmKind` | Windows support | Notes |
|---|---|---|
| `ACTIVE_DIRECTORY` | Yes | `Add-Computer` / `Remove-Computer` |
| `WORKGROUP` | Yes (Windows-specific kind) | Reported when `PartOfDomain=False` |
| `FREEIPA` | Not supported | No first-class FreeIPA path on Windows |
| `OPEN_DIRECTORY` | Not supported | macOS-only |
| `KERBEROS_ONLY` | Not supported | Per-user `klist`/`kinit` exists but is out of scope |

### AD vs local SAM divergence

Windows hosts always have a local SAM (Security Accounts Manager)
in addition to whatever directory binding is in place:

- A workgroup host has only the local SAM; users / groups live in
  `HKLM\SAM` and are surfaced by `Get-LocalUser` /
  `Get-LocalGroup`.
- A domain-joined host can resolve both local SAM principals and
  AD principals; `Get-ADUser` / `Get-ADGroup` (RSAT) targets the
  domain, while `Get-LocalUser` / `Get-LocalGroup` targets the
  local SAM.

The cross-OS contract abstracts this divergence behind a single
`resolve_user` / `resolve_group` verb pair.  This implementation
prefers the AD path (richer attribute set, group memberships, real
realm provenance) but falls back to the local SAM when RSAT is
unavailable.  The `realm` field on the result distinguishes the two
sources: AD-resolved records carry the realm extracted from the
DN; local-resolved records carry `realm=None`.

### Why no FreeIPA / Open Directory

Windows has no first-class FreeIPA bind path.  Hosts can be joined
to a FreeIPA realm only through extra software (e.g., FreeIPA's
Windows enrollment agent) which is out of scope.  Coordinators
that need IPA-on-Windows should fall through to a Linux peer.
The cross-OS contract is satisfied for Windows by the AD path
alone.

---

## Key Method Designs

### `join_realm(spec)`

1. Build the scriptblock:
   ```
   $pw = ConvertTo-SecureString -AsPlainText -Force '<pw>';
   $cred = New-Object PSCredential('<user>', $pw);
   Add-Computer -DomainName '<realm>' -Credential $cred
       [-NewName '<cn>'] [-OUPath '<ou>'] [-Server '<dc>']
       -Force -Restart:$false
   ```
2. Wrap into the canonical PowerShell argv.
3. Sanitise the recorded argv (replace embedded password literals
   with `<redacted>`).
4. If `config.dry_run` → return `DRY_RUN`.
5. Probe `_is_realm_joined(realm=spec.realm)` via
   `Win32_ComputerSystem`; when already joined → `SKIPPED`.
6. Dispatch the plaintext argv with `elevated=config.elevated`.
7. Map the runner result to `WindowsIdentityActionResult` with
   `realm=spec.realm`, `username=spec.admin_user`, and the
   sanitised argv on `command`.

### `leave_realm(admin_user, admin_password, realm=None)`

1. Build the scriptblock:
   ```
   $pw = ConvertTo-SecureString -AsPlainText -Force '<pw>';
   $cred = New-Object PSCredential('<user>', $pw);
   Remove-Computer -UnjoinDomainCredential $cred -PassThru
       -Force -Restart:$false
   ```
2. Sanitise the recorded argv.
3. If `config.dry_run` → return `DRY_RUN`.
4. Probe `get_realm_status()`; when not joined → `SKIPPED`.  When
   a non-`None` `realm` argument is supplied and the joined realm
   differs (case-insensitive) → `SKIPPED`.
5. Dispatch and map.

### `sync_users()`

1. Build the scriptblock: `klist purge -li 0x3e7`.
2. If `config.dry_run` → return `DRY_RUN`.
3. Dispatch with `elevated=config.elevated`.

### `get_realm_status()` / `list_joined_realms()`

1. `_win32_computersystem()` runs
   `Get-WmiObject Win32_ComputerSystem | Select-Object Domain,
   PartOfDomain, DomainRole, Name | ConvertTo-Json` and returns
   the parsed dict.  Non-zero exit raises
   `WindowsIdentityQueryError`.
2. `_materialise_realm_status` inspects `PartOfDomain` to decide
   `joined`; the Domain field becomes `current_realm`; the kind
   is `ACTIVE_DIRECTORY` when joined and `WORKGROUP` otherwise.
3. `list_joined_realms` calls `get_realm_status` and returns a
   single-element list when `joined=True`, otherwise empty.

### `resolve_user(username)` / `resolve_group(groupname)`

1. Try `_get_aduser_record` / `_get_adgroup_record`:
   `Get-ADUser -Identity <u> -Properties * | Select-Object ... |
   ConvertTo-Json -Compress -Depth 5`.
2. On non-zero exit, parse-failure, or RSAT-missing stderr marker,
   return `None` from the helper.
3. When the AD record is `None` and `config.fallback_to_local`:
   - `_get_localuser_record` runs `Get-LocalUser -Name <u> |
     Select-Object Name, SID, Enabled, FullName, Description |
     ConvertTo-Json -Compress -Depth 5`.
   - For groups, additionally enumerate
     `Get-LocalGroupMember -Group <g> | Select Name |
     ConvertTo-Json` and attach the parsed names as
     `record["__Members"]` so the materialiser can surface
     them.
4. Materialise the user / group resolve result from whichever
   record came back; AD records carry `realm` extracted from
   the `DC=` components of `DistinguishedName`, local records
   carry `realm=None`.

### Argv sanitiser

`_sanitise_script_passwords` walks the scriptblock string with the
regex `ConvertTo-SecureString\s+-AsPlainText\s+-Force\s+'((?:[^']|'')*)'`
and replaces every match's body with `<redacted>`.  This matches
the pattern used by `WindowsUserGroupManager.set_password` and
preserves the rest of the scriptblock so audit reviewers can still
see exactly which cmdlet was invoked.

---

## Dry-Run Behaviour

- `dry_run=True` disables: `join_realm`, `leave_realm`, `sync_users`.
- `dry_run=True` allows: `get_realm_status`, `list_joined_realms`,
  `resolve_user`, `resolve_group`.

When `dry_run=True`, the manager returns `DRY_RUN` without
dispatching the read-side `Win32_ComputerSystem` probe normally
performed by `_is_realm_joined`, so dry-run mode never stalls on a
slow / unreachable WMI subsystem.

---

## Correctness Properties

1. **Password sanitisation in recorded argv.** Every embedded
   `ConvertTo-SecureString -AsPlainText -Force '<pw>'` literal in
   the recorded scriptblock is replaced with the same wrapper
   carrying `<redacted>`.
2. **Idempotent join / leave.** `join_realm` returns `SKIPPED` when
   already joined to the requested realm; `leave_realm` returns
   `SKIPPED` when not joined or joined to a different realm.
3. **Dry-run blocks remote contact.** When `config.dry_run=True`,
   no PowerShell scriptblock is dispatched.
4. **Reads do not elevate.** `get_realm_status`,
   `list_joined_realms`, `resolve_user`, and `resolve_group`
   always dispatch with `elevated=False`, even when
   `config.elevated=True`.
5. **Query-error surface is typed.** `WindowsIdentityQueryError`
   carries the failing argv, exit code, and stderr.
6. **RSAT optional.** `resolve_user` / `resolve_group` work on
   hosts without the RSAT AD module, returning local-SAM results
   with `realm=None`.

---

## Testing Strategy

- `_MockCommandRunner` recording every `run` call.  Tests use a
  substring-matching helper to seed results because the test
  scriptblock varies in its escape / indentation details across
  calls.
- Canonical `Win32_ComputerSystem` JSON for joined and workgroup
  states.
- Canonical `Get-ADUser` / `Get-ADGroup` JSON records with
  representative DN, MemberOf, uidNumber, gidNumber attributes.
- Synthetic local-user record for the fallback path.
- Assertions for:
  - The presence of expected `Add-Computer` / `Remove-Computer`
    flags in the dispatched scriptblock.
  - `elevated` flag propagation through writes.
  - Non-elevation of reads.
  - Recorded argv carrying `<redacted>` inside every
    `ConvertTo-SecureString -AsPlainText -Force '...'` literal.
  - Idempotency-skip paths for `join_realm` / `leave_realm`.
  - Dry-run paths returning `DRY_RUN` without invoking the runner.
  - `get_realm_status` / `list_joined_realms` parsing.
  - `resolve_user` / `resolve_group` parsing for AD records,
    fall-back to local on RSAT-missing stderr, and `found=False`
    when both attempts fail.
  - `WindowsIdentityQueryError` propagation on WMI failure.
  - Captured `exit_code` and `stderr` on a synthetic failure.

No live Active Directory or RSAT installation required.

---

## Module Layout

```
source/packages/automojo/sysman/windows/
└── identitymanagement.py

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_identitymanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Local-account creation | `3-user-group-management` | Out of scope; `resolve_user` only reads |
| Enterprise CA trust for LDAPS / Kerberos-over-TLS | `3-certificate-trust-management` | Coordinator installs the anchor first, then this feature joins |
| Group Policy / MDM policy | Out of scope (cross-OS contract) | This feature only handles `Add-Computer`-driven join state |
| `klist` per-user ticket lifecycle | Out of contract scope | `sync_users` purges the system LUID only; per-user is a follow-up |

---

## Cross-OS Identity-Model Concept Mapping

The cross-OS contract is intentionally lossy.  This table maps the
abstract concepts to the concrete Windows surfaces:

| Cross-OS concept | Windows surface |
|---|---|
| Realm join | `Add-Computer -DomainName <d> -Credential $cred ...` |
| Realm leave | `Remove-Computer -UnjoinDomainCredential $cred ...` |
| Realm membership probe | `Get-WmiObject Win32_ComputerSystem` (single-domain reality) |
| Directory cache refresh | `klist purge -li 0x3e7` |
| User principal resolution | `Get-ADUser` (RSAT) → `Get-LocalUser` fallback |
| Group principal resolution | `Get-ADGroup` (RSAT) → `Get-LocalGroup` fallback |
| Computer / machine account | `Win32_ComputerSystem.Name` |
| Default realm | The single joined AD domain, if any |

Notice the divergence from the FreeIPA / SSSD model:

- Windows's join is single-domain — there is no multi-realm
  configuration analogous to SSSD's `[domain/]` blocks.
- Windows uses WMI for membership query rather than the file-based
  `realm list`-style stanza output.
- Windows's NSS-equivalent is `Lsass` plus `Get-ADUser` /
  `Get-LocalUser`; there is no `getent` analogue, and the
  RSAT-vs-local distinction is structural rather than transparent.
- Windows's Kerberos cache is per-LUID; flushing the system LUID
  via `klist purge -li 0x3e7` is the closest analogue to
  `sssctl cache-expire -E`.

These differences are why the cross-OS contract abstracts the
operations behind verbs rather than tools — every OS realises the
same workflow with different primitives.

---

## Deviations From the Cross-OS Contract

| Contract surface | Windows | Reason |
|---|---|---|
| `discover_realm(domain) → RealmDiscovery` | Not exposed | Out of Wave 4.5 scope; can be implemented via `Resolve-DnsName -Type SRV _ldap._tcp.<domain>` |
| `IdentityCredentialError` | Not raised | Credentials are required on `WindowsRealmJoinSpec` and on `leave_realm`'s argument list, so the missing-credential case is structurally impossible |
| `IdentityRealmNotFoundError` | Not raised | `leave_realm` reports `SKIPPED` when not joined or joined to a different realm; `resolve_*` returns `found=False` on missing principals |
| `RealmKind` enum on join spec | Not modelled | `WindowsRealmJoinSpec` does not carry a kind because `Add-Computer` only supports AD; an unsupported-kind path is unreachable |
| `extra_options` on join spec | Not modelled | `Add-Computer` extras (`-Server`, `-NewName`, `-OUPath`) are first-class fields on the spec; arbitrary extras belong in a follow-up |
| Per-user Kerberos ticket lifecycle | Not exposed | Per-user `klist` / `kinit` is out of contract scope and out of Wave 4.5 scope |

These deviations are intentional and tracked in `tasks.md`.  They
do not break cross-OS contract conformance because the contract's
required verb set — `join_realm`, `leave_realm`,
`get_realm_status`, `list_joined_realms`, `sync_users`,
`resolve_user`, `resolve_group` — is fully implemented.

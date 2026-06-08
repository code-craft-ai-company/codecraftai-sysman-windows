# Design

## Overview

The authentication management feature is implemented as a single
module, `ccai.sysman.windows.authenticationmanagement`, exposing
a `WindowsAuthenticationManager` class that wraps `secedit /export`,
`secedit /configure`, `secedit /validate`, the LSA registry surface
under `HKLM:\SYSTEM\CurrentControlSet\Control\Lsa`, and the
PowerShell `Get-ItemProperty` / `Set-ItemProperty` cmdlets through
the `ICommandRunner` abstraction.

Windows authentication is fundamentally different from the Linux PAM
family: there is no per-service stack composer like
`pam-auth-update`, no `/etc/pam.d/` directory, and no notion of a
PAM stack at all.  Logon authentication is driven by:

1. **LSA Authentication Packages** — a `MULTI_SZ` registry value at
   `HKLM:\SYSTEM\CurrentControlSet\Control\Lsa\Authentication Packages`
   that lists the package DLLs the LSA loads at boot. The pilot's
   `enable_method` and `disable_method` mutate this list.
2. **Local Security Policy** — a database fronted by
   `secedit` (CLI) and `secpol.msc` (GUI) that holds password,
   account-lockout, audit, privilege-rights, and registry-values
   policy.  The pilot's `set_auth_config` /  `get_auth_config` /
   `validate_auth_config` round-trip a snapshot of this database
   via `secedit /export` and `secedit /configure`.

A maintenance coordinator running in an external package drives all
operations by calling this feature's public verbs.  All commands are
executed through the `codecraftai-interfaces` command-runner
abstraction.

**See Also:**
- `3-user-group-management` for local-user creation, password
  expiry, and SSH-key management.
- `3-ssh-server-configuration` for sshd-level configuration.
- `3-identity-management` for Active Directory binding and
  Kerberos.

---

## Cross-OS Authentication Model Concept Mapping

The authentication-management contract abstracts over three very
different OS authentication models:

| Concept | Windows (this spec) | Ubuntu | RHEL | macOS |
|---|---|---|---|---|
| Stack composition tool | (n/a — registry / secpol) | `pam-auth-update` | `authselect` | (manual edit) |
| Per-service file | (none) | `/etc/pam.d/<svc>` | `/etc/pam.d/<svc>` | `/etc/pam.d/<svc>` |
| Method registry | LSA `Authentication Packages` MULTI_SZ | `/usr/share/pam-configs/<name>` | `/usr/share/authselect/profiles/<name>` | (none) |
| Method enable | add to LSA MULTI_SZ | `pam-auth-update --enable` | `authselect enable-feature` | strip leading `#` |
| Method disable | remove from LSA MULTI_SZ | `pam-auth-update --disable` | `authselect disable-feature` | prepend `#` |
| Method enumeration | parse LSA registry value | `pam-auth-update --list` | `authselect list-features` | parse `/etc/pam.d/` |
| Validation | `secedit /validate` (limited) | re-parse | `authselect check` | re-parse |
| Subsystem identifier | `LOGON` / `NETWORK` / ... | PAM service name | PAM service name | PAM service name |
| Stack entry shape | `policy_key = policy_value` (INF) | `module_type control module_path [args]` | identical | identical |
| Idempotency check | per-key value equality | byte-equal of rendered file | byte-equal | byte-equal |
| Elevation | Administrator token | `sudo` | `sudo` | `sudo` |

**Why the divergence is fundamental:** PAM is a per-application
plugin stack with explicit ordering, control flags, and a defined
grammar.  Windows authentication is a database of
key/value policy entries plus a list of DLLs the LSA loads.  Both
are "authentication management", but the data model has zero
overlap at the storage level.  The pilot collapses both onto the
contract's `AuthConfig` / `AuthMethodInfo` types by treating LSA
package names as "methods" and policy-database keys as "entries"
inside an `AuthConfig`.

**Pilot scope:**

- `LOGON` subsystem covers `[System Access]` keys.  Examples:
  `MinimumPasswordLength`, `PasswordHistorySize`,
  `LockoutBadCount`, `LockoutDuration`, `EnableAdminAccount`.
- `NETWORK` subsystem covers `[Privilege Rights]` keys.
  Examples: `SeNetworkLogonRight`, `SeDenyNetworkLogonRight`.
- `INTERACTIVE` subsystem covers `[Event Audit]` keys.
- `SERVICE` subsystem covers `[Registry Values]` keys.
- `OTHER` subsystem accepts a free-form INF section name.

**Out-of-scope (forward compat hooks only):**

- Group Policy Object (GPO) integration.  The pilot manages the
  *local* security policy via `secedit`; it does NOT manipulate
  domain-pushed GPOs.
- The `Negotiate` package configuration (which selects between
  `Kerberos` and `MSV1_0`).  The pilot enumerates and toggles
  packages as opaque names; advanced negotiate routing is a
  follow-up.
- `LSA Notification Packages` (a sibling registry value to
  `Authentication Packages`).

---

## Architecture

```
+--------------------------------------------------------------+
|            Maintenance Coordinator                           |
|  - get_auth_config(LOGON) -> WindowsAuthConfig               |
|  - set_auth_config(LOGON, declared_config) -> result         |
|  - enable_method("Kerberos") (add to LSA list)               |
|  - disable_method("WDigest") (remove from LSA list)          |
|  - validate_auth_config(LOGON)                               |
+----------------------------+---------------------------------+
                             |
                             v
+--------------------------------------------------------------+
|            WindowsAuthenticationManager                      |
|  ccai.sysman.windows.authenticationmanagement            |
|                                                              |
|  Reads:                                                      |
|    get_auth_config(subsystem)                                |
|    list_auth_methods()                                       |
|    validate_auth_config(subsystem)                           |
|                                                              |
|  Writes:                                                     |
|    set_auth_config(subsystem, config)                        |
|    enable_method(name)                                       |
|    disable_method(name)                                      |
+----------------------------+---------------------------------+
                             |  delegates to
                             v
+--------------------------------------------------------------+
|                   ICommandRunner                             |
|  secedit /export /cfg <tmp.cfg> /quiet     (read)            |
|  Get-Content <tmp.cfg>                     (read)            |
|  Get-ItemProperty 'HKLM:\...\Lsa'          (read)            |
|  secedit /configure /db <tmp.sdb> /cfg ... (write; elevated) |
|  Set-Content -Path <tmp.cfg> -Value <inf>  (write; not elev) |
|  Set-ItemProperty 'HKLM:\...\Lsa' -Value … (write; elevated) |
|  secedit /validate <tmp.cfg>               (read)            |
+--------------------------------------------------------------+
```

---

## Components

### `WindowsAuthenticationManager`

**Location:** `source/packages/ccai/sysman/windows/authenticationmanagement.py`

| Method | Verb kind | Tool | Elevation |
|---|---|---|---|
| `get_auth_config(subsystem)` | read | `secedit /export` + `Get-Content` | no |
| `set_auth_config(subsystem, config)` | write | `Set-Content` (cfg) + `secedit /configure` | yes |
| `list_auth_methods()` | read | `Get-ItemProperty 'HKLM:\...\Lsa'` | no |
| `enable_method(name)` | write | `Set-ItemProperty 'HKLM:\...\Lsa'` | yes |
| `disable_method(name)` | write | `Set-ItemProperty 'HKLM:\...\Lsa'` | yes |
| `validate_auth_config(subsystem)` | read | `secedit /export` + `secedit /validate` + parser | no |

---

## Configuration

```python
@dataclass
class WindowsAuthenticationManagerConfig(FeatureConfig):
    powershell_executable: str = "powershell.exe"
    secedit_executable: str = "secedit.exe"
    lsa_path: str = r"HKLM:\SYSTEM\CurrentControlSet\Control\Lsa"
    temp_cfg_path: str = r"C:\Windows\Temp\codecraftai-secedit.cfg"
    temp_sdb_path: str = r"C:\Windows\Temp\codecraftai-secedit.sdb"
    well_known_packages: list[str] = (
        "msv1_0", "kerberos", "negoextender",
        "cloudap", "schannel", "wdigest",
    )
```

Inherits `dry_run` and `elevated` from `FeatureConfig`. The
canonical-package list (`well_known_packages`) is enumerated even
when the package is absent from the live LSA list, so callers see a
consistent surface across hosts.

---

## Data Models

```python
class WindowsAuthActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class WindowsAuthSubsystem:
    kind: str    # 'LOGON' | 'NETWORK' | 'INTERACTIVE' | 'SERVICE' | 'OTHER'
    name: str    # 'System Access' | 'Privilege Rights' | 'Event Audit'
                 #   | 'Registry Values' | <free>


@dataclass
class WindowsAuthEntry:
    policy_key: str       # e.g. 'MinimumPasswordLength'
    policy_value: str     # e.g. '14'
    section: str          # INI section the key belongs to


@dataclass
class WindowsAuthConfig:
    subsystem: str
    entries: list[WindowsAuthEntry]


@dataclass
class WindowsAuthMethodInfo:
    name: str
    enabled: bool
    description: str | None = None
    module_path: str | None = None


@dataclass
class WindowsAuthValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]


@dataclass
class WindowsAuthActionResult(FeatureActionResult):
    subsystem: str | None = None
    method: str | None = None
```

---

## Subsystem Mapping

| `WindowsAuthSubsystem.kind` | INF Section |
|---|---|
| `LOGON` | `System Access` |
| `NETWORK` | `Privilege Rights` |
| `INTERACTIVE` | `Event Audit` |
| `SERVICE` | `Registry Values` |
| `OTHER` | `<name>` (free-form) |

---

## Key Method Designs

### `get_auth_config(subsystem)`

1. Resolve the INF section name from the subsystem.
2. Run
   `secedit /export /cfg <temp_cfg_path> /quiet` via the
   PowerShell wrapper.
3. Run `Get-Content <temp_cfg_path>` to read the INF body.
4. Parse with `_parse_secedit_inf`.
5. Filter to entries whose `section` matches the requested
   subsystem.
6. Return `WindowsAuthConfig(subsystem=<kind-or-name>,
   entries=<filtered>)`.

### `set_auth_config(subsystem, config)`

1. Read current via `get_auth_config(subsystem)`.
2. Compare per-key value equality.  If every key in the supplied
   config matches the live state -> SKIPPED.
3. Render new INF via `_render_secedit_inf(config)`.
4. If `dry_run` -> DRY_RUN with the would-have argv.
5. Write the rendered INF to `<temp_cfg_path>` via
   `Set-Content`.
6. Invoke
   `secedit /configure /db <temp_sdb_path> /cfg <temp_cfg_path> /quiet`
   with `elevated=True`.

### `list_auth_methods()`

1. Run
   `Get-ItemProperty '<lsa_path>' -Name 'Authentication Packages' | ConvertTo-Json`
   via PowerShell.
2. Parse JSON.  The `MULTI_SZ` value comes through as either a JSON
   array or a single string (PowerShell coerces).  Both shapes are
   accepted.
3. For each enabled package, emit a `WindowsAuthMethodInfo` with
   `enabled=True`.
4. For each well-known package not in the live list, emit a
   `WindowsAuthMethodInfo` with `enabled=False`.

### `enable_method(name)` / `disable_method(name)`

Symmetric.  Both fetch the live list, build a new list, and
dispatch
`Set-ItemProperty '<lsa_path>' -Name 'Authentication Packages' -Value <list>`
when the live state does not match the desired state.

### `validate_auth_config(subsystem)`

1. Export the live policy.
2. Run `secedit /validate <temp_cfg_path>`.  This validator
   accepts most syntactically-valid INF bodies even when the keys
   are unknown to the policy database, so the result is a coarse
   signal.
3. Parse the INF body and walk every `<key> = <value>` line for
   structural issues:
   - Lines outside any section -> error.
   - Duplicate section headers -> warning.
   - Lines without an `=` separator -> error (unless the line is a
     comment).
4. Return `WindowsAuthValidationResult(valid=<all-ok>, errors=...,
   warnings=...)`.

---

## INF Parser / Renderer

The parser handles standard `secedit /export` output:

```
[Unicode]
Unicode=yes
[Version]
signature="$CHICAGO$"
Revision=1
[System Access]
MinimumPasswordAge = 1
MaximumPasswordAge = 60
MinimumPasswordLength = 14
PasswordComplexity = 1
[Event Audit]
AuditSystemEvents = 1
[Privilege Rights]
SeNetworkLogonRight = *S-1-5-32-544,*S-1-5-32-545
[Registry Values]
MACHINE\Software\Microsoft\Windows\CurrentVersion\Policies\System\DisableCAD = 4,0
```

Parser rules:

- Lines starting with `[` and ending with `]` are section headers.
- Lines starting with `;` (after leading whitespace) are comments —
  skipped.
- Blank lines are skipped.
- Lines with `=` are split into key / value at the first `=`; both
  trimmed.
- Lines with no `=` are reported by `validate_auth_config` and
  skipped by the structured parser.

Renderer rules: emit `[Unicode]\r\nUnicode=yes\r\n[Version]\r\n
signature="$CHICAGO$"\r\nRevision=1\r\n` as a fixed preamble, then
each section in canonical order.  Each entry under a section is
emitted as `<key> = <value>\r\n`.

---

## PowerShell Argv Construction

PowerShell invocation is centralised in `_build_powershell_argv`,
mirroring the pattern used by
`ccai.sysman.windows.auditmanagement._build_powershell_argv`.
The argv is:

```
[powershell_executable, "-NoProfile", "-NonInteractive",
 "-Command", <script>]
```

Where `<script>` is a single PowerShell expression.  The script is
quoted by PowerShell itself; this module does not add additional
quoting.

---

## reg-Path Caveats

PowerShell registry cmdlets accept the colon-prefixed PSDrive form
(`HKLM:\SYSTEM\CurrentControlSet\Control\Lsa`).  This module always
uses that form because the LSA path is a fixed manager-config
attribute and is never round-tripped through `reg.exe`.  The
`codecraftai-sysman-windows.configurationmanagement` module documents
the same convention.

---

## Dry-Run Behaviour

Every mutating entry point follows the same shape:

- Read the live state (always — dry-run does not block reads).
- Compute desired state and compare for SKIPPED.
- If a write would be required AND `config.dry_run=True` -> return
  `DRY_RUN` with `command` populated.

The `command` field on `DRY_RUN` results carries the
`secedit /configure` argv (for `set_auth_config`) or the
`Set-ItemProperty` argv (for enable / disable).  Callers can use
this to audit what the manager would have done without running it.

---

## Correctness Properties

1. **Idempotency.** Each write returns `SKIPPED` when the live
   state already matches the requested state, verified by
   re-reading through the runner.
2. **No silent failure.** Every non-zero runner exit surfaces as
   `status=FAILED` with `exit_code` and `stderr` populated.
3. **Dry-run safety.** No mutating command is dispatched when
   `dry_run=True`.
4. **Subsystem typing.** Unknown subsystem strings surface as
   `WindowsAuthenticationSubsystemNotFoundError` rather than silent
   no-ops on enable / disable.
5. **MULTI_SZ ordering.** Enabling / disabling a package preserves
   the existing order of the LSA list, appending new names at the
   end.
6. **Round-trip stability.** `_parse_secedit_inf` followed by
   `_render_secedit_inf` produces an INF body that re-parses to
   the same `WindowsAuthEntry` set.

---

## Testing Strategy

A local `_MockCommandRunner` dataclass mirrors the pattern used in
`test_auditmanagement.py` and `test_configurationmanagement.py`.
Tests seed `RunResult` values keyed by argv tuple and assert:

- argv shape per write verb,
- elevation flag per verb,
- `SKIPPED` when current == desired,
- `DRY_RUN` short-circuits the runner,
- stderr surfaces in `FAILED` results,
- the INF parser handles every standard `secedit /export`
  section,
- the LSA `MULTI_SZ` parser accepts both JSON-array and single-
  string shapes (PowerShell's JSON serialisation collapses
  single-element arrays to a scalar).

No live system or Administrator token required.

---

## Module Layout

```
source/packages/ccai/sysman/windows/
└── authenticationmanagement.py        ← WindowsAuthenticationManager + models

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_authenticationmanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This feature |
|---|---|---|
| sshd configuration | `3-ssh-server-configuration` | This feature does not manage sshd; ssh-server-configuration owns it. |
| Per-user account state | `3-user-group-management` | This feature handles the policy database; user-group handles individual accounts. |
| Active Directory binding | `3-identity-management` | `3-identity-management` may invoke `enable_method("Kerberos")` after a domain join. |
| Compliance snapshot | `3-security-compliance` | This feature exposes read methods; compliance reads and compares against a baseline. |
| GPO push | (out of scope) | Domain-pushed GPOs override the local policy; this feature manages only local policy. |

---

## Result Field Population

For every verb the result's `subsystem` and `method` fields are
populated as follows:

| Verb | `subsystem` | `method` |
|---|---|---|
| `set_auth_config` | resolved INF section name | `None` |
| `enable_method` | `None` | supplied package name |
| `disable_method` | `None` | supplied package name |

The `command` field always carries the argv that was (or would
have been) executed. For multi-step writes (`set_auth_config`
issues `Set-Content` then `secedit /configure`), the `command`
field on a successful result reflects the `secedit /configure`
argv; on a failed result it reflects the argv of the step that
failed. The `detail` field mirrors `subsystem` or `method`. The
`message` field carries SKIPPED / FAILED reason text.

---

## LSA Authentication Packages — Quick Reference

The Microsoft-shipped LSA authentication packages every
maintenance coordinator should recognise:

| Package | Purpose |
|---|---|
| `MSV1_0` | NTLM authentication. Required for legacy interop and local-account logons on workgroup machines. |
| `Kerberos` | Kerberos authentication. Required for AD-joined machines. |
| `NegoExtender` | Negotiate-package extension that brokers between MSV1_0 and Kerberos. |
| `CloudAP` | Cloud Authentication Provider. Used by Azure AD-joined machines. |
| `Schannel` | TLS / SSL authentication used by client-certificate logons and TLS-protected services. |
| `WDigest` | Legacy HTTP digest authentication. Disabled by default on modern Windows due to plaintext-credential storage; should be DISABLED when present. |

The pilot's `enable_method` / `disable_method` accept any of
these names verbatim (case-insensitive match against the live
list). The `well_known_packages` config field defaults to a
lower-case version of the list above so that `list_auth_methods`
surfaces a stable set on every host even when only some packages
are loaded.

---

## INF File Section Reference

The four canonical sections covered by the four
`WindowsAuthSubsystem.kind` enum members:

| Section | Subsystem | Common Keys |
|---|---|---|
| `[System Access]` | `LOGON` | `MinimumPasswordAge`, `MaximumPasswordAge`, `MinimumPasswordLength`, `PasswordComplexity`, `PasswordHistorySize`, `LockoutBadCount`, `LockoutDuration`, `ResetLockoutCount`, `ClearTextPassword`, `EnableAdminAccount`, `EnableGuestAccount` |
| `[Event Audit]` | `INTERACTIVE` | `AuditSystemEvents`, `AuditLogonEvents`, `AuditObjectAccess`, `AuditPrivilegeUse`, `AuditPolicyChange`, `AuditAccountManage`, `AuditProcessTracking`, `AuditDSAccess`, `AuditAccountLogon` |
| `[Privilege Rights]` | `NETWORK` | `SeNetworkLogonRight`, `SeDenyNetworkLogonRight`, `SeInteractiveLogonRight`, `SeDenyInteractiveLogonRight`, `SeRemoteInteractiveLogonRight`, `SeBatchLogonRight`, `SeServiceLogonRight`, `SeBackupPrivilege`, `SeRestorePrivilege`, `SeShutdownPrivilege` |
| `[Registry Values]` | `SERVICE` | `MACHINE\Software\Microsoft\Windows\CurrentVersion\Policies\System\DisableCAD`, `MACHINE\Software\Microsoft\Windows\CurrentVersion\Policies\System\DontDisplayLastUserName`, plus arbitrary other keyed values |

Cross-OS callers that want to set a password-length policy on
Ubuntu / RHEL via `pam_pwquality` and on Windows via
`MinimumPasswordLength` should use the structured directive map
on the contract's `AuthConfig.directives` and let each OS
implementation translate. The Windows manager translates the map
into `WindowsAuthEntry` records under the `[System Access]`
section.

---

## Drift From the Cross-OS Contract

- **`apply_changes` verb.** The contract defines an `apply_changes`
  verb for OSes with a staging / commit model. Windows
  `secedit /configure` is itself a one-shot apply against an
  `.sdb` database; there is no staging surface. The Windows
  manager omits `apply_changes`. Coordinators that target multiple
  OSes should treat `apply_changes` as a no-op call on Windows.
- **`get_method_status` verb.** The contract defines a per-method
  read.  Windows has no per-package probe API; the manager
  exposes the data via `list_auth_methods()` only.
- **`profile`, `features`, and `directives` fields on
  `WindowsAuthConfig`.** The contract defines `profile: str |
  None`, `features: list[str]`, and `directives: dict[str, str]`.
  The Windows pilot collapses everything into the `entries` list
  of `(policy_key, policy_value, section)` triples.  Callers that
  want a structured directive map should construct entries from
  their map.
- **`secedit /validate` is best-effort.** The contract requires
  `validate_auth_config` to flag malformed config.  `secedit
  /validate` accepts most syntactically-valid INF even when the
  keys are unknown to the policy database, so the pilot
  supplements the validator with the internal parser-driven
  check.  The drift is documented in
  `validate_auth_config`'s docstring.
- **`enable_method` / `disable_method` semantics.** Ubuntu and
  RHEL implement these against a profile registry; Windows
  implements them by mutating the LSA `Authentication Packages`
  `MULTI_SZ`.  The pilot documents this difference in the
  cross-OS concept-mapping table above.

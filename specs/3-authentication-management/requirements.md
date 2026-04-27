# Requirements

## Introduction

The authentication management feature provides the
`automojo-sysman-windows` package with the ability to manage the
Windows authentication stack: the Local Security Authority (LSA)
authentication packages registered under
`HKLM:\SYSTEM\CurrentControlSet\Control\Lsa`, and the local security
policy surface accessed via `secedit /export` and
`secedit /configure`. It implements the cross-OS
authentication-management contract defined at
`automojo-sysman-common/specs/contracts/authentication-management/contract.md`
for Windows.

Windows authentication is fundamentally different from the Linux PAM
family. There is no per-service stack composer like
`pam-auth-update`. Instead, Windows logon authentication is driven by
LSA authentication packages (e.g. `Kerberos`, `MSV1_0` (NTLM),
`NegoExtender`) registered as a `MULTI_SZ` registry value, and
account / lockout / password policy is governed by the Local Security
Policy database driven by `secedit` and `secpol.msc`.

This pilot exposes:

- The `LOGON` subsystem, which maps to a snapshot of the local
  security policy obtained via
  `secedit /export /cfg <temp.cfg>` followed by
  `cat <temp.cfg>`. The INF body is parsed into structured
  policy entries.
- The LSA authentication-package list, which the manager exposes
  via `list_auth_methods` / `enable_method` /
  `disable_method`.
- A best-effort `validate_auth_config` that invokes
  `secedit /validate <cfg>` and falls back to an internal INI-shape
  parser when the validator is unavailable.

A maintenance coordinator running in an external package drives all
operations by calling this feature's public verbs. All commands are
executed through the `automojo-interfaces` command-runner abstraction.

This feature complements — and does not duplicate — the SSH-daemon
hardening surface owned by `3-ssh-server-configuration`, the
local-account surface owned by `3-user-group-management`, and the
directory-services surface owned by `3-identity-management`.

---

## Requirements

### Requirement 1: Read the LOGON Subsystem Configuration

**User Story:** As a maintenance coordinator, I want to read the
local security policy snapshot for the `LOGON` subsystem, so that I
can compare it against a declared baseline.

#### Acceptance Criteria

1. WHEN `get_auth_config(LOGON)` is called THE SYSTEM SHALL invoke
   `secedit /export /cfg <temp.cfg> /quiet` via the command runner
   with the configured temp path, then read the file via
   `Get-Content` (PowerShell), and parse the INF body.
2. THE SYSTEM SHALL return a `WindowsAuthConfig` with `subsystem`
   set to `"LOGON"` and `entries` populated with one
   `WindowsAuthEntry` per `<key> = <value>` line in the
   `[System Access]`, `[Event Audit]`, `[Privilege Rights]`, and
   `[Registry Values]` sections of the INF body.
3. WHEN `secedit /export` exits non-zero THE SYSTEM SHALL raise
   `WindowsAuthenticationQueryError` with the captured stderr.
4. WHEN the subsystem is `OTHER` THE SYSTEM SHALL accept a
   free-form policy section name and read only that section's
   keys.
5. WHEN the subsystem string is unrecognised THE SYSTEM SHALL raise
   `WindowsAuthenticationSubsystemNotFoundError`.
6. THE read SHALL execute with `elevated=False` by default.  The
   contract carves out an exception when callers explicitly opt
   into elevation for hardened systems.

---

### Requirement 2: Write the LOGON Subsystem Configuration

**User Story:** As a maintenance coordinator, I want to apply a
declared local-security-policy snapshot, so that I can drive
password / lockout / privilege policy from a versioned
source-of-truth.

#### Acceptance Criteria

1. WHEN `set_auth_config(subsystem, config)` is called THE SYSTEM
   SHALL render the supplied `WindowsAuthConfig` into INF body
   syntax and write it to a temp `.cfg` file via `Set-Content`.
2. THE SYSTEM SHALL invoke
   `secedit /configure /db <temp.sdb> /cfg <temp.cfg> /quiet` via
   the command runner with `elevated=True`.
3. THE SYSTEM SHALL probe the current state via the same
   `secedit /export` flow used by `get_auth_config` and return
   `status=SKIPPED` when every key in the supplied
   `WindowsAuthConfig.entries` already matches.
4. WHEN `config.dry_run=True` THE SYSTEM SHALL return
   `status=DRY_RUN` with the would-have-been argv populated and
   SHALL NOT invoke the runner.
5. WHEN `secedit /configure` exits non-zero THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.

---

### Requirement 3: List LSA Authentication Methods

**User Story:** As a maintenance coordinator, I want to enumerate
the LSA authentication packages registered on the host, so I can
decide which to enable or disable.

#### Acceptance Criteria

1. WHEN `list_auth_methods()` is called THE SYSTEM SHALL invoke
   `Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa' -Name 'Authentication Packages' | ConvertTo-Json`
   via the configured PowerShell executable.
2. THE SYSTEM SHALL parse the JSON output and read the
   `Authentication Packages` `MULTI_SZ` value as a list of strings.
3. THE SYSTEM SHALL emit one `WindowsAuthMethodInfo` per package
   currently in the list with `enabled=True`.
4. THE SYSTEM SHALL also enumerate the well-known package names
   (``msv1_0``, ``kerberos``, ``negoextender``,
   ``cloudap``, ``schannel``, ``wdigest``) and emit
   `WindowsAuthMethodInfo` records for those that are not
   present, with `enabled=False`.
5. THE list operation SHALL execute with `elevated=False`.

---

### Requirement 4: Enable an Authentication Method

**User Story:** As a maintenance coordinator, I want to add a named
package to the LSA `Authentication Packages` list, so that the LSA
loads and uses that package for logons.

#### Acceptance Criteria

1. WHEN `enable_method(name)` is called THE SYSTEM SHALL probe the
   live state via `list_auth_methods()` and return
   `status=SKIPPED` when the package is already in the list.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.
3. WHEN the package is absent THE SYSTEM SHALL invoke
   `Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa' -Name 'Authentication Packages' -Value <new MULTI_SZ list>`
   via PowerShell with `elevated=True`.  The new list SHALL be the
   existing list with the requested name appended.
4. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured stderr.

---

### Requirement 5: Disable an Authentication Method

**User Story:** As a maintenance coordinator, I want to remove a
named package from the LSA `Authentication Packages` list, so that
the LSA no longer loads and uses that package for logons.

#### Acceptance Criteria

1. WHEN `disable_method(name)` is called THE SYSTEM SHALL probe the
   live state via `list_auth_methods()` and return
   `status=SKIPPED` when the package is already absent.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.
3. WHEN the package is present THE SYSTEM SHALL invoke
   `Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa' -Name 'Authentication Packages' -Value <new MULTI_SZ list>`
   via PowerShell with `elevated=True`.  The new list SHALL be the
   existing list with the requested name removed.
4. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured stderr.

---

### Requirement 6: Validate the LOGON Subsystem Configuration

**User Story:** As a maintenance coordinator, I want a syntactic
validation pass over a `secedit` configuration file so I can detect
malformed sections before applying them.

#### Acceptance Criteria

1. WHEN `validate_auth_config(subsystem)` is called THE SYSTEM SHALL
   first export the live policy via the same
   `secedit /export` flow used by `get_auth_config`, then re-parse
   the INF body using the internal parser.
2. THE SYSTEM SHALL return a `WindowsAuthValidationResult` with
   `valid: bool`, `errors: list[str]`, and `warnings: list[str]`.
3. THE pilot SHALL document that `secedit /validate` is limited
   (it accepts most syntactically-valid INF bodies even when the
   keys are not recognised by the policy database) and that the
   parser-driven check covers structural issues only.
4. UNRECOGNISED section headers and missing `=` signs SHALL be
   reported as `errors`. Unrecognised key names SHALL be reported
   as `warnings`.
5. THE validate operation SHALL execute with `elevated=False`.

---

### Requirement 7: Subsystem Mapping

**User Story:** As a maintenance coordinator, I want a typed enum of
known Windows authentication subsystems so I cannot pass a typo for
a section name.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `WindowsAuthSubsystem` with members
   `LOGON`, `NETWORK`, `INTERACTIVE`, `SERVICE`, `OTHER`.
2. EACH enum member except `OTHER` SHALL map to a canonical INF
   section name (`System Access` for LOGON, `Privilege Rights`
   for NETWORK, `Event Audit` for INTERACTIVE, `Registry Values`
   for SERVICE).
3. WHEN `OTHER` is used the caller MUST supply a `name` field used
   as the literal section name.
4. THE manager's verbs SHALL accept either a
   `WindowsAuthSubsystem` instance or a raw string; raw strings
   are routed through the OTHER pathway internally.

---

### Requirement 8: Idempotency

**User Story:** As a maintenance coordinator, I want every write
verb to be idempotent so I can re-run the manager without side
effects.

#### Acceptance Criteria

1. THE SYSTEM SHALL return `SKIPPED` from `set_auth_config` when
   every key in the supplied `WindowsAuthConfig.entries` already
   matches the live policy.
2. THE SYSTEM SHALL return `SKIPPED` from `enable_method` when the
   named package is already present in the LSA list.
3. THE SYSTEM SHALL return `SKIPPED` from `disable_method` when the
   named package is already absent from the LSA list.
4. Reads (`get_auth_config`, `list_auth_methods`,
   `validate_auth_config`) SHALL never short-circuit.

---

### Requirement 9: Elevation, Dry-Run, and Error Reporting

**User Story:** As a maintenance coordinator, I want every policy
write to elevate, every read to stay unprivileged, and every failure
to surface with the underlying exit code and stderr.

#### Acceptance Criteria

1. THE SYSTEM SHALL pass `elevated=True` for every write verb when
   `config.elevated=True`.
2. THE SYSTEM SHALL pass `elevated=False` for every read verb.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke the
   runner for any write verb.
4. WHEN the runner returns a non-zero exit code THE SYSTEM SHALL
   return `status=FAILED` with `exit_code`, `stdout`, `stderr`,
   and `command` populated.
5. THE SYSTEM SHALL NOT catch `SemanticError` and SHALL NOT
   silently swallow exceptions.
6. THE returned `WindowsAuthActionResult` SHALL carry `subsystem`
   and `method` attributes when meaningful so downstream reports
   can route results without re-parsing the `command` argv.

---

### Requirement 10: Secedit Round-Trip Format

**User Story:** As a maintenance coordinator, I want a stable INF
body format on the file the manager hands to `secedit /configure`,
so that round-trip diffs between exports and applies are auditable.

#### Acceptance Criteria

1. THE SYSTEM SHALL render the supplied `WindowsAuthConfig.entries`
   into an INF body with section headers in the canonical order
   `[Unicode]`, `[Version]`, `[System Access]`, `[Event Audit]`,
   `[Privilege Rights]`, `[Registry Values]`.
2. EACH entry's `policy_key` SHALL be emitted on its own line as
   `<policy_key> = <policy_value>` under the section the entry
   belongs to (recorded on the entry's `section` field).
3. THE rendered body SHALL terminate with a single trailing CRLF
   to match Windows INF conventions.
4. THE renderer SHALL emit the canonical `[Unicode]` /
   `signature="$CHICAGO$"` header pair so that
   `secedit /configure` accepts the file without re-conversion.

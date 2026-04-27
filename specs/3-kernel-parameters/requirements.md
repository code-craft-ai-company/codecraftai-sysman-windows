# Requirements

## Introduction

The kernel parameters feature provides the `automojo-sysman-windows` package
with the ability to read and write kernel tunables on Windows systems.  This
spec implements the kernel-parameters contract defined at
`automojo-sysman-common/specs/contracts/kernel-parameters/contract.md` for
Windows.

Windows has no `sysctl(8)` equivalent.  Kernel-level tunables on Windows are
exposed as registry values under well-known paths in the `HKLM` hive (mostly
under `HKLM:\SYSTEM\CurrentControlSet\Services\<Service>\Parameters` and a
handful under `HKLM:\SYSTEM\CurrentControlSet\Control\...`).  Many registry
writes do not take effect until reboot.  This implementation honors the
contract's verb set but applies strict scope limits:

- The manager maintains a bundled **mapping table** that translates a subset
  of dotted parameter names (matching Linux-style names where sensible) to
  concrete registry paths and value names.
- Parameters not in the mapping table return `FAILED` with
  `message="parameter_not_supported"` — the contract explicitly permits
  this and requires that the rejection be non-silent.
- Writes to registry keys flagged `requires_reboot=True` in the mapping
  table return a result whose `requires_reboot` field is `True`.
- There is no Windows equivalent of `sysctl --system`; `reload_parameters()`
  is a no-op that returns `SUCCESS` with a documented message, because
  registry writes are persistent and take effect on the next boot or
  immediately (per-key).

A maintenance coordinator running in an external package drives all operations
by calling into this feature's public interfaces.  All system commands are
executed exclusively through the `automojo-interfaces` command-runner
abstraction and the manager wraps PowerShell cmdlets (`Get-ItemProperty`,
`Set-ItemProperty`, `Remove-ItemProperty`) for registry access.

---

## Requirements

### Requirement 1: Get a Single Parameter

**User Story:** As a maintenance coordinator, I want to read the current value
of a supported kernel parameter on Windows, so that I can make conditional
decisions based on its value.

#### Acceptance Criteria

1. WHEN the coordinator provides a parameter name that IS in the bundled
   mapping table THE SYSTEM SHALL invoke `powershell.exe -NoProfile -Command
   "(Get-ItemProperty -Path <registry-path> -Name <value-name>).<value-name>"`
   via the command runner without elevation for HKCU reads and WITH elevation
   only where the contract permits (HKLM protected keys — see Requirement 8).
2. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsParameterValue` with `name=<name>`, `value=<stripped stdout>`,
   and `source=<registry-path>\<value-name>`.
3. WHEN the parameter name is NOT in the mapping table THE SYSTEM SHALL
   return `None`.
4. WHEN the registry value does not exist (but the path does) THE SYSTEM
   SHALL return `None`.
5. WHEN the PowerShell invocation fails for any other reason THE SYSTEM
   SHALL raise a `WindowsKernelParameterQueryError` carrying the exit code
   and captured stderr.

---

### Requirement 2: Set a Parameter

**User Story:** As a maintenance coordinator, I want to write a supported
kernel parameter value on Windows, so that I can enforce a desired kernel
tuning state.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_parameter(name, value, persistent)` AND
   the parameter name is NOT in the bundled mapping table THE SYSTEM SHALL
   return a `WindowsKernelParameterActionResult` with status `FAILED` and
   `message="parameter_not_supported: <name>"` AND SHALL NOT invoke the
   runner.
2. WHEN the parameter IS in the mapping table AND the current registry
   value already equals the requested value THE SYSTEM SHALL return a
   `WindowsKernelParameterActionResult` with status `SKIPPED` and populate
   `current_value` / `applied_value` with the matching value.
3. WHEN the current value differs THE SYSTEM SHALL invoke `powershell.exe
   -NoProfile -Command "Set-ItemProperty -Path <registry-path> -Name
   <value-name> -Value <value> -Type <type>"` via the command runner.
4. WHEN the registry type is specified in the mapping table THE SYSTEM
   SHALL emit `-Type <DWord|String|ExpandString|MultiString|QWord>`
   matching that type.
5. WHEN the mapping entry is flagged `requires_reboot=True` THE SYSTEM
   SHALL set `requires_reboot=True` on the returned result.
6. WHEN `persistent=False` is passed AND the mapping entry is
   runtime-volatile (rare on Windows) THE SYSTEM SHALL honor that flag by
   not persisting; for the standard case where `persistent` is irrelevant
   on Windows (all registry writes persist) the parameter is recorded in
   the result's `persistent=True` regardless.
7. WHEN the command fails THE SYSTEM SHALL return a
   `WindowsKernelParameterActionResult` with status `FAILED`, the exit
   code, captured stderr, and the `command` that was attempted.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command
   and SHALL return a `WindowsKernelParameterActionResult` with status
   `DRY_RUN` populating `command`, `current_value`, `applied_value`, and
   (when applicable) `requires_reboot`.

---

### Requirement 3: List All Parameters

**User Story:** As a maintenance coordinator, I want to enumerate every
kernel parameter the manager understands and its current value, so that I
can audit system configuration without knowing the mapping table in
advance.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_parameters()` THE SYSTEM SHALL iterate
   every entry in the bundled mapping table and SHALL invoke
   `Get-ItemProperty` via the command runner for each entry.
2. WHEN an individual registry read succeeds THE SYSTEM SHALL include the
   `<name>: <value>` pair in the returned `dict[str, str]`.
3. WHEN an individual registry read fails with "property not found" the
   entry SHALL be silently omitted from the returned dict (consistent with
   the default-not-set semantics of Windows registry defaults).
4. WHEN an individual registry read fails for any other reason THE SYSTEM
   SHALL raise a `WindowsKernelParameterQueryError` with a `message`
   identifying which entry failed; partial results are not returned.
5. WHEN the mapping table is empty THE SYSTEM SHALL return an empty dict
   without invoking the runner.
6. THE SYSTEM SHALL NOT attempt to enumerate every Windows kernel tunable
   — enumeration is bounded by the mapping table because Windows has no
   `sysctl -a` equivalent.

---

### Requirement 4: Reload Persisted Parameters

**User Story:** As a maintenance coordinator, I want a uniform
`reload_parameters()` API on Windows, so that mixed-OS orchestration is not
forced to special-case the Windows kernel-parameter manager.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_parameters()` THE SYSTEM SHALL NOT
   invoke any command.
2. THE SYSTEM SHALL return a `WindowsKernelParameterActionResult` with
   status `SUCCESS`, `name="*"`, and `message="Windows applies kernel
   parameters on next boot or immediately per-key; no reload supported"`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return a result with
   status `DRY_RUN` and the same message.
4. THE SYSTEM SHALL NOT raise an exception — the method is deliberately a
   no-op so cross-OS code can call it unconditionally.

---

### Requirement 5: Remove a Persisted Parameter

**User Story:** As a maintenance coordinator, I want to remove a previously
written registry value so that the OS reverts to its default behavior after
reboot.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_persisted_parameter(name)` AND the
   name is NOT in the mapping table THE SYSTEM SHALL return a
   `WindowsKernelParameterActionResult` with status `FAILED` and
   `message="parameter_not_supported"` without invoking the runner.
2. WHEN the registry value does not currently exist THE SYSTEM SHALL
   return a `WindowsKernelParameterActionResult` with status `SKIPPED`.
3. WHEN the registry value exists THE SYSTEM SHALL invoke `powershell.exe
   -NoProfile -Command "Remove-ItemProperty -Path <registry-path> -Name
   <value-name>"` via the command runner.
4. WHEN the command succeeds THE SYSTEM SHALL return a result with status
   `SUCCESS` and `requires_reboot` copied from the mapping entry.
5. WHEN the command fails THE SYSTEM SHALL return a result with status
   `FAILED`, the exit code, and captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command
   and SHALL return a result with status `DRY_RUN` populating `command`.

---

### Requirement 6: Bundled Mapping Table

**User Story:** As a maintenance coordinator, I want a documented set of
dotted parameter names that the Windows manager understands, so that I can
use the same name-oriented API across RHEL/Ubuntu/macOS/Windows for the
parameters where a meaningful translation exists.

#### Acceptance Criteria

1. THE SYSTEM SHALL ship a bundled mapping table keyed by dotted parameter
   name and carrying, for each entry: registry path, value name, registry
   type (`DWord`, `String`, `ExpandString`, `MultiString`, `QWord`), and
   a `requires_reboot` flag.
2. THE SYSTEM SHALL expose the mapping table via a read-only accessor
   `get_mapping_table()` returning a copy of the dict, so tests and the
   coordinator can inspect supported names.
3. THE SYSTEM SHALL NOT accept mapping-table overrides via the config —
   the table is part of the package.
4. WHEN a parameter name is queried via `get_parameter` or listed via
   `list_parameters`, the returned `source` field SHALL use the notation
   `<registry-path>\<value-name>`.

---

### Requirement 7: PowerShell Invocation Contract

**User Story:** As a maintenance coordinator, I want every PowerShell
command dispatched via the runner to be constructed in a uniform way, so
that aspect composition (retry, persistence, timeout) sees a stable argv
shape.

#### Acceptance Criteria

1. THE SYSTEM SHALL construct every PowerShell command as
   `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
   <script>]`.
2. THE SYSTEM SHALL NOT use `cmd.exe /c` or `wmic` — all registry access
   is via PowerShell cmdlets.
3. THE SYSTEM SHALL wrap the inner script with a trailing `; exit
   $LASTEXITCODE` so that cmdlet-level failures translate into runner
   exit codes.
4. THE SYSTEM SHALL escape any value that contains double-quotes, dollar
   signs, or backticks using PowerShell's single-quoted-string rules.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all kernel-parameter
write commands to run with the Administrator token required by HKLM
registry writes, so that mutations succeed on a standard system where the
process does not run as Administrator.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL
   pass `elevated=True` to the command runner for every HKLM write command
   (`Set-ItemProperty`, `Remove-ItemProperty`).
2. WHEN performing read-only operations (`get_parameter`,
   `list_parameters`) THE SYSTEM SHALL NOT pass `elevated=True`, regardless
   of config; HKLM read-only access is permitted to a standard user for
   the keys in the mapping table.
3. WHEN the system is running as Administrator THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default
   to no elevation and allow the runner to surface the resulting
   permission error as a `WindowsKernelParameterActionResult` with
   `status=FAILED`.
5. THE SYSTEM SHALL document in the mapping-table metadata if any listed
   key requires elevation for reads (currently: none of the default set
   require elevated reads).

---

### Requirement 9: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode that
produces the full would-be command sequence without mutating the system, so
that change reviews are possible before roll-out.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM SHALL NOT
   invoke any `Set-ItemProperty` or `Remove-ItemProperty` command.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command` field
   of every returned `WindowsKernelParameterActionResult` with the argv
   that would have been executed.
3. WHEN dry-run is active THE SYSTEM SHALL still perform read operations
   to evaluate idempotency guards — dry-run is a write-only short-circuit.
4. WHEN dry-run is active AND the mapping entry is flagged
   `requires_reboot=True` THE SYSTEM SHALL still set `requires_reboot=True`
   on the returned result.

# Requirements

## Introduction

The security compliance feature provides the `automojo-sysman-windows`
package with the ability to run compliance baselines, iterate their
findings, and remediate individual findings on Windows systems.  This
spec implements the security-compliance contract defined at
`automojo-sysman-common/specs/contracts/security-compliance/contract.md`
for Windows using the Microsoft **Security Compliance Toolkit** and
native tooling: `secedit /analyze` / `secedit /configure` for Local
Security Policy baselines, LGPO.exe for Group Policy template
application, and PowerShell cmdlets for Defender, BitLocker, and
registry-based baseline items.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  All system
commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction, and all baseline-metadata mutations are
executed through the `automojo-interfaces` file-operator abstraction.

Unlike firewall / package / service management where the verb set maps
cleanly, security-compliance is intrinsically **advisory** — the verb set
is honored across OSes but each OS's compliance tooling produces findings
with OS-native shapes.  This Windows spec adopts the Security Compliance
Toolkit baselines (shipped as a directory tree containing
`<Baseline>\GPOs\{GUID}\DomainSysvol\GPO\...` plus a
`<Baseline>.json` analysis rules file) as the primary engine.

---

## Requirements

### Requirement 1: List Available Profiles

**User Story:** As a maintenance coordinator, I want to enumerate every
Microsoft Security Compliance Toolkit baseline available on the system,
so that I can choose one to evaluate.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_profiles()` THE SYSTEM SHALL
   enumerate subdirectories of `<baselines_dir>` (default
   `C:\ProgramData\Automojo\Compliance\Baselines\`) via the
   `IFileOperator` and SHALL include only those subdirectories that
   contain a `GPOs\` directory AND a `<BaselineName>-findings.json`
   rules manifest.
2. WHEN the command succeeds THE SYSTEM SHALL return a
   `list[WindowsComplianceProfile]` with one entry per baseline:
   `name`=subdirectory name, `description`=the manifest's
   `description` field (or subdirectory name when absent), `checks=[]`.
3. WHEN `<baselines_dir>` does not exist THE SYSTEM SHALL raise a
   `WindowsSecurityComplianceError` with a `message` recommending
   the user download the Security Compliance Toolkit from Microsoft
   and extract it into that path.
4. WHEN the coordinator has configured `baselines_dir` in the config
   THE SYSTEM SHALL use that path instead of the default.
5. THE SYSTEM SHALL recognize baseline names matching the Microsoft
   release pattern (e.g., `Windows 11 v23H2 Security Baseline`,
   `Windows Server 2022 Security Baseline`) but MUST NOT reject any
   subdirectory meeting the structural requirement.

---

### Requirement 2: Get a Single Profile

**User Story:** As a maintenance coordinator, I want to resolve a
baseline by name, so that I can inspect its description.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_profile(profile_name)` THE SYSTEM
   SHALL invoke `list_profiles()` internally and return the matching
   entry.
2. WHEN no profile matches THE SYSTEM SHALL return `None`.
3. WHEN `list_profiles()` raises THE SYSTEM SHALL propagate the
   `WindowsSecurityComplianceError`.

---

### Requirement 3: Run a Compliance Profile

**User Story:** As a maintenance coordinator, I want to evaluate a
baseline end-to-end and receive a structured report, so that I can act
on each finding.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_compliance_profile(profile_name)`
   THE SYSTEM SHALL:
   a. Invoke `secedit /export /cfg <results_dir>\<baseline>-current.inf
      /quiet` via the runner to export the current effective local
      security policy.
   b. Read the `<baselines_dir>\<baseline>\<baseline>-findings.json`
      manifest via the `IFileOperator`, which lists the finding
      specs (rule IDs, severities, expected settings, and per-rule
      check-dispatch metadata).
   c. Iterate each finding spec.  For each one, dispatch the
      appropriate check: INF compare against the exported INF for
      `secedit` rules, `Get-MpPreference`-based PowerShell reads for
      Defender rules, `Get-BitLockerVolume` for BitLocker rules, or
      `Get-ItemProperty` for registry rules.
   d. Collect results into a `WindowsComplianceReport`.
2. WHEN `secedit /export` fails with a non-zero exit THE SYSTEM SHALL
   raise `WindowsSecurityComplianceError` with the exit code and
   captured stderr.
3. WHEN the profile directory does not exist THE SYSTEM SHALL raise
   `WindowsSecurityComplianceError` with
   `message="unknown_profile: <name>"` before invoking the runner.
4. WHEN individual per-rule checks fail THE SYSTEM SHALL capture the
   failure in the report with `compliant=False` and `error=<reason>`
   — such failures MUST NOT raise.
5. WHEN populating each `WindowsComplianceFinding` THE SYSTEM SHALL
   set `finding_id`=`<profile>:<rule_id>`, `check_id`=`<rule_id>`,
   `description` from the manifest, `severity` mapped from the
   manifest's severity string, `compliant` from the per-check
   outcome, `actual` / `expected` populated from the comparison,
   `remediable`=True only for rule types that the manager's
   remediation dispatch supports.
6. WHEN `started_at` and `finished_at` are set THE SYSTEM SHALL use
   UTC datetimes.
7. WHEN dry-run mode is active THE SYSTEM SHALL still execute the
   profile run — compliance evaluation is a read.

---

### Requirement 4: Remediate a Finding

**User Story:** As a maintenance coordinator, I want to remediate a
specific finding identified in a report, so that the system comes into
compliance.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate(finding_id)` THE SYSTEM SHALL
   split the finding ID on `:` into `<profile>:<rule_id>`, re-read the
   rule's definition from the baseline manifest, and dispatch one of:
   a. `secedit /configure /cfg <remediation-inf> /quiet` for `secedit`
      rules (the remediation INF is synthesized in pure Python from
      the rule's expected-value field).
   b. `powershell.exe -NoProfile -Command "Set-MpPreference -<Name>
      <Value>; exit $LASTEXITCODE"` for Defender rules.
   c. `powershell.exe -NoProfile -Command "Set-ItemProperty -Path
      <path> -Name <name> -Value <value> -Type <type>; exit
      $LASTEXITCODE"` for registry rules.
   d. `powershell.exe -NoProfile -Command "Enable-BitLocker -MountPoint
      <drive> -EncryptionMethod <method>; exit $LASTEXITCODE"` for
      BitLocker rules.
2. WHEN the rule type is outside the dispatch table THE SYSTEM SHALL
   return a `WindowsSecurityComplianceActionResult` with status
   `FAILED` and `message="not_remediable"` without invoking the
   runner.
3. WHEN the system is ALREADY compliant for the rule THE SYSTEM SHALL
   return a result with status `SKIPPED` and
   `message="already_compliant"` — idempotency is evaluated by
   re-running the per-rule check before issuing the remediation
   command.
4. WHEN the command succeeds THE SYSTEM SHALL return a result with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a result with
   status `FAILED`, the exit code, captured stderr, and the
   `command`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   remediation command and SHALL return a result with status
   `DRY_RUN`.

---

### Requirement 5: Batch Remediate

**User Story:** As a maintenance coordinator, I want to apply every
remediable finding in a report in one call.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate_all(report)` THE SYSTEM SHALL
   iterate `report.findings` in list order and SHALL invoke
   `remediate(finding_id)` for every finding whose `remediable=True`
   AND `compliant=False`.
2. WHEN a finding has `compliant=True` THE SYSTEM SHALL skip it
   silently (no result returned).
3. WHEN a finding has `remediable=False` AND `compliant=False` THE
   SYSTEM SHALL include a result with status `FAILED` and
   `message="not_remediable"`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `DRY_RUN` for
   every attempted remediation.
5. THE SYSTEM SHALL return one result per attempted remediation in
   the order findings were processed.

---

### Requirement 6: Get Baseline

**User Story:** As a maintenance coordinator, I want to read the most
recently applied baseline metadata so that audits can report on active
policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_baseline()` THE SYSTEM SHALL invoke
   `IFileOperator.read_text('%ProgramData%\\Automojo\\compliance\\current_baseline.json')`
   (the literal path expansion is performed on the target system;
   the dataclass stores the unexpanded string).
2. WHEN the file does not exist THE SYSTEM SHALL return a
   `WindowsBaselineInfo` with empty `name`, empty `settings`, and
   `source=None`.
3. WHEN the file exists THE SYSTEM SHALL parse JSON with keys `name`
   and `settings` and SHALL return a `WindowsBaselineInfo` populated
   from those fields with `source=<file path>`.
4. WHEN the JSON is malformed THE SYSTEM SHALL raise a
   `WindowsSecurityComplianceError` with
   `message="baseline_corrupt: <reason>"`.

---

### Requirement 7: Set Baseline

**User Story:** As a maintenance coordinator, I want to persist the
active baseline and apply it, so that a known policy is enforced and
recorded.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_baseline(spec)` AND `get_baseline()`
   already returns a baseline equal to `spec` THE SYSTEM SHALL return
   a result with status `SKIPPED`.
2. WHEN the baselines differ THE SYSTEM SHALL first run
   `run_compliance_profile(spec.name)`, then `remediate_all(report)`,
   and finally write the updated baseline JSON via the
   `IFileOperator`.
3. WHEN the profile run OR any remediation fails THE SYSTEM SHALL
   return `FAILED` with a `message` identifying the failing step,
   AND SHALL NOT write the baseline file.
4. WHEN the baseline write succeeds THE SYSTEM SHALL return `SUCCESS`
   with `target=%ProgramData%\Automojo\compliance\current_baseline.json`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any
   mutation and SHALL return `DRY_RUN`.

---

### Requirement 8: Check-Type Support

**User Story:** As a maintenance coordinator, I want to know which
check types the manager understands.

#### Acceptance Criteria

1. THE SYSTEM SHALL support the `check_type` discriminators:
   `secedit_inf`, `registry`, `defender`, and `bitlocker`.
2. WHEN `run_compliance_profile` encounters a check with a
   `check_type` outside this set THE SYSTEM SHALL include the check
   in the report with `compliant=False`, `error="unsupported_check_type:
   <type>"`, `remediable=False` — no raise.
3. THE SYSTEM SHALL NOT attempt to parse GPT.INI / registry.pol
   directly — GPO-based baselines are applied via the LGPO.exe
   remediation path which is out of scope for the base contract.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all compliance
evaluations and remediations to run with the Administrator token
required by `secedit`, Defender, BitLocker, and HKLM registry writes.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL
   pass `elevated=True` to the command runner for
   `run_compliance_profile` (per-check reads that inspect protected
   state), `remediate`, and `set_baseline`.
2. WHEN invoking `list_profiles` / `get_profile` / `get_baseline` THE
   SYSTEM SHALL NOT pass `elevated=True` — those reads target
   directories under `%ProgramData%` which are readable to standard
   users.
3. WHEN the system is running as Administrator THE SYSTEM SHALL NOT
   pass `elevated=True`.
4. WHEN elevation is not configured THE SYSTEM SHALL default to no
   elevation and allow the runner to surface the resulting
   permission error as a `WindowsSecurityComplianceActionResult`
   with `status=FAILED`.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that simulates remediation without mutating the system.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM SHALL
   NOT invoke any `secedit /configure`, `Set-MpPreference`,
   `Set-ItemProperty`, `Enable-BitLocker`, or baseline JSON write.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command`
   field of every returned result with the argv that would have
   been executed.
3. WHEN dry-run is active THE SYSTEM SHALL still execute
   `run_compliance_profile` (reads).
4. WHEN dry-run is active THE SYSTEM SHALL return status `DRY_RUN`
   on every result returned from `remediate`, `remediate_all`, or
   `set_baseline`.

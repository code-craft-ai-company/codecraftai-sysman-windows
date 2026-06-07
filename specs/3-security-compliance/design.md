# Design

## Overview

The security compliance feature is implemented as a single module,
`ccai.sysman.windows.securitycompliance`, that exposes a
`WindowsSecurityComplianceManager` class.  This spec implements the
security-compliance contract defined at
`codecraftai-sysman-common/specs/contracts/security-compliance/contract.md`
for Windows using the Microsoft Security Compliance Toolkit baselines
(as directory trees under `C:\ProgramData\CodeCraftAI\Compliance\Baselines\`)
plus native tooling: `secedit` for Local Security Policy, PowerShell
`Set-MpPreference` for Defender, `Enable-BitLocker` for BitLocker, and
`Set-ItemProperty` for registry-based baseline items.

All command invocation is delegated to an `ICommandRunner` and all
baseline-manifest reads and baseline-JSON writes go through an
`IFileOperator`.  No direct filesystem or `subprocess` calls are made
inside the module.

Unlike firewall / package / service management where the verb set maps
cleanly, security-compliance is intrinsically **advisory** — the
contract verbs are honored but the `WindowsComplianceFinding` dataclass
is flexible by design and is populated from the per-rule check dispatch
results.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/WinRM execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator (Local or WinRM)     │
│  - Configures aspects (optional)                            │
│  - Injects into WindowsSecurityComplianceManager            │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           WindowsSecurityComplianceManager                  │
│  ccai.sysman.windows.securitycompliance                 │
│                                                             │
│  list_profiles()        get_profile()                       │
│  run_compliance_profile()  remediate()                      │
│  remediate_all()                                            │
│  get_baseline()         set_baseline()                      │
└──────────────┬──────────────────────────────┬───────────────┘
               │ runner                       │ file I/O
               ▼                              ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run(args, elev)    │         │   read / write /     │
│                      │         │   exists / listdir   │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
   ┌───────┴────────┐             ┌─────────┴────────┐
   ▼                ▼             ▼                  ▼
┌─────────────┐  ┌──────────────┐ ┌──────────────┐ ┌────────────┐
│LocalCommand │  │WinRMCommand  │ │LocalFile     │ │WinRMFile   │
│Runner       │  │Runner        │ │Operator      │ │Operator    │
└─────────────┘  └──────────────┘ └──────────────┘ └────────────┘
           │                                │
           └────────────┬───────────────────┘
                        │ optional
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsSecurityComplianceManager`

**Location:** `source/packages/ccai/sysman/windows/securitycompliance.py`

| Method | Signature | Description |
|---|---|---|
| `list_profiles` | `() → list[WindowsComplianceProfile]` | Scan `baselines_dir/` |
| `get_profile` | `(profile_name) → WindowsComplianceProfile \| None` | Enumerate + filter |
| `run_compliance_profile` | `(profile_name) → WindowsComplianceReport` | Per-check dispatch |
| `remediate` | `(finding_id) → WindowsSecurityComplianceActionResult` | Per-rule-type dispatch |
| `remediate_all` | `(report) → list[WindowsSecurityComplianceActionResult]` | Iterate + delegate |
| `get_baseline` | `() → WindowsBaselineInfo` | Read baseline JSON |
| `set_baseline` | `(spec) → WindowsSecurityComplianceActionResult` | Run + remediate + write JSON |

### `WindowsSecurityComplianceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, remediations pass `elevated=True` |
| `baselines_dir` | `str` | `"C:\\ProgramData\\CodeCraftAI\\Compliance\\Baselines"` | Toolkit install root |
| `baseline_path` | `str` | `"C:\\ProgramData\\CodeCraftAI\\compliance\\current_baseline.json"` | Baseline JSON |
| `results_dir` | `str` | `"C:\\ProgramData\\CodeCraftAI\\compliance\\results"` | INF + JSON output directory |

---

## Data Models

### Enums

```python
class WindowsSecurityComplianceActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

class WindowsComplianceSeverity(Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
```

### Dataclasses

```python
@dataclass
class WindowsComplianceCheck:
    check_id: str
    description: str
    check_type: str           # "secedit_inf" | "registry" | "defender" | "bitlocker"
    severity: WindowsComplianceSeverity
    parameters: dict          # rule-type-specific expected values

@dataclass
class WindowsComplianceProfile:
    name: str
    description: str
    checks: list[WindowsComplianceCheck]

@dataclass
class WindowsComplianceFinding:
    finding_id: str
    check_id: str
    description: str
    severity: WindowsComplianceSeverity
    compliant: bool
    actual: str | None
    expected: str | None
    remediable: bool
    error: str | None

@dataclass
class WindowsComplianceReport:
    profile_name: str
    started_at: datetime
    finished_at: datetime
    findings: list[WindowsComplianceFinding]
    total: int
    compliant: int
    non_compliant: int
    errored: int

@dataclass
class WindowsBaselineSpec:
    name: str
    settings: dict[str, str]

@dataclass
class WindowsBaselineInfo:
    name: str
    settings: dict[str, str]
    source: str | None

@dataclass
class WindowsSecurityComplianceActionResult:
    status: WindowsSecurityComplianceActionStatus
    finding_id: str | None
    target: str | None
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class WindowsSecurityComplianceError(Exception):
    """Framework-level failures."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `IFileOperator` (from `codecraftai-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
    def listdir(self, path: str, elevated: bool = False) -> list[str]: ...
```

---

## Command Mappings

| Operation | Command |
|---|---|
| Export current policy INF | `secedit /export /cfg <results_dir>\<baseline>-current.inf /quiet` |
| Apply remediation INF | `secedit /configure /cfg <remediation.inf> /quiet` |
| Defender read | `powershell.exe -NoProfile -NonInteractive -Command "(Get-MpPreference).<Name>; exit $LASTEXITCODE"` |
| Defender remediate | `powershell.exe -NoProfile -NonInteractive -Command "Set-MpPreference -<Name> <Value>; exit $LASTEXITCODE"` |
| Registry read | `powershell.exe -NoProfile -NonInteractive -Command "(Get-ItemProperty -Path '<path>' -Name '<name>').'<name>'; exit $LASTEXITCODE"` |
| Registry remediate | `powershell.exe -NoProfile -NonInteractive -Command "Set-ItemProperty -Path '<path>' -Name '<name>' -Value '<value>' -Type <type>; exit $LASTEXITCODE"` |
| BitLocker read | `powershell.exe -NoProfile -NonInteractive -Command "(Get-BitLockerVolume -MountPoint '<drive>').ProtectionStatus; exit $LASTEXITCODE"` |
| BitLocker remediate | `powershell.exe -NoProfile -NonInteractive -Command "Enable-BitLocker -MountPoint '<drive>' -EncryptionMethod '<method>'; exit $LASTEXITCODE"` |

### Read file (via `IFileOperator`)

| Operation | Path |
|---|---|
| List baselines | `listdir(<baselines_dir>)` |
| Read manifest | `read_text("<baselines_dir>\\<name>\\<name>-findings.json")` |
| Read exported INF | `read_text("<results_dir>\\<baseline>-current.inf")` |
| Read baseline JSON | `read_text(<baseline_path>)` |

### Write file (via `IFileOperator`)

| Operation | Path |
|---|---|
| Write remediation INF (in-memory INF synthesized then written) | `<results_dir>\<baseline>-remediate.inf` |
| Write baseline JSON | `<baseline_path>` |

---

## Output Parsing

### `secedit /export` INF parsing

The exported INF is a UTF-16-LE (BOM-prefixed) text file in Windows
INI-format.  The parser:

1. Strips the BOM and decodes as UTF-16-LE.
2. Uses `configparser.RawConfigParser` with `strict=False` because
   Windows INF allows duplicate keys in some sections.
3. Flattens the result to a `dict[section.key → value]` for
   comparison against expected rule settings.

### `<Baseline>-findings.json` manifest parsing

The manifest is a JSON array of rule objects:

```json
[
  {"id": "AccountPolicy.MinimumPasswordLength",
   "description": "Enforce minimum password length",
   "severity": "high",
   "check_type": "secedit_inf",
   "section": "System Access",
   "key": "MinimumPasswordLength",
   "expected": "14"},
  {"id": "Defender.RealtimeMonitoring",
   "description": "Enable Defender real-time monitoring",
   "severity": "critical",
   "check_type": "defender",
   "name": "DisableRealtimeMonitoring",
   "expected": "False"}
]
```

The parser constructs one `WindowsComplianceCheck` per entry and
maps `check_type` into the dispatch table.

### PowerShell cmdlet output parsing

Each cmdlet script is wrapped with `; exit $LASTEXITCODE` so that exit
codes are meaningful.  The manager strips trailing whitespace from
stdout and treats the remaining string as the raw value.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `remediate` | Rule already compliant | Per-check dispatch re-read |
| `set_baseline` | Current baseline equals requested spec | `get_baseline()` |

`run_compliance_profile` has no guard; `remediate_all` skips
`compliant=True` directly.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsSecurityComplianceError` | `list_profiles` baseline-dir missing; `secedit /export` unexpected exit; unknown profile; corrupt baseline JSON |

Per-rule check failures are captured inside the report
(`compliant=False`, `error=...`) rather than raised.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: List-Profiles Scans Baselines Dir

*For any* call to `list_profiles()`, the manager should enumerate `<baselines_dir>` via the file operator without elevation.

**Validates: Requirements 1.1**

### Property 2: List-Profiles Structural Filter

*For any* directory entry, `list_profiles()` should include it only if `GPOs\` AND `<name>-findings.json` both exist.

**Validates: Requirements 1.1, 1.2**

### Property 3: List-Profiles Missing Dir Raises

*For any* call where `<baselines_dir>` does not exist, `list_profiles()` should raise `WindowsSecurityComplianceError` with a toolkit-download hint.

**Validates: Requirements 1.3**

### Property 4: Get-Profile Returns None For Unknown

*For any* unknown profile name, `get_profile()` should return `None`.

**Validates: Requirements 2.2**

### Property 5: Run-Profile Exports Current Policy First

*For any* call to `run_compliance_profile()`, the manager should invoke `secedit /export /cfg ... /quiet` BEFORE any per-rule dispatch.

**Validates: Requirements 3.1a**

### Property 6: Run-Profile Raises On Secedit Export Failure

*For any* non-zero exit from `secedit /export`, the manager should raise `WindowsSecurityComplianceError`.

**Validates: Requirements 3.2**

### Property 7: Run-Profile Unknown Profile Raises Pre-Runner

*For any* profile name whose directory does not exist, `run_compliance_profile()` should raise without invoking the runner.

**Validates: Requirements 3.3**

### Property 8: Run-Profile Per-Rule Errors Captured Not Raised

*For any* individual per-rule check failure, the finding should be `compliant=False` with the error text captured; the method should NOT raise.

**Validates: Requirements 3.4**

### Property 9: Run-Profile Finding ID Format

*For any* rule result, the produced `finding_id` should equal `<profile>:<rule_id>`.

**Validates: Requirements 3.5**

### Property 10: Run-Profile Severity Mapping

*For any* rule with `severity` set in the manifest, the manager should produce a finding with the matching `WindowsComplianceSeverity`.

**Validates: Requirements 3.5**

### Property 11: Run-Profile Remediable Only For Supported Types

*For any* rule whose `check_type` is NOT in the dispatch table, the finding's `remediable` should be `False`.

**Validates: Requirements 3.5, 8.2**

### Property 12: Run-Profile UTC Timestamps

*For any* successful report, `started_at` and `finished_at` should be UTC datetimes.

**Validates: Requirements 3.6**

### Property 13: Run-Profile Dry-Run Is A Read

*For any* run with `dry_run=True`, the manager should still execute the per-check reads.

**Validates: Requirements 3.7**

### Property 14: Remediate Dispatches By Type — secedit_inf

*For any* `secedit_inf` rule, `remediate()` should synthesize a remediation INF and dispatch `secedit /configure /cfg <path> /quiet`.

**Validates: Requirements 4.1a**

### Property 15: Remediate Dispatches By Type — defender

*For any* `defender` rule, `remediate()` should dispatch a `Set-MpPreference -<Name> <Value>` PowerShell command.

**Validates: Requirements 4.1b**

### Property 16: Remediate Dispatches By Type — registry

*For any* `registry` rule, `remediate()` should dispatch a `Set-ItemProperty` PowerShell command with the manifest-specified `-Type`.

**Validates: Requirements 4.1c**

### Property 17: Remediate Dispatches By Type — bitlocker

*For any* `bitlocker` rule, `remediate()` should dispatch an `Enable-BitLocker` PowerShell command against the specified drive.

**Validates: Requirements 4.1d**

### Property 18: Remediate Unknown Type Short-Circuit

*For any* rule whose `check_type` is outside the dispatch table, `remediate()` should return `FAILED` with `message="not_remediable"` without invoking the runner.

**Validates: Requirements 4.2**

### Property 19: Remediate Idempotency

*For any* rule that the per-check dispatch reports as already compliant, `remediate()` should return `SKIPPED` with `message="already_compliant"`.

**Validates: Requirements 4.3**

### Property 20: Remediate Dry-Run

*For any* `remediate()` call with `dry_run=True`, the remediation command should not be invoked and the result should be `DRY_RUN`.

**Validates: Requirements 4.6**

### Property 21: Remediate-All Skips Compliant

*For any* finding with `compliant=True`, `remediate_all()` should not include a result for it.

**Validates: Requirements 5.2**

### Property 22: Remediate-All Emits Not-Remediable Failures

*For any* finding with `compliant=False, remediable=False`, `remediate_all()` should include a `FAILED message="not_remediable"` result.

**Validates: Requirements 5.3**

### Property 23: Remediate-All Order Preserved

*For any* call to `remediate_all(report)`, results should appear in the same order as `report.findings`.

**Validates: Requirements 5.5**

### Property 24: Get-Baseline Missing File Returns Empty

*For any* call where the baseline file does not exist, the result should be a `WindowsBaselineInfo` with empty `name`, empty `settings`, and `source=None`.

**Validates: Requirements 6.2**

### Property 25: Get-Baseline Corrupt JSON Raises

*For any* malformed baseline JSON, `get_baseline()` should raise `WindowsSecurityComplianceError` with `message` starting `"baseline_corrupt:"`.

**Validates: Requirements 6.4**

### Property 26: Set-Baseline Idempotency

*For any* `set_baseline(spec)` where `get_baseline()` already returns an equal spec, the result should be `SKIPPED`.

**Validates: Requirements 7.1**

### Property 27: Set-Baseline Ordering

*For any* `set_baseline(spec)` with non-matching current state, the manager should run `run_compliance_profile(spec.name)`, then `remediate_all(report)`, and finally write the baseline JSON.

**Validates: Requirements 7.2**

### Property 28: Set-Baseline Step-Failure Identification

*For any* `set_baseline(spec)` whose profile-run or any remediation step fails, the manager should return `FAILED` with `message` identifying the failing step AND MUST NOT write the baseline file.

**Validates: Requirements 7.3**

### Property 29: Set-Baseline Target Field

*For any* successful `set_baseline(spec)`, the result's `target` should equal the baseline file path.

**Validates: Requirements 7.4**

### Property 30: Unsupported Check Type In Report

*For any* check with `check_type` outside the supported set, the report should include it with `compliant=False, error="unsupported_check_type:<type>", remediable=False`; no raise.

**Validates: Requirements 8.2**

### Property 31: Elevation For Remediation And Profile Run

*For any* `run_compliance_profile`, `remediate`, or `set_baseline` invocation when `elevated=True` is configured, the runner should be called with `elevated=True`.

**Validates: Requirements 9.1**

### Property 32: No Elevation For Profile Listing And Baseline Reads

*For any* `list_profiles`, `get_profile`, or `get_baseline` call, the file operator should not be invoked with `elevated=True`.

**Validates: Requirements 9.2**

### Property 33: Dry-Run Short-Circuit For Remediation

*For any* remediation or baseline-write operation with `dry_run=True`, neither the remediation command nor the baseline JSON write should execute, and the returned status should be `DRY_RUN`.

**Validates: Requirements 4.6, 5.4, 7.5, 10.1, 10.4**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── securitycompliance.py      ← WindowsSecurityComplianceManager, config,
                                  models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_securitycompliance.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.windows.fileops import LocalFileOperator, LocalFileOperatorConfig
from ccai.sysman.windows.securitycompliance import (
    WindowsSecurityComplianceManager,
    WindowsSecurityComplianceManagerConfig,
    WindowsBaselineSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator(LocalFileOperatorConfig())

config = WindowsSecurityComplianceManagerConfig(elevated=True, dry_run=False)
sc_mgr = WindowsSecurityComplianceManager(runner=runner, file_operator=file_op, config=config)

profiles = sc_mgr.list_profiles()
for p in profiles:
    print(f"{p.name}: {p.description}")

report = sc_mgr.run_compliance_profile(profile_name="Windows 11 v23H2 Security Baseline")
print(f"{report.compliant}/{report.total} compliant")

results = sc_mgr.remediate_all(report=report)
for r in results:
    print(r.status, r.finding_id, r.message)
```

### WinRM Execution with Aspects

```python
from ccai.sysman.windows.runners import WinRMCommandRunner, WinRMRunnerConfig
from ccai.sysman.windows.fileops import WinRMFileOperator, WinRMFileOperatorConfig
from ccai.sysman.windows.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage
from ccai.sysman.windows.securitycompliance import (
    WindowsSecurityComplianceManager,
    WindowsSecurityComplianceManagerConfig,
    WindowsBaselineSpec,
)

runner = WinRMCommandRunner(WinRMRunnerConfig(host="win-host", username="Administrator"))
file_op = WinRMFileOperator(WinRMFileOperatorConfig(host="win-host", username="Administrator"))

storage = SQLiteStorage(path="C:\\ProgramData\\CodeCraftAI\\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=1800.0)))

sc_mgr = WindowsSecurityComplianceManager(
    runner=runner,
    file_operator=file_op,
    config=WindowsSecurityComplianceManagerConfig(elevated=True),
)

spec = WindowsBaselineSpec(
    name="Windows 11 v23H2 Security Baseline",
    settings={"AccountPolicy.MinimumPasswordLength": "14"},
)
result = sc_mgr.set_baseline(spec=spec)
print(result.status, result.message)
```

### Dry-Run Mode

```python
config = WindowsSecurityComplianceManagerConfig(elevated=True, dry_run=True)
sc_mgr = WindowsSecurityComplianceManager(runner=runner, file_operator=file_op, config=config)

report = sc_mgr.run_compliance_profile(profile_name="Windows 11 v23H2 Security Baseline")
results = sc_mgr.remediate_all(report=report)
for r in results:
    assert r.status == WindowsSecurityComplianceActionStatus.DRY_RUN
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner` and `MockFileOperator`.
- `listdir` mock responses drive `list_profiles` structural-filter
  tests.
- Manifest JSON fixtures drive `run_compliance_profile` dispatch tests
  including every supported `check_type` and at least one
  unsupported type for the pass-through branch.
- INF export fixtures drive the `secedit_inf` check path; the INF is
  UTF-16-LE BOM-prefixed in the fixture.
- `baselines_dir` and `baseline_path` are overridden in tests to
  synthetic locations.
- Dry-run tests assert zero `Set-*`/`secedit /configure`/baseline
  JSON writes.
- A dedicated test covers PowerShell single-quote escaping for
  registry and Defender rule values.
- A dedicated test covers `remediate_all` ordering and the
  not-remediable pass-through.
- No test requires a live Windows host or Security Compliance Toolkit
  installation.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (profile names, rule IDs,
  severity strings, expected values).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: security-compliance (windows), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | Windows implementation | Notes |
|---|---|---|
| `list_profiles` | Scan `<baselines_dir>` | Structural filter. |
| `get_profile` | Enumerate + filter | `None` on miss. |
| `run_compliance_profile` | `secedit /export` + per-rule dispatch | Exit failure raises. |
| `remediate` | Per-rule-type dispatch | 4 dispatch paths. |
| `remediate_all` | Iterate + delegate | Order preserved. |
| `get_baseline` | JSON read via file operator | Missing → empty. |
| `set_baseline` | Run + remediate_all + write JSON | Fails pre-write on any step. |

---

## Extensions beyond the contract

- **Microsoft Security Compliance Toolkit as the engine.**  Each
  subdirectory of `<baselines_dir>` is treated as a compliance
  profile.  The `<name>-findings.json` manifest drives per-rule
  dispatch.
- **Per-rule-type remediation dispatch.**  The manager supports
  `secedit_inf`, `registry`, `defender`, and `bitlocker` rules.
  Adding new dispatch paths is a future enhancement.
- **Baseline persistence location.**  Baseline JSON lives at
  `%ProgramData%\CodeCraftAI\compliance\current_baseline.json`, managed
  via `IFileOperator`.
- **Results directory.**  Each profile run writes the exported INF
  and the synthesized remediation INF under
  `%ProgramData%\CodeCraftAI\compliance\results\` for auditability.

## Out of Scope

- **Microsoft DSC resource authoring** — the toolkit ships DSC
  configurations but this manager does not use them.
- **GPT.INI / registry.pol direct parsing** — GPO-shaped baselines
  are applied only through the per-rule dispatch; the manager does
  not parse GPO files directly.
- **LGPO.exe integration** — deferred to a future enhancement.
- **MBSA / MBAP analysis backends** — deprecated by Microsoft;
  intentionally excluded.
- **Signed / evidence-collection report output** — not implemented.
- **Attestation via TPM** — not implemented.
- **Continuous-monitoring / agent-mode** — the manager is invoked
  ad-hoc by the coordinator.
- **Remediation rollback** — `secedit` / Defender / BitLocker
  remediations are one-way; callers must capture pre-state
  externally if rollback is required.
- **Cross-OS parity with Ubuntu OpenSCAP or macOS
  baseline-automation findings** — findings are intentionally
  OS-shaped by the contract.
- **Feature-gap disclosure**: security-compliance is the single most
  OS-divergent feature.  Each OS's compliance tooling produces
  findings with native shapes.  The `WindowsComplianceFinding`
  dataclass is deliberately flexible to absorb that.  Cross-OS
  callers MUST treat findings as advisory and MUST NOT rely on a
  universal `check_type` enumeration beyond what the contract names.

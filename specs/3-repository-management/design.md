# Design

## Overview

The repository management feature is implemented as a single module,
`ccai.sysman.windows.repositorymanagement`, that exposes a
`WindowsRepositoryManager` class.  This spec implements the
repository-management contract defined at
`codecraftai-sysman-common/specs/contracts/repository-management/contract.md`
for Windows using `winget.exe`'s source subcommand.

The coordinator instantiates `WindowsRepositoryManager` and calls its
methods.  All command invocation is delegated to an `ICommandRunner`.
**No file operator is required** for Windows repository-management
because `winget` stores its source registry in an internal SQLite
database that the manager does not edit directly.

Winget sources are named and typed; type-aware operations are
supported via the contract's `channel` field mapping (see
Repository-Name Mapping).  Many contract fields (GPG, mirror list,
priority) are not applicable to winget; the manager accepts them with
a documented warning.  `enable_repository` is a documented SKIPPED /
FAILED no-op, `disable_repository` is a documented FAILED no-op, and
`set_priority` / `import_gpg_key` are documented SKIPPED no-ops
(with NotFound / KeyFileNotFound precondition checks).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/WinRM execution, reliability tracking, and
aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or WinRM)                     │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsRepositoryManager             │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           WindowsRepositoryManager                          │
│  ccai.sysman.windows.repositorymanagement               │
│                                                             │
│  add_repository()       remove_repository()                 │
│  enable_repository()    disable_repository()                │
│  list_repositories()    get_repository()                    │
│  refresh_repositories() set_priority()                      │
│  import_gpg_key()                                           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ WinRMCommandRunner   │
│ - subprocess w/ UAC  │      │ - remote PowerShell  │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsRepositoryManager`

**Location:** `source/packages/ccai/sysman/windows/repositorymanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_repository` | `(spec) → WindowsRepositoryActionResult` | `winget source add --name <n> --arg <url> [--type <t>]` |
| `remove_repository` | `(name) → WindowsRepositoryActionResult` | `winget source remove --name <n>` |
| `enable_repository` | `(name) → WindowsRepositoryActionResult` | No-op: SKIPPED if present, FAILED if absent |
| `disable_repository` | `(name) → WindowsRepositoryActionResult` | No-op: FAILED "winget_no_disable" |
| `list_repositories` | `() → list[WindowsRepositoryInfo]` | `winget source list` |
| `get_repository` | `(name) → WindowsRepositoryInfo \| None` | Enumerate + filter |
| `refresh_repositories` | `(name=None) → WindowsRepositoryActionResult` | `winget source update` |
| `set_priority` | `(name, priority) → WindowsRepositoryActionResult` | No-op SKIPPED (raises if unknown) |
| `import_gpg_key` | `(key_source) → WindowsRepositoryActionResult` | No-op SKIPPED (raises for missing local file) |

### `WindowsRepositoryManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `repos_dir` | `str \| None` | `None` | Unused on Windows; contract-mandated field |

---

## Data Models

### Enums

```python
class WindowsRepositoryActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

### Dataclasses

```python
@dataclass
class WindowsRepositorySpec:
    name: str                    # winget source name
    display_name: str
    base_url: str | None         # winget --arg
    mirror_list_url: str | None  # N/A — accepted with warning
    enabled: bool
    gpg_check: bool              # N/A — accepted with warning
    gpg_key: str | None          # N/A — accepted with warning
    priority: int | None         # N/A — accepted with warning
    channel: str | None          # winget --type: "Microsoft.PreIndexed.Package" | "Microsoft.Rest"

@dataclass
class WindowsRepositoryInfo:
    name: str
    display_name: str
    enabled: bool
    url: str | None
    priority: int | None

@dataclass
class WindowsRepositoryActionResult:
    status: WindowsRepositoryActionStatus
    name: str
    file_path: str | None         # always None on Windows
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    message: str | None

class WindowsRepositoryQueryError(Exception):
    """Raised when list_repositories fails unexpectedly."""

class WindowsRepositoryNotFoundError(Exception):
    """Raised when set_priority targets an unknown source."""

class WindowsRepositoryKeyFileNotFoundError(Exception):
    """Raised when import_gpg_key is given a missing local file path."""
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

Windows does not need an `IFileOperator` for this feature.

---

## Repository-Name Mapping

| Contract field | Windows mapping | Notes |
|---|---|---|
| `name` | `winget source` name | — |
| `display_name` | Informational only | winget does not persist it. |
| `base_url` | `--arg <url>` | Required for add. |
| `mirror_list_url` | N/A | Accepted with warning. |
| `enabled` | Always True once added | `disable` returns FAILED. |
| `gpg_check` | N/A | Signature verification via source type. |
| `gpg_key` | N/A | Accepted with warning; `import_gpg_key` returns SKIPPED. |
| `priority` | N/A | `set_priority` returns SKIPPED or raises NotFound. |
| `channel` | `--type <Microsoft.PreIndexed.Package\|Microsoft.Rest>` | Forwarded verbatim if recognized. |

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| Add source | `winget source add --name <name> --arg <url> [--type <type>]` |
| Remove source | `winget source remove --name <name>` |
| Refresh all | `winget source update` |
| Refresh one | `winget source update --name <name>` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| List sources | `winget source list` |

### No-op verbs (no runner invocation)

- `enable_repository` — SKIPPED when present, FAILED when absent.
- `disable_repository` — FAILED always (winget has no disable).
- `set_priority` — SKIPPED (or raise `WindowsRepositoryNotFoundError`).
- `import_gpg_key` — SKIPPED (or raise
  `WindowsRepositoryKeyFileNotFoundError`).

---

## Output Parsing

### `winget source list` output

`winget source list` emits a fixed-width table with a header row and
a dash-separator row followed by one data row per source.  Columns
vary by winget version but always include `Name` and `Argument` and
typically include `Type`, `Identifier`, `Explicit`.

The parser:

1. Skips leading blank lines and any `winget` progress-indicator
   lines (lines containing only `|`, `/`, `-`, `\` with spaces).
2. Detects the header row by looking for the literal `Name`
   followed by at least two spaces.
3. Computes column offsets from the header row.
4. Skips the dash-separator row.
5. For each remaining non-empty line, slices the line at the
   computed offsets and strips whitespace to produce the columns.
6. Maps `Name` → `name`, `Argument` → `url`, omits `priority`
   (set to `None`).

### Idempotency comparison

`add_repository` compares `spec.name` against the `Name` column and
`spec.base_url` against the `Argument` column.  Matches with same
URL → SKIPPED.  A source with the same name but different URL is
treated as a mismatch, and the manager dispatches the add command
(winget itself will reject the conflict with a meaningful exit
code, which flows through as `FAILED`).

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `add_repository` | Source already present with matching URL | `winget source list` parse |
| `remove_repository` | Source not present | `winget source list` parse |
| `enable_repository` | N/A — always SKIPPED or FAILED | — |
| `disable_repository` | N/A — always FAILED | — |
| `set_priority` | N/A — always SKIPPED or NotFound | — |
| `import_gpg_key` | N/A — always SKIPPED or KeyFileNotFound | — |

`refresh_repositories` has no guard.  `list_repositories` /
`get_repository` are reads.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsRepositoryQueryError` | `winget source list` returns non-zero |
| `WindowsRepositoryNotFoundError` | `set_priority` for an unknown source |
| `WindowsRepositoryKeyFileNotFoundError` | `import_gpg_key` with missing local file |

All other write failures are surfaced as
`WindowsRepositoryActionResult` with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Add-Source Command Construction

*For any* spec with `base_url` set and no `channel`, `add_repository()` should construct `winget source add --name <name> --arg <url>`.

**Validates: Requirements 1.1**

### Property 2: Add-Source With Type

*For any* spec with `channel="Microsoft.PreIndexed.Package"` or `channel="Microsoft.Rest"`, `add_repository()` should include `--type <channel>` in the command.

**Validates: Requirements 1.2**

### Property 3: Add-Source Missing URL Fails

*For any* spec whose `base_url` is `None`, `add_repository()` should return `FAILED` with `message="base_url_required"` without invoking the runner.

**Validates: Requirements 1.3**

### Property 4: Add-Source Idempotency

*For any* source already present in `winget source list` with a matching URL, `add_repository()` should return `SKIPPED`.

**Validates: Requirements 1.4**

### Property 5: Add-Source N/A Field Warning

*For any* spec with `gpg_key`, `mirror_list_url`, or `priority` set, the manager should accept the spec and log a one-time warning that the field is not applicable to winget.

**Validates: Requirements 1.7**

### Property 6: Remove-Source Command Construction

*For any* `remove_repository(name)`, the manager should dispatch `winget source remove --name <name>`.

**Validates: Requirements 2.1**

### Property 7: Remove-Source Idempotency

*For any* `remove_repository(name)` where the source is absent, the result should be `SKIPPED`.

**Validates: Requirements 2.2**

### Property 8: Enable-Repository SKIPPED When Present

*For any* `enable_repository(name)` where the source is present, the result should be `SKIPPED` with `message="winget_sources_always_enabled"`.

**Validates: Requirements 3.1**

### Property 9: Enable-Repository FAILED When Absent

*For any* `enable_repository(name)` where the source is absent, the result should be `FAILED` with `message="repository_not_found"`.

**Validates: Requirements 3.2**

### Property 10: Enable-Repository Never Mutates

*For any* `enable_repository()` call, the runner should not be invoked to mutate (only read via `winget source list` for presence check).

**Validates: Requirements 3.3**

### Property 11: Disable-Repository FAILED Always

*For any* `disable_repository(name)`, the result should be `FAILED` with `message="winget_no_disable: remove_required"`.

**Validates: Requirements 4.1**

### Property 12: Disable-Repository Never Invokes Runner

*For any* `disable_repository()` call, the runner should not be invoked.

**Validates: Requirements 4.2**

### Property 13: List-Sources Command Construction

*For any* call to `list_repositories()`, the manager should invoke `winget source list` without elevation.

**Validates: Requirements 5.1**

### Property 14: List-Sources Fixed-Width Parser

*For any* tabular `winget source list` output, `list_repositories()` should return one entry per data row with `name`, `url` extracted from the `Name` and `Argument` columns respectively.

**Validates: Requirements 5.3**

### Property 15: List-Sources Skips Progress And Separator Lines

*For any* progress-indicator or dash-separator line, the parser should skip it.

**Validates: Requirements 5.4**

### Property 16: List-Sources Empty Output Returns Empty List

*For any* `winget source list` that emits no data rows, the result should be `[]`.

**Validates: Requirements 5.5**

### Property 17: List-Sources Failure Raises

*For any* non-zero exit from `winget source list`, the method should raise `WindowsRepositoryQueryError`.

**Validates: Requirements 5.2**

### Property 18: Get-Repository Returns None For Unknown

*For any* unknown name, `get_repository()` should return `None`.

**Validates: Requirements 6.2**

### Property 19: Refresh-All Command Construction

*For any* `refresh_repositories(name=None)`, the manager should dispatch `winget source update`.

**Validates: Requirements 7.1**

### Property 20: Refresh-One Command Construction

*For any* `refresh_repositories(name=<name>)`, the manager should dispatch `winget source update --name <name>`.

**Validates: Requirements 7.2**

### Property 21: Refresh No Idempotency Guard

*For any* `refresh_repositories()` call in non-dry-run mode, the runner should be invoked — no `SKIPPED`.

**Validates: Requirements 7.6**

### Property 22: Set-Priority Not-Found Raises

*For any* `set_priority(name, ...)` where the source is absent, `WindowsRepositoryNotFoundError` should be raised.

**Validates: Requirements 8.1**

### Property 23: Set-Priority SKIPPED When Source Exists

*For any* `set_priority(name, ...)` where the source exists, the result should be `SKIPPED` with `message="winget_has_no_priority_concept"`.

**Validates: Requirements 8.2**

### Property 24: Set-Priority Never Invokes Runner Mutations

*For any* `set_priority()` call, the runner should not be invoked to mutate.

**Validates: Requirements 8.3**

### Property 25: Import-Key Missing Local Raises

*For any* `import_gpg_key(local_path)` where the path does not exist, `WindowsRepositoryKeyFileNotFoundError` should be raised.

**Validates: Requirements 9.1**

### Property 26: Import-Key SKIPPED Otherwise

*For any* `import_gpg_key` with valid URL or extant local file, the result should be `SKIPPED` with `message="winget_verifies_via_source_type_signing"`.

**Validates: Requirements 9.2**

### Property 27: Import-Key Never Invokes Runner

*For any* `import_gpg_key()` call, the runner should not be invoked.

**Validates: Requirements 9.3**

### Property 28: Elevation For Writes

*For any* write operation with `elevated=True` in the config, the runner should be invoked with `elevated=True`.

**Validates: Requirements 10.1**

### Property 29: No Elevation For Reads

*For any* read operation, the runner should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 10.2**

### Property 30: Dry-Run Short-Circuit For Mutating Methods

*For any* `add_repository`, `remove_repository`, or `refresh_repositories` call with `dry_run=True`, the runner's write commands should not be invoked, and the result should be `DRY_RUN`.

**Validates: Requirements 1.8, 2.5, 7.5, 11.1**

### Property 31: Dry-Run Precondition Exceptions Still Raise

*For any* dry-run `import_gpg_key` with a missing local file, or dry-run `set_priority` against an unknown source, the appropriate exception should still be raised.

**Validates: Requirements 11.4**

### Property 32: Dry-Run Still Reads For Idempotency

*For any* `add_repository` / `remove_repository` call in dry-run mode, the `winget source list` idempotency read should still execute.

**Validates: Requirements 11.3**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── repositorymanagement.py    ← WindowsRepositoryManager, config,
                                  models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_repositorymanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.windows.repositorymanagement import (
    WindowsRepositoryManager,
    WindowsRepositoryManagerConfig,
    WindowsRepositorySpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsRepositoryManagerConfig(elevated=True, dry_run=False)
repo_mgr = WindowsRepositoryManager(runner=runner, config=config)

# Add a winget source
spec = WindowsRepositorySpec(
    name="mycorp",
    display_name="Corporate winget source",
    base_url="https://winget.corp.example.com/index",
    mirror_list_url=None,
    enabled=True,
    gpg_check=False,
    gpg_key=None,
    priority=None,
    channel="Microsoft.PreIndexed.Package",
)
result = repo_mgr.add_repository(spec=spec)
print(f"Add: {result.status}")

# List
for info in repo_mgr.list_repositories():
    print(f"{info.name} url={info.url}")

# Refresh
repo_mgr.refresh_repositories()
```

### WinRM Execution with Aspects

```python
from ccai.sysman.windows.runners import WinRMCommandRunner, WinRMRunnerConfig
from ccai.sysman.windows.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage
from ccai.sysman.windows.repositorymanagement import (
    WindowsRepositoryManager,
    WindowsRepositoryManagerConfig,
)

runner = WinRMCommandRunner(WinRMRunnerConfig(host="win-host", username="Administrator"))

storage = SQLiteStorage(path="C:\\ProgramData\\CodeCraftAI\\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=300.0)))

repo_mgr = WindowsRepositoryManager(
    runner=runner,
    config=WindowsRepositoryManagerConfig(elevated=True),
)

repo_mgr.refresh_repositories()
```

### Dry-Run Mode

```python
config = WindowsRepositoryManagerConfig(elevated=True, dry_run=True)
repo_mgr = WindowsRepositoryManager(runner=runner, config=config)

result = repo_mgr.add_repository(spec=spec)
assert result.status == WindowsRepositoryActionStatus.DRY_RUN
print(result.command)   # ["winget", "source", "add", "--name", "mycorp", ...]
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner`.
- `winget source list` fixture strings drive idempotency-guard and
  parser tests, including fixtures with progress lines and
  dash-separator rows.
- Dry-run tests assert zero `winget source add/remove/update`
  mutating invocations.
- The `enable_repository` SKIPPED / FAILED behavior is tested with
  present and absent sources.
- The `disable_repository` always-FAILED behavior is tested.
- The `set_priority` raises-on-not-found branch is tested.
- The `import_gpg_key` raises-on-missing-local-file branch is
  tested.
- A dedicated test covers the `base_url_required` short-circuit
  when `spec.base_url` is `None`.
- A dedicated test covers `channel` → `--type` forwarding for the
  two recognized types.
- No test requires a live winget installation.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.
Each correctness property is implemented as a property-based test
that:

- Generates random but valid inputs (source names, URLs, channels).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: repository-management (windows), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | Windows implementation | Notes |
|---|---|---|
| `add_repository` | `winget source add --name --arg [--type]` | base_url required. |
| `remove_repository` | `winget source remove --name` | Idempotent. |
| `enable_repository` | No-op: SKIPPED or FAILED | Sources always enabled. |
| `disable_repository` | No-op: FAILED | winget has no disable. |
| `list_repositories` | `winget source list` + fixed-width parse | — |
| `get_repository` | Enumerate + filter | `None` on miss. |
| `refresh_repositories` | `winget source update [--name]` | No idempotency guard. |
| `set_priority` | No-op: SKIPPED (or NotFound) | No priority concept. |
| `import_gpg_key` | No-op: SKIPPED (or KeyFileNotFound) | Source-type verification. |

---

## Extensions beyond the contract

- **Documented no-op verbs.**  `enable_repository`,
  `disable_repository`, `set_priority`, and `import_gpg_key` are
  retained for cross-OS API uniformity but are deliberate no-ops
  that surface the winget feature gap loudly via SKIPPED / FAILED
  messages.
- **Channel → `--type` mapping.**  The contract's `channel` field
  maps to winget's `--type <Microsoft.PreIndexed.Package |
  Microsoft.Rest>` argument when set to a recognized value; unknown
  channels are logged and passed through with a warning.
- **N/A field warnings.**  Specs that set `gpg_key`,
  `mirror_list_url`, or `priority` are accepted with a one-time
  warning at construction or first-use time.
- **Required `base_url`.**  `winget source add` requires an
  argument URL; the manager short-circuits with a FAILED result
  rather than invoking a command that would fail.

## Out of Scope

- **Chocolatey source management** — a separate Chocolatey-specific
  manager would be a future feature; the contract explicitly lists
  both `winget` and Chocolatey under Windows but this spec covers
  only winget.
- **User-scope vs machine-scope distinction** — the manager does
  not introspect scope; it honors the coordinator's elevation
  choice.
- **Rest source authentication credentials** (`--header`) — not
  wired through; future enhancement.
- **Winget manifest authoring** — out of scope.
- **Repository mirroring / local mirror hosting** — out of scope.
- **Client-certificate authentication** — out of scope.
- **Mass-apply from a structured policy document** (YAML / TOML) —
  out of scope.

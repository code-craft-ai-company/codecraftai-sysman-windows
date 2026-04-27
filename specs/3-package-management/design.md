# Design

## Overview

The package-management feature is implemented as a single module,
`automojo.sysman.windows.packagemanagement`, that exposes a
`WindowsPackageManager` class.  This spec implements the
package-management contract defined at
`automojo-sysman-common/specs/contracts/package-management/contract.md`
for Windows using the Windows Package Manager (`winget.exe`).  The
coordinator instantiates the manager with a command runner and a config,
then calls install / remove / upgrade verbs plus a read-only query
surface.

All system interaction happens through `ICommandRunner`.  Idempotency
guards rely on `winget list --exact --id <name>` (is-installed check) and
`winget show --exact --id <name>` (version check).  No `IFileOperator` is
required; `winget` owns its own persistence under `%ProgramData%` (for
machine-scope) and `%LocalAppData%` (for user-scope).

Writes into machine-scope packages on Windows 10 1809+ require an
Administrator token; writes into user-scope packages do not.  The
manager honors `config.elevated` as the standard contract flag.

The feature works identically whether commands execute locally (via
`WindowsLocalCommandRunner`) or remotely over WinRM (via
`WindowsWinRMCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/WinRM execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or WinRM)                     │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsPackageManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              WindowsPackageManager                          │
│  automojo.sysman.windows.packagemanagement                  │
│                                                             │
│  install_package()  remove_package()  upgrade_package()     │
│  upgrade_all()      is_installed()                          │
│  get_package_info() list_installed()                        │
│  list_upgradable()  install_local()                         │
│  refresh_metadata()                                         │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsPackageManager`

**Location:** `source/packages/automojo/sysman/windows/packagemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `install_package` | `(name, version=None) → WindowsPackageActionResult` | `winget install --exact --id <name> [--version <version>] --silent --accept-package-agreements --accept-source-agreements` |
| `remove_package` | `(name) → WindowsPackageActionResult` | `winget uninstall --exact --id <name> --silent` |
| `upgrade_package` | `(name, version=None) → WindowsPackageActionResult` | `winget upgrade --exact --id <name> [--version <version>] --silent` |
| `upgrade_all` | `() → WindowsPackageActionResult` | `winget upgrade --all --silent --accept-package-agreements --accept-source-agreements` |
| `is_installed` | `(name) → bool` | `winget list --exact --id <name>` |
| `get_package_info` | `(name) → WindowsPackageInfo \| None` | `winget show --exact --id <name>` |
| `list_installed` | `() → list[WindowsPackageInfo]` | `winget list` |
| `list_upgradable` | `() → list[WindowsPackageUpdateInfo]` | `winget upgrade` |
| `install_local` | `(filepath) → WindowsPackageActionResult` | `winget install --silent --accept-package-agreements [--manifest] <filepath>` |
| `refresh_metadata` | `() → WindowsPackageActionResult` | `winget source update` |

### `WindowsPackageManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no write dispatches |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `assume_yes` | `bool` | `True` | When `True`, writes include `--accept-package-agreements` and `--accept-source-agreements` |

---

## Data Models

### Enum

```python
class WindowsPackageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### Dataclasses

```python
@dataclass
class WindowsPackageActionResult:
    status: WindowsPackageActionStatus
    package_name: str
    version: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None


@dataclass
class WindowsPackageInfo:
    name: str                       # winget package Id
    version: str
    release: str                    # always "" — winget has no release/revision split
    arch: str                       # "any" unless `winget show` reports an explicit arch
    origin: str | None              # winget source name (e.g. "winget", "msstore")


@dataclass
class WindowsPackageUpdateInfo:
    name: str
    installed_version: str
    available_version: str
    arch: str                       # always "any"


class WindowsPackageQueryError(Exception):
    """Raised when list_installed, list_upgradable, or get_package_info fails unexpectedly."""


class WindowsPackageFileNotFoundError(Exception):
    """Raised when install_local() is called with a non-existent path."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Command Mappings

### Write Commands

| Operation | argv (base) | Extra flags (assume_yes=True) |
|---|---|---|
| Install | `winget install --exact --id <name> --silent` | `--accept-package-agreements --accept-source-agreements` |
| Install at version | `winget install --exact --id <name> --version <version> --silent` | `--accept-package-agreements --accept-source-agreements` |
| Remove | `winget uninstall --exact --id <name> --silent` | — |
| Upgrade | `winget upgrade --exact --id <name> --silent` | — |
| Upgrade at version | `winget upgrade --exact --id <name> --version <version> --silent` | — |
| Upgrade all | `winget upgrade --all --silent` | `--accept-package-agreements --accept-source-agreements` |
| Install local (package file) | `winget install --silent <filepath>` | `--accept-package-agreements` |
| Install local (manifest `.yaml`) | `winget install --silent --manifest <filepath>` | `--accept-package-agreements` |
| Refresh metadata | `winget source update` | — |

All writes pass `elevated=True` when `config.elevated=True`.  Reads
never elevate.

### Read Commands (no elevation)

| Operation | argv |
|---|---|
| Is installed | `winget list --exact --id <name>` |
| Package info | `winget show --exact --id <name>` |
| List installed | `winget list` |
| List upgradable | `winget upgrade` |

---

## Output Parsing

### `winget list --exact --id <name>` Output

```
Name        Id           Version   Source
-----------------------------------------------
Git         Git.Git      2.45.0    winget
```

Parser: skip the header row and the separator row (leading dashes),
then split each data row by column position (winget emits
space-aligned columns).  Match `Id` column to `<name>` exactly.  A
zero exit + a matched row → installed.  Non-zero exit or no match →
not installed.

### `winget show --exact --id <name>` Output

```
Found Git [Git.Git]
Version: 2.45.0
Publisher: The Git Development Community
Description: Git for Windows ...
Homepage: https://git-scm.com/
Installer Type: inno
Installer Locale: en-US
Source: winget
```

Parser:
- If stderr contains `No package found matching input criteria.`,
  return `None`.
- `Name` → line starting with `Found ` up to the opening bracket.
- `Version` → line starting with `Version: `.
- `Source` → line starting with `Source: `.
- `arch` → `"any"` (winget does not consistently report per-package
  arch in `show`).

### `winget list` Output

```
Name                  Id               Version      Available  Source
----------------------------------------------------------------------
Windows Terminal      Microsoft.WindowsTerminal  1.18.10  1.19.10302  winget
Git                   Git.Git          2.45.0                  winget
```

Parser: skip header + separator rows; split each row on two-or-more
spaces to obtain `[Name, Id, Version, Available?, Source?]`.
Missing `Available` and `Source` columns are represented as empty
strings.

### `winget upgrade` Output

Same columns as `winget list` but only rows with a non-empty
`Available` column are returned.  Windows exits with code `0x8A150011`
(decimal `-2147023087`) when "No applicable upgrade found"; the parser
maps that specific code to an empty list.

---

## Idempotency Guards

| Method | Guard | Expected state to skip |
|---|---|---|
| `install_package(name, None)` | `is_installed(name)` is `True` | already installed |
| `install_package(name, V)` | `get_package_info(name).version == V` | already at target version |
| `remove_package(name)` | `is_installed(name)` is `False` | already removed |
| `upgrade_package(name, None)` | `is_installed(name)` is `False` → SKIPPED; else `winget list --exact --id <name>`'s `Available` column matches `Version` | no upgrade available |
| `upgrade_package(name, V)` | installed version equals `V` | already at target version |
| `upgrade_all` | never skips | — |
| `install_local` | never skips | — |
| `refresh_metadata` | never skips (contract: operational) | — |

All idempotency probes go through read paths and do NOT pass
`elevated=True`.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsPackageQueryError` | `list_installed`, `list_upgradable`, or `get_package_info` fails unexpectedly |
| `WindowsPackageFileNotFoundError` | `install_local()` called with a path that does not exist on disk |

All write failures surface as
`WindowsPackageActionResult(status=FAILED, …)`.

---

## Correctness Properties

### Property 1: Install Command Construction

*For any* `install_package(name, None)` whose guard does not skip, the runner argv is `["winget", "install", "--exact", "--id", name, "--silent", "--accept-package-agreements", "--accept-source-agreements"]`.

**Validates: Requirements 1.1**

### Property 2: Install Versioned Command Construction

*For any* `install_package(name, "1.2.3")` whose guard does not skip, the runner argv contains `--version 1.2.3`.

**Validates: Requirements 1.1**

### Property 3: Install Idempotency (Any Version)

*For any* already-installed package, `install_package(name, None)` returns `SKIPPED` without dispatching.

**Validates: Requirements 1.3**

### Property 4: Install Idempotency (Exact Version)

*For any* package installed at version `V`, `install_package(name, V)` returns `SKIPPED` without dispatching.

**Validates: Requirements 1.4**

### Property 5: Assume-Yes Off Omits Acceptance Flags

*For any* install call with `config.assume_yes=False`, the runner argv excludes `--accept-package-agreements` and `--accept-source-agreements` but still includes `--silent`.

**Validates: Requirements 1.2**

### Property 6: Remove Command Construction

*For any* `remove_package(name)` whose guard does not skip, the runner argv is `["winget", "uninstall", "--exact", "--id", name, "--silent"]`.

**Validates: Requirements 2.1**

### Property 7: Remove Idempotency

*For any* package not installed, `remove_package(name)` returns `SKIPPED`.

**Validates: Requirements 2.2**

### Property 8: Upgrade Command Construction

*For any* `upgrade_package(name, None)` whose guard does not skip, the runner argv is `["winget", "upgrade", "--exact", "--id", name, "--silent"]`.

**Validates: Requirements 3.1**

### Property 9: Upgrade Not Installed Skipped

*For any* `upgrade_package(name)` where the package is not installed, the result is `SKIPPED` with `message="package is not installed; nothing to upgrade"`.

**Validates: Requirements 3.2**

### Property 10: Upgrade Already At Version

*For any* `upgrade_package(name, V)` where the installed version equals `V`, the result is `SKIPPED`.

**Validates: Requirements 3.3**

### Property 11: Upgrade No Available Skipped

*For any* `upgrade_package(name, None)` where `winget list --exact --id <name>` shows `Available == Version`, the result is `SKIPPED`.

**Validates: Requirements 3.4**

### Property 12: Upgrade All Command

*For any* `upgrade_all()`, the runner argv is `["winget", "upgrade", "--all", "--silent", "--accept-package-agreements", "--accept-source-agreements"]`.

**Validates: Requirements 4.1**

### Property 13: Upgrade All Never Skips

*For any* `upgrade_all()`, the idempotency guard does not skip.

**Validates: Requirements 4.5**

### Property 14: Is Installed True

*For any* package whose `winget list --exact --id <name>` returns a row matching `Id`, `is_installed` returns `True`.

**Validates: Requirements 5.2**

### Property 15: Is Installed False

*For any* package whose `winget list --exact --id` returns no matching row or exits non-zero, `is_installed` returns `False`.

**Validates: Requirements 5.3**

### Property 16: Package Info Parse

*For any* `winget show` output with `Name`, `Version`, and `Source` fields, `get_package_info` returns a `WindowsPackageInfo` with those values.

**Validates: Requirements 5.4**

### Property 17: Package Info Not Installed

*For any* `winget show` failure matching "No package found matching input criteria.", `get_package_info` returns `None`.

**Validates: Requirements 5.5**

### Property 18: Package Info Unexpected Error

*For any* other `winget show` failure, `WindowsPackageQueryError` is raised.

**Validates: Requirements 5.6**

### Property 19: List Installed Parse

*For any* valid `winget list` output, `list_installed` returns one `WindowsPackageInfo` per data row, keyed on `Id`.

**Validates: Requirements 6.2**

### Property 20: List Installed Error

*For any* failure of `winget list`, `WindowsPackageQueryError` is raised.

**Validates: Requirements 6.3**

### Property 21: List Upgradable Parse

*For any* valid `winget upgrade` output, `list_upgradable` returns one `WindowsPackageUpdateInfo` per row with a non-empty `Available` column.

**Validates: Requirements 7.2**

### Property 22: List Upgradable Empty (Expected Exit Code)

*For any* `winget upgrade` exit with the "No applicable upgrade found" exit code `0x8A150011`, `list_upgradable` returns an empty list without raising.

**Validates: Requirements 7.3**

### Property 23: List Upgradable Unexpected Error

*For any* other non-zero exit from `winget upgrade`, `list_upgradable` raises `WindowsPackageQueryError`.

**Validates: Requirements 7.4**

### Property 24: Install Local Missing File

*For any* `install_local(path)` where `path` does not exist, `WindowsPackageFileNotFoundError` is raised and the runner is NOT invoked.

**Validates: Requirements 8.2**

### Property 25: Install Local Command (Package File)

*For any* `install_local(path)` where `path` has a non-`.yaml` extension, the runner argv is `["winget", "install", "--silent", "--accept-package-agreements", path]`.

**Validates: Requirements 8.1**

### Property 26: Install Local Command (Manifest)

*For any* `install_local(path)` where `path` ends with `.yaml`, the runner argv includes `--manifest <path>`.

**Validates: Requirements 8.1**

### Property 27: Install Local Dry-Run After File Check

*For any* `install_local(path)` in dry-run mode with a non-existent file, the result is still `WindowsPackageFileNotFoundError`.

**Validates: Requirements 8.2, 8.5**

### Property 28: Refresh Metadata Command

*For any* `refresh_metadata()`, the runner argv is `["winget", "source", "update"]`.

**Validates: Requirements 9.1**

### Property 29: Refresh Metadata Never Skips

*For any* `refresh_metadata()`, the idempotency guard does not skip.

**Validates: Requirements 9.5**

### Property 30: Writes Elevate When Configured

*For any* write with `config.elevated=True`, the runner is invoked with `elevated=True`.

**Validates: Requirements 10.1**

### Property 31: Reads Do Not Elevate

*For any* read or idempotency-probe call, the runner is NEVER invoked with `elevated=True`.

**Validates: Requirements 10.2, 10.3**

### Property 32: Dry-Run Safety (All Writes)

*For any* write verb with `config.dry_run=True`, the runner is NOT invoked and the result has `status=DRY_RUN`.

**Validates: Requirements 1.7, 2.5, 3.7, 4.4, 8.5, 9.4**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
└── packagemanagement.py       ← WindowsPackageManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_packagemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import (
    WindowsLocalCommandRunner, WindowsLocalRunnerConfig,
)
from automojo.sysman.windows.packagemanagement import (
    WindowsPackageManager, WindowsPackageManagerConfig,
)

runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
config = WindowsPackageManagerConfig(elevated=True, dry_run=False, assume_yes=True)
pkg_mgr = WindowsPackageManager(runner=runner, config=config)

refresh = pkg_mgr.refresh_metadata()
print(f"refresh: {refresh.status.value}")

result = pkg_mgr.install_package(name="Git.Git", version=None)
print(f"install git: {result.status.value} version={result.version}")
```

### WinRM Execution With Aspects

```python
from automojo.sysman.windows.runners import (
    WindowsWinRMCommandRunner, WindowsWinRMRunnerConfig,
)
from automojo.sysman.windows.aspects import (
    RetryAspect, RetryAspectConfig, TimeoutAspect, TimeoutAspectConfig,
)

runner = WindowsWinRMCommandRunner(
    WindowsWinRMRunnerConfig(host="win-fleet-01", username="DOMAIN\\ops"),
)
runner.add_aspect(aspect=RetryAspect(
    RetryAspectConfig(max_retries=3, retryable_exit_codes=[-1978335222]),
))
runner.add_aspect(aspect=TimeoutAspect(
    TimeoutAspectConfig(default_timeout=900.0),
))

pkg_mgr = WindowsPackageManager(
    runner=runner,
    config=WindowsPackageManagerConfig(elevated=True),
)
pkg_mgr.upgrade_all()
```

### Dry-Run Preview

```python
config = WindowsPackageManagerConfig(elevated=True, dry_run=True)
pkg_mgr = WindowsPackageManager(runner=runner, config=config)
result = pkg_mgr.remove_package(name="Microsoft.WindowsTerminal")
assert result.status.value == "dry_run"
```

---

## Testing Strategy

1. **MockCommandRunner** keyed on argv prefix queues responses for
   `winget list --exact --id`, `winget show --exact --id`,
   `winget list`, `winget upgrade`, and the write commands
   independently.
2. One command-construction test per write verb asserts the exact
   argv, including the `--silent` / `--accept-package-agreements` /
   `--accept-source-agreements` flags.
3. Idempotency tests cover installed / not-installed and
   version-match / version-miss / available-equal scenarios.
4. `get_package_info` tests cover the installed path, the "No
   package found" path (returns `None`), and an unexpected-error
   path (raises).
5. `list_installed` parsing is tested with a realistic multi-row
   `winget list` output including rows with empty `Available` and
   `Source` columns.
6. `list_upgradable` parsing covers: non-empty `Available` rows,
   the expected "No applicable upgrade found" exit code
   (`0x8A150011` → empty list), and an unexpected exit (raises).
7. `install_local` tests assert `WindowsPackageFileNotFoundError`
   when the path is missing, `--manifest` is included for `.yaml`
   paths, and direct file path is passed for other extensions.
8. `assume_yes=False` test asserts the acceptance flags are
   omitted but `--silent` remains.
9. Elevation tests assert `elevated=True` for writes and never for
   reads or probes.
10. No test requires Administrator access or a live `winget`
    installation.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `install_package` | `winget install --exact --id <name>` |
| `remove_package` | `winget uninstall --exact --id <name>` |
| `upgrade_package` | `winget upgrade --exact --id <name>` |
| `upgrade_all` | `winget upgrade --all` |
| `is_installed` | `winget list --exact --id <name>` |
| `get_package_info` | `winget show --exact --id <name>` |
| `list_installed` | `winget list` |
| `list_upgradable` | `winget upgrade` |
| `install_local` | `winget install [--manifest] <filepath>` |
| `refresh_metadata` | `winget source update` |

---

## Extensions Beyond the Contract

### Chocolatey Backend

A future extension may expose a companion `WindowsChocolateyPackageManager`
that honors the same contract surface but dispatches to `choco`.  The
two managers can be composed behind a selector; the default remains
`WindowsPackageManager` (winget).

### MSI Install Fallback

When `install_local` receives a path with a `.msi` extension and
`winget install <filepath>` fails with a "not a valid winget package"
exit code, a future extension may fall back to invoking `msiexec /i
<filepath> /quiet /norestart`.  This fallback is NOT implemented in
the base contract and would require an explicit opt-in flag on the
config.

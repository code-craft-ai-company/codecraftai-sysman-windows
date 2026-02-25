# Design

## Overview

The repository management feature is implemented as a single module,
`automojo.sysman.linux.windows.repositorymanagement`, that exposes a `RepositoryManager`
class.  The coordinator instantiates `RepositoryManager` and calls its methods to perform
discrete maintenance actions against the `/etc/yum.repos.d/` directory and the `dnf`
toolchain.

All system interaction is delegated to a command runner provided by `automojo-interfaces`.
File write and delete operations are also expressed as commands through the runner so that
dry-run mode, auditing, and test mocking apply uniformly to both shell commands and file
system mutations.  `RepositoryManager` has no direct dependency on the file system or
on `subprocess`.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│          RepositoryManager              │  automojo.sysman.linux.windows.repositorymanagement
│                                         │
│  add()           remove()               │
│  enable()        disable()              │
│  refresh()       list_repos()           │
│  set_priority()  import_gpg_key()       │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│                                         │
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
```

---

## Components

### `RepositoryManager`

**Location:** `source/packages/automojo/sysman/linux/windows/repositorymanagement.py`

The primary public class.  Constructed with an `ICommandRunner` and a
`RepositoryManagerConfig`.

| Method | Description |
|---|---|
| `add(repo_def)` | Write a `.repo` file for a new repository definition |
| `remove(repo_id)` | Delete the `.repo` file for a repository |
| `enable(repo_id)` | Enable a repository using `dnf config-manager` |
| `disable(repo_id)` | Disable a repository using `dnf config-manager` |
| `refresh(repo_id=None)` | Clean and rebuild `dnf` metadata cache |
| `list_repos()` → `list[RepoInfo]` | Return all configured repositories and their state |
| `set_priority(repo_id, priority)` | Set the `priority=` field in a `.repo` file |
| `import_gpg_key(key_source)` | Import a GPG key by URL or local file path |

### `RepositoryManagerConfig`

**Location:** `source/packages/automojo/sysman/linux/windows/repositorymanagement.py`

A dataclass holding runtime configuration for `RepositoryManager`.

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands or file writes are executed |
| `elevated` | `bool` | `False` | When `True`, write operations are run with sudo |
| `repos_dir` | `str` | `"/etc/yum.repos.d"` | Path to repository definition directory |

### `RepoDefinition`

An input dataclass passed to `add()` describing the repository to create.

| Field | Type | Required | Description |
|---|---|---|---|
| `repo_id` | `str` | Yes | Unique repository identifier (used as file name stem) |
| `name` | `str` | Yes | Human-readable repository name |
| `base_url` | `str` | No | Base URL for package downloads |
| `mirror_list` | `str` | No | URL to a mirror list file (mutually exclusive with `base_url`) |
| `enabled` | `bool` | Yes | Initial enabled state (`1` or `0` in file) |
| `gpg_check` | `bool` | Yes | Whether to enforce GPG signature checking |
| `gpg_key` | `str` | No | URL or path to the GPG key for this repository |
| `priority` | `int` | No | Repository priority (lower number = higher priority) |

---

## Data Models

### `RepoActionResult`

Returned by all write operations (add, remove, enable, disable, refresh, set-priority,
import-key).

```python
@dataclass
class RepoActionResult:
    status: RepoActionStatus      # SUCCESS | FAILED | SKIPPED | DRY_RUN
    repo_id: str
    exit_code: int | None         # raw exit code from command runner, if applicable
    stdout: str | None
    stderr: str | None
    command: list[str] | None     # the command that was (or would have been) run
    file_path: str | None         # the file path affected, for file operations
```

### `RepoActionStatus`

```python
class RepoActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"   # already in desired state
    DRY_RUN  = "dry_run"   # would have acted, but dry_run=True
```

### `RepoInfo`

Returned as elements of `list_repos()`.

```python
@dataclass
class RepoInfo:
    repo_id: str
    name: str
    enabled: bool
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

`RepositoryManager` depends on this interface, injected at construction time.  File write
and delete operations are expressed as `tee` and `rm` commands so they flow through the
same runner and are subject to the same dry-run and elevation handling as `dnf` commands.

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `automojo-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    command: list[str]
```

---

## Command Mappings

| Operation | Command |
|---|---|
| Write `.repo` file | `tee /etc/yum.repos.d/<repo-id>.repo` (stdin = rendered content) |
| Remove `.repo` file | `rm -f /etc/yum.repos.d/<repo-id>.repo` |
| Enable repository | `dnf config-manager --set-enabled <repo-id>` |
| Disable repository | `dnf config-manager --set-disabled <repo-id>` |
| Refresh all repos | `dnf clean metadata` then `dnf makecache` |
| Refresh one repo | `dnf clean metadata --disablerepo="*" --enablerepo=<id>` then `dnf makecache --disablerepo="*" --enablerepo=<id>` |
| List repositories | `dnf repolist all` |
| Set priority | `dnf config-manager --setopt=<repo-id>.priority=<n> --save` |
| Import GPG key | `rpm --import <key-url-or-path>` |

### `.repo` File Rendering

The `add()` method renders the `RepoDefinition` into an INI-style `.repo` file.  The
rendering is done in pure Python (no file system calls) and the resulting string is written
via the command runner using `tee`.

Example rendered output:

```ini
[myrepo]
name=My Custom Repository
baseurl=https://example.com/repo/
enabled=1
gpgcheck=1
gpgkey=https://example.com/repo/RPM-GPG-KEY
priority=10
```

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `RepoQueryError` | `dnf repolist all` returns a non-zero exit code |
| `RepoNotFoundError` | `set_priority()` is called for a repo whose `.repo` file does not exist |
| `RepoKeyFileNotFoundError` | `import_gpg_key()` is called with a local file path that does not exist |

All other failures are surfaced as a `RepoActionResult` with `status=FAILED` so the
coordinator can decide how to respond without catching exceptions for normal operational
failures.

### Idempotency Guards

| Method | Guard |
|---|---|
| `add()` | Reads existing `.repo` file content (via `cat` through the runner) and compares to rendered content before writing |
| `remove()` | Checks for existence of `.repo` file before issuing `rm` |
| `enable()` | Calls `list_repos()` to confirm current enabled state before running `config-manager` |
| `disable()` | Calls `list_repos()` to confirm current disabled state before running `config-manager` |
| `set_priority()` | Reads the current `priority=` value in the file before writing |

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── repositorymanagement.py       ← RepositoryManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_repositorymanagement.py  ← unit tests using MockCommandRunner
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` injected into `RepositoryManager` at construction time.
- Tests verify the *arguments* passed to the runner, not file system or system state.
- The `repos_dir` field in `RepositoryManagerConfig` is overridden in tests to a temp path
  so that any file-path construction is exercised without touching `/etc/yum.repos.d/`.
- Each public method has at minimum: a success path test, a skip/idempotency test, a
  failure path test, and a dry-run test.
- `refresh()` is tested for both the all-repos and single-repo variants.
- `import_gpg_key()` is tested for both URL and local file path inputs.
- No test requires root access or a live `dnf` installation.

# Design

## Overview

The command-execution feature is implemented as a single module,
`ccai.sysman.windows.commandexecution`, that exposes a `WindowsCommandExecutor`
class.  This spec implements the command-execution contract defined at
`codecraftai-sysman-common/specs/contracts/command-execution/contract.md` for Windows
using `subprocess.run` (wrapped inside `WindowsLocalCommandRunner`) for local execution
and `paramiko` (wrapped inside `WindowsSSHCommandRunner`, which lives in
`codecraftai-protocol-ssh`) for remote execution over OpenSSH Server.  The coordinator
instantiates the executor with a runner and a config, then calls `run`, `run_sequence`,
or `run_script` to dispatch work.

No `subprocess` call lives in the executor itself.  The only direct filesystem work
performed in the module is:

- `tempfile.NamedTemporaryFile(suffix=".ps1", delete=False)` for `run_script` temp
  files.
- `pathlib.Path(working_dir).is_dir()` for the working-directory precondition.
- `pathlib.Path(tmpfile).unlink(missing_ok=True)` in the `finally` block of
  `run_script`.

A tiny `_is_process_elevated()` helper uses `ctypes.windll.shell32.IsUserAnAdmin` (or
equivalently, a `WindowsIdentity`/`WindowsPrincipal` check via `ctypes` wrappers) to
evaluate the host process token when the caller passes `elevated=True`.

Environment merging is performed in pure Python using `os.environ`.  Elevation on
Windows is an *assertion* — the executor cannot acquire an Administrator token at
subprocess time (Windows' `runas` is interactive-only).  If the caller requests
`elevated=True` and the host process is not already elevated, the executor returns a
typed failure **without invoking the runner**.  SSH runs delegate the elevation check
to the remote SSH server's token.

The feature works identically whether commands execute locally (via
`WindowsLocalCommandRunner`) or remotely over SSH (via `WindowsSSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsCommandExecutor               │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsCommandExecutor                           │
│  ccai.sysman.windows.commandexecution                   │
│                                                             │
│  run()           run_sequence()                             │
│  run_script()                                               │
│  _is_process_elevated()                                     │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, cwd, env, timeout) → RunResult         │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌────────────────────────────┐  ┌────────────────────────────┐
│ WindowsLocalCommandRunner  │  │ WindowsSSHCommandRunner    │
│ - subprocess.run()         │  │ - paramiko connection      │
│ - shell=False argv exec    │  │ - reliability metrics      │
│ - relies on host token     │  │ - remote OpenSSH Server    │
└────────────────────────────┘  └────────────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsCommandExecutor`

**Location:** `source/packages/ccai/sysman/windows/commandexecution.py`

| Method | Signature | Description |
|---|---|---|
| `run` | `(args, *, elevated, working_dir, env, timeout_seconds) → WindowsCommandResult` | Execute a single argv-based command |
| `run_sequence` | `(commands, *, stop_on_failure) → WindowsSequenceResult` | Execute an ordered `list[WindowsCommandSpec]` |
| `run_script` | `(script, *, elevated, working_dir, env, timeout_seconds) → WindowsCommandResult` | Write script to `.ps1` temp file and invoke `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <tmpfile>` |
| `_is_process_elevated` | `() → bool` | Evaluate the host process's Administrator token; returns `True` on elevated contexts |

### `WindowsCommandExecutorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no runner call occurs for any verb |
| `default_timeout_seconds` | `float \| None` | `None` | Fallback timeout when no per-call value is supplied |
| `powershell_path` | `str` | `"powershell.exe"` | Interpreter used by `run_script`; callers may override with `pwsh.exe` for PowerShell 7+ |
| `skip_local_elevation_check` | `bool` | `False` | When `True`, bypass the local elevation pre-check (used for SSH runners whose token is remote) |

---

## Data Models

### Status Enum

```python
class WindowsCommandExecutionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"    # reserved for coordinator use; executor never emits this
    DRY_RUN  = "dry_run"
```

### Spec

```python
@dataclass
class WindowsCommandSpec:
    args: list[str]
    elevated: bool = False
    working_dir: str | None = None
    env: dict[str, str] | None = None
    timeout_seconds: float | None = None
    label: str | None = None
```

### Result Types

```python
@dataclass
class WindowsCommandResult:
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    args: list[str]
    timed_out: bool = False
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    elevated: bool = False

@dataclass
class WindowsSequenceResult:
    all_succeeded: bool
    results: list[WindowsCommandResult]
    failed_at: int | None
    dry_run: bool = False
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    command: list[str]
    timed_out: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

---

## Command Mappings

### Local Execution

| Situation | argv passed to `subprocess.run` by `WindowsLocalCommandRunner` |
|---|---|
| Any | `[*args]` with `shell=False`; the runner relies on `PATHEXT` expansion by the OS when the executable name is unqualified |
| `run_script` | `[config.powershell_path, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "<tmpfile>"]` |

No `runas` is ever emitted.  When `elevated=True` the executor's pre-check guarantees
the host process token is already Administrator; the runner launches the child process
which inherits that token automatically.

### SSH Execution

| Situation | Behaviour of `WindowsSSHCommandRunner` |
|---|---|
| Any invocation | Joined argv with double-quoting per arg, sent to the remote OpenSSH Server (which defaults to PowerShell as the subsystem shell on Windows 10 build 1809+) |
| `elevated=True` with `skip_local_elevation_check=True` | Pass through; remote session token enforces the elevation |
| `run_script` | The executor supplies the local `.ps1` path; runner uploads the file via SFTP, invokes PowerShell, and cleans up via `Remove-Item -Force` |

---

## Elevation

| Situation | Executor behaviour | Runner behaviour |
|---|---|---|
| `elevated=True`, local runner, host elevated | Invoke runner with `elevated=True` | Runner spawns child which inherits Administrator token |
| `elevated=True`, local runner, host NOT elevated | Return typed `WindowsCommandResult(success=False, stderr="elevation requested but host process is not Administrator")` **without invoking runner** | — |
| `elevated=True`, SSH runner (`skip_local_elevation_check=True`) | Invoke runner with `elevated=True` | Remote server enforces elevation; if the SSH user lacks Administrator, the server returns a non-zero exit |
| `elevated=False` | Invoke runner with `elevated=False` | Runner spawns child with the caller's standard token |
| `dry_run=True` | Skip the elevation pre-check entirely and reflect the requested value in the synthetic result | — |

The helper `_is_process_elevated()` is the canonical answer to "am I elevated?".  It
wraps `ctypes.windll.shell32.IsUserAnAdmin()` (returns a 32-bit BOOL) and caches the
result per executor instance to avoid repeated syscalls in `run_sequence`.

### Extensions beyond the contract

The contract states: *"OS mechanism: `sudo` on RHEL/Ubuntu/macOS; Administrator token /
UAC on Windows; …The runner owns the mechanism."*  Windows adds a hard **pre-check**
because a non-elevated `subprocess.run` silently runs with a standard token — if the
executor simply forwarded `elevated=True` and the runner could not acquire Admin, the
caller would only discover the privilege loss through downstream access-denied errors.
The pre-check turns a latent misconfiguration into a typed, non-executing failure.

---

## `run()` Logic

1. If `config.dry_run` is `True`: return
   `WindowsCommandResult(success=True, exit_code=0, stdout="", stderr="", args=args,
   dry_run=True, elevated=elevated)`.
2. If `args` is an empty list: return a `WindowsCommandResult` with `success=False`,
   `exit_code=None`, and `stderr="args must be a non-empty list"` without invoking the
   runner.
3. If `elevated=True` and `config.skip_local_elevation_check=False`:
   - Call `self._is_process_elevated()`.
   - If the helper returns `False`: return
     `WindowsCommandResult(success=False, exit_code=None, stderr="elevation requested
     but host process is not Administrator", args=args, elevated=True)` and SKIP the
     runner.
4. Compute `effective_timeout` as `timeout_seconds` if supplied, else
   `config.default_timeout_seconds`, else `None`.
5. If `working_dir` is not `None` and `pathlib.Path(working_dir).is_dir()` is `False`:
   return `WindowsCommandResult(success=False, exit_code=None, stderr=f"working_dir
   does not exist: {working_dir}", args=args, elevated=elevated)`.
6. Compute `merged_env` per the Environment Merging section.
7. Record `started_at = datetime.utcnow()`.
8. Call `self._runner.run(args=args, elevated=elevated, cwd=working_dir,
   env=merged_env, timeout=effective_timeout)`.
9. Record `finished_at = datetime.utcnow()` and `duration_ms`.
10. Map the `RunResult` to `WindowsCommandResult` with `success = (exit_code == 0 and
    not timed_out)` and `timed_out = run_result.timed_out`.
11. When `timed_out=True` and `stderr` is empty, inject
    `f"Command timed out after {effective_timeout} seconds"`.

## `run_sequence()` Logic

1. If `commands` is an empty list: return
   `WindowsSequenceResult(all_succeeded=True, results=[], failed_at=None)` without
   invoking the runner.
2. If `config.dry_run=True`: build one synthetic `WindowsCommandResult` per spec with
   `dry_run=True`, return `WindowsSequenceResult(all_succeeded=True, results=[...],
   dry_run=True, failed_at=None)`.
3. Iterate `commands`:
   - Call `self.run(args=spec.args, elevated=spec.elevated,
     working_dir=spec.working_dir, env=spec.env,
     timeout_seconds=spec.timeout_seconds)`.  Each per-spec call independently
     re-evaluates the elevation pre-check.
   - Append the resulting `WindowsCommandResult` to `results`.
   - If `not result.success` and `stop_on_failure=True`: break out of the loop.
4. Compute `all_succeeded = all(r.success for r in results)`.
5. Compute `failed_at` as the first index where `not result.success`, else `None`.
6. Return `WindowsSequenceResult(all_succeeded=all_succeeded, results=results,
   failed_at=failed_at)`.

## `run_script()` Logic

1. If `config.dry_run=True`: return
   `WindowsCommandResult(success=True, exit_code=0, stdout="", stderr="",
   args=[self._config.powershell_path, "-NoProfile", "-File", "<dry-run>"],
   dry_run=True, elevated=elevated)`.
2. If `script` is an empty string: return a `WindowsCommandResult` with
   `success=False` and `stderr="script must be a non-empty string"` without creating a
   temp file.
3. Use `tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False, dir=None,
   encoding="utf-8-sig")` to write the script text — the `utf-8-sig` encoding prevents
   PowerShell from mis-detecting the file as ASCII when the script contains non-ASCII
   text.  Capture the resulting path as `tmpfile_path`.
4. Wrap the following in `try/finally`:
   - `try:` call
     `self.run(args=[self._config.powershell_path, "-NoProfile", "-ExecutionPolicy",
     "Bypass", "-File", tmpfile_path], elevated=elevated, working_dir=working_dir,
     env=env, timeout_seconds=timeout_seconds)`.
   - `finally:` call `pathlib.Path(tmpfile_path).unlink(missing_ok=True)`.
5. Return the result from step 4.

If step 3 raises an `OSError`, the executor catches it, returns a
`WindowsCommandResult(success=False, stderr=str(os_err),
args=[self._config.powershell_path, "-NoProfile", "-File", "<tempfile-error>"])`, and
does NOT invoke the runner.

---

## Environment Merging

The merged environment is computed by the executor in pure Python:

```python
if env is None:
    merged_env = None
else:
    merged_env = {**os.environ, **env}
```

- `env is None` → runner receives `env=None` and uses its own default.
- `env == {}` → runner receives a fresh copy of `os.environ` so mutations in aspects do
  not leak into the caller.
- Caller-supplied keys always win on conflicts.
- Windows environment variable names are case-insensitive at OS level but
  case-sensitive in Python dicts; the executor preserves the caller's casing.

---

## Error Handling

### Typed Exceptions

Command-execution does not define typed query exceptions — all failures are surfaced on
`WindowsCommandResult` or `WindowsSequenceResult`.

| Situation | Surface |
|---|---|
| Non-zero exit | `WindowsCommandResult(success=False, exit_code=N, ...)` |
| Timeout | `WindowsCommandResult(success=False, timed_out=True, ...)` |
| Missing `working_dir` | `WindowsCommandResult(success=False, exit_code=None, ...)` |
| Empty `args` | `WindowsCommandResult(success=False, exit_code=None, ...)` |
| Host process not Administrator when `elevated=True` | `WindowsCommandResult(success=False, exit_code=None, stderr="elevation requested but host process is not Administrator")` |
| Tempfile OSError in `run_script` | `WindowsCommandResult(success=False, exit_code=None, ...)` |
| Runner raises an uncaught exception | Propagates — executor does not swallow |

---

## Dry-Run Behaviour

When `WindowsCommandExecutorConfig.dry_run=True`:

- `run()` returns immediately with a synthetic `WindowsCommandResult(dry_run=True,
  success=True, exit_code=0, stdout="", stderr="", args=args, elevated=elevated)`.
  The elevation pre-check is bypassed.
- `run_sequence()` builds one synthetic result per spec and returns a
  `WindowsSequenceResult(all_succeeded=True, dry_run=True, ...)`.
- `run_script()` never creates the temp file and returns a synthetic result whose
  `args=[powershell_path, "-NoProfile", "-File", "<dry-run>"]`.

Synthetic results have `started_at=None`, `finished_at=None`, and `duration_ms=None`.

---

## Correctness Properties

### Property 1: Single-Command Success Mapping

*For any* runner returning `exit_code=0` and `timed_out=False`, `run()` should return a
`WindowsCommandResult` with `success=True`, the observed exit code, and the observed
stdout/stderr.

**Validates: Requirements 1.2, 7.1**

### Property 2: Single-Command Failure Mapping

*For any* runner returning `exit_code != 0`, `run()` should return a
`WindowsCommandResult` with `success=False` and the observed exit code.

**Validates: Requirements 1.3, 7.2**

### Property 3: Timeout Surfacing

*For any* runner returning `timed_out=True`, `run()` should return a
`WindowsCommandResult` with `timed_out=True`, `success=False`, and a non-empty
`stderr`.

**Validates: Requirements 1.4, 6.4, 7.3**

### Property 4: Empty Args Rejection

*For any* call to `run([])`, the system should return a `WindowsCommandResult` with
`success=False` and SHALL NOT invoke the runner.

**Validates: Requirements 1.5**

### Property 5: Run Dry-Run Short-Circuit

*For any* call to `run()` with `dry_run=True`, the runner is never invoked and the
result has `dry_run=True`, `success=True`, `exit_code=0`.

**Validates: Requirements 1.6, 9.1**

### Property 6: Sequence Ordering

*For any* sequence call, the order of `results` matches the order of `commands`, with
one result per executed or dry-run spec.

**Validates: Requirements 2.1**

### Property 7: Stop-On-Failure Stops

*For any* sequence with `stop_on_failure=True` where command N fails, no runner call
happens for commands with index > N, and `failed_at == N`.

**Validates: Requirements 2.2**

### Property 8: Continue-On-Failure Continues

*For any* sequence with `stop_on_failure=False`, every command is dispatched exactly
once, and `failed_at` is the lowest failing index or `None`.

**Validates: Requirements 2.3**

### Property 9: Empty Sequence

*For any* empty `commands` list, `run_sequence` returns `all_succeeded=True`,
`results=[]`, `failed_at=None` without invoking the runner.

**Validates: Requirements 2.4**

### Property 10: Sequence Dry-Run

*For any* sequence call with `dry_run=True`, the runner is never invoked and one
synthetic result per spec appears in `results`.

**Validates: Requirements 2.5, 9.2**

### Property 11: Sequence Spec Forwarding

*For any* `WindowsCommandSpec` carrying `elevated`, `working_dir`, `env`, or
`timeout_seconds`, the executor forwards those values to the per-command `run` call.

**Validates: Requirements 2.6**

### Property 12: Script Temp File Lifecycle

*For any* `run_script()` call that is not dry-run, a temp file with suffix `.ps1` is
created, the runner is invoked with argv starting with
`[powershell_path, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ...]`, and the
file is deleted after the call regardless of success.

**Validates: Requirements 3.1, 3.2**

### Property 13: Script Dry-Run

*For any* `run_script()` call with `dry_run=True`, no temp file is created and the
returned result has `args[-1] == "<dry-run>"` and `dry_run=True`.

**Validates: Requirements 3.3, 9.3**

### Property 14: Empty Script Rejection

*For any* `run_script("")` call, a failed `WindowsCommandResult` is returned without
creating a temp file or invoking the runner.

**Validates: Requirements 3.4**

### Property 15: Tempfile OSError Surface

*For any* failure from `tempfile.NamedTemporaryFile`, the result has `success=False`
and the runner is not invoked.

**Validates: Requirements 3.5**

### Property 16: Working-Dir None is Forwarded

*For any* `run()` call with `working_dir=None`, the runner receives `cwd=None`.

**Validates: Requirements 4.1**

### Property 17: Working-Dir Existence Accepted

*For any* `run()` call where `working_dir` resolves to an existing directory, the
runner receives that path via `cwd`.

**Validates: Requirements 4.2**

### Property 18: Working-Dir Missing Rejected

*For any* `run()` call with a non-existent `working_dir`, the result has
`success=False` and the runner is not invoked.

**Validates: Requirements 4.3**

### Property 19: Working-Dir Skipped in Dry-Run

*For any* `run()` call in dry-run mode, `working_dir` existence is not inspected and
the result is synthetic success.

**Validates: Requirements 4.4**

### Property 20: Env None Propagation

*For any* `run()` call with `env=None`, the runner receives `env=None`.

**Validates: Requirements 5.1**

### Property 21: Env Merge Precedence

*For any* `run()` call with `env={k: v}`, the runner's env contains every key of
`os.environ` plus `k`, with the caller's `v` winning on conflicts.

**Validates: Requirements 5.2**

### Property 22: Empty Env Merge

*For any* `run()` call with `env={}`, the runner receives a dict equal to `os.environ`,
and the caller's empty dict is never mutated.

**Validates: Requirements 5.3**

### Property 23: Default Timeout Fallback

*For any* `run()` call with `timeout_seconds=None` and a
`config.default_timeout_seconds` set, the runner receives that default.

**Validates: Requirements 6.2**

### Property 24: Dual None Timeout

*For any* `run()` call with both `timeout_seconds=None` and
`config.default_timeout_seconds=None`, the runner receives `timeout=None`.

**Validates: Requirements 6.3**

### Property 25: Elevation Pre-Check Accepts Elevated Host

*For any* `run()` call with `elevated=True` where the host process IS Administrator,
the runner receives `elevated=True`.

**Validates: Requirements 8.1, 8.2**

### Property 26: Elevation Pre-Check Rejects Non-Elevated Host

*For any* `run()` call with `elevated=True` where the host process is NOT
Administrator, the runner is NOT invoked and the result has
`success=False, stderr="elevation requested but host process is not Administrator"`.

**Validates: Requirements 8.3**

### Property 27: Elevation False Bypasses Pre-Check

*For any* `run()` call with `elevated=False`, the elevation pre-check is not consulted
and the runner is invoked with `elevated=False`.

**Validates: Requirements 8.4**

### Property 28: Dry-Run Preserves Elevation On Result

*For any* `run()` call with `dry_run=True`, `WindowsCommandResult.elevated` equals the
caller's requested value and the pre-check is not consulted.

**Validates: Requirements 8.5, 9.4**

### Property 29: SSH Skip-Check Path

*For any* `run()` call with `config.skip_local_elevation_check=True`, the pre-check is
not consulted and elevation is forwarded verbatim.

**Validates: Requirements 8.6**

### Property 30: Aspect Transparency

*For any* runner with no aspects and an otherwise identical runner with Persistence /
Retry / Timeout aspects attached, the `WindowsCommandResult` fields `args`, `elevated`,
and `dry_run` agree.

**Validates: Requirements 10.1, 10.3**

### Property 31: Retry Flip

*For any* retry aspect that converts the final attempt's non-zero exit to zero, the
returned `success` equals `True`; `args`, `elevated`, and `dry_run` are preserved.

**Validates: Requirements 10.2**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
├── commandexecution.py      ← WindowsCommandExecutor, config, spec, results, enum
└── _elevation.py            ← _is_process_elevated() helper using ctypes

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_commandexecution.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import (
    WindowsLocalCommandRunner,
    WindowsLocalRunnerConfig,
)
from ccai.sysman.windows.commandexecution import (
    WindowsCommandExecutor,
    WindowsCommandExecutorConfig,
    WindowsCommandSpec,
)

runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
config = WindowsCommandExecutorConfig(dry_run=False, default_timeout_seconds=120.0)
executor = WindowsCommandExecutor(runner=runner, config=config)

# Single command — requires the host process to already be elevated
result = executor.run(
    args=["winget", "upgrade", "--all", "--silent"],
    elevated=True,
    env={"POWERSHELL_TELEMETRY_OPTOUT": "1"},
    timeout_seconds=600.0,
)
print(f"exit_code={result.exit_code} success={result.success}")
```

### Sequence with Stop-on-Failure

```python
commands = [
    WindowsCommandSpec(args=["Stop-Service", "-Name", "W3SVC"],
                       elevated=True, label="stop"),
    WindowsCommandSpec(args=["winget", "upgrade", "--exact",
                             "--id", "Microsoft.IIS.Extension",
                             "--silent"],
                       elevated=True, label="upgrade"),
    WindowsCommandSpec(args=["Start-Service", "-Name", "W3SVC"],
                       elevated=True, label="start"),
]

seq_result = executor.run_sequence(commands=commands, stop_on_failure=True)
print(f"all_succeeded={seq_result.all_succeeded} failed_at={seq_result.failed_at}")
```

### SSH with Aspects

```python
from ccai.sysman.windows.runners import (
    WindowsSSHCommandRunner,
    WindowsSSHRunnerConfig,
)
from ccai.sysman.windows.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage

runner = WindowsSSHCommandRunner(
    WindowsSSHRunnerConfig(host="win-prod-01", username="Administrator"),
)
runner.add_aspect(aspect=PersistenceAspect(
    PersistenceAspectConfig(storage_backend=SQLiteStorage(path="C:/Temp/cmds.db")),
))
runner.add_aspect(aspect=RetryAspect(
    RetryAspectConfig(max_retries=3, retryable_exit_codes=[255]),
))
runner.add_aspect(aspect=TimeoutAspect(
    TimeoutAspectConfig(default_timeout=300.0),
))

executor = WindowsCommandExecutor(
    runner=runner,
    config=WindowsCommandExecutorConfig(skip_local_elevation_check=True),
)
result = executor.run_script(
    script="Get-Service | Where-Object { $_.Status -eq 'Running' } | Format-Table Name,Status",
    elevated=False,
)
```

### Dry-Run Preview

```python
config = WindowsCommandExecutorConfig(dry_run=True)
executor = WindowsCommandExecutor(runner=runner, config=config)

result = executor.run(args=["Remove-Item", "-Recurse", "-Force",
                            "C:/Logs/old"], elevated=True)
assert result.dry_run is True
assert result.success is True
assert result.started_at is None
```

---

## Testing Strategy

1. **Mock runner** — a small `_MockRunner` dataclass stubs `ICommandRunner`, letting
   each test queue a sequence of `RunResult` instances and inspect the call history.
2. **Success mapping** — a canned `RunResult(exit_code=0, ...)` produces
   `success=True`, and a non-zero run produces `success=False`.
3. **Timeout surface** — a `RunResult(timed_out=True, stderr="")` produces
   `timed_out=True`, `success=False`, and a synthesised `stderr`.
4. **Elevation pre-check** — a monkey-patched `_is_process_elevated` returning `False`
   makes `run(elevated=True)` return a typed failure without invoking the runner; when
   returning `True`, the runner IS invoked.
5. **Skip-check opt-out** — `skip_local_elevation_check=True` bypasses the helper and
   forwards the call regardless.
6. **Working-dir** — a known-bad path returns a failure result and asserts the mock
   runner is never called.
7. **Env merge** — `monkeypatch.setenv` patches `os.environ`, and the test asserts the
   `env` kwarg passed to the mock runner contains both process and caller keys with
   caller winning.
8. **Sequence stop-on-failure** — a three-spec sequence with a middle failure asserts
   exactly two runner calls and `failed_at==1`.
9. **Script lifecycle** — the mock runner inspects
   `args == [powershell_path, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
   "<tmpfile>"]`, and `Path(tmpfile).exists()` is `False` after the call.
10. **Script dry-run** — `Path` is never probed; the result's last `args[-1] ==
    "<dry-run>"`.
11. **Aspect composition** — a test wires `RetryAspect` to flip a failed exit into
    success and asserts the `args`/`elevated`/`dry_run` fields remain stable.
12. No test requires Administrator rights, a live `winget`, or real SSH — the
    `_is_process_elevated` helper is stubbed in tests.

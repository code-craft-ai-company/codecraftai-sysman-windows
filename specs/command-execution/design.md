# Design

## Overview

The command execution feature is implemented as a single module,
`automojo.sysman.linux.windows.commandexecution`, that exposes a `CommandExecutor` class.
The coordinator instantiates `CommandExecutor` and calls its methods to run commands,
sequences, and shell scripts through the `ICommandRunner` abstraction.

No `subprocess` calls, file system access, or environment inspection occurs directly
inside this module, except for:
- Creating and deleting a temporary script file for `run_script()` (via `tempfile` and
  `pathlib`).
- Checking that a `working_dir` path exists (via `pathlib.Path.is_dir()`).
- Merging caller-supplied `env` with `os.environ` for `run()` and `run_script()`.

Everything else goes through `ICommandRunner`.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│            CommandExecutor              │  automojo.sysman.linux.windows.commandexecution
│                                         │
│  run()          run_sequence()          │
│  run_script()                           │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│  run(args, elevated, cwd, env,          │
│      timeout, on_output, on_error)      │
│      → RunResult                        │
└─────────────────────────────────────────┘
```

---

## Components

### `CommandExecutor`

**Location:** `source/packages/automojo/sysman/linux/windows/commandexecution.py`

| Method | Signature | Description |
|---|---|---|
| `run` | `(args, *, elevated, working_dir, env, timeout_seconds, on_output, on_error) → CommandResult` | Execute a single command |
| `run_sequence` | `(commands, *, stop_on_failure) → SequenceResult` | Execute an ordered list of `CommandSpec` objects |
| `run_script` | `(script, *, elevated, working_dir, env, timeout_seconds, on_output, on_error) → CommandResult` | Write script to temp file and run via `bash` |

All keyword-only arguments are optional with documented defaults.

### `CommandExecutorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |

---

## Data Models

### `CommandSpec`

Represents a single command within a sequence.

```python
@dataclass
class CommandSpec:
    args: list[str]
    elevated: bool = False
    working_dir: str | None = None
    env: dict[str, str] | None = None
    timeout_seconds: float | None = None
    on_output: Callable[[str], None] | None = None
    on_error: Callable[[str], None] | None = None
    label: str | None = None         # optional human-readable name for reporting
```

### `CommandResult`

```python
@dataclass
class CommandResult:
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    args: list[str]
    timed_out: bool = False
    dry_run: bool = False
```

### `SequenceResult`

```python
@dataclass
class SequenceResult:
    all_succeeded: bool
    results: list[CommandResult]
    failed_at: int | None     # index of first failed command, or None
    dry_run: bool = False
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
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        on_output: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Method Designs

### `run(args, *, elevated, working_dir, env, timeout_seconds, on_output, on_error)`

1. If `dry_run=True`: return `CommandResult(success=True, exit_code=0, stdout="", stderr="", args=args, dry_run=True)`.
2. If `working_dir` is not `None` and `Path(working_dir).is_dir()` is `False`:
   return `CommandResult(success=False, exit_code=1, stdout="", stderr=f"working_dir does not exist: {working_dir}", args=args)`.
3. Build merged env: `{**os.environ, **env}` when `env` is provided; else `None`.
4. Call `self._runner.run(args, elevated=elevated, cwd=working_dir, env=merged_env, timeout=timeout_seconds, on_output=on_output, on_error=on_error)`.
5. Map `RunResult` to `CommandResult`:
   - `success = (run_result.exit_code == 0)`
   - `timed_out = run_result.timed_out`
   - If `timed_out`, set `stderr` to a standard timeout message if runner did not populate it.

### `run_sequence(commands, *, stop_on_failure=True)`

1. If `commands` is empty: return `SequenceResult(all_succeeded=True, results=[], failed_at=None)`.
2. If `dry_run=True`: build one dry-run `CommandResult` per spec and return `SequenceResult(all_succeeded=True, results=[...], dry_run=True)`.
3. Iterate `commands`:
   - Call `self.run(spec.args, elevated=spec.elevated, working_dir=spec.working_dir, env=spec.env, timeout_seconds=spec.timeout_seconds, on_output=spec.on_output, on_error=spec.on_error)`.
   - Append `CommandResult` to `results`.
   - If `not result.success` and `stop_on_failure=True`: return `SequenceResult(all_succeeded=False, results=results, failed_at=i)`.
4. `all_succeeded = all(r.success for r in results)`
5. `failed_at = next((i for i, r in enumerate(results) if not r.success), None)`
6. Return `SequenceResult(all_succeeded=all_succeeded, results=results, failed_at=failed_at)`.

### `run_script(script, *, elevated, working_dir, env, timeout_seconds, on_output, on_error)`

1. If `dry_run=True`: return `CommandResult(success=True, exit_code=0, stdout="", stderr="", args=["bash", "<dry-run>"], dry_run=True)`.
2. Write `script` to a `tempfile.NamedTemporaryFile` with `suffix=".sh"`, `delete=False`.
3. `chmod` the temp file to `0o755` via `os.chmod`.
4. Call `self.run(["bash", tmpfile_path], elevated=elevated, working_dir=working_dir, env=env, timeout_seconds=timeout_seconds, on_output=on_output, on_error=on_error)`.
5. Delete the temp file via `Path(tmpfile_path).unlink(missing_ok=True)` in a `finally` block.
6. Return the `CommandResult` from step 4.

---

## Environment Merging

The merged environment is computed as `{**os.environ, **caller_env}` so that caller
values take precedence over inherited process environment.  When `env=None` the runner
receives `None` and uses its own default (typically the unmodified process environment).

---

## Dry-Run Behaviour

When `CommandExecutorConfig.dry_run=True`:
- `run()` returns immediately with a synthetic success result and `dry_run=True`.
- `run_sequence()` builds one synthetic result per spec and returns immediately.
- `run_script()` returns immediately — the temp file is never created.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── commandexecution.py         ← CommandExecutor, config, models

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_commandexecution.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` whose `.run()` return value is configured per test.
- `working_dir` existence checks are tested with `tmp_path` (pytest fixture) for existing
  directories and a known-bad path for the not-found case.
- Temp file creation and cleanup for `run_script()` is verified by inspecting the
  `MockCommandRunner` call args (`args[0] == "bash"`, `args[1]` ends with `.sh`).
- Sequence stop-on-failure is verified by counting runner invocations.
- Dry-run for all three methods is verified by asserting the runner is never called.
- Timeout handling uses a `MockCommandRunner` that returns `RunResult(timed_out=True)`.
- Environment merging is verified by inspecting the `env` kwarg passed to the runner.
- No test requires a live system or root access.

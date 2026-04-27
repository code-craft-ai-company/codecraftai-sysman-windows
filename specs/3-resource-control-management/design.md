# Design

## Overview

`WindowsResourceControlManager` implements the cross-OS
resource-control-management contract using **logical group
records** persisted as JSON files (one per group) plus
**per-PID PowerShell adjustments** (`PriorityClass`,
`ProcessorAffinity`).

Windows Job Objects (`CreateJobObject` / `SetInformationJobObject`
in `Kernel32.dll`) are the kernel-level mechanism for
arbitrary process-group resource ceilings — but PowerShell
exposes no clean cmdlets to create or attach to Job Objects.
The pilot therefore takes the "logical group" path: the JSON
spec is the source of truth, attached PIDs are listed in the
spec, and the spec's recorded `PriorityClass` is applied to
each PID via `Get-Process`.  Where a verb cannot be
implemented at all via PowerShell — `set_memory_limit` (no
PS cmdlet for `JOB_OBJECT_LIMIT_PROCESS_MEMORY`) and
`set_io_limits` (no PS cmdlet for I/O bandwidth) — the verb
returns a typed `FAILED` result with a `windows_*` detail
token.

The manager is **stateless beyond config**.  Every public
verb takes its inputs from arguments, dispatches commands
through the injected `ICommandRunner`, and returns a result
dataclass.

---

## Cross-OS Resource-Control Concept Mapping

| Concept                   | Ubuntu (cgroups v2 + systemd)                | macOS (launchd + taskpolicy)                      | Windows (this package)                          |
|---------------------------|----------------------------------------------|---------------------------------------------------|-------------------------------------------------|
| Group container           | systemd `<name>.slice`                       | launchd label `automojo-rc-<name>`                | logical record `<group_dir>\<name>.json`        |
| On-disk representation    | `/etc/systemd/system/<name>.slice`           | `/Library/LaunchDaemons/automojo-rc-<name>.plist` | `<group_dir>\<name>.json`                       |
| Kernel substrate          | cgroups v2 at `/sys/fs/cgroup/<name>.slice/` | none                                              | Job Objects (NOT exposed via PowerShell)        |
| CPU ceiling tool          | `systemctl set-property CPUQuota=N%`         | unsupported per-group                             | `(Get-Process).PriorityClass = '<class>'` (best-effort, coarse) |
| Memory ceiling tool       | `systemctl set-property MemoryMax=N`         | launchd `RLIMIT_AS`                               | **NOT SUPPORTED via PowerShell**                |
| IO ceiling tool           | `systemctl set-property IOWeight=N`          | unsupported per-group                             | **NOT SUPPORTED via PowerShell**                |
| Attach process            | write PID to `cgroup.procs`                  | `taskpolicy -p <pid>` (best-effort)               | spec append + `PriorityClass` apply             |
| Detach process            | write PID to `system.slice/cgroup.procs`     | `taskpolicy -p <pid> -c default`                  | spec drop + `PriorityClass` reset to `Normal`   |
| Usage source              | `cpu.stat`, `memory.current`, `io.stat`      | per-PID `top -l 1`                                | per-PID `Get-Process | Select WorkingSet64,CPU` |
| Persistent reload         | `systemctl daemon-reload`                    | `launchctl bootout` + `bootstrap`                 | none required                                   |
| Idempotency anchor        | byte-equal slice file + `systemctl show`     | byte-equal plist body                             | byte-equal JSON spec                            |

---

## Windows Limitations — Prominent

The following gaps are intentional and surface as
contract-conformant `FAILED` results with typed `detail` /
`message` tokens:

### Gap 1: `set_memory_limit` — NOT SUPPORTED via PowerShell

Windows Job Objects support `JOB_OBJECT_LIMIT_PROCESS_MEMORY`,
but PowerShell does not expose a cmdlet to create / configure
Job Objects in a portable way.  The pilot therefore returns:

```python
WindowsResourceControlActionResult(
    status=WindowsResourceControlActionStatus.FAILED,
    detail="windows_no_powershell_job_object_memory_limit",
    message="PowerShell exposes no clean Job Object memory limit cmdlet",
    command=["powershell.exe", ..., "(Get-Process -Id <pid>).WorkingSet64"],
)
```

A future P/Invoke-based companion manager (outside the
PowerShell-only scope of the pilot) can add full Job Object
support without overlapping this module.

### Gap 2: `set_io_limits` — NOT SUPPORTED via PowerShell

Windows has no PowerShell cmdlet for per-process I/O bandwidth
limits.  The pilot returns `FAILED` with
`detail="windows_no_powershell_io_limit"`.

### Gap 3: `set_cpu_limit` is a coarse `PriorityClass` mapping

Windows has no native CPU-percent throttle for arbitrary
process groups.  The manager translates the requested percent
to a `System.Diagnostics.ProcessPriorityClass` value:

| Percent       | PriorityClass |
|---------------|---------------|
| 0..25         | `Idle`        |
| 26..50        | `BelowNormal` |
| 51..75        | `Normal`      |
| 76..99        | `AboveNormal` |
| 100+          | `High`        |

This is a coarse approximation.  Operators needing accurate
percent ceilings should use the future P/Invoke companion
manager that creates Job Objects with
`JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION` set.

### Gap 4: `attach_process` is bookkeeping + per-PID apply

The PID is added to the spec's `attached_pids` array and the
spec's recorded `PriorityClass` is applied to the running
process.  The PID is NOT a member of a Windows Job Object;
when the PID exits its membership disappears with it.

### Gap 5: `get_group_usage` IO bytes are `None`

`Get-Process` does not surface per-PID I/O byte counters in a
portable PowerShell shape.  The pilot reports `None` for
`io_read_bytes` / `io_write_bytes` and surfaces only memory
(`WorkingSet64`) and CPU (cumulative seconds via the `CPU`
property) per attached PID.

---

## Component Diagram

```
                    Maintenance coordinator
                            │
                            │  injects runner, config
                            ▼
              WindowsResourceControlManager
                            │
                            │  .run(argv, elevated=…)
                            ▼
                      ICommandRunner
                            │
              ┌─────────────┴──────────────┐
              ▼                            ▼
       LocalCommandRunner            WinRMCommandRunner
              │                            │
              └─────────────┬──────────────┘
                            ▼
        powershell.exe -NoProfile -NonInteractive -Command "..."
                            │
                            ▼
            <group_dir>\<name>.json
            Get-Process / Set-Content
```

---

## Public Surface

### Class

```python
class WindowsResourceControlManager(BaseFeatureManager):
    result_class = WindowsResourceControlActionResult
    status_class = WindowsResourceControlActionStatus
    feature_name = "resource-control-management"

    def create_group(self, spec: WindowsResourceGroupSpec) -> WindowsResourceControlActionResult: ...
    def delete_group(self, name: str) -> WindowsResourceControlActionResult: ...
    def set_cpu_limit(self, name: str, percent: int) -> WindowsResourceControlActionResult: ...
    def set_memory_limit(self, name: str, bytes_: int) -> WindowsResourceControlActionResult: ...   # FAILED
    def set_io_limits(self, name: str, spec: WindowsIoLimitSpec) -> WindowsResourceControlActionResult: ...   # FAILED
    def attach_process(self, name: str, pid: int) -> WindowsResourceControlActionResult: ...
    def detach_process(self, name: str, pid: int) -> WindowsResourceControlActionResult: ...
    def list_groups(self) -> list[WindowsResourceGroupInfo]: ...
    def get_group_usage(self, name: str) -> WindowsResourceGroupUsage: ...
```

### Data models

```python
WindowsResourceControlActionStatus = ActionStatus

@dataclass
class WindowsResourceControlManagerConfig(FeatureConfig):
    powershell_executable: str = "powershell.exe"
    group_dir: str = r"C:\ProgramData\Automojo\rcgroups"

@dataclass
class WindowsResourceControlActionResult(FeatureActionResult):
    group_name: Optional[str] = None
    pid: Optional[int] = None
    message: Optional[str] = None

@dataclass
class WindowsIoLimitSpec:
    weight: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None

@dataclass
class WindowsResourceGroupSpec:
    name: str
    cpu_percent: Optional[int] = None
    memory_bytes: Optional[int] = None
    io_weight: Optional[int] = None
    io_read_bps: Optional[int] = None
    io_write_bps: Optional[int] = None
    attached_pids: list[int] = field(default_factory=list)

@dataclass
class WindowsResourceGroupInfo:
    name: str
    cpu_percent: Optional[int]
    memory_bytes: Optional[int]
    io_weight: Optional[int]
    io_read_bps: Optional[int]
    io_write_bps: Optional[int]
    attached_pids: list[int]
    is_active: bool
    effective_cpu_percent: Optional[int]
    effective_memory_bytes: Optional[int]

@dataclass
class WindowsResourceGroupUsage:
    name: str
    cpu_usage_seconds: Optional[float]
    memory_used_bytes: Optional[int]
    io_read_bytes: Optional[int]
    io_write_bytes: Optional[int]
    process_count: int

class WindowsResourceControlQueryError(Exception):
    ...
```

---

## Verb Implementations

All commands are dispatched as PowerShell scriptblocks through
`powershell.exe -NoProfile -NonInteractive -Command "..."`.

### `create_group(spec)`

```
1. validate spec.name (regex ^[A-Za-z0-9][A-Za-z0-9._\-]*$)
2. spec_path = "<group_dir>\<name>.json"
3. rendered_json = json.dumps(spec, indent=2, sort_keys=True)
4. if config.dry_run -> DRY_RUN with the planned Set-Content argv
5. existing_json = _read_spec(name)
6. if existing_json == rendered_json -> SKIPPED
7. _ensure_group_dir()  (PowerShell: New-Item -ItemType Directory -Path <group_dir> -Force)
8. powershell -Command "Set-Content -LiteralPath '<spec_path>' -Value '...' -Encoding UTF8"
9. SUCCESS / FAILED
```

The rendered JSON document has shape:

```json
{
  "name": "batch",
  "cpu_percent": 50,
  "memory_bytes": 1073741824,
  "io_weight": null,
  "io_read_bps": null,
  "io_write_bps": null,
  "attached_pids": [111, 222]
}
```

### `delete_group(name)`

```
1. validate name
2. if config.dry_run -> DRY_RUN
3. if not _spec_exists(name) -> SKIPPED
4. powershell -Command "Remove-Item -LiteralPath '<spec_path>' -Force"
5. SUCCESS / FAILED
```

### `set_cpu_limit(name, percent)`

```
1. validate name
2. priority_class = _percent_to_priority_class(percent)
3. if config.dry_run -> DRY_RUN with the planned argv
4. existing_spec = _read_spec(name)
5. if existing_spec is None -> FAILED("group_not_found")
6. existing_class = _percent_to_priority_class(existing_spec.cpu_percent)
7. if existing_class == priority_class -> SKIPPED
8. update spec.cpu_percent ; rewrite spec.json via Set-Content
9. for each pid in spec.attached_pids:
       powershell -Command "(Get-Process -Id <pid>).PriorityClass = '<class>'"
10. SUCCESS / FAILED
```

### `set_memory_limit(name, bytes_)`  -- FAILED

```
1. validate name
2. if config.dry_run -> DRY_RUN with the documented argv
3. return FAILED(detail="windows_no_powershell_job_object_memory_limit",
                 message="PowerShell exposes no clean Job Object memory limit cmdlet",
                 command=["powershell.exe", "-Command",
                          "(Get-Process -Id <pid>).WorkingSet64"])
```

### `set_io_limits(name, spec)`  -- FAILED

```
1. validate name
2. if config.dry_run -> DRY_RUN with the documented argv
3. return FAILED(detail="windows_no_powershell_io_limit",
                 message="PowerShell exposes no per-process IO bandwidth limit",
                 command=["powershell.exe", "-Command",
                          "Set-IoPriority"])
```

### `attach_process(name, pid)`

```
1. validate name; pid > 0
2. if config.dry_run -> DRY_RUN with the planned `Get-Process` argv
3. existing_spec = _read_spec(name)
4. if existing_spec is None -> FAILED("group_not_found")
5. if pid in existing_spec.attached_pids -> SKIPPED
6. updated_spec = replace(existing_spec, attached_pids=existing_spec.attached_pids + [pid])
7. _write_spec(updated_spec) via Set-Content
8. apply priority class:
       powershell -Command "(Get-Process -Id <pid>).PriorityClass = '<class>'"
9. SUCCESS / FAILED
```

### `detach_process(name, pid)`

```
1. validate name; pid > 0
2. if config.dry_run -> DRY_RUN
3. existing_spec = _read_spec(name)
4. if existing_spec is None -> FAILED("group_not_found")
5. if pid not in existing_spec.attached_pids -> SKIPPED
6. updated_spec = replace(existing_spec, attached_pids=[p for p in pids if p != pid])
7. _write_spec(updated_spec)
8. reset priority class:
       powershell -Command "(Get-Process -Id <pid>).PriorityClass = 'Normal'"
9. SUCCESS / FAILED
```

### `list_groups()`

```
1. powershell -Command "Get-ChildItem -LiteralPath '<group_dir>' -Filter '*.json' | Select-Object -ExpandProperty Name | ConvertTo-Json -Compress"
2. parse JSON -> list of file names
3. for each name -> _read_spec(stem) -> assemble WindowsResourceGroupInfo
4. return list
```

### `get_group_usage(name)`

```
1. existing_spec = _read_spec(name)
2. if existing_spec is None -> raise WindowsResourceControlQueryError(detail="unknown_group")
3. for pid in existing_spec.attached_pids:
       powershell -Command "Get-Process -Id <pid> | Select-Object WorkingSet64,CPU | ConvertTo-Json -Compress"
4. parse JSON ; sum WorkingSet64 ; sum CPU
5. return WindowsResourceGroupUsage(
       cpu_usage_seconds=<sum_cpu>,
       memory_used_bytes=<sum_ws>,
       io_read_bytes=None,
       io_write_bytes=None,
       process_count=<len>,
   )
```

---

## Idempotency

| Verb              | Probe                                            | SKIPPED When                                       |
|-------------------|--------------------------------------------------|----------------------------------------------------|
| `create_group`    | `Get-Content <spec_path>`                        | byte-equal JSON spec                                |
| `delete_group`    | `Test-Path <spec_path>`                          | spec file absent                                    |
| `set_cpu_limit`   | `Get-Content <spec_path>` -> spec.cpu_percent    | priority class derived from existing percent matches|
| `set_memory_limit`| n/a — always FAILED                              | (never; Windows PowerShell limitation)              |
| `set_io_limits`   | n/a — always FAILED                              | (never; Windows PowerShell limitation)              |
| `attach_process`  | `Get-Content <spec_path>` -> spec.attached_pids  | PID already in list                                 |
| `detach_process`  | `Get-Content <spec_path>` -> spec.attached_pids  | PID NOT in list                                     |

Every probe is dispatched with `elevated=False`.

---

## Elevation

| Method group | `elevated` value |
|--------------|------------------|
| writes (`create_group`, `delete_group`, `set_cpu_limit`, `attach_process`, `detach_process`) | `config.elevated` |
| reads (`list_groups`, `get_group_usage`)                                                      | always `False`    |
| internal probes (`Get-Content`, `Test-Path`, `Get-ChildItem`)                                 | always `False`    |

---

## PowerShell Argv Construction

Every call shape is:

```python
[
    config.powershell_executable,
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "<scriptblock>",
]
```

Single-quoted PowerShell strings escape embedded single
quotes by doubling them (`''`).  A small helper
`_ps_escape_single_quoted` performs the substitution.

---

## Error Reporting

Every write verb funnels failures through the same surface:

```python
WindowsResourceControlActionResult(
    status=WindowsResourceControlActionStatus.FAILED,
    detail=name_or_windows_token,
    exit_code=run_result.exit_code,
    stdout=run_result.stdout,
    stderr=run_result.stderr,
    command=list(failing_argv),
    group_name=name,
    pid=pid_or_none,
    message="<short_reason_token>",
)
```

The `message` field carries one of:

- `"spec_write_failed"` — `Set-Content` returned non-zero.
- `"spec_delete_failed"` — `Remove-Item` returned non-zero.
- `"priority_class_apply_failed"` — `Get-Process | PriorityClass = ...` returned non-zero.
- `"group_not_found"` — pre-flight detected the spec was missing.
- `"PowerShell exposes no clean Job Object memory limit cmdlet"` — set_memory_limit gap.
- `"PowerShell exposes no per-process IO bandwidth limit"` — set_io_limits gap.

Plus the typed `detail` tokens:

- `"windows_no_powershell_job_object_memory_limit"`
- `"windows_no_powershell_io_limit"`

---

## Internal helpers

```python
def _validate_name(name: str) -> None
def _validate_pid(pid: int) -> None
def _spec_path(name: str) -> str
def _percent_to_priority_class(percent: Optional[int]) -> str
def _ps_command_argv(scriptblock: str) -> list[str]
def _ps_escape_single_quoted(value: str) -> str

def _read_spec(name: str) -> Optional[WindowsResourceGroupSpec]
def _write_spec(spec: WindowsResourceGroupSpec) -> WindowsResourceControlActionResult
def _spec_exists(name: str) -> bool

def _render_spec_json(spec: WindowsResourceGroupSpec) -> str
def _parse_spec_json(text: str) -> Optional[WindowsResourceGroupSpec]
def _parse_get_process_output(text: str) -> tuple[Optional[int], Optional[float]]
```

---

## Test plan

- 14+ pytest functions in
  `source/testroots/testbase/automojo/tests/sysman/windows/test_resourcecontrolmanagement.py`.
- Coverage:

| Test                                                  | Covers                                                  |
|-------------------------------------------------------|---------------------------------------------------------|
| `test_create_group_invokes_set_content`               | `Set-Content` argv shape                                |
| `test_create_group_skipped_when_present`              | byte-equal JSON spec -> SKIPPED                         |
| `test_create_group_dry_run`                           | DRY_RUN short-circuit                                   |
| `test_delete_group_invokes_remove_item`               | `Remove-Item` argv shape                                |
| `test_set_cpu_limit_invokes_priority_class`           | priority class derivation + apply                       |
| `test_set_cpu_limit_skipped_when_match`               | mapped class already matches                            |
| `test_set_memory_limit_returns_failed_with_windows_detail` | typed FAILED with documented detail                |
| `test_set_io_limits_returns_failed_with_windows_detail`  | typed FAILED with documented detail                  |
| `test_attach_process_invokes_priority_class`          | spec append + PriorityClass apply                       |
| `test_attach_process_skipped_when_present`            | PID already attached                                    |
| `test_detach_process_invokes_priority_class`          | spec drop + PriorityClass reset                         |
| `test_list_groups_returns_list`                       | enumerate spec files                                    |
| `test_get_group_usage_parses_get_process_output`      | aggregate `Get-Process` snapshots                       |
| `test_writes_elevate`                                 | `elevated=True` reaches the runner for writes           |
| `test_reads_never_elevate`                            | reads always run with `elevated=False`                  |
| `test_failed_command_surfaces_stderr`                 | non-zero exit -> FAILED with stderr captured            |

---

## Out of Scope

- Native Job Object support via P/Invoke.
- Windows System Resource Manager (WSRM) policy authoring.
- WMI-based memory limit (`Win32_Process.SetInformation`)
  attempts — unreliable and gated behind privilege
  requirements that change per Windows SKU.
- `Set-ProcessMitigation` for Exploit Protection — orthogonal
  to the resource-control contract.
- Per-volume I/O priority via `setiopolicy_np` — the contract
  speaks of bandwidth, not priority.

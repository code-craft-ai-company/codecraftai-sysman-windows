# Design

## Overview

The kdump-management feature on Windows is implemented as a single
module, `automojo.sysman.windows.kdumpmanagement`, that exposes a
`WindowsKdumpManager` class. The coordinator instantiates the class
with a command runner and calls its methods to manage the
`HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl` registry key
and the on-disk dump artifacts.

The Windows realisation honours the cross-OS kdump-management
contract but maps the verbs onto Windows' fundamentally different
native model: a kernel-resident dump engine that writes a single
target file at boot-time-configured paths.

All system command execution is delegated to PowerShell cmdlets
dispatched through an `ICommandRunner`. This module does NOT consume
an `IFileOperator`; every read and write flows through PowerShell
because Windows registry values cannot be safely round-tripped
through generic `read_text` / `write_text` operations and because
filesystem reads benefit from `Get-ChildItem`'s native JSON
projection.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely (via SSH / WinRM-backed runners)
because the manager never assumes a particular transport.

## Cross-OS Concept Mapping

The kdump-management contract is **Linux-primary**. Windows' native
model is fundamentally different from Linux's kdump:

| Cross-OS verb            | Windows native primitive                              | Native concept                  | Reduces to FAILED on Windows? |
|--------------------------|-------------------------------------------------------|---------------------------------|-------------------------------|
| `enable_kdump`           | `Set-ItemProperty CrashDumpEnabled -Type DWord`       | CrashControl registry key       | NO                            |
| `disable_kdump`          | `Set-ItemProperty CrashDumpEnabled 0 -Type DWord`     | CrashControl registry key       | NO                            |
| `configure_kdump_target` (LOCAL_PATH) | `Set-ItemProperty DumpFile -Type ExpandString`        | CrashControl registry key       | NO                            |
| `configure_kdump_target` (SSH/NFS)    | (none)                                                | (none)                          | YES                           |
| `get_kdump_status`       | `Get-ItemProperty CrashControl ` + JSON parse         | CrashControl registry key       | NO                            |
| `list_recent_dumps`      | `Get-ChildItem Minidump\*.dmp,MEMORY.DMP` + JSON      | Minidump folder + Memory.DMP    | NO                            |
| `clear_old_dumps`        | `Get-ChildItem ... | Where ... | Remove-Item`         | Minidump folder + Memory.DMP    | NO                            |
| `get_dump_info`          | `Get-Item ` + JSON                                    | filesystem metadata only        | NO                            |

Comparison with sister implementations:

| OS      | Configuration store              | Service unit               | Reboot required for target change? | Pre-reserved memory? |
|---------|----------------------------------|----------------------------|------------------------------------|----------------------|
| RHEL    | `/etc/kdump.conf`                | `kdump.service`            | No                                 | YES (`crashkernel=`) |
| Ubuntu  | `/etc/default/kdump-tools`       | `kdump-tools.service`      | No                                 | YES (`crashkernel=`) |
| macOS   | (none -- always-on)              | (none)                     | N/A                                | NO                   |
| Windows | `HKLM:\...\CrashControl`         | (none -- kernel-resident)  | **YES**                            | NO (uses pagefile)   |

### Why CrashControl Writes Require a Reboot

The Windows kernel reads CrashControl values during the boot path,
when the kernel pagefile is being prepared as the crashdump
write-target. Changing `CrashDumpEnabled` or `DumpFile` while the
kernel is running has no immediate effect; the new values will be
honoured only on the next boot. Every successful CrashControl write
therefore returns `requires_reboot=True`, even though the registry
mutation itself completed without rebooting.

`clear_old_dumps` does NOT touch the registry, so it always returns
`requires_reboot=False`.

### Why SSH and NFS Targets Are Unsupported

The Windows kernel-dump engine writes a single file via the kernel's
pagefile fast-IO path. It does not have a network stack available at
crash time; it cannot stream a dump over SSH or NFS. Operators who
want a remote artifact typically copy the dump file post-boot via a
separate tool. This manager rejects `SSH` and `NFS` target kinds
with stderr token `unsupported_target_kind` so that compliance
reporting honestly reflects the Windows model.

## Architecture

```
+-------------------------------------------------------------+
|              Maintenance Coordinator                        |
|  - Instantiates LocalCommandRunner / SSHCommandRunner       |
|  - Configures aspects (optional)                            |
|  - Injects runner into WindowsKdumpManager                  |
+----------------------+--------------------------------------+
                       | instantiates & injects
                       v
+-------------------------------------------------------------+
|            WindowsKdumpManager                              |
|  automojo.sysman.windows.kdumpmanagement                    |
|                                                             |
|  enable_kdump()              disable_kdump()                |
|  configure_kdump_target()    clear_old_dumps()              |
|  get_kdump_status()          list_recent_dumps()            |
|  get_dump_info()                                            |
+----------------------+--------------------------------------+
                       | builds PowerShell argv & dispatches
                       v
+-------------------------------------------------------------+
|            ICommandRunner                                   |
|  run(args, elevated, stdin, ...) -> RunResult               |
+-------------------------------------------------------------+
```

---

## Components

### `WindowsKdumpManager`

**Location:**
`source/packages/automojo/sysman/windows/kdumpmanagement.py`

The primary public class. Constructed with an `ICommandRunner` and a
`WindowsKdumpManagerConfig`.

| Method | Description |
|---|---|
| `enable_kdump()` -> `WindowsKdumpActionResult` | Set CrashDumpEnabled to default_enable_mode. Marks reboot. |
| `disable_kdump()` -> `WindowsKdumpActionResult` | Set CrashDumpEnabled to 0. Marks reboot. |
| `configure_kdump_target(spec)` -> `WindowsKdumpActionResult` | Set DumpFile (LOCAL_PATH only); SSH/NFS -> FAILED. |
| `clear_old_dumps(age_days)` -> `WindowsKdumpActionResult` | Get-ChildItem | Where | Remove-Item pipeline. |
| `get_kdump_status()` -> `WindowsKdumpStatus` | Parsed snapshot of CrashControl. |
| `list_recent_dumps()` -> `list[WindowsKdumpInfo]` | Get-ChildItem on Minidump + MEMORY.DMP. |
| `get_dump_info(dump_id)` -> `WindowsKdumpInfo` | Get-Item on a single dump file. |

### `WindowsKdumpManagerConfig`

A dataclass holding runtime configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dry_run` | `bool` | `False` | Inherited. When `True`, no commands execute. |
| `elevated` | `bool` | `False` | Inherited. When `True`, write verbs elevate. |
| `powershell_executable` | `str` | `powershell.exe` | PATH-resolved PowerShell launcher. |
| `crashcontrol_path` | `str` | `HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl` | Registry path. |
| `minidump_dir` | `str` | `%SystemRoot%\Minidump` | Minidump search root. |
| `memory_dmp` | `str` | `%SystemRoot%\MEMORY.DMP` | Complete-mode dump path. |
| `default_enable_mode` | `int` | `7` | Mode written by `enable_kdump`. |

---

## Data Models

### `WindowsKdumpActionStatus`

Aliased to the canonical `ActionStatus` enum from `featurebase.py`.

### `WindowsKdumpTargetKind`

```python
class WindowsKdumpTargetKind(Enum):
    LOCAL_PATH = "local_path"
    SSH        = "ssh"   # rejected with FAILED
    NFS        = "nfs"   # rejected with FAILED
```

The `SSH` and `NFS` values exist for cross-OS contract conformance;
the manager rejects them with stderr token `unsupported_target_kind`.

### `WindowsKdumpTargetSpec`

```python
@dataclass
class WindowsKdumpTargetSpec:
    target_kind: WindowsKdumpTargetKind
    path: Optional[str] = None
    ssh_user: Optional[str] = None     # reserved
    ssh_host: Optional[str] = None     # reserved
    nfs_export: Optional[str] = None   # reserved
    compression: Optional[str] = None  # reserved
```

### `WindowsKdumpActionResult`

```python
@dataclass
class WindowsKdumpActionResult(FeatureActionResult):
    target: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None
```

### `WindowsKdumpStatus`

```python
@dataclass
class WindowsKdumpStatus:
    enabled: bool
    running: bool
    target_kind: Optional[WindowsKdumpTargetKind] = None
    target_path: Optional[str] = None
    kernel: Optional[str] = None              # always None
    reserved_memory_mb: Optional[int] = None  # always None
    message: Optional[str] = None
```

### `WindowsKdumpInfo`

```python
@dataclass
class WindowsKdumpInfo:
    id: str
    path: str
    created: str
    size_bytes: Optional[int] = None
    kernel: Optional[str] = None         # always None
    panic_message: Optional[str] = None  # always None
```

### `WindowsKdumpQueryError`

Raised by `list_recent_dumps()` and `get_dump_info()` when a read
fails for a reason other than the target being absent. Carries
`command`, `exit_code`, and `stderr`.

---

## CrashDumpEnabled Mode Reference

| Value | Mode                  | Comment                                                                |
|-------|-----------------------|------------------------------------------------------------------------|
| `0`   | Disabled              | No crash dump captured.                                                |
| `1`   | Complete Memory Dump  | Full physical memory dumped. Largest output.                           |
| `2`   | Kernel Memory Dump    | Kernel-mode memory only.                                               |
| `3`   | Small Memory Dump     | Minidump only (`%SystemRoot%\Minidump`).                               |
| `7`   | Automatic Memory Dump | Modern default; behaves like Kernel but auto-sizes the pagefile.       |

`enable_kdump` writes `default_enable_mode` (default `7`); operators
who need a different mode can override the config.

---

## Idempotency

| Verb | Skip when ... |
|------|---------------|
| `enable_kdump` | CrashDumpEnabled already equals `default_enable_mode` |
| `disable_kdump` | CrashDumpEnabled already equals `0` |
| `configure_kdump_target` | DumpFile already equals `spec.path` |
| `clear_old_dumps` | never (the pipeline is itself idempotent) |

The skip check uses `Get-ItemProperty` to read the CrashControl key.
The probe never elevates.

---

## Correctness Properties

### Property 1: Enable Idempotency

For any host whose CrashControl registry value `CrashDumpEnabled`
already equals `default_enable_mode`, `enable_kdump()` returns
`SKIPPED` and the runner is NOT invoked for the
`Set-ItemProperty` write.

**Validates: Requirements 1.2**

### Property 2: Disable Idempotency

For any host whose `CrashDumpEnabled` already equals `0`,
`disable_kdump()` returns `SKIPPED` and the runner is NOT invoked
for the `Set-ItemProperty` write.

**Validates: Requirements 2.2**

### Property 3: Configure-Target Idempotency

For any spec whose `path` matches the current `DumpFile` registry
value, `configure_kdump_target` returns `SKIPPED` and no
`Set-ItemProperty` write occurs.

**Validates: Requirements 3.5**

### Property 4: Reboot Advisory On Successful Writes

For any successful `enable_kdump`, `disable_kdump`, or
`configure_kdump_target` invocation, the result has
`requires_reboot=True`.

**Validates: Requirements 8.1**

### Property 5: Reboot Advisory On Skipped Writes Is False

For any `SKIPPED` result from `enable_kdump`, `disable_kdump`, or
`configure_kdump_target`, the result has `requires_reboot=False`.

**Validates: Requirements 8.3**

### Property 6: SSH/NFS Rejection

For any `configure_kdump_target` call with
`target_kind=SSH` or `target_kind=NFS`, the result has status
`FAILED` with `stderr="unsupported_target_kind"` and the runner is
NOT invoked.

**Validates: Requirements 3.2, 3.3**

### Property 7: Dry-Run Safety

For any mutating verb invoked with `config.dry_run=True`, the runner
is NOT invoked for the mutating step, and the returned result has
status `DRY_RUN` with the `command` field populated. CrashControl
writes also carry `requires_reboot=True`.

**Validates: Requirements 1.4, 2.4, 6.4, 8.2, 10.x**

### Property 8: Elevation Consistency

For any write verb invoked with `config.elevated=True`, every
mutating runner call is invoked with `elevated=True`. For any read
verb or idempotency probe, every runner call is invoked with
`elevated=False`, regardless of the config.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 9: Failed-Command Surface

For any write verb whose mutating step returns a non-zero exit code,
the result has status `FAILED`, with `exit_code`, `stderr`, and
`command` populated.

**Validates: Requirements 1.5**

### Property 10: List Returns Empty On No-Match

For any host whose Minidump directory is empty and whose Memory.DMP
does not exist, `list_recent_dumps()` returns an empty list rather
than raising.

**Validates: Requirements 5.4**

### Property 11: Status Tolerates Missing Key

For any host whose CrashControl key is absent or whose
`Get-ItemProperty` invocation fails, `get_kdump_status()` returns a
status with `enabled=False`, `running=False`, and `target_*=None`,
rather than raising.

**Validates: Requirements 4.4**

### Property 12: Age Validation

For any `clear_old_dumps(age_days=N)` with `N < 0`, the runner is
NOT invoked and the result has status `FAILED` with
`message="age_days must be >= 0"`.

**Validates: Requirements 6.3**

---

## Interfaces

### `ICommandRunner`

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        stdin: Optional[str] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult`

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

---

## Command Mappings

| Operation                       | PowerShell pipeline                                                                                |
|---------------------------------|----------------------------------------------------------------------------------------------------|
| Read CrashControl               | `Get-ItemProperty -Path '<crashcontrol>' \| ConvertTo-Json -Depth 5 -Compress`                     |
| Set CrashDumpEnabled            | `Set-ItemProperty -Path '<crashcontrol>' -Name 'CrashDumpEnabled' -Value <int> -Type DWord`        |
| Set DumpFile                    | `Set-ItemProperty -Path '<crashcontrol>' -Name 'DumpFile' -Value '<path>' -Type ExpandString`      |
| List dumps                      | `Get-ChildItem -Path '<mini>','<memory>' -File \| Sort-Object LastWriteTime -Descending \| Select` |
| Clear old dumps                 | `Get-ChildItem ... \| Where-Object { $_.LastWriteTime -lt $cutoff } \| Remove-Item -Force`         |
| Get one dump                    | `Get-Item -LiteralPath '<path>' \| Select-Object Name,FullName,LastWriteTime,Length \| ConvertTo-Json` |

All scripts are wrapped in `powershell.exe -NoProfile
-NonInteractive -Command "<script>"` argv tuples.

---

## Error Handling

### Typed Exceptions

| Exception | Raised when |
|-----------|-------------|
| `WindowsKdumpQueryError` | A read fails for a reason other than the target being absent. |

### Tolerant Status Read

`get_kdump_status()` deliberately does NOT raise on read failure: a
host whose CrashControl key has been removed (e.g. by a misconfigured
Group Policy) should still produce a status object whose `enabled`
field is `False`. The exception path is reserved for catastrophic
underlying failures of the runner itself.

### PowerShell JSON Single-Element Quirk

`ConvertTo-Json` collapses a single-element list into a single
object, not a 1-element list. The `_parse_listing_json` helper
normalises both shapes into a list to keep call sites simple.

---

## Module Layout

```
source/packages/automojo/sysman/windows/
+-- __init__.py
+-- kdumpmanagement.py            <- WindowsKdumpManager + models

source/testroots/testbase/
+-- automojo/tests/sysman/windows/
    +-- test_kdumpmanagement.py   <- unit tests using a MockCommandRunner
```

---

## Testing Strategy

### Unit Tests

Every test uses a `_MockCommandRunner` that records every `run` call
and returns pre-seeded `RunResult` values keyed by argv tuple. Tests
verify:

* the exact argv passed to the runner (including the embedded
  PowerShell script body),
* the elevation flag on every call,
* the result `status`, `command`, `exit_code`, `stderr`,
  `requires_reboot`, and `message` fields,
* the `unsupported_target_kind` rejection for SSH / NFS targets.

### Test Coverage

Required tests:

* `test_enable_kdump_invokes_set_itemproperty`
* `test_enable_kdump_skipped_when_enabled`
* `test_enable_kdump_dry_run_short_circuits`
* `test_disable_kdump_invokes_set_itemproperty`
* `test_configure_kdump_target_invokes_set_itemproperty`
* `test_configure_kdump_target_rejects_ssh`
* `test_configure_kdump_target_skipped_when_already_set`
* `test_get_kdump_status_parses_get_itemproperty_output`
* `test_get_kdump_status_disabled_when_value_is_zero`
* `test_list_recent_dumps_parses_directory_listing`
* `test_clear_old_dumps_invokes_find_with_mtime`
* `test_get_dump_info_parses_get_item_output`
* `test_writes_elevate`
* `test_reads_never_elevate`
* `test_writes_set_requires_reboot`
* `test_failed_command_surfaces_stderr`
* `test_list_recent_dumps_raises_on_unexpected_failure`

Total: 17 tests.

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.windows.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from automojo.sysman.windows.kdumpmanagement import (
    WindowsKdumpManager, WindowsKdumpManagerConfig,
    WindowsKdumpTargetKind, WindowsKdumpTargetSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsKdumpManagerConfig(elevated=True)
manager = WindowsKdumpManager(runner=runner, config=config)

# Enable Automatic Memory Dump.
enable_result = manager.enable_kdump()
print(f"enable status={enable_result.status} reboot={enable_result.requires_reboot}")

# Re-target to D:\dumps.
spec = WindowsKdumpTargetSpec(
    target_kind=WindowsKdumpTargetKind.LOCAL_PATH,
    path=r"D:\dumps\MEMORY.DMP",
)
target_result = manager.configure_kdump_target(spec=spec)
print(f"target status={target_result.status} reboot={target_result.requires_reboot}")
```

### Dry-Run Mode

```python
config = WindowsKdumpManagerConfig(elevated=True, dry_run=True)
manager = WindowsKdumpManager(runner=runner, config=config)
result = manager.enable_kdump()
assert result.status.value == "dry_run"
assert result.requires_reboot is True
print(result.command)
```

### Status Snapshot

```python
status = manager.get_kdump_status()
print(f"enabled={status.enabled} target_path={status.target_path}")
```

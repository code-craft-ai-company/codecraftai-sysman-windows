# Design

## Concept Mapping (Hyper-V vs KVM/libvirt vs Apple Virtualization)

The cross-OS virtualization-management contract talks about *VMs*,
*disks*, and *snapshots*.  Each OS's native hypervisor stack carries
a different vocabulary; the table below grounds the abstract contract
in concrete Hyper-V / libvirt / Apple-Virtualization terms.

| Contract concept | Windows (this spec) | Ubuntu / macOS-libvirt | Apple Virtualization (out of scope) |
|---|---|---|---|
| Hypervisor host | Hyper-V (Windows feature) | libvirt + KVM (Linux) / libvirt + QEMU (macOS-Homebrew) | macOS `Virtualization.framework` |
| VM | Hyper-V VM | libvirt **domain** | `VZVirtualMachine` (Swift only) |
| VM identifier | `Name` field of `Get-VM` | Domain name | Object reference |
| Power state | `Running` / `Off` / `Paused` / `Saved` (or numeric `2`/`3`/`32768`/`32769`) | `running` / `shut off` / `paused` / `pmsuspended` | `state` enum |
| Memory | `MemoryAssigned` (bytes) | `Max memory` (KiB) | `memorySize` |
| vCPU | `ProcessorCount` | `CPU(s)` | `cpuCount` |
| Generation | `Generation 1` (legacy BIOS) / `Generation 2` (UEFI / Secure Boot) | n/a | n/a |
| Create | `New-VM -Name ... -MemoryStartupBytes ... -Generation <n> [-NewVHDPath ...] [-SwitchName ...]` followed by `Set-VMProcessor -Count <n>` | `virt-install --name ... --memory ... --vcpus ...` | `start()` after configuration |
| Start | `Start-VM` | `virsh start` | `start()` |
| Graceful stop | `Stop-VM` (default) | `virsh shutdown` | `requestStop()` |
| Force stop | `Stop-VM -Force -TurnOff` | `virsh destroy` | `stop()` |
| Pause | `Suspend-VM` | `virsh suspend` | `pause()` |
| Resume | `Resume-VM` | `virsh resume` | `resume()` |
| Delete | `Stop-VM -Force -TurnOff; Remove-VM -Force` | `virsh undefine --remove-all-storage` | n/a |
| Attach disk | `Add-VMHardDiskDrive -VMName ... -Path ... -ControllerType <SCSI\|IDE>` | `virsh attach-disk --persistent` | `addStorageDevice` (config-time) |
| Detach disk | `Remove-VMHardDiskDrive -VMName ... -ControllerType ... -ControllerNumber ... -ControllerLocation ...` | `virsh detach-disk --persistent` | n/a |
| Snapshot create | `Checkpoint-VM -Name ... -SnapshotName ...` | `virsh snapshot-create-as` | n/a |
| Snapshot restore | `Restore-VMSnapshot -VMName ... -Name ... -Confirm:$false` | `virsh snapshot-revert` | n/a |
| Snapshot list | `Get-VMSnapshot -VMName ...` | `virsh snapshot-list` | n/a |
| Required runtime | Hyper-V Windows feature + Hyper-V Administrators group | libvirt + qemu-kvm | macOS 11+; Swift |

Hyper-V differs from libvirt in three structural ways that drive
this spec:

1. **Generation matters.** Hyper-V VMs are either Generation 1
   (legacy BIOS) or Generation 2 (UEFI / Secure Boot).  We expose
   this via `WindowsVmSpec.boot_kind`.
2. **Disk attachment is by controller coordinates.**  Detaching a
   disk requires the `(ControllerType, ControllerNumber,
   ControllerLocation)` tuple, not a guest-side label.  We resolve
   the tuple at detach time by filtering `Get-VMHardDiskDrive` on
   `Path`.
3. **`New-VM` does not accept the vCPU count.**  We follow up with
   `Set-VMProcessor -Count <n>` in the same scriptblock.

Apple's Virtualization framework is **out of scope** for this
spec — its API is Swift-only and binding it from PowerShell does
not apply.  Linux's libvirt is covered by the Ubuntu / RHEL / macOS
spec branches.  WSL2 distribution management is intentionally
disjoint — WSL2 does not satisfy the Hyper-V VM contract even though
it shares the underlying hypervisor.

---

## Overview

The virtualization-management feature is implemented as a single
module,
`ccai.sysman.windows.virtualizationmanagement`, that exposes a
`WindowsVirtualizationManager` class.  The coordinator instantiates
`WindowsVirtualizationManager` and calls its methods to perform
discrete VM-lifecycle, disk, and snapshot actions.

This feature **complements — and does not duplicate** — the storage
manager in `3-storage-management` (which manages Storage-Spaces pools
that may back Hyper-V VHDXs), the firewall manager in
`3-firewall-management` (which opens ports on virtual switches), and
the service manager in `3-service-management` (which brings the
``vmms`` Hyper-V management service up before any verb here is
called).

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The
`WindowsVirtualizationManager` never directly calls `subprocess`,
shell commands, or `open()`; it only constructs PowerShell
scriptblocks and hands them to the injected runner.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over WinRM / SSH (via
`SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsVirtualizationManager         │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        WindowsVirtualizationManager                         │
│  ccai.sysman.windows.virtualizationmanagement           │
│                                                             │
│  list_vms()              get_vm_info()                      │
│  create_vm()             delete_vm()                        │
│  start_vm()              stop_vm()                          │
│  pause_vm()              resume_vm()                        │
│  attach_disk()           detach_disk()                      │
│  snapshot_vm()           restore_snapshot()                 │
│  list_snapshots()                                           │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner (powershell.exe scriptblocks)
          ▼
┌─────────────────────────────────────────────────────────────┐
│          LocalCommandRunner / SSHCommandRunner              │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsVirtualizationManager`

**Location:**
`source/packages/ccai/sysman/windows/virtualizationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_vms` | `() → list[WindowsVmInfo]` | `Get-VM \| Select-Object ... \| ConvertTo-Json` |
| `get_vm_info` | `(name) → WindowsVmInfo \| None` | `Get-VM -Name <name>` parsed |
| `create_vm` | `(spec) → WindowsVirtualizationActionResult` | `New-VM ...; Set-VMProcessor -Count <n>` |
| `delete_vm` | `(name) → WindowsVirtualizationActionResult` | `Stop-VM -Force -TurnOff; Remove-VM -Force` with data-loss advisory |
| `start_vm` | `(name) → WindowsVirtualizationActionResult` | `Start-VM -Name '<name>'` |
| `stop_vm` | `(name, force=False) → WindowsVirtualizationActionResult` | `Stop-VM` (graceful) or `Stop-VM -Force -TurnOff` (force) |
| `pause_vm` | `(name) → WindowsVirtualizationActionResult` | `Suspend-VM -Name '<name>'` |
| `resume_vm` | `(name) → WindowsVirtualizationActionResult` | `Resume-VM -Name '<name>'` |
| `attach_disk` | `(vm, disk_spec) → WindowsVirtualizationActionResult` | `Add-VMHardDiskDrive -VMName ... -Path ... -ControllerType <type>` |
| `detach_disk` | `(vm, disk_id) → WindowsVirtualizationActionResult` | Filter by `Path` then `Remove-VMHardDiskDrive` by controller coordinates |
| `snapshot_vm` | `(vm, snapshot_name) → WindowsVirtualizationActionResult` | `Checkpoint-VM -Name ... -SnapshotName ...` |
| `restore_snapshot` | `(vm, snapshot_name) → WindowsVirtualizationActionResult` | `Restore-VMSnapshot -VMName ... -Name ... -Confirm:$false` (unconditional) |
| `list_snapshots` | `(vm) → list[WindowsSnapshotInfo]` | `Get-VMSnapshot -VMName ...` parsed |

### `WindowsVirtualizationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Mutations short-circuit when `True` |
| `elevated` | `bool` | `False` | Writes pass `elevated=True` when `True` |
| `powershell_executable` | `str` | `"powershell.exe"` | Path to PowerShell |
| `default_switch` | `str \| None` | `None` | Optional default `-SwitchName` for `New-VM` |
| `default_vhd_dir` | `str \| None` | `None` | Optional directory used when only `disk_size_gb` is supplied — VHD path resolves to `<dir>\<vm_name>.vhdx` |

---

## Data Models

```python
class WindowsVirtualizationActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class WindowsVirtualizationActionResult(FeatureActionResult):
    vm_name: str | None = None
    snapshot_name: str | None = None
    message: str | None = None

class WindowsVmState(Enum):
    RUNNING   = "running"
    STOPPED   = "stopped"
    PAUSED    = "paused"
    SUSPENDED = "suspended"     # Hyper-V "Saved" state
    OTHER     = "other"

@dataclass
class WindowsVmSpec:
    name: str
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_gb: int | None = None
    disk_path: str | None = None
    os_variant: str | None = None     # ignored on Hyper-V
    network: str | None = None        # SwitchName override
    iso_path: str | None = None
    graphics: str = "none"            # ignored on Hyper-V
    boot_kind: int = 1                # Generation 1 vs 2

@dataclass
class WindowsVmInfo:
    name: str
    state: WindowsVmState
    cpu_count: int | None = None
    memory_mb: int | None = None
    uptime_seconds: int | None = None
    os_type: str | None = None
    vm_id: str | None = None

@dataclass
class WindowsVmDiskSpec:
    path: str
    target: str | None = None             # ignored on Hyper-V
    controller_type: str | None = None    # "SCSI" / "IDE"
    bus: str | None = None                # ignored on Hyper-V

@dataclass
class WindowsSnapshotInfo:
    name: str
    created: str
    description: str | None = None
    parent: str | None = None
    vm_state: WindowsVmState | None = None

class WindowsVirtualizationQueryError(Exception): ...
```

---

## Command Construction

### PowerShell argv prefix

```python
def _build_powershell_argv(self, *, script: str) -> list:
    return [
        self._typed_config().powershell_executable,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
```

### `create_vm` scriptblock

```
New-VM -Name '<name>' -MemoryStartupBytes <bytes> -Generation <1|2> \
    [-NewVHDPath '<path>' [-NewVHDSizeBytes <bytes>]] \
    [-SwitchName '<switch>']; \
Set-VMProcessor -VMName '<name>' -Count <cpu_count>
```

`Set-VMProcessor` is mandatory because `New-VM` does not accept a
processor count directly.

### Disk path resolution

| `disk_path` | `disk_size_gb` | `default_vhd_dir` | Effect |
|---|---|---|---|
| set | any | any | `-NewVHDPath '<disk_path>'` (with optional `-NewVHDSizeBytes`) |
| `None` | set | set | `-NewVHDPath '<dir>\<name>.vhdx' -NewVHDSizeBytes <bytes>` |
| `None` | set | `None` | No VHD args (Hyper-V uses default location) |
| `None` | `None` | any | No VHD args |

### Stop semantics

| `force` | Scriptblock |
|---|---|
| `False` | `Stop-VM -Name '<name>'` |
| `True` | `Stop-VM -Name '<name>' -Force -TurnOff` |

### Detach disk scriptblock

```powershell
$drive = Get-VMHardDiskDrive -VMName '<vm>' `
    | Where-Object { $_.Path -eq '<disk_id>' } `
    | Select-Object -First 1
if ($null -eq $drive) {
    throw 'no matching drive'
} else {
    Remove-VMHardDiskDrive -VMName '<vm>' `
        -ControllerType $drive.ControllerType `
        -ControllerNumber $drive.ControllerNumber `
        -ControllerLocation $drive.ControllerLocation
}
```

This shape is required because `Remove-VMHardDiskDrive` does not
accept a path argument — the controller coordinates resolved from
`Get-VMHardDiskDrive` are the only addressing scheme.

---

## Idempotency Probes

| Verb | Probe | SKIPPED condition |
|---|---|---|
| `create_vm` | `Get-VM -Name <name>` | VM exists |
| `delete_vm` | `Get-VM -Name <name>` | VM does not exist |
| `start_vm` | `Get-VM -Name <name>` | `state == RUNNING` |
| `stop_vm` | `Get-VM -Name <name>` | `state == STOPPED` |
| `pause_vm` | `Get-VM -Name <name>` | `state == PAUSED` |
| `resume_vm` | `Get-VM -Name <name>` | `state == RUNNING` |
| `attach_disk` | `Get-VMHardDiskDrive -VMName <vm>` | A drive's `Path` matches `disk_spec.path` |
| `detach_disk` | `Get-VMHardDiskDrive -VMName <vm>` | No drive's `Path` matches `disk_id` |
| `snapshot_vm` | `Get-VMSnapshot -VMName <vm>` | A snapshot named `snapshot_name` exists |
| `restore_snapshot` | — (always reverts) | Never |

Probes always pass `elevated=False`.

---

## Dry-Run Behaviour

Dry-run disables every mutating entry point and returns
`DRY_RUN` with the would-have-been argv recorded on the result.
Read verbs and idempotency probes run freely.

---

## Elevation Policy

Every write verb pulls `elevated` from the config.  Hyper-V cmdlets
require either Administrator privilege or `Hyper-V Administrators`
group membership; both situations are represented by
`elevated=True`.  Reads and idempotency probes always pass
`elevated=False`.

---

## State Normalisation

Hyper-V's `State` field is reported as either a string or a numeric
enum value in `ConvertTo-Json` payloads:

| Numeric | String | `WindowsVmState` |
|---|---|---|
| 2 | `Running` | `RUNNING` |
| 3 | `Off` | `STOPPED` |
| 32768 | `Paused` | `PAUSED` |
| 32769 | `Saved` | `SUSPENDED` |
| anything else | anything else | `OTHER` |

`_normalise_state` accepts both shapes.

---

## Correctness Properties

1. **Idempotency.**  Every lifecycle transition returns `SKIPPED`
   when the resource is already in the requested state.  The contract
   exempts `restore_snapshot`.
2. **No raises from writes.**  Failures from PowerShell surface as
   `FAILED` results with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
3. **Typed read errors.**  `list_vms`, `get_vm_info`, and
   `list_snapshots` raise `WindowsVirtualizationQueryError` for
   unexpected failures.  `get_vm_info` returns `None` for the
   well-defined "unknown VM" case.
4. **Detach-by-path.**  Detach resolves the controller coordinates
   inside the scriptblock so callers never have to know the
   `(ControllerType, ControllerNumber, ControllerLocation)` tuple.

---

## Testing Strategy

- `_MockCommandRunner` queues `RunResult` objects keyed by argv tuple.
- Unit tests per public method covering happy / SKIPPED / FAILED /
  DRY_RUN branches.
- JSON-payload parser tests (string-form `State`, numeric-form
  `State`).
- Stop-semantics test asserting `force=True` and `force=False`
  dispatch different scriptblocks.
- Elevation tests asserting writes pass `elevated=True` and reads
  always pass `elevated=False`.
- Detach-disk filter test asserting the script references
  `$drive.ControllerType`, `$drive.ControllerNumber`, and
  `$drive.ControllerLocation`.
- No live Hyper-V is required.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Hyper-V management service (`vmms`) | `3-service-management` | Coordinator brings the service up |
| Virtual switch creation | `3-firewall-management` (or future `3-network-virtualization`) | Coordinator creates the switch; this feature consumes the switch name via `default_switch` / `spec.network` |
| Storage-Spaces VHDX backing | `3-storage-management` | Coordinator creates the VHDX; this feature attaches it |
| WSL2 | n/a | Out of scope; separate feature area |

---

## Out of Scope (this iteration)

- Virtual switch lifecycle (`New-VMSwitch`, `Remove-VMSwitch`).
- Storage-Spaces pool / virtual-disk creation.
- WSL2 distribution management.
- Live migration (`Move-VM`) and Hyper-V Replica.
- VM cloning beyond snapshot-and-restore.
- Hyper-V host configuration (memory weight, NUMA, etc.).
- Apple Virtualization, KVM/libvirt, and cloud-hypervisor surfaces.

These extensions are tracked under `tasks.md` follow-ups.

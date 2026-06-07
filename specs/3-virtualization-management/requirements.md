# Requirements

## Introduction

The virtualization-management feature provides the
`codecraftai-sysman-windows` package with the ability to manage local
Hyper-V virtual machines on Windows hosts via the Hyper-V PowerShell
module.  This spec implements the cross-OS virtualization-management
contract defined at
`codecraftai-sysman-common/specs/contracts/virtualization-management/contract.md`
for Windows.

A maintenance coordinator running in an external package drives every
operation by calling into this feature's public interfaces.  The
feature does not decide *which* VMs should exist, *what shape* their
disks should take, or *when* snapshots should be created — it provides
the capability to manipulate Hyper-V VM state reliably, idempotently,
and with full auditability.  All system commands are executed
exclusively through the `codecraftai-interfaces` command-runner
abstraction (PowerShell scriptblock invocations).

The Hyper-V Windows feature **must be enabled** on the host
(``Enable-WindowsOptionalFeature -Online -FeatureName
Microsoft-Hyper-V``).  WSL2 distribution management is NOT in scope —
WSL2 does not satisfy the Hyper-V VM contract.  Cloud-hypervisor
surfaces (Azure Stack HCI, etc.) are also out of scope.

This feature stops at the Hyper-V VM boundary: VM lifecycle, disk
attach / detach, and snapshot create / revert / list.  Virtual-switch
creation and Storage-Spaces management (the Hyper-V-network and
storage-pool surfaces beyond an attached VHDX) are intentionally
excluded — they live in `firewall-management` (network) or
`storage-management` (Storage Spaces).

---

## Requirements

### Requirement 1: Hyper-V Role Requirement

**User Story:** As a maintenance coordinator, I want clear semantics
when the Hyper-V role is not present, so that the feature fails fast
instead of producing confusing PowerShell errors.

#### Acceptance Criteria

1. THE SYSTEM SHALL document that the Hyper-V Windows feature must be
   enabled and the calling user must be a member of the
   ``Hyper-V Administrators`` group OR the manager must run with
   ``elevated=True`` (Administrator token).
2. WHEN ``Get-VM`` (or any other Hyper-V cmdlet) reports a non-zero
   exit because the role is missing THE SYSTEM SHALL surface the
   PowerShell stderr verbatim through the result's ``stderr`` field.

---

### Requirement 2: List VMs

**User Story:** As a maintenance coordinator, I want to enumerate
every VM on the Hyper-V host with its power state, vCPU count, and
memory.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_vms()` THE SYSTEM SHALL dispatch
   the scriptblock
   ``Get-VM | Select-Object Name, State, ProcessorCount,
   MemoryAssigned, Uptime, OperatingSystem, Id | ConvertTo-Json
   -Compress -Depth 5`` through the runner with `elevated=False`.
2. THE SYSTEM SHALL parse the JSON payload into a list of
   `WindowsVmInfo` rows.  Bare-object payloads are normalised to a
   single-element list.
3. THE SYSTEM SHALL map the Hyper-V `State` field onto
   `WindowsVmState` (string-form: `Running`, `Off`, `Paused`, `Saved`;
   numeric-form: `2`, `3`, `32768`, `32769`).  Unrecognised values
   become `OTHER`.
4. THE SYSTEM SHALL convert `MemoryAssigned` from bytes to MiB.
5. THE SYSTEM SHALL convert `Uptime.TotalSeconds` to an integer
   `uptime_seconds`.
6. WHEN `Get-VM` exits non-zero THE SYSTEM SHALL raise
   `WindowsVirtualizationQueryError`.

---

### Requirement 3: Get VM Info

**User Story:** As a maintenance coordinator, I want to look up a
single VM by name.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_vm_info(name)` THE SYSTEM SHALL
   dispatch ``Get-VM -Name <name> | Select-Object ... |
   ConvertTo-Json -Compress -Depth 5`` with `elevated=False`.
2. WHEN the VM is not defined THE SYSTEM SHALL return `None` (the
   stderr ``Hyper-V was unable to find a virtual machine`` /
   ``ObjectNotFound`` is recognised heuristically).
3. WHEN the cmdlet fails for any other reason THE SYSTEM SHALL raise
   `WindowsVirtualizationQueryError`.

---

### Requirement 4: Create VM

**User Story:** As a maintenance coordinator, I want to provision a
fresh Hyper-V VM via `New-VM`, so that bringing a guest online is a
single call.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_vm(spec)` THE SYSTEM SHALL
   render a multi-step PowerShell scriptblock that:
   - Runs `New-VM -Name '<name>' -MemoryStartupBytes <bytes>
     -Generation <1|2>` plus the appropriate VHD / switch arguments.
   - Runs `Set-VMProcessor -VMName '<name>' -Count <n>` to apply the
     vCPU count.
2. WHEN `spec.disk_path` is set THE SYSTEM SHALL pass
   `-NewVHDPath '<disk_path>'` to `New-VM`; when `disk_size_gb` is
   also set, THE SYSTEM SHALL also pass `-NewVHDSizeBytes <bytes>`.
3. WHEN `spec.disk_path` is `None` AND `spec.disk_size_gb` is set
   AND `config.default_vhd_dir` is set THE SYSTEM SHALL compose
   `<default_vhd_dir>\<name>.vhdx` and pass it as `-NewVHDPath` along
   with `-NewVHDSizeBytes`.
4. WHEN `spec.network` is set THE SYSTEM SHALL pass
   `-SwitchName '<network>'`; otherwise WHEN `config.default_switch`
   is set THE SYSTEM SHALL pass it.
5. WHEN `spec.boot_kind` is 1 or 2 THE SYSTEM SHALL pass
   `-Generation <kind>`; other values fall back to 1.
6. WHEN a VM with `spec.name` already exists THE SYSTEM SHALL return
   `SKIPPED` without dispatching.
7. WHEN `dry_run=True` THE SYSTEM SHALL return `DRY_RUN` carrying the
   would-have-been argv.

---

### Requirement 5: Delete VM

**User Story:** As a maintenance coordinator, I want to force-stop and
remove a VM in a single call.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_vm(name)` THE SYSTEM SHALL
   dispatch a scriptblock that runs
   `Stop-VM -Name '<name>' -Force -TurnOff -ErrorAction
   SilentlyContinue` followed by `Remove-VM -Name '<name>' -Force`.
2. WHEN the VM is not defined THE SYSTEM SHALL return `SKIPPED`.
3. WHEN the operation succeeds THE SYSTEM SHALL populate
   `WindowsVirtualizationActionResult.message` with a data-loss
   advisory noting that the VM definition was destroyed and that
   backing VHDs were NOT removed.

---

### Requirement 6: Power-State Verbs

**User Story:** As a maintenance coordinator, I want to start, stop,
pause, and resume VMs.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_vm(name)` THE SYSTEM SHALL
   dispatch `Start-VM -Name '<name>'`; SKIPPED when already
   `RUNNING`.
2. WHEN the coordinator calls `stop_vm(name, force=False)` THE SYSTEM
   SHALL dispatch `Stop-VM -Name '<name>'` (graceful via integration
   services); SKIPPED when already `STOPPED`.
3. WHEN the coordinator calls `stop_vm(name, force=True)` THE SYSTEM
   SHALL dispatch `Stop-VM -Name '<name>' -Force -TurnOff` (hard
   power-off); SKIPPED when already `STOPPED`.
4. WHEN the coordinator calls `pause_vm(name)` THE SYSTEM SHALL
   dispatch `Suspend-VM -Name '<name>'`; SKIPPED when already
   `PAUSED`.
5. WHEN the coordinator calls `resume_vm(name)` THE SYSTEM SHALL
   dispatch `Resume-VM -Name '<name>'`; SKIPPED when already
   `RUNNING`.
6. WHEN `dry_run=True` for any of these verbs THE SYSTEM SHALL return
   `DRY_RUN` and not invoke the runner.

---

### Requirement 7: Attach Disk

**User Story:** As a maintenance coordinator, I want to attach an
existing VHDX to a VM.

#### Acceptance Criteria

1. WHEN the coordinator calls `attach_disk(vm, disk_spec)` THE SYSTEM
   SHALL dispatch
   `Add-VMHardDiskDrive -VMName '<vm>' -Path '<disk_spec.path>'
   -ControllerType <type>` (controller_type defaults to ``"SCSI"``).
2. WHEN ``Get-VMHardDiskDrive -VMName <vm>`` already reports a drive
   whose `Path` matches `disk_spec.path` (case-insensitive) THE
   SYSTEM SHALL return `SKIPPED`.

---

### Requirement 8: Detach Disk

**User Story:** As a maintenance coordinator, I want to detach a VHDX
from a VM.

#### Acceptance Criteria

1. WHEN the coordinator calls `detach_disk(vm, disk_id)` THE SYSTEM
   SHALL dispatch a scriptblock that:
   - Filters `Get-VMHardDiskDrive -VMName '<vm>'` to records whose
     `Path` matches `disk_id`.
   - Picks the first match and runs
     `Remove-VMHardDiskDrive -VMName '<vm>' -ControllerType
     $drive.ControllerType -ControllerNumber $drive.ControllerNumber
     -ControllerLocation $drive.ControllerLocation`.
2. WHEN no drive whose `Path` matches `disk_id` is present THE SYSTEM
   SHALL return `SKIPPED`.
3. WHEN the operation succeeds THE SYSTEM SHALL populate
   `WindowsVirtualizationActionResult.message` with a data-loss
   advisory noting that the backing VHDX itself was NOT removed.

---

### Requirement 9: Snapshot Lifecycle

**User Story:** As a maintenance coordinator, I want to create, list,
and revert checkpoints on a VM.

#### Acceptance Criteria

1. WHEN the coordinator calls `snapshot_vm(vm, snapshot_name)` THE
   SYSTEM SHALL dispatch
   `Checkpoint-VM -Name '<vm>' -SnapshotName '<snapshot_name>'`.
2. WHEN a snapshot with that name already exists for `vm` (probed via
   `Get-VMSnapshot`) THE SYSTEM SHALL return `SKIPPED`.
3. WHEN the coordinator calls `restore_snapshot(vm, snapshot_name)`
   THE SYSTEM SHALL dispatch
   `Restore-VMSnapshot -VMName '<vm>' -Name '<snapshot_name>'
   -Confirm:$false` UNCONDITIONALLY and SHALL populate `message` with
   a data-loss advisory on `SUCCESS`.
4. WHEN the coordinator calls `list_snapshots(vm)` THE SYSTEM SHALL
   dispatch `Get-VMSnapshot -VMName '<vm>' | Select-Object Name,
   CreationTime, ParentSnapshotName | ConvertTo-Json -Compress
   -Depth 5` and parse the result.

---

### Requirement 10: Elevation Policy

**User Story:** As a maintenance coordinator, I want every mutating
Hyper-V operation to run with the Administrator token, so that the
Hyper-V cmdlets succeed on a standard user session.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write verb.
2. WHEN performing read verbs and idempotency probes THE SYSTEM SHALL
   NEVER pass `elevated=True`.
3. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL NEVER pass `elevated=True`.

---

### Requirement 11: Dry-Run, Failure Reporting, and Result Shape

**User Story:** As a maintenance coordinator, I want every mutating
operation to respect dry-run, surface failures structurally, and carry
the targeted VM / snapshot name in the result.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command and SHALL return
   `WindowsVirtualizationActionResult(status=DRY_RUN)` carrying the
   would-have-been argv.
2. WHEN the runner returns non-zero THE SYSTEM SHALL return
   `WindowsVirtualizationActionResult(status=FAILED, exit_code=<code>,
   stdout=<captured>, stderr=<captured>)`.
3. THE SYSTEM SHALL populate `vm_name` on every result and
   `snapshot_name` on snapshot-related results.
4. THE SYSTEM SHALL store the data-loss advisory in `message` and
   echo it onto `detail` for `delete_vm`, `detach_disk`, and
   `restore_snapshot` `SUCCESS` outcomes.

---

### Requirement 12: Out-of-Scope Surfaces

**User Story:** As a maintenance coordinator, I want a clear list of
the Hyper-V surfaces this feature does NOT cover, so that callers
know where to look for missing capabilities.

#### Acceptance Criteria

1. THE SYSTEM SHALL document that virtual switch creation /
   destruction (`New-VMSwitch`, `Remove-VMSwitch`) is out of scope and
   lives in `firewall-management`.
2. THE SYSTEM SHALL document that Storage-Spaces pool / virtual-disk
   creation lives in `storage-management`.
3. THE SYSTEM SHALL document that WSL2 distribution management is a
   separate surface and not covered by this contract.
4. THE SYSTEM SHALL document that live migration between hosts and
   replica configuration are out of scope.

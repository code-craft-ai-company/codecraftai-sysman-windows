# Requirements

## Introduction

The kernel parameters feature provides the `automojo-sysman-linux-windows` package with the
ability to read and write `sysctl` kernel parameters at runtime, persist them across reboots
via drop-in files in `/etc/sysctl.d/`, load and unload kernel modules, and persist module
load configuration in `/etc/modules-load.d/`.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* parameters or
modules to manage — it provides the capability to *do it* reliably, idempotently, and with
full auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, enabling dry-run simulation, auditing,
and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Read a Kernel Parameter

**User Story:** As a maintenance coordinator, I want to read the current value of a sysctl
kernel parameter, so that I can make conditional decisions about whether a write is
necessary without taking any action.

#### Acceptance Criteria

1. WHEN the coordinator provides a sysctl key THE SYSTEM SHALL invoke
   `sysctl -n <key>` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `KernelParamInfo` record containing
   the key and the current value string.
3. WHEN the key does not exist THE SYSTEM SHALL return `None`.
4. WHEN the command fails for an unexpected reason THE SYSTEM SHALL raise a
   `KernelParamQueryError` with the exit code and captured stderr.

---

### Requirement 2: Write a Kernel Parameter at Runtime

**User Story:** As a maintenance coordinator, I want to set a sysctl kernel parameter value
at runtime, so that kernel behaviour is adjusted immediately without rebooting the system.

#### Acceptance Criteria

1. WHEN the coordinator provides a sysctl key and a value THE SYSTEM SHALL invoke
   `sysctl -w <key>=<value>` via the command runner.
2. WHEN the current value already matches the desired value THE SYSTEM SHALL skip execution
   and return a `KernelParamActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `KernelParamActionResult` with
   status `SUCCESS` and the applied value.
4. WHEN the command fails THE SYSTEM SHALL return a `KernelParamActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `KernelParamActionResult` with status `DRY_RUN` showing the key, current value, and
   desired value.

---

### Requirement 3: Persist a Kernel Parameter Across Reboots

**User Story:** As a maintenance coordinator, I want to persist a sysctl kernel parameter
setting in a drop-in configuration file under `/etc/sysctl.d/`, so that the value is
applied automatically at every boot.

#### Acceptance Criteria

1. WHEN the coordinator provides a sysctl key, value, and a drop-in file name THE SYSTEM
   SHALL write or update the `<key> = <value>` line in
   `/etc/sysctl.d/<filename>.conf` via the command runner.
2. WHEN the key already exists in the file with the same value THE SYSTEM SHALL skip the
   write and return a `KernelParamActionResult` with status `SKIPPED`.
3. WHEN the key already exists in the file with a different value THE SYSTEM SHALL update
   the line and return status `SUCCESS`.
4. WHEN the key does not exist in the file THE SYSTEM SHALL append it and return status
   `SUCCESS`.
5. WHEN the write fails THE SYSTEM SHALL return a `KernelParamActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `KernelParamActionResult` with status `DRY_RUN` with the diff of what would change.

---

### Requirement 4: Remove a Persisted Kernel Parameter

**User Story:** As a maintenance coordinator, I want to remove a sysctl key from a drop-in
configuration file, so that a previously persisted parameter is no longer applied at boot.

#### Acceptance Criteria

1. WHEN the coordinator provides a sysctl key and a drop-in file name THE SYSTEM SHALL
   remove the matching `<key> = <value>` line from `/etc/sysctl.d/<filename>.conf` via
   the command runner.
2. WHEN the key does not exist in the file THE SYSTEM SHALL skip the write and return a
   `KernelParamActionResult` with status `SKIPPED`.
3. WHEN the key is removed successfully THE SYSTEM SHALL return a
   `KernelParamActionResult` with status `SUCCESS`.
4. WHEN the drop-in file does not exist THE SYSTEM SHALL return a
   `KernelParamActionResult` with status `SKIPPED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `KernelParamActionResult` with status `DRY_RUN`.

---

### Requirement 5: Apply All Persisted Kernel Parameters

**User Story:** As a maintenance coordinator, I want to apply all persisted sysctl settings
from `/etc/sysctl.d/` to the running kernel, so that newly written drop-in files take
effect without requiring a reboot.

#### Acceptance Criteria

1. WHEN the coordinator requests application of all persisted parameters THE SYSTEM SHALL
   invoke `sysctl --system` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `KernelParamActionResult` with
   status `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `KernelParamActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `KernelParamActionResult` with status `DRY_RUN`.

---

### Requirement 6: Query All Current Kernel Parameters

**User Story:** As a maintenance coordinator, I want to retrieve all current sysctl kernel
parameter values, so that I can audit the running kernel configuration and identify
parameters that differ from the desired state.

#### Acceptance Criteria

1. WHEN the coordinator requests all kernel parameters THE SYSTEM SHALL invoke `sysctl -a`
   via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a list of `KernelParamInfo` records
   parsed from the output.
3. WHEN the command fails THE SYSTEM SHALL raise a `KernelParamQueryError` with the exit
   code and captured stderr.

---

### Requirement 7: Load a Kernel Module

**User Story:** As a maintenance coordinator, I want to load a kernel module into the
running kernel, so that hardware support or kernel functionality provided by that module
is available immediately.

#### Acceptance Criteria

1. WHEN the coordinator provides a module name THE SYSTEM SHALL invoke
   `modprobe <module>` via the command runner.
2. WHEN the coordinator provides module parameters THE SYSTEM SHALL append them to the
   `modprobe` command as `<param>=<value>` pairs.
3. WHEN the module is already loaded THE SYSTEM SHALL skip execution and return a
   `KernelModuleActionResult` with status `SKIPPED`.
4. WHEN the modprobe command succeeds THE SYSTEM SHALL return a
   `KernelModuleActionResult` with status `SUCCESS`.
5. WHEN the modprobe command fails THE SYSTEM SHALL return a `KernelModuleActionResult`
   with status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `KernelModuleActionResult` with status `DRY_RUN`.

---

### Requirement 8: Unload a Kernel Module

**User Story:** As a maintenance coordinator, I want to unload a kernel module from the
running kernel, so that a module can be replaced, reconfigured, or removed during
maintenance.

#### Acceptance Criteria

1. WHEN the coordinator provides a module name THE SYSTEM SHALL invoke
   `modprobe -r <module>` via the command runner.
2. WHEN the module is not currently loaded THE SYSTEM SHALL skip execution and return a
   `KernelModuleActionResult` with status `SKIPPED`.
3. WHEN the modprobe -r command succeeds THE SYSTEM SHALL return a
   `KernelModuleActionResult` with status `SUCCESS`.
4. WHEN the modprobe -r command fails THE SYSTEM SHALL return a
   `KernelModuleActionResult` with status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `KernelModuleActionResult` with status `DRY_RUN`.

---

### Requirement 9: Query Loaded Kernel Modules

**User Story:** As a maintenance coordinator, I want to retrieve the list of currently
loaded kernel modules, so that I can determine module load state before issuing load or
unload operations.

#### Acceptance Criteria

1. WHEN the coordinator requests the list of loaded modules THE SYSTEM SHALL invoke
   `lsmod` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a list of `KernelModuleInfo` records
   each containing module name, size in bytes, and use count.
3. WHEN the command fails THE SYSTEM SHALL raise a `KernelModuleQueryError` with the exit
   code and captured stderr.

---

### Requirement 10: Persist a Module Load at Boot

**User Story:** As a maintenance coordinator, I want to add a kernel module name to a
drop-in file in `/etc/modules-load.d/`, so that the module is loaded automatically at
every boot.

#### Acceptance Criteria

1. WHEN the coordinator provides a module name and a drop-in file name THE SYSTEM SHALL
   ensure the module name appears as a line in
   `/etc/modules-load.d/<filename>.conf` via the command runner.
2. WHEN the module name is already present in the file THE SYSTEM SHALL skip the write and
   return a `KernelModuleActionResult` with status `SKIPPED`.
3. WHEN the file does not exist THE SYSTEM SHALL create it and write the module name.
4. WHEN the write succeeds THE SYSTEM SHALL return a `KernelModuleActionResult` with
   status `SUCCESS`.
5. WHEN the write fails THE SYSTEM SHALL return a `KernelModuleActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `KernelModuleActionResult` with status `DRY_RUN`.

---

### Requirement 11: Remove a Module from Boot Persistence

**User Story:** As a maintenance coordinator, I want to remove a kernel module name from a
drop-in file in `/etc/modules-load.d/`, so that the module is no longer loaded
automatically at boot.

#### Acceptance Criteria

1. WHEN the coordinator provides a module name and a drop-in file name THE SYSTEM SHALL
   remove the matching line from `/etc/modules-load.d/<filename>.conf` via the command
   runner.
2. WHEN the module name is not present in the file THE SYSTEM SHALL return a
   `KernelModuleActionResult` with status `SKIPPED`.
3. WHEN the file does not exist THE SYSTEM SHALL return a `KernelModuleActionResult` with
   status `SKIPPED`.
4. WHEN the removal succeeds THE SYSTEM SHALL return a `KernelModuleActionResult` with
   status `SUCCESS`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `KernelModuleActionResult` with status `DRY_RUN`.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all kernel parameter and module write
commands to run with the required privileges, so that sysctl writes and modprobe operations
succeed on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (sysctl -w, sysctl --system, modprobe,
   modprobe -r, drop-in file writes).
2. WHEN performing read-only operations (sysctl -n, sysctl -a, lsmod, cat drop-in files)
   THE SYSTEM SHALL NOT use elevation.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.

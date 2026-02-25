# Requirements

## Introduction

The filesystem management feature provides the `automojo-sysman-linux-windows` package with
the ability to create and remove directories, manage symbolic links, query disk usage, mount
and unmount filesystems, and manage `/etc/fstab` entries on Windows Linux systems.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* filesystem
changes to make — it provides the capability to *do it* reliably, idempotently, and with
full auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, enabling dry-run simulation, auditing,
and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Create a Directory

**User Story:** As a maintenance coordinator, I want to create a directory at a specified
path with specific permissions and ownership, so that the required directory structure is
present for software that depends on it.

#### Acceptance Criteria

1. WHEN the coordinator provides a directory path THE SYSTEM SHALL invoke
   `mkdir -p <path>` via the command runner.
2. WHEN the directory already exists THE SYSTEM SHALL skip the mkdir and return a
   `FilesystemActionResult` with status `SKIPPED`.
3. WHEN the coordinator specifies a permission mode THE SYSTEM SHALL invoke
   `chmod <mode> <path>` after a successful mkdir.
4. WHEN the coordinator specifies an owner and/or group THE SYSTEM SHALL invoke
   `chown <owner>:<group> <path>` after a successful mkdir.
5. WHEN the mkdir command succeeds THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `SUCCESS`.
6. WHEN the mkdir command fails THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 2: Remove a Directory

**User Story:** As a maintenance coordinator, I want to remove a directory from the system,
so that obsolete directory structures are cleaned up during maintenance.

#### Acceptance Criteria

1. WHEN the coordinator provides a directory path THE SYSTEM SHALL invoke `rmdir <path>`
   via the command runner to remove an empty directory.
2. WHEN the coordinator sets `recursive=True` THE SYSTEM SHALL invoke `rm -rf <path>`
   instead of `rmdir`.
3. WHEN the directory does not exist THE SYSTEM SHALL skip execution and return a
   `FilesystemActionResult` with status `SKIPPED`.
4. WHEN the remove command succeeds THE SYSTEM SHALL return a `FilesystemActionResult`
   with status `SUCCESS`.
5. WHEN the remove command fails THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 3: Create a Symbolic Link

**User Story:** As a maintenance coordinator, I want to create a symbolic link from a
source path to a target path, so that software can reference files or directories through
a consistent alias regardless of the actual location.

#### Acceptance Criteria

1. WHEN the coordinator provides a link path and a target path THE SYSTEM SHALL invoke
   `ln -s <target> <link>` via the command runner.
2. WHEN a symbolic link already exists at the link path and points to the same target THE
   SYSTEM SHALL skip execution and return a `FilesystemActionResult` with status `SKIPPED`.
3. WHEN a symbolic link already exists at the link path but points to a different target
   THE SYSTEM SHALL remove the existing link and create a new one, returning status
   `SUCCESS`.
4. WHEN a non-symlink file or directory already exists at the link path THE SYSTEM SHALL
   return a `FilesystemActionResult` with status `FAILED` and a message indicating a
   non-symlink path conflict.
5. WHEN the symlink creation succeeds THE SYSTEM SHALL return a `FilesystemActionResult`
   with status `SUCCESS` and the resolved link and target paths.
6. WHEN the symlink creation fails THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 4: Remove a Symbolic Link

**User Story:** As a maintenance coordinator, I want to remove a symbolic link, so that
stale or incorrect aliases are cleaned up without affecting the link target.

#### Acceptance Criteria

1. WHEN the coordinator provides a link path THE SYSTEM SHALL invoke `unlink <path>` via
   the command runner.
2. WHEN no file exists at the link path THE SYSTEM SHALL skip execution and return a
   `FilesystemActionResult` with status `SKIPPED`.
3. WHEN the path exists but is not a symbolic link THE SYSTEM SHALL return a
   `FilesystemActionResult` with status `FAILED` and a message indicating the path is not
   a symlink.
4. WHEN the unlink command succeeds THE SYSTEM SHALL return a `FilesystemActionResult`
   with status `SUCCESS`.
5. WHEN the unlink command fails THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 5: Query Path Information

**User Story:** As a maintenance coordinator, I want to query the type, permissions,
ownership, and size of a path on the filesystem, so that I can make conditional decisions
about what changes are needed without taking any action.

#### Acceptance Criteria

1. WHEN the coordinator provides a path THE SYSTEM SHALL invoke
   `stat -c "%F %a %U %G %s" <path>` via the command runner.
2. WHEN the path exists THE SYSTEM SHALL return a `PathInfo` record containing path type,
   permission mode, owner, group, and size in bytes.
3. WHEN the path does not exist THE SYSTEM SHALL return `None`.
4. WHEN the stat command fails for an unexpected reason THE SYSTEM SHALL raise a
   `FilesystemQueryError` with the exit code and captured stderr.

---

### Requirement 6: Query Disk Usage on a Mount Point

**User Story:** As a maintenance coordinator, I want to query the total, used, and available
disk space on a mount point, so that I can detect low-disk conditions and include capacity
information in maintenance reports.

#### Acceptance Criteria

1. WHEN the coordinator provides a mount point path THE SYSTEM SHALL invoke
   `df -B1 --output=size,used,avail,pcent <path>` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `DiskUsageInfo` record containing
   total bytes, used bytes, available bytes, and use percentage as an integer.
3. WHEN the path is not a mount point THE SYSTEM SHALL return the disk usage of the
   filesystem that contains the path.
4. WHEN the command fails THE SYSTEM SHALL raise a `FilesystemQueryError` with the exit
   code and captured stderr.

---

### Requirement 7: Mount a Filesystem

**User Story:** As a maintenance coordinator, I want to mount a filesystem at a specified
mount point, so that storage devices or network shares are accessible to the system.

#### Acceptance Criteria

1. WHEN the coordinator provides a device and mount point THE SYSTEM SHALL invoke
   `mount <device> <mountpoint>` via the command runner.
2. WHEN the coordinator specifies a filesystem type THE SYSTEM SHALL pass
   `-t <fstype>` to the mount command.
3. WHEN the coordinator specifies mount options THE SYSTEM SHALL pass `-o <options>` to
   the mount command.
4. WHEN the filesystem is already mounted at the given mount point THE SYSTEM SHALL skip
   execution and return a `FilesystemActionResult` with status `SKIPPED`.
5. WHEN the mount command succeeds THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `SUCCESS`.
6. WHEN the mount command fails THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 8: Unmount a Filesystem

**User Story:** As a maintenance coordinator, I want to unmount a filesystem from a
specified mount point, so that storage devices or shares can be safely detached during
maintenance.

#### Acceptance Criteria

1. WHEN the coordinator provides a mount point THE SYSTEM SHALL invoke `umount <mountpoint>`
   via the command runner.
2. WHEN the coordinator sets `lazy=True` THE SYSTEM SHALL pass `-l` to `umount`.
3. WHEN the filesystem is not currently mounted THE SYSTEM SHALL skip execution and return
   a `FilesystemActionResult` with status `SKIPPED`.
4. WHEN the umount command succeeds THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `SUCCESS`.
5. WHEN the umount command fails THE SYSTEM SHALL return a `FilesystemActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 9: Query Mounted Filesystems

**User Story:** As a maintenance coordinator, I want to retrieve the list of currently
mounted filesystems, so that I can determine mount state before issuing mount or unmount
operations.

#### Acceptance Criteria

1. WHEN the coordinator requests the list of mounted filesystems THE SYSTEM SHALL invoke
   `findmnt --json` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON output and return a list of
   `MountInfo` records each containing device, mount point, filesystem type, and options.
3. WHEN no filesystems are mounted THE SYSTEM SHALL return an empty list.
4. WHEN the command fails THE SYSTEM SHALL raise a `FilesystemQueryError` with the exit
   code and captured stderr.

---

### Requirement 10: Add an fstab Entry

**User Story:** As a maintenance coordinator, I want to add a persistent mount entry to
`/etc/fstab`, so that a filesystem is automatically mounted at boot or when `mount -a` is
invoked.

#### Acceptance Criteria

1. WHEN the coordinator provides an fstab entry definition THE SYSTEM SHALL append the
   entry to `/etc/fstab` via the command runner.
2. WHEN an entry for the same device or mount point already exists in `/etc/fstab` THE
   SYSTEM SHALL skip the write and return a `FilesystemActionResult` with status `SKIPPED`.
3. WHEN the entry is written successfully THE SYSTEM SHALL return a `FilesystemActionResult`
   with status `SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `FilesystemActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN` with the entry that would have been
   appended.

---

### Requirement 11: Remove an fstab Entry

**User Story:** As a maintenance coordinator, I want to remove an entry from `/etc/fstab`
by device or mount point, so that a filesystem is no longer automatically mounted after
a maintenance operation removes the need for it.

#### Acceptance Criteria

1. WHEN the coordinator provides a device path or mount point THE SYSTEM SHALL read
   `/etc/fstab`, remove the matching line, and write the result back via the command
   runner.
2. WHEN no matching entry exists THE SYSTEM SHALL skip the write and return a
   `FilesystemActionResult` with status `SKIPPED`.
3. WHEN the entry is removed successfully THE SYSTEM SHALL return a `FilesystemActionResult`
   with status `SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `FilesystemActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `FilesystemActionResult` with status `DRY_RUN`.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all filesystem write commands to run
with the privileges required by the underlying tools, so that operations on protected paths
succeed on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (mkdir, rmdir/rm, ln, unlink, mount,
   umount, fstab write).
2. WHEN performing read-only operations (stat, df, findmnt, cat fstab) THE SYSTEM SHALL
   NOT use elevation.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.

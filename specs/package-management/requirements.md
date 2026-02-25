# Requirements

## Introduction

The package management feature provides the `automojo-sysman-linux-windows` package with the
ability to install, remove, update, query, and otherwise manage RPM software packages on
Windows Linux systems using `dnf` and `rpm` as the underlying package tools.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* to do — it
provides the capability to *do it* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the `automojo-interfaces` command-runner
abstraction, enabling dry-run simulation, command auditing, and test mocking without altering
the maintenance logic.

---

## Requirements

### Requirement 1: Install a Package

**User Story:** As a maintenance coordinator, I want to install a named RPM package at a
specific version, so that the target system has the required software available for use.

#### Acceptance Criteria

1. WHEN the coordinator requests installation of a package by name THE SYSTEM SHALL invoke
   `dnf install -y <package>` via the automojo-interfaces command runner.
2. WHEN the coordinator requests installation of a package at a specific version THE SYSTEM
   SHALL invoke `dnf install -y <package>-<version>` via the command runner.
3. WHEN the requested package is already installed at the requested version THE SYSTEM SHALL
   skip execution and return a `PackageActionResult` with status `SKIPPED`.
4. WHEN the package installation succeeds THE SYSTEM SHALL return a `PackageActionResult`
   with status `SUCCESS` and the resolved installed version.
5. WHEN the package installation fails THE SYSTEM SHALL return a `PackageActionResult` with
   status `FAILED`, the exit code, and the captured stderr output.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command and SHALL
   return a `PackageActionResult` with status `DRY_RUN` describing what would have been done.

---

### Requirement 2: Remove a Package

**User Story:** As a maintenance coordinator, I want to remove a named RPM package from the
system, so that unwanted or obsolete software is cleanly uninstalled.

#### Acceptance Criteria

1. WHEN the coordinator requests removal of a package by name THE SYSTEM SHALL invoke
   `dnf remove -y <package>` via the command runner.
2. WHEN the requested package is not installed THE SYSTEM SHALL skip execution and return a
   `PackageActionResult` with status `SKIPPED`.
3. WHEN the package removal succeeds THE SYSTEM SHALL return a `PackageActionResult` with
   status `SUCCESS`.
4. WHEN the package removal fails THE SYSTEM SHALL return a `PackageActionResult` with
   status `FAILED`, the exit code, and the captured stderr output.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command and SHALL
   return a `PackageActionResult` with status `DRY_RUN`.

---

### Requirement 3: Update a Package

**User Story:** As a maintenance coordinator, I want to update an installed package to a
newer version, so that the system receives bug fixes, security patches, and new capabilities.

#### Acceptance Criteria

1. WHEN the coordinator requests an update of a specific package THE SYSTEM SHALL invoke
   `dnf update -y <package>` via the command runner.
2. WHEN the coordinator requests an update to a specific target version THE SYSTEM SHALL
   invoke `dnf install -y <package>-<version>` via the command runner.
3. WHEN no newer version is available THE SYSTEM SHALL skip execution and return a
   `PackageActionResult` with status `SKIPPED`.
4. WHEN the update succeeds THE SYSTEM SHALL return a `PackageActionResult` with status
   `SUCCESS` and the new installed version.
5. WHEN the update fails THE SYSTEM SHALL return a `PackageActionResult` with status
   `FAILED`, the exit code, and the captured stderr output.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command and SHALL
   return a `PackageActionResult` with status `DRY_RUN` listing the version that would
   have been installed.

---

### Requirement 4: Query Installed Package State

**User Story:** As a maintenance coordinator, I want to query whether a package is installed
and at what version, so that I can make conditional decisions about what maintenance to apply.

#### Acceptance Criteria

1. WHEN the coordinator queries the installation state of a package THE SYSTEM SHALL invoke
   `rpm -q --queryformat '%{NAME} %{VERSION} %{RELEASE} %{ARCH}\n' <package>` via the
   command runner.
2. WHEN the package is installed THE SYSTEM SHALL return a `PackageInfo` record containing
   name, version, release, and architecture.
3. WHEN the package is not installed THE SYSTEM SHALL return `None`.
4. WHEN the query command fails for a reason other than package-not-found THE SYSTEM SHALL
   raise a `PackageQueryError` with the exit code and captured stderr.

---

### Requirement 5: List Available Updates

**User Story:** As a maintenance coordinator, I want to retrieve a list of packages that
have updates available, so that I can plan and schedule targeted update operations.

#### Acceptance Criteria

1. WHEN the coordinator requests a list of available updates THE SYSTEM SHALL invoke
   `dnf check-update` via the command runner.
2. WHEN packages with available updates exist THE SYSTEM SHALL return a list of
   `PackageUpdateInfo` records each containing package name, installed version, and
   available version.
3. WHEN no updates are available THE SYSTEM SHALL return an empty list.
4. WHEN `dnf check-update` exits with code 100 (updates available) THE SYSTEM SHALL treat
   this as a successful response, not a failure.
5. WHEN the command fails with any exit code other than 0 or 100 THE SYSTEM SHALL raise a
   `PackageQueryError`.

---

### Requirement 6: Install from a Local RPM File

**User Story:** As a maintenance coordinator, I want to install a package from a local `.rpm`
file path, so that packages can be deployed from internal artifact stores without requiring
external repository access.

#### Acceptance Criteria

1. WHEN the coordinator requests installation from a local file path THE SYSTEM SHALL invoke
   `dnf install -y <filepath>` via the command runner.
2. WHEN the specified file path does not exist THE SYSTEM SHALL raise a
   `PackageFileNotFoundError` before invoking any command.
3. WHEN the installation succeeds THE SYSTEM SHALL return a `PackageActionResult` with
   status `SUCCESS` and the installed package name and version.
4. WHEN the installation fails THE SYSTEM SHALL return a `PackageActionResult` with status
   `FAILED`, the exit code, and captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command and SHALL
   return a `PackageActionResult` with status `DRY_RUN`.

---

### Requirement 7: Hold (Pin) a Package Version

**User Story:** As a maintenance coordinator, I want to pin a package at its current version
to prevent it from being upgraded, so that system stability is maintained for
version-sensitive software.

#### Acceptance Criteria

1. WHEN the coordinator requests a version hold on a package THE SYSTEM SHALL invoke
   `dnf versionlock add <package>` via the command runner.
2. WHEN the package is already held at the current version THE SYSTEM SHALL skip execution
   and return a `PackageActionResult` with status `SKIPPED`.
3. WHEN the hold is applied successfully THE SYSTEM SHALL return a `PackageActionResult`
   with status `SUCCESS`.
4. WHEN the hold fails THE SYSTEM SHALL return a `PackageActionResult` with status `FAILED`
   and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command.

---

### Requirement 8: Release a Package Hold

**User Story:** As a maintenance coordinator, I want to remove a version hold from a package,
so that future update operations can upgrade it normally.

#### Acceptance Criteria

1. WHEN the coordinator requests removal of a version hold THE SYSTEM SHALL invoke
   `dnf versionlock delete <package>` via the command runner.
2. WHEN no hold exists for the package THE SYSTEM SHALL skip execution and return a
   `PackageActionResult` with status `SKIPPED`.
3. WHEN the hold removal succeeds THE SYSTEM SHALL return a `PackageActionResult` with
   status `SUCCESS`.
4. WHEN the hold removal fails THE SYSTEM SHALL return a `PackageActionResult` with status
   `FAILED` and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command.

---

### Requirement 9: Manage Package Groups

**User Story:** As a maintenance coordinator, I want to install or remove a `dnf` package
group by name, so that sets of related packages can be deployed or removed as a unit.

#### Acceptance Criteria

1. WHEN the coordinator requests installation of a package group THE SYSTEM SHALL invoke
   `dnf groupinstall -y "<group-name>"` via the command runner.
2. WHEN the coordinator requests removal of a package group THE SYSTEM SHALL invoke
   `dnf groupremove -y "<group-name>"` via the command runner.
3. WHEN the group name does not exist THE SYSTEM SHALL return a `PackageActionResult` with
   status `FAILED` and include the dnf error output.
4. WHEN the operation succeeds THE SYSTEM SHALL return a `PackageActionResult` with status
   `SUCCESS`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any system command.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all package management commands to run
with the privileges required by `dnf`, so that installations and removals succeed on a
standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL prefix all `dnf`
   and `rpm` write commands with `sudo` via the command runner's elevation mechanism.
2. WHEN the system is running as root THE SYSTEM SHALL NOT prefix commands with `sudo`.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the command runner to surface the resulting permission error.

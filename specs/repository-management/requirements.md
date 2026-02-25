# Requirements

## Introduction

The repository management feature provides the `automojo-sysman-linux-windows` package with
the ability to add, remove, enable, disable, and refresh `dnf`/`yum` software repositories
on Windows Linux systems.  Repository definitions live in `/etc/yum.repos.d/` as `.repo`
files and control where `dnf` sources packages during install and update operations.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* repositories to
manage — it provides the capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction, enabling dry-run simulation, auditing, and test mocking without
requiring a live system.

---

## Requirements

### Requirement 1: Add a Repository Definition

**User Story:** As a maintenance coordinator, I want to add a new repository definition
file to the system, so that packages from that repository become available for installation
and updates.

#### Acceptance Criteria

1. WHEN the coordinator provides a repository ID, base URL, and optional metadata THE SYSTEM
   SHALL write a `.repo` file to `/etc/yum.repos.d/<repo-id>.repo` with the supplied values.
2. WHEN a `.repo` file with the same repository ID already exists and the content is
   identical THE SYSTEM SHALL skip the write and return a `RepoActionResult` with status
   `SKIPPED`.
3. WHEN a `.repo` file with the same repository ID already exists but the content differs
   THE SYSTEM SHALL overwrite the file and return a `RepoActionResult` with status `SUCCESS`.
4. WHEN the `.repo` file is written successfully THE SYSTEM SHALL return a `RepoActionResult`
   with status `SUCCESS` and the path of the file written.
5. WHEN the file write fails THE SYSTEM SHALL return a `RepoActionResult` with status
   `FAILED` and the error detail.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `RepoActionResult` with status `DRY_RUN` describing what would have been written.

---

### Requirement 2: Remove a Repository Definition

**User Story:** As a maintenance coordinator, I want to remove a repository definition file
from the system, so that packages from that source are no longer available to `dnf`.

#### Acceptance Criteria

1. WHEN the coordinator requests removal of a repository by ID THE SYSTEM SHALL delete
   `/etc/yum.repos.d/<repo-id>.repo` via the command runner.
2. WHEN no `.repo` file exists for the given repository ID THE SYSTEM SHALL skip execution
   and return a `RepoActionResult` with status `SKIPPED`.
3. WHEN the file is removed successfully THE SYSTEM SHALL return a `RepoActionResult` with
   status `SUCCESS`.
4. WHEN the removal fails THE SYSTEM SHALL return a `RepoActionResult` with status `FAILED`
   and the error detail.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT delete any file and SHALL return a
   `RepoActionResult` with status `DRY_RUN`.

---

### Requirement 3: Enable a Repository

**User Story:** As a maintenance coordinator, I want to enable a disabled repository by ID,
so that `dnf` will include it as a source during package operations.

#### Acceptance Criteria

1. WHEN the coordinator requests that a repository be enabled THE SYSTEM SHALL invoke
   `dnf config-manager --set-enabled <repo-id>` via the command runner.
2. WHEN the repository is already enabled THE SYSTEM SHALL skip execution and return a
   `RepoActionResult` with status `SKIPPED`.
3. WHEN the enable operation succeeds THE SYSTEM SHALL return a `RepoActionResult` with
   status `SUCCESS`.
4. WHEN the repository ID does not exist on the system THE SYSTEM SHALL return a
   `RepoActionResult` with status `FAILED` and the captured stderr.
5. WHEN the enable command fails for any other reason THE SYSTEM SHALL return a
   `RepoActionResult` with status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `RepoActionResult` with status `DRY_RUN`.

---

### Requirement 4: Disable a Repository

**User Story:** As a maintenance coordinator, I want to disable a repository by ID without
removing its definition, so that `dnf` excludes it from package operations while keeping
the configuration available for future re-enabling.

#### Acceptance Criteria

1. WHEN the coordinator requests that a repository be disabled THE SYSTEM SHALL invoke
   `dnf config-manager --set-disabled <repo-id>` via the command runner.
2. WHEN the repository is already disabled THE SYSTEM SHALL skip execution and return a
   `RepoActionResult` with status `SKIPPED`.
3. WHEN the disable operation succeeds THE SYSTEM SHALL return a `RepoActionResult` with
   status `SUCCESS`.
4. WHEN the repository ID does not exist on the system THE SYSTEM SHALL return a
   `RepoActionResult` with status `FAILED` and the captured stderr.
5. WHEN the disable command fails for any other reason THE SYSTEM SHALL return a
   `RepoActionResult` with status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `RepoActionResult` with status `DRY_RUN`.

---

### Requirement 5: Refresh Repository Metadata

**User Story:** As a maintenance coordinator, I want to force a refresh of `dnf` repository
metadata, so that package queries and updates reflect the current state of the configured
repositories.

#### Acceptance Criteria

1. WHEN the coordinator requests a metadata refresh for all repositories THE SYSTEM SHALL
   invoke `dnf clean metadata` followed by `dnf makecache` via the command runner.
2. WHEN the coordinator requests a metadata refresh for a specific repository ID THE SYSTEM
   SHALL invoke `dnf clean metadata --disablerepo="*" --enablerepo=<repo-id>` followed by
   `dnf makecache --disablerepo="*" --enablerepo=<repo-id>`.
3. WHEN both commands succeed THE SYSTEM SHALL return a `RepoActionResult` with status
   `SUCCESS`.
4. WHEN either command fails THE SYSTEM SHALL return a `RepoActionResult` with status
   `FAILED`, the exit code, and the captured stderr of the failing command.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `RepoActionResult` with status `DRY_RUN`.

---

### Requirement 6: List Configured Repositories

**User Story:** As a maintenance coordinator, I want to retrieve the list of all configured
repositories and their enabled/disabled state, so that I can make conditional decisions about
which repositories need to be added, enabled, or disabled.

#### Acceptance Criteria

1. WHEN the coordinator requests the repository list THE SYSTEM SHALL invoke
   `dnf repolist all` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a list of `RepoInfo` records each
   containing the repository ID, name, and enabled state.
3. WHEN no repositories are configured THE SYSTEM SHALL return an empty list.
4. WHEN the command fails THE SYSTEM SHALL raise a `RepoQueryError` with the exit code and
   captured stderr.

---

### Requirement 7: Set Repository Priority

**User Story:** As a maintenance coordinator, I want to set the priority value of a
repository, so that package resolution favours repositories with lower priority numbers
when the same package is available from multiple sources.

#### Acceptance Criteria

1. WHEN the coordinator provides a repository ID and a priority integer THE SYSTEM SHALL
   update the `priority=<value>` field in `/etc/yum.repos.d/<repo-id>.repo` via the command
   runner.
2. WHEN the priority value is already set to the requested value THE SYSTEM SHALL skip the
   write and return a `RepoActionResult` with status `SKIPPED`.
3. WHEN the `.repo` file for the given ID does not exist THE SYSTEM SHALL raise a
   `RepoNotFoundError`.
4. WHEN the priority is updated successfully THE SYSTEM SHALL return a `RepoActionResult`
   with status `SUCCESS`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return a
   `RepoActionResult` with status `DRY_RUN`.

---

### Requirement 8: Configure GPG Key Trust

**User Story:** As a maintenance coordinator, I want to import a GPG key for a repository,
so that package signatures from that repository can be verified by `dnf`.

#### Acceptance Criteria

1. WHEN the coordinator provides a GPG key URL THE SYSTEM SHALL invoke
   `rpm --import <key-url>` via the command runner.
2. WHEN the coordinator provides a local GPG key file path THE SYSTEM SHALL verify the file
   exists, then invoke `rpm --import <key-filepath>` via the command runner.
3. WHEN the key file path does not exist THE SYSTEM SHALL raise a `RepoKeyFileNotFoundError`
   before invoking any command.
4. WHEN the import succeeds THE SYSTEM SHALL return a `RepoActionResult` with status
   `SUCCESS`.
5. WHEN the import fails THE SYSTEM SHALL return a `RepoActionResult` with status `FAILED`,
   the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `RepoActionResult` with status `DRY_RUN`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all repository write commands to run
with the privileges required by `dnf config-manager` and `rpm`, so that repository
configuration succeeds on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (add, remove, enable, disable, refresh,
   set-priority, import-key).
2. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.
4. WHEN performing read-only operations (list repositories) THE SYSTEM SHALL NOT use
   elevation.

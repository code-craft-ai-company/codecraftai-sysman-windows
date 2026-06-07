# Requirements

## Introduction

The package-management feature provides the `codecraftai-sysman-windows`
package with the ability to manage `winget`-managed software packages on
Windows systems.  This spec implements the package-management contract
defined at
`codecraftai-sysman-common/specs/contracts/package-management/contract.md`
for Windows using the Windows Package Manager (`winget.exe`).  It covers
install / remove / upgrade / upgrade-all, installed-package queries, a
packaged listing, an upgradable listing, local `.msix` / `.appx` /
`.msi` installation, and source-metadata refresh.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interface.  The feature
does not decide *what* to install — it provides the capability to do so
reliably, idempotently, and auditably.  All system interaction happens
through `ICommandRunner`; no direct `subprocess` call lives in the
manager.

Because `winget` writes under `%ProgramData%` for machine-scope packages
and under `%LocalAppData%` for user-scope packages, Administrator
privileges are required for machine-scope writes on Windows 10 1809+
and all Windows 11 / Server 2022+ systems.  The manager honors the
standard contract elevation flags (`config.elevated=True` maps to the
runner's Administrator-token execution).  Reads (`winget list`,
`winget show`) do not require elevation.

Non-interactive execution is enforced by passing `--silent
--accept-package-agreements --accept-source-agreements` on writes when
`assume_yes=True`.

---

## Requirements

### Requirement 1: Install a Package

**User Story:** As a maintenance coordinator, I want to install a winget
package at the latest version or at a pinned version, idempotently and
with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_package(name, version=None)` THE
   SYSTEM SHALL invoke `["winget", "install", "--exact", "--id", name,
   "--silent", "--accept-package-agreements",
   "--accept-source-agreements"]` via the runner; with a supplied
   `version` THE SYSTEM SHALL additionally include `--version <version>`.
2. WHEN `config.assume_yes=False` THE SYSTEM SHALL omit
   `--accept-package-agreements` and `--accept-source-agreements`; it
   SHALL always pass `--silent` (winget requires a non-interactive flag
   for every unattended run).
3. WHEN the package is already installed at any version AND
   `version is None` THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN the package is already installed at a version that matches
   `version` THE SYSTEM SHALL return `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsPackageActionResult` with `status=SUCCESS`, the resolved
   version, and the argv in `command`.
6. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with
   captured `exit_code`, `stdout`, `stderr`, and `command`.
7. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
8. WHEN `config.elevated=True` THE SYSTEM SHALL pass `elevated=True` to
   the runner so the dispatch runs with an Administrator token.

---

### Requirement 2: Remove a Package

**User Story:** As a maintenance coordinator, I want to remove an
installed winget package, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_package(name)` THE SYSTEM SHALL
   invoke `["winget", "uninstall", "--exact", "--id", name,
   "--silent"]` via the runner.
2. WHEN the package is not installed THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 3: Upgrade a Package

**User Story:** As a maintenance coordinator, I want to upgrade a
single winget package, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `upgrade_package(name, version=None)` THE
   SYSTEM SHALL invoke `["winget", "upgrade", "--exact", "--id", name,
   "--silent"]`; with a supplied `version` THE SYSTEM SHALL additionally
   include `--version <version>`.
2. WHEN the package is not installed THE SYSTEM SHALL return
   `status=SKIPPED` with `message="package is not installed; nothing
   to upgrade"`.
3. WHEN the installed version already matches (or exceeds) the
   requested version THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `version is None` AND `winget list --exact --id <name>` shows
   no available upgrade (the `Available` column is empty or matches
   the installed version) THE SYSTEM SHALL return `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 4: Upgrade All Packages

**User Story:** As a maintenance coordinator, I want to upgrade every
installed package to its latest version in one call.

#### Acceptance Criteria

1. WHEN the coordinator calls `upgrade_all()` THE SYSTEM SHALL invoke
   `["winget", "upgrade", "--all", "--silent",
   "--accept-package-agreements", "--accept-source-agreements"]` via
   the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `WindowsPackageActionResult(status=SUCCESS, package_name="", …)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. THE SYSTEM SHALL NOT apply an idempotency guard on `upgrade_all` —
   winget itself no-ops per-package when nothing is outdated.

---

### Requirement 5: Query Installed State

**User Story:** As a maintenance coordinator, I want to know whether a
package is installed and get its metadata without elevation.

#### Acceptance Criteria

1. WHEN the coordinator calls `is_installed(name)` THE SYSTEM SHALL
   invoke `["winget", "list", "--exact", "--id", name]` via the
   runner without elevation.
2. WHEN the command exits 0 AND stdout contains a table row whose
   `Id` column matches `name` exactly THE SYSTEM SHALL return `True`.
3. WHEN the command exits non-zero OR no row matches THE SYSTEM SHALL
   return `False`.
4. WHEN the coordinator calls `get_package_info(name)` THE SYSTEM
   SHALL invoke `["winget", "show", "--exact", "--id", name]`; on
   success THE SYSTEM SHALL parse the labelled output into a
   `WindowsPackageInfo(name=<Name>, version=<Version>, release="",
   arch="any", origin=<Source>)`.
5. WHEN `winget show` fails with the stderr pattern "No package found
   matching input criteria." THE SYSTEM SHALL return `None` WITHOUT
   raising.
6. WHEN `winget show` fails with any other error THE SYSTEM SHALL
   raise `WindowsPackageQueryError`.

---

### Requirement 6: List Installed Packages

**User Story:** As a maintenance coordinator, I want a uniform list of
installed packages for inventory.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_installed()` THE SYSTEM SHALL
   invoke `["winget", "list"]` via the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the tabular output
   (columns `Name`, `Id`, `Version`, `Available`, `Source`) and
   return a list of `WindowsPackageInfo` objects using the `Id` as
   `name`, `Version` as `version`, `Source` as `origin`, and
   `arch="any"`.
3. WHEN the command fails THE SYSTEM SHALL raise
   `WindowsPackageQueryError`.
4. WHEN `winget list` emits only the header and separator row THE
   SYSTEM SHALL return an empty list.

---

### Requirement 7: List Upgradable Packages

**User Story:** As a maintenance coordinator, I want the list of
packages with available upgrades.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_upgradable()` THE SYSTEM SHALL
   invoke `["winget", "upgrade"]` via the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each row whose
   `Available` column is non-empty and return a list of
   `WindowsPackageUpdateInfo` objects with `name=<Id>`,
   `installed_version=<Version>`, `available_version=<Available>`,
   and `arch="any"`.
3. WHEN the command fails with the "No applicable upgrade found" exit
   code THE SYSTEM SHALL return an empty list.
4. WHEN the command fails with any other non-zero exit THE SYSTEM
   SHALL raise `WindowsPackageQueryError`.
5. WHEN only the header row is present THE SYSTEM SHALL return an
   empty list.

---

### Requirement 8: Install From a Local File

**User Story:** As a maintenance coordinator, I want to install a
packaged file (`.msix`, `.msixbundle`, `.appx`, `.appxbundle`, or
`.msi`) from disk.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_local(filepath)` AND the file
   exists THE SYSTEM SHALL invoke `["winget", "install", "--silent",
   "--accept-package-agreements", "--manifest", filepath]` when the
   file's extension is `.yaml` (a local manifest file) OR
   `["winget", "install", "--silent", "--accept-package-agreements",
   filepath]` otherwise via the runner.
2. WHEN the file does NOT exist THE SYSTEM SHALL raise
   `WindowsPackageFileNotFoundError` before invoking the runner.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   AFTER the file-existence check.

---

### Requirement 9: Refresh Metadata

**User Story:** As a maintenance coordinator, I want to refresh winget
source metadata before making decisions about available versions.

#### Acceptance Criteria

1. WHEN the coordinator calls `refresh_metadata()` THE SYSTEM SHALL
   invoke `["winget", "source", "update"]` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `WindowsPackageActionResult(status=SUCCESS, package_name="", …)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. THE SYSTEM SHALL NOT apply an idempotency guard on
   `refresh_metadata`.

---

### Requirement 10: Elevation

**User Story:** As a maintenance coordinator, I want writes to pass
`elevated=True` when the config enables elevation, so that machine-
scope package installs succeed.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (install_package, remove_package, upgrade_package, upgrade_all,
   install_local, refresh_metadata) THE SYSTEM SHALL pass
   `elevated=True` to the runner.
2. WHEN the verb is a read (`is_installed`, `get_package_info`,
   `list_installed`, `list_upgradable`) THE SYSTEM SHALL NEVER pass
   `elevated=True` regardless of config.
3. WHEN the idempotency-probe reads (`winget list --exact --id`,
   `winget show --exact --id`) are issued as part of a write's guard
   THE SYSTEM SHALL NOT pass `elevated=True` — probes never elevate.
4. WHEN `config.elevated=False` THE SYSTEM SHALL NEVER pass
   `elevated=True`; a resulting permission error on machine-scope
   writes SHALL surface as `status=FAILED` with the runner's exit
   code / stderr.
5. WHEN the process is already running as Administrator THE SYSTEM
   SHALL NOT pass `elevated=True`.

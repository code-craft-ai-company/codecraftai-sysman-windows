# Requirements

## Introduction

The python-environment-management feature provides the `codecraftai-sysman-windows`
package with the ability to deploy Python virtual environments with a declared
dependency set on Windows hosts.  This spec implements the
python-environment-management contract defined at
`codecraftai-sysman-common/specs/contracts/python-environment-management/contract.md`.

Every venv / pip verb is inherited unchanged from
`ccai.sysman.common.pythonenvironmentmanagementbase.PythonEnvironmentManagementBase`
(see `codecraftai-sysman-common/specs/python-environment-management-base/`).  This
package contributes exactly two things:

1. The Windows layout — `<venv>\Scripts\python.exe`, `py -3` launcher, PowerShell
   `Test-Path` / `Remove-Item` probes bound to a configurable PowerShell executable.
2. `ensure_prerequisites()`, which installs a winget CPython package through the
   existing `WindowsPackageManager` rather than issuing `winget` itself.

Windows has no `sudo`; `config.elevated=True` is forwarded to the runner, and the
Windows runners' `elevation_blocked` extension is surfaced by the shared base as
`detail="elevation_blocked"`.

---

## Requirements

### Requirement 1: Select the Windows Layout

#### Acceptance Criteria

1. THE SYSTEM SHALL return a `WindowsPythonEnvironmentLayout` from `_build_layout()`
   bound to `config.powershell_executable`.
2. THE SYSTEM SHALL resolve the environment interpreter to
   `<path>\Scripts\python.exe` and the default base interpreter to `["py", "-3"]`.
3. THE SYSTEM SHALL probe existence with
   `<powershell> -NoProfile -NonInteractive -Command "if (Test-Path -LiteralPath
   '<path>' -PathType Container) { exit 0 } else { exit 1 }"` without elevation.
4. THE SYSTEM SHALL remove with
   `<powershell> -NoProfile -NonInteractive -Command "Remove-Item -LiteralPath
   '<path>' -Recurse -Force"` with `config.elevated`.
5. THE SYSTEM SHALL double single quotes inside `<path>` in both scriptblocks.

---

### Requirement 2: Ensure winget Prerequisites

#### Acceptance Criteria

1. WHEN the coordinator calls `ensure_prerequisites()` THE SYSTEM SHALL install
   `Python.Python.3.12` via `WindowsPackageManager.install_package`, sharing this
   manager's runner.
2. THE SYSTEM SHALL construct the `WindowsPackageManagerConfig` with this manager's
   `dry_run` and `elevated` flags and the config's `winget_executable`.
3. WHEN `config.prerequisite_packages` is set THE SYSTEM SHALL use it; WHEN the
   `packages` argument is supplied THE SYSTEM SHALL use it in preference to both.
4. WHEN an install returns `FAILED` THE SYSTEM SHALL stop and return the results
   collected so far.
5. WHEN dry-run is active THE SYSTEM SHALL return `DRY_RUN` per package and SHALL
   NOT dispatch `winget install`.

---

### Requirement 3: Surface Elevation Blocking

#### Acceptance Criteria

1. WHEN the runner returns a `RunResult` with `elevation_blocked=True` for a write
   THE SYSTEM SHALL return `FAILED` with `detail="elevation_blocked"`.

---

### Requirement 4: Accept a Plain Base Config

#### Acceptance Criteria

1. WHEN constructed with a plain `PythonEnvManagerConfig` THE SYSTEM SHALL use
   `powershell.exe`, `winget`, and the default prerequisite list.

---

### Requirement 5: OS-Prefixed Aliases

#### Acceptance Criteria

1. THE SYSTEM SHALL export `WindowsPythonEnvActionStatus`,
   `WindowsPythonEnvActionResult`, `WindowsPythonEnvDeployResult`,
   `WindowsPythonEnvInfo`, `WindowsPythonEnvQueryError`, `WindowsPythonEnvSpec`,
   `WindowsPythonPackageInfo`, `WindowsPythonEnvManagerConfig`, and
   `WindowsPythonEnvironmentManager`.

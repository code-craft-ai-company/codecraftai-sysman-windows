# Design

## Overview

`ccai.sysman.windows.pythonenvironmentmanagement` is a thin subclass of the shared
`PythonEnvironmentManagementBase`.  Every venv and pip verb, the deploy algorithm,
the requirement probe, and the read surface are documented in
`codecraftai-sysman-common/specs/python-environment-management-base/design.md`.
This document covers only what Windows adds.

---

## Architecture

```
WindowsPythonEnvironmentManager
├── _build_layout()           → WindowsPythonEnvironmentLayout(powershell_executable)
├── ensure_prerequisites()    → WindowsPackageManager.install_package(id) × N   (winget)
│                                (same runner, same dry_run / elevated, winget_executable)
└── (inherited)               → create_environment, remove_environment,
                                install_packages, install_requirements,
                                uninstall_packages, upgrade_pip, deploy, reads
```

---

## Components

### `WindowsPythonEnvManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| *(contract fields)* | — | — | inherited from `PythonEnvManagerConfig` |
| `powershell_executable` | `str` | `"powershell.exe"` | Binary for the `Test-Path` / `Remove-Item` scriptblocks |
| `winget_executable` | `str` | `"winget"` | Forwarded to `WindowsPackageManagerConfig` |
| `prerequisite_packages` | `list[str] \| None` | `None` → `["Python.Python.3.12"]` | winget ids for `ensure_prerequisites` |

### `WindowsPythonEnvironmentManager`

| Method | Description |
|---|---|
| `_build_layout()` | Returns `WindowsPythonEnvironmentLayout(powershell_executable=…)` |
| `ensure_prerequisites(packages=None)` | Installs each id via `WindowsPackageManager`; stops at first `FAILED` |
| `_resolve_prerequisite_packages(packages)` | argument > config > default |
| `_build_package_manager()` | Builds `WindowsPackageManager(runner, WindowsPackageManagerConfig(dry_run, elevated, winget_executable))` |
| `_typed_config()` | Upgrades a plain `PythonEnvManagerConfig` |

---

## Command Mappings

| Step | Command (through the runner) | Elevated |
|---|---|---|
| Prerequisite probe | `winget list --exact --id <id>` (by `WindowsPackageManager`) | never |
| Prerequisite install | `winget install --exact --id <id> --silent --accept-package-agreements --accept-source-agreements` | `config.elevated` |
| Create venv | `py -3 -m venv [--system-site-packages] <path>` | `config.elevated` |
| Existence probe | `powershell.exe -NoProfile -NonInteractive -Command "if (Test-Path -LiteralPath '<path>' -PathType Container) { exit 0 } else { exit 1 }"` | never |
| Validity probe | `<path>\Scripts\python.exe -c "import platform; print(platform.python_version())"` | never |
| pip (any) | `<path>\Scripts\python.exe -m pip --disable-pip-version-check --no-input …` | writes only |
| Remove | `powershell.exe -NoProfile -NonInteractive -Command "Remove-Item -LiteralPath '<path>' -Recurse -Force"` | `config.elevated` |

Launcher notes: the `py` launcher ships with python.org and winget CPython builds
but not with the Microsoft Store build; set
`base_interpreter=[r"C:\Python312\python.exe"]` (or `["py", "-3.12"]` for a
specific stream) when needed.  Paths are passed as discrete argv tokens, so
spaces need no quoting; the PowerShell scriptblocks single-quote the path and
double any embedded quote.

---

## Correctness Properties

1. **Layout.** `manager.layout` is a `WindowsPythonEnvironmentLayout` whose
   `powershell_executable` matches the config; venv python is
   `<path>\Scripts\python.exe`; default launcher is `["py", "-3"]`.
2. **Existence probe.** `environment_exists` dispatches the `Test-Path` scriptblock
   with `elevated=False`.
3. **Remove.** `remove_environment` dispatches the `Remove-Item` scriptblock with
   `config.elevated`.
4. **Prerequisite default.** `winget install --exact --id Python.Python.3.12 …`
   with `--silent`, elevated per config.
5. **Prerequisite precedence and executable.** Argument > config > default; the
   configured `winget_executable` is argv[0].
6. **Prerequisite short-circuit and dry-run.** First `FAILED` is last attempted;
   dry-run dispatches nothing.
7. **Plain config.** Accepted; defaults apply.
8. **Elevation-blocked.** A blocked write yields `FAILED` with
   `detail="elevation_blocked"`.
9. **Deploy argv.** A fresh `deploy` issues `py -3 -m venv`, pip upgrade, and pip
   install with `Scripts\python.exe` in order.

---

## Module Layout

```
source/packages/ccai/sysman/windows/
└── pythonenvironmentmanagement.py

source/testroots/testbase/ccai/tests/sysman/windows/
└── test_pythonenvironmentmanagement.py
```

---

## Testing Strategy

The shared base suite covers the verbs.  The Windows suite asserts layout binding,
both PowerShell probes, the winget delegation (argv, executable, precedence,
short-circuit, dry-run), plain config acceptance, the `elevation_blocked` token via
a `RunResult` subclass, and one end-to-end deploy argv sequence, all through a
`_MockCommandRunner`.

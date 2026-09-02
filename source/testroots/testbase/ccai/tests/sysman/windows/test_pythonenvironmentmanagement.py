"""
    Tests for :class:`WindowsPythonEnvironmentManager`.

    The venv / pip verbs are exercised in depth by the shared base tests
    in ``codecraftai-sysman-common``; these tests cover what the Windows
    subclass adds — Windows layout selection bound to the configured
    PowerShell executable, the winget prerequisite step, the
    ``elevation_blocked`` pass-through, and one end-to-end deploy on
    Windows argv.  No live ``winget``, ``py``, PowerShell or ``pip`` is
    invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.common.pythonenvironmentlayout import WindowsPythonEnvironmentLayout
from ccai.sysman.common.pythonenvironmentmanagementbase import (
    DETAIL_ELEVATION_BLOCKED,
    PythonEnvManagerConfig,
)

from ccai.sysman.windows.packagemanagement import WindowsPackageActionStatus
from ccai.sysman.windows.pythonenvironmentmanagement import (
    WindowsPythonEnvActionStatus,
    WindowsPythonEnvManagerConfig,
    WindowsPythonEnvSpec,
    WindowsPythonEnvironmentManager,
)


_VENV = r"C:\venvs\app"
_VENV_PYTHON = r"C:\venvs\app\Scripts\python.exe"
_PIP_PREFIX = [_VENV_PYTHON, "-m", "pip", "--disable-pip-version-check", "--no-input"]
_VERSION_SNIPPET = "import platform; print(platform.python_version())"
_PS_PREFIX = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
_EXISTS_SCRIPT = (
    "if (Test-Path -LiteralPath 'C:\\venvs\\app' -PathType Container) "
    "{ exit 0 } else { exit 1 }"
)


@dataclass
class _ElevationAwareRunResult(RunResult):
    """
        Stand-in for ``WindowsRunResult`` carrying ``elevation_blocked``.
    """
    elevation_blocked: bool = False


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in keyed by argv tuple, with an
        optional list of argv prefixes that fail and an optional
        elevation-blocked mode.
    """

    results: dict = field(default_factory=dict)
    fail_prefixes: list = field(default_factory=list)
    block_elevation: bool = False
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        self.calls.append({"args": list(args), "elevated": elevated})

        if elevated and self.block_elevation:
            rtnval = _ElevationAwareRunResult(
                exit_code=-1, stdout="", stderr="Process is not elevated", elevation_blocked=True,
            )
            return rtnval

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        for prefix in self.fail_prefixes:
            if tuple(args[:len(prefix)]) == tuple(prefix):
                rtnval = RunResult(exit_code=1, stdout="", stderr="winget failure")
                return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _winget_install_calls(runner: _MockCommandRunner) -> list:
    rtnval = [c for c in runner.calls if c["args"][1:2] == ["install"]]
    return rtnval


def _pip_argv(*pip_args: str) -> list:
    rtnval = list(_PIP_PREFIX)
    rtnval.extend(pip_args)
    return rtnval


def test_layout_is_windows_and_uses_configured_powershell():
    """
        The Windows layout is selected and bound to the configured
        PowerShell executable.
    """
    manager = WindowsPythonEnvironmentManager(
        runner=_MockCommandRunner(),
        config=WindowsPythonEnvManagerConfig(powershell_executable=r"C:\ps\pwsh.exe"),
    )
    testbase.assert_true(isinstance(manager.layout, WindowsPythonEnvironmentLayout))
    testbase.assert_equal(manager.layout.venv_python_path(path=_VENV), _VENV_PYTHON)
    testbase.assert_equal(manager.layout.default_interpreter_argv(), ["py", "-3"])
    testbase.assert_equal(manager.layout.build_remove_tree_argv(path=_VENV)[0], r"C:\ps\pwsh.exe")
    return


def test_environment_exists_uses_powershell_test_path():
    """
        The existence probe is a PowerShell Test-Path scriptblock run
        without elevation.
    """
    runner = _MockCommandRunner()
    manager = WindowsPythonEnvironmentManager(
        runner=runner, config=WindowsPythonEnvManagerConfig(elevated=True),
    )

    testbase.assert_equal(manager.environment_exists(path=_VENV), True)
    testbase.assert_equal(runner.calls[0]["args"], _PS_PREFIX + [_EXISTS_SCRIPT])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_remove_environment_uses_powershell_remove_item():
    """
        Removal runs Remove-Item -Recurse -Force with elevation.
    """
    runner = _MockCommandRunner()
    manager = WindowsPythonEnvironmentManager(
        runner=runner, config=WindowsPythonEnvManagerConfig(elevated=True),
    )

    result = manager.remove_environment(path=_VENV)

    testbase.assert_equal(result.status, WindowsPythonEnvActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"],
        _PS_PREFIX + ["Remove-Item -LiteralPath 'C:\\venvs\\app' -Recurse -Force"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


def test_ensure_prerequisites_installs_default_winget_package():
    """
        The default prerequisite is Python.Python.3.12 via winget with
        the manager's elevation flag.
    """
    runner = _MockCommandRunner()
    manager = WindowsPythonEnvironmentManager(
        runner=runner, config=WindowsPythonEnvManagerConfig(elevated=True),
    )

    results = manager.ensure_prerequisites()

    testbase.assert_equal([r.status for r in results], [WindowsPackageActionStatus.SUCCESS])
    installs = _winget_install_calls(runner)
    testbase.assert_equal(len(installs), 1)
    testbase.assert_equal(installs[0]["args"][:5], ["winget", "install", "--exact", "--id", "Python.Python.3.12"])
    testbase.assert_true("--silent" in installs[0]["args"])
    testbase.assert_equal(installs[0]["elevated"], True)
    return


def test_ensure_prerequisites_honours_override_config_and_winget_executable():
    """
        The config package list and winget executable are honoured, and
        an explicit list overrides the config.
    """
    runner = _MockCommandRunner()
    config = WindowsPythonEnvManagerConfig(
        prerequisite_packages=["Python.Python.3.11"], winget_executable=r"C:\winget\winget.exe",
    )
    manager = WindowsPythonEnvironmentManager(runner=runner, config=config)

    manager.ensure_prerequisites()
    manager.ensure_prerequisites(packages=["Python.Python.3.13"])

    installs = _winget_install_calls(runner)
    testbase.assert_equal([c["args"][0] for c in installs], [r"C:\winget\winget.exe"] * 2)
    testbase.assert_equal([c["args"][4] for c in installs], ["Python.Python.3.11", "Python.Python.3.13"])
    return


def test_ensure_prerequisites_stops_at_first_failure_and_dry_run():
    """
        A failed install aborts the rest; dry-run dispatches nothing.
    """
    runner = _MockCommandRunner(fail_prefixes=[["winget", "install", "--exact", "--id", "A"]])
    manager = WindowsPythonEnvironmentManager(runner=runner)
    results = manager.ensure_prerequisites(packages=["A", "B"])
    testbase.assert_equal([r.status for r in results], [WindowsPackageActionStatus.FAILED])
    testbase.assert_equal(len(_winget_install_calls(runner)), 1)

    dry_runner = _MockCommandRunner()
    dry_manager = WindowsPythonEnvironmentManager(
        runner=dry_runner, config=WindowsPythonEnvManagerConfig(dry_run=True),
    )
    dry_results = dry_manager.ensure_prerequisites()
    testbase.assert_equal([r.status for r in dry_results], [WindowsPackageActionStatus.DRY_RUN])
    testbase.assert_equal(dry_runner.calls, [])
    return


def test_plain_base_config_is_accepted():
    """
        A plain PythonEnvManagerConfig works; Windows-only fields default.
    """
    runner = _MockCommandRunner()
    manager = WindowsPythonEnvironmentManager(runner=runner, config=PythonEnvManagerConfig())

    testbase.assert_equal(manager.layout.powershell_executable, "powershell.exe")
    manager.ensure_prerequisites()
    testbase.assert_equal(_winget_install_calls(runner)[0]["args"][0], "winget")
    return


def test_elevation_blocked_surfaces_detail_token():
    """
        When the runner refuses elevation the write is FAILED with the
        stable elevation_blocked detail token.
    """
    runner = _MockCommandRunner(block_elevation=True)
    manager = WindowsPythonEnvironmentManager(
        runner=runner, config=WindowsPythonEnvManagerConfig(elevated=True),
    )

    result = manager.upgrade_pip(path=_VENV)

    testbase.assert_equal(result.status, WindowsPythonEnvActionStatus.FAILED)
    testbase.assert_equal(result.detail, DETAIL_ELEVATION_BLOCKED)
    return


def test_deploy_end_to_end_on_windows_argv():
    """
        A fresh deploy issues py -3 -m venv, pip upgrade and pip install
        with Scripts\\python.exe through the runner.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_PS_PREFIX + [_EXISTS_SCRIPT])] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(_VENV_PYTHON, "-c", _VERSION_SNIPPET)] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[tuple(_pip_argv("list", "--format", "json"))] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    manager = WindowsPythonEnvironmentManager(
        runner=runner, config=WindowsPythonEnvManagerConfig(elevated=True),
    )

    result = manager.deploy(spec=WindowsPythonEnvSpec(path=_VENV, packages=["pywin32"]))

    testbase.assert_equal(result.status, WindowsPythonEnvActionStatus.SUCCESS)
    writes = [c["args"] for c in runner.calls if c["elevated"] is True]
    testbase.assert_equal(
        writes,
        [
            ["py", "-3", "-m", "venv", _VENV],
            _pip_argv("install", "--upgrade", "pip"),
            _pip_argv("install", "pywin32"),
        ],
    )
    return

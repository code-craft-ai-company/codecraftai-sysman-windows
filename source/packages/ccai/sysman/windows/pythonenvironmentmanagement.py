"""
    Windows python-environment-management feature.

    Implements the cross-OS python-environment-management contract for
    Windows.  All venv / pip behaviour is inherited from
    :class:`PythonEnvironmentManagementBase`; this module only selects
    the Windows layout (``<venv>\\Scripts\\python.exe``, ``py -3``
    launcher, PowerShell ``Test-Path`` / ``Remove-Item`` probes) and adds
    the Windows prerequisite step.

    Interpreter launcher:
        The default base interpreter is the Python launcher for Windows
        (``py -3``), which is installed with every python.org and winget
        CPython build and resolves the newest installed CPython 3.  Set
        ``base_interpreter`` to ``["py", "-3.12"]`` for a specific
        stream or to an absolute ``python.exe`` path when the launcher
        is absent (for example the Microsoft Store build).

    Elevation policy:
        Windows has no ``sudo``.  ``config.elevated=True`` is forwarded
        to the runner for write verbs; the Windows runners pre-check the
        process token and return ``elevation_blocked=True`` when it is
        not elevated.  The base manager surfaces that as the stable
        ``detail`` token ``elevation_blocked`` so coordinators can
        distinguish it from a real pip / venv failure.

    Prerequisites:
        :meth:`WindowsPythonEnvironmentManager.ensure_prerequisites`
        installs the ``Python.Python.3.12`` winget package (configurable)
        through the existing :class:`WindowsPackageManager` rather than
        re-implementing winget calls here.

    See:
        * :doc:`/specs/contracts/python-environment-management/contract.md`
        * :doc:`/specs/python-environment-management-base/design.md`
        * :doc:`/specs/3-python-environment-management/design.md`
"""

from dataclasses import dataclass
from typing import List, Optional

from ccai.sysman.common.pythonenvironmentlayout import (
    PythonEnvironmentLayout,
    WindowsPythonEnvironmentLayout,
)
from ccai.sysman.common.pythonenvironmentmanagementbase import (
    PythonEnvActionResult,
    PythonEnvActionStatus,
    PythonEnvDeployResult,
    PythonEnvInfo,
    PythonEnvManagerConfig,
    PythonEnvQueryError,
    PythonEnvSpec,
    PythonEnvironmentManagementBase,
    PythonPackageInfo,
)

from ccai.sysman.windows.packagemanagement import (
    WindowsPackageActionResult,
    WindowsPackageActionStatus,
    WindowsPackageManager,
    WindowsPackageManagerConfig,
)


WindowsPythonEnvActionStatus = PythonEnvActionStatus
WindowsPythonEnvActionResult = PythonEnvActionResult
WindowsPythonEnvDeployResult = PythonEnvDeployResult
WindowsPythonEnvInfo = PythonEnvInfo
WindowsPythonEnvQueryError = PythonEnvQueryError
WindowsPythonEnvSpec = PythonEnvSpec
WindowsPythonPackageInfo = PythonPackageInfo


_DEFAULT_POWERSHELL = "powershell.exe"
_DEFAULT_WINGET = "winget"
_DEFAULT_PREREQUISITE_PACKAGES = ["Python.Python.3.12"]


@dataclass
class WindowsPythonEnvManagerConfig(PythonEnvManagerConfig):
    """
        Configuration for :class:`WindowsPythonEnvironmentManager`.
        Inherits every contract field from :class:`PythonEnvManagerConfig`.

        :param powershell_executable: PowerShell binary used for the
                                      directory probe and removal.
                                      Defaults to ``powershell.exe``
                                      (PATH-resolved).
        :param winget_executable: Forwarded to
                                  :class:`WindowsPackageManagerConfig`
                                  for prerequisite installs.
        :param prerequisite_packages: winget package ids installed by
                                      :meth:`ensure_prerequisites`.
                                      ``None`` (the default) means
                                      ``Python.Python.3.12``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    winget_executable: str = _DEFAULT_WINGET
    prerequisite_packages: Optional[List[str]] = None


class WindowsPythonEnvironmentManager(PythonEnvironmentManagementBase):
    """
        Windows concrete implementation of the
        python-environment-management contract.
    """

    def _build_layout(self) -> PythonEnvironmentLayout:
        """
            :returns: The Windows layout bound to the configured
                      PowerShell executable.
        """
        config = self._typed_config()
        rtnval = WindowsPythonEnvironmentLayout(
            powershell_executable=config.powershell_executable,
        )
        return rtnval

    def ensure_prerequisites(
        self,
        packages: Optional[List[str]] = None,
    ) -> List[WindowsPackageActionResult]:
        """
            Install the winget packages providing the base interpreter,
            delegating to :class:`WindowsPackageManager`.

            Each package is installed in order; the sequence stops at the
            first ``FAILED`` result.  Dry-run and elevation follow this
            manager's config.

            :param packages: Override of the package-id list; ``None``
                             uses the config, then the module default.

            :returns: One :class:`WindowsPackageActionResult` per package
                      attempted, in order.
        """
        names = self._resolve_prerequisite_packages(packages=packages)
        package_manager = self._build_package_manager()

        rtnval: List[WindowsPackageActionResult] = []
        for name in names:
            result = package_manager.install_package(name=name)
            rtnval.append(result)
            if result.status == WindowsPackageActionStatus.FAILED:
                break
        return rtnval

    def _resolve_prerequisite_packages(
        self,
        *,
        packages: Optional[List[str]],
    ) -> List[str]:
        """
            Pick the prerequisite list: explicit argument, then config,
            then the module default.

            :param packages: Caller-supplied override or ``None``.

            :returns: A copy of the chosen list.
        """
        if packages is not None and len(packages) > 0:
            rtnval = list(packages)
            return rtnval

        config = self._typed_config()
        if config.prerequisite_packages is not None and len(config.prerequisite_packages) > 0:
            rtnval = list(config.prerequisite_packages)
            return rtnval

        rtnval = list(_DEFAULT_PREREQUISITE_PACKAGES)
        return rtnval

    def _build_package_manager(self) -> WindowsPackageManager:
        """
            Construct the :class:`WindowsPackageManager` used for
            prerequisite installs, sharing this manager's runner and its
            ``dry_run`` / ``elevated`` flags.

            :returns: The package manager.
        """
        config = self._typed_config()
        package_config = WindowsPackageManagerConfig(
            dry_run=config.dry_run,
            elevated=config.elevated,
            winget_executable=config.winget_executable,
        )
        rtnval = WindowsPackageManager(runner=self._runner, config=package_config)
        return rtnval

    def _typed_config(self) -> WindowsPythonEnvManagerConfig:
        """
            Return the config as :class:`WindowsPythonEnvManagerConfig`,
            upgrading a plain :class:`PythonEnvManagerConfig` so the
            Windows-only fields read as their defaults.

            :returns: The typed config.
        """
        if isinstance(self._config, WindowsPythonEnvManagerConfig):
            rtnval = self._config
            return rtnval

        rtnval = WindowsPythonEnvManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
            base_interpreter=self._config.base_interpreter,
            index_url=self._config.index_url,
            extra_index_urls=self._config.extra_index_urls,
            trusted_hosts=self._config.trusted_hosts,
            pip_options=self._config.pip_options,
            upgrade_pip=self._config.upgrade_pip,
            system_site_packages=self._config.system_site_packages,
        )
        return rtnval


__all__ = [
    "WindowsPythonEnvActionResult",
    "WindowsPythonEnvActionStatus",
    "WindowsPythonEnvDeployResult",
    "WindowsPythonEnvInfo",
    "WindowsPythonEnvManagerConfig",
    "WindowsPythonEnvQueryError",
    "WindowsPythonEnvSpec",
    "WindowsPythonEnvironmentManager",
    "WindowsPythonPackageInfo",
]

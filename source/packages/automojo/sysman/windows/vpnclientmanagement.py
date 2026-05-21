"""
    Windows vpn-client-management feature.

    Implements the cross-OS vpn-client-management contract for Windows
    using the official Tailscale and Twingate Connector MSIs, with
    Windows Services controlled via PowerShell ``Set-Service`` /
    ``Start-Service`` / ``Stop-Service`` cmdlets and probed via
    ``Get-Service`` and ``Get-Package``.

    File I/O policy:
        Every read / write / probe is routed through the injected
        :class:`ICommandRunner` (typically a PowerShell-over-WinRM
        runner).  This module does NOT consume an :class:`IFileOperator`
        and does NOT shell out directly.

    Elevation policy:
        Reads (``tailscale status``, ``Get-Service``, ``Get-Package``)
        NEVER pass ``elevated=True``.  Writes (``msiexec``,
        ``Set-Service``, ``Start-Service``, ``Stop-Service``,
        ``tailscale up``, state-directory ``Remove-Item``) honour
        ``config.elevated``.

    Elevation-blocked convention:
        When a Windows runner refuses a write because the host process
        lacks an Administrator token, it returns a
        :class:`WindowsRunResult` with ``elevation_blocked=True`` and
        ``exit_code=-1``.  This module inspects that flag (via
        ``getattr``, so plain :class:`RunResult` mocks remain
        compatible) and surfaces a stable ``"elevation_blocked"`` token
        in the :class:`VPNActionResult.detail` field consistent with
        the package-wide Windows convention also used by
        ``3-firewall-management`` and the planned ``3-service-
        management`` feature.

    See:
        - vpn-client-management contract:
          ``automojo-sysman-common/specs/contracts/vpn-client-management/contract.md``
        - shared base:
          :class:`automojo.sysman.common.vpnclientmanagementbase.VPNClientManagementBase`
"""

import base64
import json
import os

from dataclasses import dataclass
from typing import Dict, List, Optional

from automojo.aspects.aspects import Aspects
from automojo.interfaces.icommandrunner import ICommandRunner, RunResult

from automojo.sysman.common.vpnactionresult import VPNActionResult
from automojo.sysman.common.vpnactionstatus import VPNActionStatus
from automojo.sysman.common.vpnclientmanagementbase import (
    VERB_KIND_WRITE,
    VPNClientManagementBase,
)
from automojo.sysman.common.vpnclientmanagerconfig import VPNClientManagerConfig
from automojo.sysman.common.vpnclientqueryerror import VPNClientQueryError
from automojo.sysman.common.vpnclientstatus import VPNClientStatus
from automojo.sysman.common.vpnprovider import VPNProvider


# Re-export shared contract types under Windows-prefixed aliases so
# callers can pin to the OS-flavour symbol from a single module.
WindowsVPNActionStatus = VPNActionStatus
WindowsVPNActionResult = VPNActionResult
WindowsVPNClientStatus = VPNClientStatus
WindowsVPNClientQueryError = VPNClientQueryError


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_DEFAULT_TAILSCALE_SERVICE = "Tailscale"
_DEFAULT_TWINGATE_SERVICE = "twingate.connector"
_DEFAULT_TWINGATE_CONFIG_PATH = "C:\\ProgramData\\Twingate\\connector.conf"
_DEFAULT_MSI_CACHE_DIR = "C:\\Windows\\Temp\\automojo-vpn-msi"
_DEFAULT_TAILSCALE_PRODUCT = "Tailscale"
_DEFAULT_TWINGATE_PRODUCT = "Twingate Connector"

_DEFAULT_TAILSCALE_STATE_DIR = "C:\\ProgramData\\Tailscale"
_DEFAULT_TWINGATE_STATE_DIR = "C:\\ProgramData\\Twingate"

# Path of the short-lived file the Tailscale auth key is staged to so
# that ``tailscale up`` can read it via ``--auth-key=file:<path>``
# instead of carrying the secret on the process command line (visible
# to ``Get-Process`` / Sysmon for the process lifetime).
_DEFAULT_AUTHKEY_FILE = "C:\\Windows\\Temp\\automojo-vpn-msi\\tailscale-authkey"

# Architecture token used in the Tailscale MSI file name.  The
# published MSIs are arch-qualified; an arch-less ``tailscale-setup-
# <version>.msi`` form does not exist on ``pkgs.tailscale.com`` and
# 404s.
_DEFAULT_TAILSCALE_MSI_ARCH = "amd64"

# Stable Tailscale MSI URL pattern.  The published artifacts are
# ``tailscale-setup-<version>-<arch>.msi`` (and ``tailscale-setup-
# latest-<arch>.msi`` for the channel head) under the channel folder.
_TAILSCALE_MSI_URL_TEMPLATE = (
    "https://pkgs.tailscale.com/{channel}/"
    "tailscale-setup-{version}-{arch}.msi"
)
_TWINGATE_MSI_URL_TEMPLATE = (
    "https://binaries.twingate.com/connector/{version}.msi"
)

_ELEVATION_BLOCKED_TOKEN = "elevation_blocked"

# Sentinel lines emitted by the ``_read_file_content`` probe script so
# the "file absent" case can be distinguished from "file present but
# empty" — ``Get-Content -Raw`` alone returns an empty string for both.
_FILE_PRESENT_SENTINEL = "__AUTOMOJO_FILE_PRESENT__"
_FILE_ABSENT_SENTINEL = "__AUTOMOJO_FILE_ABSENT__"

_SUPPORTED_PROVIDERS = (VPNProvider.TAILSCALE, VPNProvider.TWINGATE)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


@dataclass
class WindowsVPNClientManagerConfig(VPNClientManagerConfig):
    """
        Configuration for :class:`WindowsVPNClientManager`.  Extends
        the shared :class:`VPNClientManagerConfig` with Windows-only
        knobs.

        :param tailscale_service_name: Name of the Tailscale Windows
                                      Service.
        :param twingate_service_name: Name of the Twingate Connector
                                      Windows Service.
        :param twingate_config_path: Filesystem path for the Twingate
                                     connector configuration file.
        :param msi_cache_dir: Directory used to stage downloaded MSI
                              installers.
        :param tailscale_product_name: Product name used to look up the
                                       installed Tailscale package.
        :param twingate_product_name: Product name used to look up the
                                      installed Twingate Connector
                                      package.
        :param powershell_executable: PowerShell executable path used
                                      by every wrapper argv.
        :param tailscale_state_dir: State directory removed when
                                    ``uninstall(purge=True)`` targets
                                    Tailscale.
        :param twingate_state_dir: State directory removed when
                                   ``uninstall(purge=True)`` targets
                                   Twingate.
        :param tailscale_msi_arch: Architecture token used in the
                                   Tailscale MSI file name (the
                                   published artifacts are
                                   arch-qualified).
    """
    tailscale_service_name: str = _DEFAULT_TAILSCALE_SERVICE
    twingate_service_name: str = _DEFAULT_TWINGATE_SERVICE
    twingate_config_path: str = _DEFAULT_TWINGATE_CONFIG_PATH
    msi_cache_dir: str = _DEFAULT_MSI_CACHE_DIR
    tailscale_product_name: str = _DEFAULT_TAILSCALE_PRODUCT
    twingate_product_name: str = _DEFAULT_TWINGATE_PRODUCT
    powershell_executable: str = _DEFAULT_POWERSHELL
    tailscale_state_dir: str = _DEFAULT_TAILSCALE_STATE_DIR
    twingate_state_dir: str = _DEFAULT_TWINGATE_STATE_DIR
    authkey_file_path: str = _DEFAULT_AUTHKEY_FILE
    tailscale_msi_arch: str = _DEFAULT_TAILSCALE_MSI_ARCH


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: Raw text.

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _is_elevation_blocked(*, run_result: RunResult) -> bool:
    """
        Detect a Windows runner's elevation-refusal signal.

        Windows runners (e.g. :class:`WindowsLocalCommandRunner`)
        return a :class:`WindowsRunResult` whose ``elevation_blocked``
        flag is True when the runner refused the command because the
        host process lacks an Administrator token.  Plain
        :class:`RunResult` instances used in unit tests will not carry
        the attribute; in that case we return False.

        :param run_result: The runner's return value.

        :returns: True when the runner reported ``elevation_blocked``.
    """
    flag = getattr(run_result, "elevation_blocked", False)
    rtnval = bool(flag)
    return rtnval


def _parse_tailscale_status_json(
    *,
    payload: str,
    provider: VPNProvider,
) -> WindowsVPNClientStatus:
    """
        Parse ``tailscale status --json`` output into a
        :class:`WindowsVPNClientStatus`.

        Total over backend states; never raises for shape variations.

        :param payload: Raw stdout from ``tailscale status --json``.
        :param provider: Provider to stamp onto the result.

        :returns: The parsed status snapshot.
    """
    stripped = (payload or "").strip()
    if len(stripped) == 0:
        rtnval = WindowsVPNClientStatus(
            provider=provider,
            installed=False,
            running=False,
        )
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = WindowsVPNClientStatus(
            provider=provider,
            installed=True,
            running=False,
        )
        return rtnval

    if not isinstance(parsed, dict):
        rtnval = WindowsVPNClientStatus(
            provider=provider,
            installed=True,
            running=False,
        )
        return rtnval

    backend_state_raw = parsed.get("BackendState")
    backend_state = ""
    if backend_state_raw is not None:
        backend_state = str(backend_state_raw)

    running_flag = backend_state == "Running"

    version_raw = parsed.get("Version")
    if version_raw is None:
        version_value: Optional[str] = None
    else:
        version_value = str(version_raw)

    self_section = parsed.get("Self")
    self_address: Optional[str] = None
    hostname: Optional[str] = None
    if isinstance(self_section, dict):
        tailscale_ips = self_section.get("TailscaleIPs")
        if isinstance(tailscale_ips, list) and len(tailscale_ips) > 0:
            first_ip = tailscale_ips[0]
            if first_ip is not None:
                self_address = str(first_ip)
        host_raw = self_section.get("HostName")
        if host_raw is not None:
            hostname = str(host_raw)

    tailnet_section = parsed.get("CurrentTailnet")
    tailnet_value: Optional[str] = None
    if isinstance(tailnet_section, dict):
        tailnet_raw = tailnet_section.get("Name")
        if tailnet_raw is not None:
            tailnet_value = str(tailnet_raw)

    peer_addresses: Dict[str, str] = {}
    peer_section = parsed.get("Peer")
    if isinstance(peer_section, dict):
        for peer_value in peer_section.values():
            if not isinstance(peer_value, dict):
                continue
            peer_host_raw = peer_value.get("HostName")
            peer_ips = peer_value.get("TailscaleIPs")
            if peer_host_raw is None:
                continue
            if not isinstance(peer_ips, list) or len(peer_ips) == 0:
                continue
            peer_host = str(peer_host_raw)
            peer_ip = str(peer_ips[0])
            peer_addresses[peer_host] = peer_ip

    rtnval = WindowsVPNClientStatus(
        provider=provider,
        installed=True,
        running=running_flag,
        self_address=self_address,
        hostname=hostname,
        tailnet=tailnet_value,
        peer_addresses=peer_addresses,
        version=version_value,
    )
    return rtnval


def _parse_get_service_json(
    *,
    payload: str,
    provider: VPNProvider,
) -> WindowsVPNClientStatus:
    """
        Parse ``Get-Service ... | ConvertTo-Json`` output into a
        :class:`WindowsVPNClientStatus`.

        :param payload: Raw stdout from ``Get-Service ... | ConvertTo-Json``.
        :param provider: Provider to stamp onto the result.

        :returns: The parsed status snapshot.  ``self_address`` is
                  always ``None`` and ``peer_addresses`` is always
                  empty for ``Get-Service``-derived snapshots.
    """
    stripped = (payload or "").strip()
    if len(stripped) == 0:
        rtnval = WindowsVPNClientStatus(
            provider=provider,
            installed=False,
            running=False,
        )
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = WindowsVPNClientStatus(
            provider=provider,
            installed=False,
            running=False,
        )
        return rtnval

    if isinstance(parsed, list):
        if len(parsed) == 0:
            rtnval = WindowsVPNClientStatus(
                provider=provider,
                installed=False,
                running=False,
            )
            return rtnval
        record = parsed[0]
    else:
        record = parsed

    if not isinstance(record, dict):
        rtnval = WindowsVPNClientStatus(
            provider=provider,
            installed=False,
            running=False,
        )
        return rtnval

    status_raw = record.get("Status")
    status_text = ""
    if isinstance(status_raw, str):
        status_text = status_raw
    elif isinstance(status_raw, int):
        # Get-Service Status enum: 4 == Running.
        if status_raw == 4:
            status_text = "Running"

    running_flag = status_text == "Running"

    hostname_raw = record.get("MachineName")
    hostname_value: Optional[str] = None
    if hostname_raw is not None:
        hostname_value = str(hostname_raw)

    rtnval = WindowsVPNClientStatus(
        provider=provider,
        installed=True,
        running=running_flag,
        self_address=None,
        hostname=hostname_value,
        tailnet=None,
        peer_addresses={},
        version=None,
    )
    return rtnval


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsVPNClientManager(VPNClientManagementBase):
    """
        Windows implementation of the vpn-client-management contract.

        Every write routes through the injected
        :class:`ICommandRunner`, honours the manager config's
        ``dry_run`` / ``elevated`` flags, and performs an idempotency
        probe before dispatching.  Reads never elevate.

        Supported providers: ``VPNProvider.TAILSCALE`` and
        ``VPNProvider.TWINGATE``.
    """

    feature_name: Optional[str] = "vpn-client-management"

    def __init__(
        self,
        *,
        runner: ICommandRunner,
        config: Optional[VPNClientManagerConfig] = None,
    ) -> None:
        """
            :param runner: PowerShell-capable command runner.  Every
                           write and read flows through this runner.
            :param config: Manager configuration.  Defaults to a
                           freshly-constructed
                           :class:`WindowsVPNClientManagerConfig`
                           when omitted.
        """
        if config is None:
            effective_config: VPNClientManagerConfig = WindowsVPNClientManagerConfig()
        else:
            effective_config = config
        super().__init__(runner=runner, config=effective_config)
        return

    # ------------------------------------------------------------------
    # Public verbs
    # ------------------------------------------------------------------

    def install_tailscale(
        self,
        *,
        version: Optional[str],
        channel: str,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Install the Tailscale client via the official MSI.

            Performs an idempotency probe via ``Get-Package`` before
            dispatching.  Honours dry-run and elevation.

            :param version: Specific package version, or ``None`` to
                            use the channel default ("latest").
            :param channel: Release channel (``"stable"``,
                            ``"unstable"``, ...).
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        cfg = self._typed_config()

        effective_version = self._resolve_effective_version(
            requested_version=version,
        )
        msi_url = _TAILSCALE_MSI_URL_TEMPLATE.format(
            channel=channel,
            version=effective_version,
            arch=cfg.tailscale_msi_arch,
        )
        msi_path = self._tailscale_msi_path(version=effective_version)
        script = self._render_install_msi_script(
            msi_url=msi_url,
            msi_path=msi_path,
            cache_dir=cfg.msi_cache_dir,
        )
        command = self._build_powershell_argv(script=script)
        detail = "install_tailscale:{}".format(effective_version)

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=command,
            detail=detail,
        )
        if dry is not None:
            return dry

        installed_version = self._get_installed_version(
            product_name=cfg.tailscale_product_name,
            aspects=aspects,
        )
        if installed_version is not None:
            if version is None or installed_version == version:
                skipped = WindowsVPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TAILSCALE,
                    detail="tailscale_already_installed:{}".format(
                        installed_version,
                    ),
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=command,
                )
                return skipped

        rtnval = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=command,
            detail=detail,
            aspects=aspects,
        )
        return rtnval

    def install_twingate_connector(
        self,
        *,
        version: Optional[str],
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Install the Twingate Connector via the official MSI.

            Performs an idempotency probe via ``Get-Package`` before
            dispatching.  Honours dry-run and elevation.

            :param version: Specific package version, or ``None`` for
                            "latest".
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        cfg = self._typed_config()

        effective_version = self._resolve_effective_version(
            requested_version=version,
        )
        msi_url = _TWINGATE_MSI_URL_TEMPLATE.format(version=effective_version)
        msi_path = self._twingate_msi_path(version=effective_version)
        script = self._render_install_msi_script(
            msi_url=msi_url,
            msi_path=msi_path,
            cache_dir=cfg.msi_cache_dir,
        )
        command = self._build_powershell_argv(script=script)
        detail = "install_twingate_connector:{}".format(effective_version)

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=command,
            detail=detail,
        )
        if dry is not None:
            return dry

        installed_version = self._get_installed_version(
            product_name=cfg.twingate_product_name,
            aspects=aspects,
        )
        if installed_version is not None:
            if version is None or installed_version == version:
                skipped = WindowsVPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TWINGATE,
                    detail="twingate_already_installed:{}".format(
                        installed_version,
                    ),
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=command,
                )
                return skipped

        rtnval = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TWINGATE,
            command=command,
            detail=detail,
            aspects=aspects,
        )
        return rtnval

    def configure(
        self,
        *,
        provider: VPNProvider,
        settings: Dict[str, str],
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Write provider configuration.

            Tailscale has no on-disk configuration step on Windows;
            for that provider this verb returns ``SKIPPED``.  Twingate
            writes ``<twingate_config_path>`` via ``Set-Content
            -Encoding UTF8``.  Honours dry-run and elevation.

            :param provider: Target provider.
            :param settings: Provider key/value settings.  For
                             Twingate, rendered as ``KEY=VALUE`` lines
                             sorted by key for deterministic output.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        rejection = self._validate_provider(
            provider=provider, supported=_SUPPORTED_PROVIDERS,
        )
        if rejection is not None:
            return rejection

        if provider is VPNProvider.TAILSCALE:
            rtnval = WindowsVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale has no on-disk configure step on windows",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=None,
            )
            return rtnval

        cfg = self._typed_config()
        rendered_body = self._render_twingate_config(settings=settings)

        script = self._render_set_content_script(
            path=cfg.twingate_config_path,
            body=rendered_body,
        )
        command = self._build_powershell_argv(script=script)
        detail = "configure_twingate:{}".format(cfg.twingate_config_path)

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=command,
            detail=detail,
        )
        if dry is not None:
            return dry

        existing_body = self._read_file_content(
            path=cfg.twingate_config_path,
            aspects=aspects,
        )
        if existing_body is not None and existing_body == rendered_body:
            skipped = WindowsVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate_config_unchanged",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=command,
            )
            return skipped

        rtnval = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TWINGATE,
            command=command,
            detail=detail,
            aspects=aspects,
        )
        return rtnval

    def bring_up(
        self,
        *,
        provider: VPNProvider,
        authkey: str,
        hostname: Optional[str],
        advertise_tags: Optional[List[str]],
        advertise_routes: Optional[List[str]],
        exit_node: bool,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Bring the mesh-VPN tunnel up.

            For Tailscale: enables and starts the ``Tailscale``
            service, then runs ``tailscale up --authkey ...``.  For
            Twingate: enables and starts the connector service
            (configuration is expected to have been written by a
            previous :meth:`configure` call).

            Honours dry-run, elevation, and idempotency
            (``status()`` already shows ``running=True``).

            :param provider: Target provider.
            :param authkey: Vendor-issued auth key (Tailscale only;
                            ignored for Twingate).
            :param hostname: Optional hostname override (Tailscale).
            :param advertise_tags: Optional Tailscale tags.
            :param advertise_routes: Optional Tailscale routes.
            :param exit_node: When True, advertise the host as an exit
                              node (Tailscale).
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        rejection = self._validate_provider(
            provider=provider, supported=_SUPPORTED_PROVIDERS,
        )
        if rejection is not None:
            return rejection

        if provider is VPNProvider.TAILSCALE:
            rtnval = self._bring_up_tailscale(
                authkey=authkey,
                hostname=hostname,
                advertise_tags=advertise_tags,
                advertise_routes=advertise_routes,
                exit_node=exit_node,
                aspects=aspects,
            )
            return rtnval

        rtnval = self._bring_up_twingate(aspects=aspects)
        return rtnval

    def bring_down(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Bring the tunnel down without uninstalling.

            Tailscale: runs ``tailscale down`` then ``Stop-Service
            Tailscale``.  Twingate: runs ``Stop-Service
            twingate.connector``.

            Honours dry-run, elevation, and idempotency.

            :param provider: Target provider.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        rejection = self._validate_provider(
            provider=provider, supported=_SUPPORTED_PROVIDERS,
        )
        if rejection is not None:
            return rejection

        if provider is VPNProvider.TAILSCALE:
            rtnval = self._bring_down_tailscale(aspects=aspects)
            return rtnval

        rtnval = self._bring_down_twingate(aspects=aspects)
        return rtnval

    def status(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> WindowsVPNClientStatus:
        """
            Report the daemon state.

            Tailscale: invokes ``tailscale status --json`` and parses
            the result.  Twingate: invokes ``Get-Service
            twingate.connector | ConvertTo-Json`` and parses the
            result.  Never elevates.

            :param provider: Target provider.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNClientStatus`.
            :raises WindowsVPNClientQueryError: When the underlying
                                                query fails for
                                                reasons other than
                                                "daemon not installed"
                                                or "daemon not
                                                running".
        """
        if provider not in _SUPPORTED_PROVIDERS:
            err_msg_lines = ["Unsupported VPN provider for status query."]
            err_msg_lines.append("    PROVIDED: {!r}".format(provider))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsVPNClientQueryError(message=err_msg)

        if provider is VPNProvider.TAILSCALE:
            rtnval = self._status_tailscale(aspects=aspects)
            return rtnval

        rtnval = self._status_twingate(aspects=aspects)
        return rtnval

    def uninstall(
        self,
        *,
        provider: VPNProvider,
        purge: bool,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Uninstall the provider daemon.

            Looks up the installed MSI's product code via
            ``Get-Package`` and dispatches ``msiexec /x <code> /quiet
            /norestart``.  When ``purge=True``, additionally removes
            the provider's state directory.

            :param provider: Target provider.
            :param purge: When True, additionally remove the
                          provider's state directory.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        rejection = self._validate_provider(
            provider=provider, supported=_SUPPORTED_PROVIDERS,
        )
        if rejection is not None:
            return rejection

        cfg = self._typed_config()
        if provider is VPNProvider.TAILSCALE:
            product_name = cfg.tailscale_product_name
            state_dir = cfg.tailscale_state_dir
        else:
            product_name = cfg.twingate_product_name
            state_dir = cfg.twingate_state_dir

        detail = "uninstall:{}".format(product_name)

        # The uninstall script resolves the MSI ProductCode itself via
        # an inline ``Get-Package`` lookup and then runs ``msiexec /x``
        # against the resolved GUID.  This keeps the dry-run command
        # fully self-contained and runnable — no un-runnable
        # ``<..._product_code>`` placeholder is ever emitted, and the
        # dry-run path still never invokes the runner.
        uninstall_script = self._render_uninstall_msi_script(
            product_name=product_name,
        )
        uninstall_command = self._build_powershell_argv(
            script=uninstall_script,
        )

        dry = self._short_circuit_dry_run(
            provider=provider,
            command=uninstall_command,
            detail=detail,
        )
        if dry is not None:
            return dry

        product_code = self._get_product_code(
            product_name=product_name,
            aspects=aspects,
        )
        if product_code is None:
            skipped = WindowsVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=provider,
                detail="{}_already_absent".format(product_name),
                exit_code=None,
                stdout=None,
                stderr=None,
                command=uninstall_command,
            )
            return skipped

        uninstall_result = self._dispatch_write_with_elevation_check(
            provider=provider,
            command=uninstall_command,
            detail=detail,
            aspects=aspects,
        )
        if uninstall_result.status is not VPNActionStatus.SUCCESS:
            return uninstall_result

        if purge is False:
            return uninstall_result

        purge_script = self._render_remove_directory_script(path=state_dir)
        purge_command = self._build_powershell_argv(script=purge_script)
        purge_detail = "{}+purge:{}".format(detail, state_dir)

        purge_result = self._dispatch_write_with_elevation_check(
            provider=provider,
            command=purge_command,
            detail=purge_detail,
            aspects=aspects,
        )
        return purge_result

    # ------------------------------------------------------------------
    # Provider-specific helpers
    # ------------------------------------------------------------------

    def _bring_up_tailscale(
        self,
        *,
        authkey: str,
        hostname: Optional[str],
        advertise_tags: Optional[List[str]],
        advertise_routes: Optional[List[str]],
        exit_node: bool,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Bring the Tailscale tunnel up.

            See :meth:`bring_up` for parameter semantics.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        cfg = self._typed_config()
        set_service_script = self._render_set_service_startup_script(
            name=cfg.tailscale_service_name,
        )
        start_service_script = self._render_start_service_script(
            name=cfg.tailscale_service_name,
        )
        up_argv = self._render_tailscale_up_argv(
            authkey_file_path=cfg.authkey_file_path,
            hostname=hostname,
            advertise_tags=advertise_tags,
            advertise_routes=advertise_routes,
            exit_node=exit_node,
        )
        # We record the third command (tailscale up) as the canonical
        # would-have-been command for dry-run and SKIPPED results.
        canonical_command = up_argv
        detail = "bring_up_tailscale"

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=canonical_command,
            detail=detail,
        )
        if dry is not None:
            return dry

        existing_status = self._status_tailscale_safe(aspects=aspects)
        if existing_status is not None and existing_status.running is True:
            host_matches = True
            if hostname is not None and existing_status.hostname is not None:
                host_matches = existing_status.hostname == hostname
            if host_matches is True:
                skipped = WindowsVPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TAILSCALE,
                    detail="tailscale_already_running",
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=canonical_command,
                )
                return skipped

        set_service_command = self._build_powershell_argv(
            script=set_service_script,
        )
        result_set_service = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=set_service_command,
            detail="bring_up_tailscale:set_service",
            aspects=aspects,
        )
        if result_set_service.status is not VPNActionStatus.SUCCESS:
            return result_set_service

        start_service_command = self._build_powershell_argv(
            script=start_service_script,
        )
        result_start = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=start_service_command,
            detail="bring_up_tailscale:start_service",
            aspects=aspects,
        )
        if result_start.status is not VPNActionStatus.SUCCESS:
            return result_start

        # Stage the auth key to a file so it never appears on the
        # ``tailscale up`` process command line.  The file is removed
        # immediately after the bring-up attempt, success or failure.
        stage_script = self._render_set_content_script(
            path=cfg.authkey_file_path,
            body=authkey,
        )
        stage_command = self._build_powershell_argv(script=stage_script)
        result_stage = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=stage_command,
            detail="bring_up_tailscale:stage_authkey",
            aspects=aspects,
        )
        if result_stage.status is not VPNActionStatus.SUCCESS:
            return result_stage

        result_up = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=up_argv,
            detail=detail,
            aspects=aspects,
        )

        remove_script = self._render_remove_file_script(
            path=cfg.authkey_file_path,
        )
        remove_command = self._build_powershell_argv(script=remove_script)
        # Best-effort cleanup: the bring-up outcome is what callers care
        # about, so a cleanup failure does not override result_up.
        self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=remove_command,
            detail="bring_up_tailscale:remove_authkey",
            aspects=aspects,
        )
        return result_up

    def _bring_up_twingate(self, *, aspects: Aspects) -> WindowsVPNActionResult:
        """
            Bring the Twingate Connector service up.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        cfg = self._typed_config()
        set_service_script = self._render_set_service_startup_script(
            name=cfg.twingate_service_name,
        )
        start_service_script = self._render_start_service_script(
            name=cfg.twingate_service_name,
        )
        start_command = self._build_powershell_argv(script=start_service_script)
        detail = "bring_up_twingate"

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=start_command,
            detail=detail,
        )
        if dry is not None:
            return dry

        existing_status = self._status_twingate_safe(aspects=aspects)
        if existing_status is not None and existing_status.running is True:
            skipped = WindowsVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate_already_running",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=start_command,
            )
            return skipped

        set_service_command = self._build_powershell_argv(
            script=set_service_script,
        )
        result_set = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TWINGATE,
            command=set_service_command,
            detail="bring_up_twingate:set_service",
            aspects=aspects,
        )
        if result_set.status is not VPNActionStatus.SUCCESS:
            return result_set

        result_start = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TWINGATE,
            command=start_command,
            detail=detail,
            aspects=aspects,
        )
        return result_start

    def _bring_down_tailscale(
        self,
        *,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Bring the Tailscale tunnel down.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        cfg = self._typed_config()
        down_argv = ["tailscale.exe", "down"]
        stop_script = self._render_stop_service_script(
            name=cfg.tailscale_service_name,
        )
        stop_command = self._build_powershell_argv(script=stop_script)
        detail = "bring_down_tailscale"

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=down_argv,
            detail=detail,
        )
        if dry is not None:
            return dry

        existing_status = self._status_tailscale_safe(aspects=aspects)
        if existing_status is not None and existing_status.running is False:
            skipped = WindowsVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale_already_down",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=down_argv,
            )
            return skipped

        result_down = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=down_argv,
            detail="bring_down_tailscale:down",
            aspects=aspects,
        )
        if result_down.status is not VPNActionStatus.SUCCESS:
            return result_down

        result_stop = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TAILSCALE,
            command=stop_command,
            detail=detail,
            aspects=aspects,
        )
        return result_stop

    def _bring_down_twingate(
        self,
        *,
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Bring the Twingate Connector service down.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        cfg = self._typed_config()
        stop_script = self._render_stop_service_script(
            name=cfg.twingate_service_name,
        )
        stop_command = self._build_powershell_argv(script=stop_script)
        detail = "bring_down_twingate"

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=stop_command,
            detail=detail,
        )
        if dry is not None:
            return dry

        existing_status = self._status_twingate_safe(aspects=aspects)
        if existing_status is not None and existing_status.running is False:
            skipped = WindowsVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate_already_down",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=stop_command,
            )
            return skipped

        rtnval = self._dispatch_write_with_elevation_check(
            provider=VPNProvider.TWINGATE,
            command=stop_command,
            detail=detail,
            aspects=aspects,
        )
        return rtnval

    def _status_tailscale(self, *, aspects: Aspects) -> WindowsVPNClientStatus:
        """
            Query Tailscale status via ``tailscale status --json``.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNClientStatus`.
            :raises WindowsVPNClientQueryError: When the runner returns
                                                a non-zero exit code
                                                that is not the
                                                "daemon not installed"
                                                or "daemon not
                                                running" shape.
        """
        command = ["tailscale.exe", "status", "--json"]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            rtnval = _parse_tailscale_status_json(
                payload=run_result.stdout or "",
                provider=VPNProvider.TAILSCALE,
            )
            return rtnval

        stderr_text = (run_result.stderr or "").lower()
        if "not recognized" in stderr_text or "not found" in stderr_text:
            rtnval = WindowsVPNClientStatus(
                provider=VPNProvider.TAILSCALE,
                installed=False,
                running=False,
            )
            return rtnval

        err_msg_lines = ["Tailscale status query failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(run_result.exit_code))
        err_msg_lines.append("    STDERR: {}".format(run_result.stderr))
        err_msg = os.linesep.join(err_msg_lines)
        raise WindowsVPNClientQueryError(
            message=err_msg,
            exit_code=run_result.exit_code,
            stderr=run_result.stderr,
        )

    def _status_twingate(self, *, aspects: Aspects) -> WindowsVPNClientStatus:
        """
            Query Twingate status via ``Get-Service ... | ConvertTo-Json``.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNClientStatus`.
            :raises WindowsVPNClientQueryError: When the runner returns
                                                a non-zero exit code
                                                that is not the
                                                "service not
                                                installed" shape.
        """
        cfg = self._typed_config()
        escaped_name = _ps_escape_single_quoted(value=cfg.twingate_service_name)
        script = (
            "Get-Service -Name '{}' -ErrorAction SilentlyContinue "
            "| Select-Object Name, Status, MachineName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped_name)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            rtnval = _parse_get_service_json(
                payload=run_result.stdout or "",
                provider=VPNProvider.TWINGATE,
            )
            return rtnval

        err_msg_lines = ["Twingate status query failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(run_result.exit_code))
        err_msg_lines.append("    STDERR: {}".format(run_result.stderr))
        err_msg = os.linesep.join(err_msg_lines)
        raise WindowsVPNClientQueryError(
            message=err_msg,
            exit_code=run_result.exit_code,
            stderr=run_result.stderr,
        )

    def _status_tailscale_safe(
        self,
        *,
        aspects: Aspects,
    ) -> Optional[WindowsVPNClientStatus]:
        """
            Best-effort variant of :meth:`_status_tailscale` used by
            idempotency probes.  Returns ``None`` instead of raising
            on query failure.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The status snapshot, or ``None`` on query
                      failure.
        """
        try:
            rtnval: Optional[WindowsVPNClientStatus] = self._status_tailscale(
                aspects=aspects,
            )
        except WindowsVPNClientQueryError:
            rtnval = None
        return rtnval

    def _status_twingate_safe(
        self,
        *,
        aspects: Aspects,
    ) -> Optional[WindowsVPNClientStatus]:
        """
            Best-effort variant of :meth:`_status_twingate`.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The status snapshot, or ``None`` on query
                      failure.
        """
        try:
            rtnval: Optional[WindowsVPNClientStatus] = self._status_twingate(
                aspects=aspects,
            )
        except WindowsVPNClientQueryError:
            rtnval = None
        return rtnval

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def _get_installed_version(
        self,
        *,
        product_name: str,
        aspects: Aspects,
    ) -> Optional[str]:
        """
            Look up the installed package's version via ``Get-Package``.

            :param product_name: The MSI's product name.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The installed version string, or ``None`` when
                      the package is absent or the probe fails.
        """
        escaped = _ps_escape_single_quoted(value=product_name)
        script = (
            "Get-Package -Name '{}' -ErrorAction SilentlyContinue "
            "| Select-Object Name, Version "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = None
                return rtnval
            record = parsed[0]
        else:
            record = parsed

        if not isinstance(record, dict):
            rtnval = None
            return rtnval

        version_raw = record.get("Version")
        if version_raw is None:
            rtnval = None
            return rtnval

        rtnval = str(version_raw)
        return rtnval

    def _get_product_code(
        self,
        *,
        product_name: str,
        aspects: Aspects,
    ) -> Optional[str]:
        """
            Look up the installed package's MSI ProductCode (GUID).

            :param product_name: The MSI's product name.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The product code GUID, or ``None`` when absent.
        """
        escaped = _ps_escape_single_quoted(value=product_name)
        script = (
            "Get-Package -Name '{}' -ErrorAction SilentlyContinue "
            "| Select-Object Name, "
            "@{{Name='ProductCode';Expression={{$_.FastPackageReference}}}} "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = None
                return rtnval
            record = parsed[0]
        else:
            record = parsed

        if not isinstance(record, dict):
            rtnval = None
            return rtnval

        code_raw = record.get("ProductCode")
        if code_raw is None:
            rtnval = None
            return rtnval

        rtnval = str(code_raw)
        return rtnval

    def _read_file_content(
        self,
        *,
        path: str,
        aspects: Aspects,
    ) -> Optional[str]:
        """
            Read a file's content via ``Get-Content``.

            :param path: Path to read.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The file's content, or ``None`` when the file
                      cannot be read.
        """
        escaped = _ps_escape_single_quoted(value=path)
        # ``Get-Content -Raw`` returns an empty string both for an
        # absent file and for a present-but-empty file.  A ``Test-Path``
        # guard plus a sentinel line lets the two cases be told apart:
        # an absent file emits only the sentinel; a present file emits
        # the sentinel followed by the raw content.
        script = (
            "if (Test-Path -LiteralPath '{path}') {{ "
            "Write-Output '{sentinel}'; "
            "Get-Content -LiteralPath '{path}' -Raw "
            "}} else {{ Write-Output '{absent}' }}"
        ).format(
            path=escaped,
            sentinel=_FILE_PRESENT_SENTINEL,
            absent=_FILE_ABSENT_SENTINEL,
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stdout_text = run_result.stdout or ""
        if stdout_text.startswith(_FILE_ABSENT_SENTINEL):
            rtnval = None
            return rtnval

        if stdout_text.startswith(_FILE_PRESENT_SENTINEL):
            remainder = stdout_text[len(_FILE_PRESENT_SENTINEL):]
            # Strip the single line separator the sentinel Write-Output
            # appended, without disturbing the file's own content.
            if remainder.startswith("\r\n") is True:
                rtnval = remainder[2:]
                return rtnval
            if remainder.startswith("\n") is True:
                rtnval = remainder[1:]
                return rtnval
            rtnval = remainder
            return rtnval

        # No sentinel: a runner double that does not execute the
        # branching script.  Fall back to the legacy behaviour.
        rtnval = stdout_text
        return rtnval

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_install_msi_script(
        self,
        *,
        msi_url: str,
        msi_path: str,
        cache_dir: str,
    ) -> str:
        """
            Render the PowerShell script that creates the cache
            directory, downloads the MSI, and runs ``msiexec /i ...
            /quiet /norestart``.

            :param msi_url: Download URL.
            :param msi_path: Destination path for the MSI on disk.
            :param cache_dir: MSI cache directory.

            :returns: The PowerShell scriptblock.
        """
        escaped_cache = _ps_escape_single_quoted(value=cache_dir)
        escaped_url = _ps_escape_single_quoted(value=msi_url)
        escaped_path = _ps_escape_single_quoted(value=msi_path)
        # ``msiexec`` exit code 0 (success) and 3010 (success, reboot
        # required) are the only non-failure outcomes.  The script
        # propagates any other code via ``exit`` so a failed install is
        # never mapped to SUCCESS by the dispatch layer.
        rtnval = (
            "New-Item -ItemType Directory -Force -Path '{cache}' "
            "| Out-Null; "
            "Invoke-WebRequest -Uri '{url}' -OutFile '{path}'; "
            "$proc = Start-Process -FilePath 'msiexec.exe' "
            "-ArgumentList @('/i','{path}','/quiet','/norestart') "
            "-Wait -PassThru; "
            "if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) "
            "{{ exit $proc.ExitCode }}"
        ).format(
            cache=escaped_cache,
            url=escaped_url,
            path=escaped_path,
        )
        return rtnval

    def _render_uninstall_msi_script(self, *, product_name: str) -> str:
        """
            Render a self-contained PowerShell script that resolves the
            installed package's MSI ProductCode via ``Get-Package`` and
            then runs ``msiexec /x <ProductCode> /quiet /norestart``.

            Resolving the code inside the script keeps the rendered
            command runnable as-is (the former design embedded an
            un-runnable ``<..._product_code>`` placeholder), while the
            ``exit`` propagates any genuine ``msiexec`` failure.

            :param product_name: The MSI's product name.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=product_name)
        rtnval = (
            "$pkg = Get-Package -Name '{name}' "
            "-ErrorAction SilentlyContinue; "
            "if ($null -eq $pkg) {{ exit 0 }}; "
            "$code = $pkg.FastPackageReference; "
            "$proc = Start-Process -FilePath 'msiexec.exe' "
            "-ArgumentList @('/x',$code,'/quiet','/norestart') "
            "-Wait -PassThru; "
            "if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) "
            "{{ exit $proc.ExitCode }}"
        ).format(name=escaped)
        return rtnval

    def _render_set_service_startup_script(self, *, name: str) -> str:
        """
            Render ``Set-Service -Name <name> -StartupType Automatic``.

            :param name: Service name.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=name)
        rtnval = "Set-Service -Name '{}' -StartupType Automatic".format(
            escaped,
        )
        return rtnval

    def _render_start_service_script(self, *, name: str) -> str:
        """
            Render ``Start-Service -Name <name>``.

            :param name: Service name.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=name)
        rtnval = "Start-Service -Name '{}'".format(escaped)
        return rtnval

    def _render_stop_service_script(self, *, name: str) -> str:
        """
            Render ``Stop-Service -Name <name>``.

            :param name: Service name.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=name)
        rtnval = "Stop-Service -Name '{}'".format(escaped)
        return rtnval

    def _render_set_content_script(self, *, path: str, body: str) -> str:
        """
            Render a PowerShell script that writes ``body`` to ``path``.

            The body is base64-encoded and decoded inside PowerShell
            before the write.  Inlining multi-line content directly into
            a single-quoted PowerShell string is fragile — embedded
            quotes and line endings do not round-trip reliably — so the
            base64 round-trip is used to make arbitrary content
            (multi-line connector configs included) byte-exact.

            :param path: Destination path.
            :param body: File content.

            :returns: The PowerShell scriptblock.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        body_bytes = body.encode("utf-8")
        encoded_body = base64.b64encode(body_bytes).decode("ascii")
        rtnval = (
            "$content = [System.Text.Encoding]::UTF8.GetString("
            "[System.Convert]::FromBase64String('{body}')); "
            "[System.IO.File]::WriteAllText('{path}', $content, "
            "(New-Object System.Text.UTF8Encoding $false))"
        ).format(body=encoded_body, path=escaped_path)
        return rtnval

    def _render_remove_file_script(self, *, path: str) -> str:
        """
            Render ``Remove-Item -Force -Path <path>`` for a single
            file (used to scrub the staged auth-key file).

            :param path: File path to remove.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=path)
        rtnval = (
            "Remove-Item -Force -Path '{}' -ErrorAction SilentlyContinue"
        ).format(escaped)
        return rtnval

    def _render_remove_directory_script(self, *, path: str) -> str:
        """
            Render ``Remove-Item -Recurse -Force -Path <path>``.

            :param path: Path to remove.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=path)
        rtnval = (
            "Remove-Item -Recurse -Force -Path '{}' "
            "-ErrorAction SilentlyContinue"
        ).format(escaped)
        return rtnval

    def _render_tailscale_up_argv(
        self,
        *,
        authkey_file_path: str,
        hostname: Optional[str],
        advertise_tags: Optional[List[str]],
        advertise_routes: Optional[List[str]],
        exit_node: bool,
    ) -> List[str]:
        """
            Build the ``tailscale.exe up ...`` argv.

            The auth key is referenced via ``--auth-key=file:<path>``
            rather than being embedded directly on the command line, so
            the secret is never visible to ``Get-Process`` / Sysmon
            command-line auditing for the process lifetime.

            :param authkey_file_path: Path of the file holding the auth
                                      key (staged by the caller, removed
                                      afterwards).
            :param hostname: Optional hostname.
            :param advertise_tags: Optional tags list.
            :param advertise_routes: Optional routes list.
            :param exit_node: When True, advertise this host as exit
                              node.

            :returns: The argv as a list of strings.
        """
        rtnval: List[str] = ["tailscale.exe", "up"]
        rtnval.append("--auth-key=file:{}".format(authkey_file_path))
        if hostname is not None and len(hostname) > 0:
            rtnval.append("--hostname={}".format(hostname))
        if advertise_tags is not None and len(advertise_tags) > 0:
            joined_tags = ",".join(advertise_tags)
            rtnval.append("--advertise-tags={}".format(joined_tags))
        if advertise_routes is not None and len(advertise_routes) > 0:
            joined_routes = ",".join(advertise_routes)
            rtnval.append("--advertise-routes={}".format(joined_routes))
        if exit_node is True:
            rtnval.append("--advertise-exit-node")
        return rtnval

    def _render_twingate_config(self, *, settings: Dict[str, str]) -> str:
        """
            Render the Twingate connector config body deterministically.

            :param settings: Provider settings.

            :returns: A multi-line string in ``KEY=VALUE`` form, sorted
                      by key, terminated by a final newline.
        """
        lines: List[str] = []
        for key in sorted(settings.keys()):
            value = settings[key]
            lines.append("{}={}".format(key, value))
        rtnval = "\n".join(lines) + "\n"
        return rtnval

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_write_with_elevation_check(
        self,
        *,
        provider: VPNProvider,
        command: List[str],
        detail: Optional[str],
        aspects: Aspects,
    ) -> WindowsVPNActionResult:
        """
            Dispatch a write through the runner and translate the
            ``elevation_blocked`` runner flag into a stable
            ``"elevation_blocked"`` token on the resulting
            :class:`VPNActionResult`.

            :param provider: Provider the command targets.
            :param command: argv to execute.
            :param detail: Human-readable description.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: The :class:`WindowsVPNActionResult`.
        """
        command_copy = list(command)
        run_elevated = self._should_elevate(kind=VERB_KIND_WRITE)

        run_result: RunResult = self._runner.run(
            command_copy,
            elevated=run_elevated,
        )

        if _is_elevation_blocked(run_result=run_result) is True:
            blocked_detail = self._compose_elevation_blocked_detail(
                base_detail=detail,
            )
            rtnval = WindowsVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=provider,
                detail=blocked_detail,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=command_copy,
            )
            return rtnval

        if run_result.exit_code == 0:
            mapped_status = VPNActionStatus.SUCCESS
        else:
            mapped_status = VPNActionStatus.FAILED

        rtnval = WindowsVPNActionResult(
            status=mapped_status,
            provider=provider,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=command_copy,
        )
        return rtnval

    def _compose_elevation_blocked_detail(
        self,
        *,
        base_detail: Optional[str],
    ) -> str:
        """
            Build the stable elevation-blocked detail token.  Prefixes
            the base detail (if any) so callers can both pattern-match
            on the stable token and read the original operation name.

            :param base_detail: Optional operation descriptor.

            :returns: ``"elevation_blocked"`` when no base detail, or
                      ``"elevation_blocked: <base_detail>"`` when one
                      is supplied.
        """
        if base_detail is None or len(base_detail) == 0:
            rtnval = _ELEVATION_BLOCKED_TOKEN
            return rtnval

        rtnval = "{}: {}".format(_ELEVATION_BLOCKED_TOKEN, base_detail)
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> List[str]:
        """
            Build the canonical PowerShell argv for a scriptblock.

            :param script: PowerShell scriptblock text.

            :returns: The full argv list.
        """
        cfg = self._typed_config()
        rtnval = [
            cfg.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _typed_config(self) -> WindowsVPNClientManagerConfig:
        """
            Return the manager config typed as a
            :class:`WindowsVPNClientManagerConfig`.  When the caller
            supplied a base :class:`VPNClientManagerConfig`, a
            defaults-filled Windows wrapper is built on the fly.

            :returns: The typed config.
        """
        if isinstance(self._config, WindowsVPNClientManagerConfig):
            rtnval = self._config
            return rtnval

        rtnval = WindowsVPNClientManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
        )
        return rtnval

    def _resolve_effective_version(
        self,
        *,
        requested_version: Optional[str],
    ) -> str:
        """
            Resolve the version token used in URLs and cached file
            names.

            :param requested_version: Caller-supplied version or
                                      ``None``.

            :returns: ``"latest"`` when ``requested_version`` is
                      ``None``; otherwise the supplied version.
        """
        if requested_version is None:
            rtnval = "latest"
            return rtnval
        rtnval = requested_version
        return rtnval

    def _tailscale_msi_path(self, *, version: str) -> str:
        """
            Compute the staged path for the Tailscale MSI.

            :param version: Effective version token.

            :returns: ``<msi_cache_dir>\\tailscale-setup-<version>-
                      <arch>.msi``.
        """
        cfg = self._typed_config()
        rtnval = "{}\\tailscale-setup-{}-{}.msi".format(
            cfg.msi_cache_dir, version, cfg.tailscale_msi_arch,
        )
        return rtnval

    def _twingate_msi_path(self, *, version: str) -> str:
        """
            Compute the staged path for the Twingate Connector MSI.

            :param version: Effective version token.

            :returns: ``<msi_cache_dir>\\twingate-connector-<version>.msi``.
        """
        cfg = self._typed_config()
        rtnval = "{}\\twingate-connector-{}.msi".format(
            cfg.msi_cache_dir, version,
        )
        return rtnval


__all__ = [
    "WindowsVPNActionResult",
    "WindowsVPNActionStatus",
    "WindowsVPNClientManager",
    "WindowsVPNClientManagerConfig",
    "WindowsVPNClientQueryError",
    "WindowsVPNClientStatus",
    "VPNProvider",
]

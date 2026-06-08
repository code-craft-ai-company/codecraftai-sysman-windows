"""
    Windows precision-time-management feature.

    Implements the cross-OS precision-time-management contract for
    Windows using the Windows Time Service (``w32tm.exe``) plus
    ``net.exe`` / ``sc.exe`` for the lifecycle of the underlying
    ``W32Time`` Windows service.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator`.  Persistent configuration lives in the
        registry under ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\W32Time``;
        ``w32tm /config /update`` is the supported public surface for
        applying changes, and that is what this manager calls.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_time_status``, ``get_time_sources``, ``get_drift``,
        idempotency probes) never elevate.

    Service lifecycle:
        Lifecycle is performed via ``net start`` / ``net stop`` plus
        ``sc.exe config <service> start=`` for boot-time posture.
        ``Start-Service`` / ``Set-Service`` PowerShell wrappers are NOT
        used here; the manager keeps the verb surface to native
        Win32 utilities so a host without PowerShell still answers.

    Output parsing:
        ``w32tm /query /status /verbose`` and ``w32tm /query /peers``
        emit human-formatted ``Key: Value`` lines.  The parsers are
        pure-Python keyed-line splitters that tolerate whitespace
        padding in column-aligned output.  The drift parser handles
        the four-column ``w32tm /stripchart`` ``dataonly`` payload.
"""

import os
import re

from dataclasses import dataclass
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_W32TM = "w32tm"
_NET = "net"
_SC = "sc"

_DEFAULT_SERVICE_NAME = "w32time"

# ``w32tm /stripchart`` lines look like::
#
#     17:30:01, +00.0001234s
#
# The parser pulls the offset out of the trailing token; we accept an
# optional sign, decimal, and ``s`` suffix.
_STRIPCHART_OFFSET_PATTERN = re.compile(
    r"([+-]?\d+\.\d+)s",
)


WindowsPrecisionTimeActionStatus = ActionStatus


@dataclass
class WindowsPrecisionTimeManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsPrecisionTimeManager`.

        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param service_name: The Windows service name for the
                             Windows Time Service.  Defaults to
                             ``"w32time"``.
    """
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class WindowsPrecisionTimeActionResult(FeatureActionResult):
    """
        Result of a precision-time write operation.

        :param server: The server hostname or address the action
                       targeted, or ``None`` for verbs that do not
                       target a single server (``force_sync_now``,
                       ``enable_sync``, ``disable_sync``).
    """
    server: Optional[str] = None


@dataclass
class WindowsTimeStatus:
    """
        Snapshot of the host's overall time-sync status.

        :param enabled: Whether the Windows Time Service is running.
        :param synchronized: Whether ``w32tm /query /status`` reports
                             a valid stratum (i.e. the host has
                             received at least one sync).
        :param current_server: The current source as reported by
                               ``Source:`` in
                               ``w32tm /query /status /verbose``.
        :param stratum: Stratum reported by ``Stratum:``.
        :param last_sync: ``Last Successful Sync Time:`` value as a
                          string.
        :param offset_seconds: Phase offset reported by
                               ``Phase Offset:`` in seconds, or
                               ``None``.
    """
    enabled: bool
    synchronized: bool
    current_server: Optional[str]
    stratum: Optional[int]
    last_sync: Optional[str]
    offset_seconds: Optional[float]


@dataclass
class WindowsTimeSourceInfo:
    """
        One configured upstream time source.

        :param server: Hostname or address of the source.
        :param state: Native state token.  Mapped from the
                      ``State:`` line of ``w32tm /query /peers`` to
                      ``"selected"`` / ``"candidate"`` / ``"unreachable"``.
        :param stratum: Source stratum, or ``None``.
        :param offset_seconds: Last reported offset in seconds, or
                               ``None``.
        :param last_response: ``Last Successful Sync Time:`` token, or
                              ``None``.
    """
    server: str
    state: str
    stratum: Optional[int]
    offset_seconds: Optional[float]
    last_response: Optional[str]


@dataclass
class WindowsTimeDrift:
    """
        Measured drift snapshot.

        :param offset_seconds: Most recent offset reported by
                               ``w32tm /stripchart``.
        :param frequency_ppm: Always ``None`` — Windows does not
                              expose a frequency-adjustment value
                              through ``w32tm`` user-space surface.
        :param jitter_seconds: Always ``None`` for the same reason.
        :param stratum: Stratum from ``w32tm /query /status`` if
                        available.
    """
    offset_seconds: float
    frequency_ppm: Optional[float]
    jitter_seconds: Optional[float]
    stratum: Optional[int]


class WindowsPrecisionTimeQueryError(Exception):
    """
        Raised when a precision-time read fails for a reason other
        than the W32Time service simply not running.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Precision-time query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsPrecisionTimeManager(BaseFeatureManager):
    """
        Windows precision-time manager.

        Routes every operation through the injected
        :class:`ICommandRunner`.  Maps the contract verbs onto
        ``w32tm.exe`` plus ``net.exe`` / ``sc.exe`` for the
        ``W32Time`` service lifecycle.
    """

    result_class = WindowsPrecisionTimeActionResult
    status_class = WindowsPrecisionTimeActionStatus
    feature_name = "precision-time-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_time_status(self) -> WindowsTimeStatus:
        """
            Query the host's overall sync status via
            ``w32tm /query /status /verbose``.  Never elevates.

            When the service is not running the method returns a
            ``WindowsTimeStatus`` with ``enabled=False`` and
            ``synchronized=False`` rather than raising.

            :returns: The :class:`WindowsTimeStatus`.
            :raises WindowsPrecisionTimeQueryError: When ``w32tm``
                                                     fails for a
                                                     reason other
                                                     than the
                                                     service being
                                                     down.
        """
        command = [_W32TM, "/query", "/status", "/verbose"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if _stderr_indicates_inactive(stderr=stderr_text):
                rtnval = WindowsTimeStatus(
                    enabled=False,
                    synchronized=False,
                    current_server=None,
                    stratum=None,
                    last_sync=None,
                    offset_seconds=None,
                )
                return rtnval
            raise WindowsPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_w32tm_status(text=run_result.stdout)
        return rtnval

    def get_time_sources(self) -> list[WindowsTimeSourceInfo]:
        """
            Enumerate configured time sources via
            ``w32tm /query /peers``.  Never elevates.

            :returns: One :class:`WindowsTimeSourceInfo` per peer.
                      May be empty when the service is inactive.
            :raises WindowsPrecisionTimeQueryError: When ``w32tm``
                                                     fails
                                                     unexpectedly.
        """
        command = [_W32TM, "/query", "/peers"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if _stderr_indicates_inactive(stderr=stderr_text):
                rtnval: list[WindowsTimeSourceInfo] = []
                return rtnval
            raise WindowsPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_w32tm_peers(text=run_result.stdout)
        return rtnval

    def get_drift(self) -> WindowsTimeDrift:
        """
            Read the current drift snapshot via
            ``w32tm /stripchart /computer:<server> /samples:1
            /dataonly``.  Never elevates.

            Picks the currently-configured peer (from
            ``w32tm /query /status /verbose``).

            :returns: The :class:`WindowsTimeDrift`.
            :raises WindowsPrecisionTimeQueryError: When ``w32tm``
                                                     fails
                                                     unexpectedly.
        """
        status = self.get_time_status()
        server = status.current_server
        stratum = status.stratum
        if server is None or len(server) == 0:
            rtnval = WindowsTimeDrift(
                offset_seconds=0.0,
                frequency_ppm=None,
                jitter_seconds=None,
                stratum=stratum,
            )
            return rtnval

        command = [
            _W32TM, "/stripchart",
            "/computer:{}".format(server),
            "/samples:1",
            "/dataonly",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        offset = _parse_w32tm_stripchart_offset(text=run_result.stdout)
        rtnval = WindowsTimeDrift(
            offset_seconds=offset,
            frequency_ppm=None,
            jitter_seconds=None,
            stratum=stratum,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_time_source(
        self, server: str,
    ) -> WindowsPrecisionTimeActionResult:
        """
            Add an upstream time source.

            Reads the current peer list, appends ``server``, and
            invokes
            ``w32tm /config /manualpeerlist:"<list>"
            /syncfromflags:manual /update`` followed by a service
            restart.

            Idempotent: ``SKIPPED`` when the server is already in
            the current list.

            :param server: Server hostname or address.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        action = "add_time_source:{}".format(server)
        config_command_template = [
            _W32TM, "/config",
            "/manualpeerlist:{}".format(server),
            "/syncfromflags:manual",
            "/update",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=config_command_template,
                server=server,
            )
            return dry

        current_peers = self._read_current_peers()
        if server in current_peers:
            skipped = self._build_result(
                status=WindowsPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=config_command_template,
                server=server,
            )
            return skipped

        new_peers = list(current_peers)
        new_peers.append(server)
        rtnval = self._apply_peer_list(
            peers=new_peers, detail=action, server=server,
        )
        return rtnval

    def remove_time_source(
        self, server: str,
    ) -> WindowsPrecisionTimeActionResult:
        """
            Remove an upstream time source.

            Idempotent: ``SKIPPED`` when the server is not present.

            :param server: Server hostname or address.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        action = "remove_time_source:{}".format(server)
        placeholder_command = [
            _W32TM, "/config",
            "/manualpeerlist:",
            "/syncfromflags:manual",
            "/update",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=placeholder_command,
                server=server,
            )
            return dry

        current_peers = self._read_current_peers()
        if server not in current_peers:
            skipped = self._build_result(
                status=WindowsPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=placeholder_command,
                server=server,
            )
            return skipped

        new_peers = [item for item in current_peers if item != server]
        rtnval = self._apply_peer_list(
            peers=new_peers, detail=action, server=server,
        )
        return rtnval

    def set_time_servers(
        self, servers: list[str],
    ) -> WindowsPrecisionTimeActionResult:
        """
            Replace the active set of upstream sources.

            Idempotent: ``SKIPPED`` when the current sorted list
            equals the desired sorted list.

            :param servers: Ordered list of desired servers.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        action = "set_time_servers:{}".format(",".join(servers))
        rendered = " ".join(servers)
        config_command = [
            _W32TM, "/config",
            "/manualpeerlist:{}".format(rendered),
            "/syncfromflags:manual",
            "/update",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=config_command,
                server=None,
            )
            return dry

        current_peers = self._read_current_peers()
        if sorted(current_peers) == sorted(servers):
            skipped = self._build_result(
                status=WindowsPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=config_command,
                server=None,
            )
            return skipped

        rtnval = self._apply_peer_list(
            peers=list(servers), detail=action, server=None,
        )
        return rtnval

    def force_sync_now(self) -> WindowsPrecisionTimeActionResult:
        """
            Force an immediate re-sync via ``w32tm /resync /force``.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        command = [_W32TM, "/resync", "/force"]
        rtnval = self._dispatch_write(
            command=command,
            detail="force_sync_now",
            server=None,
        )
        return rtnval

    def enable_sync(self) -> WindowsPrecisionTimeActionResult:
        """
            Enable the W32Time service via ``net start <service>``
            and ``sc config <service> start= auto``.

            Idempotent: ``SKIPPED`` when the service is already
            running.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        service = self._config.service_name
        command = [_NET, "start", service]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsPrecisionTimeActionStatus.DRY_RUN,
                detail="enable_sync",
                command=command,
                server=None,
            )
            return dry

        if self._service_is_running():
            skipped = self._build_result(
                status=WindowsPrecisionTimeActionStatus.SKIPPED,
                detail="enable_sync",
                command=command,
                server=None,
            )
            return skipped

        config_command = [
            _SC, "config", service, "start=", "auto",
        ]
        config_run = self._runner.run(
            list(config_command),
            elevated=self._config.elevated,
        )
        if config_run.exit_code != 0:
            failed = WindowsPrecisionTimeActionResult(
                status=WindowsPrecisionTimeActionStatus.FAILED,
                detail="enable_sync",
                exit_code=config_run.exit_code,
                stdout=config_run.stdout,
                stderr=config_run.stderr,
                command=list(config_command),
                server=None,
            )
            return failed

        rtnval = self._dispatch_write(
            command=command,
            detail="enable_sync",
            server=None,
        )
        return rtnval

    def disable_sync(self) -> WindowsPrecisionTimeActionResult:
        """
            Disable the W32Time service via ``net stop <service>``
            and ``sc config <service> start= disabled``.

            Idempotent: ``SKIPPED`` when the service is already
            stopped.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        service = self._config.service_name
        command = [_NET, "stop", service]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsPrecisionTimeActionStatus.DRY_RUN,
                detail="disable_sync",
                command=command,
                server=None,
            )
            return dry

        if not self._service_is_running():
            skipped = self._build_result(
                status=WindowsPrecisionTimeActionStatus.SKIPPED,
                detail="disable_sync",
                command=command,
                server=None,
            )
            return skipped

        stop_run = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if stop_run.exit_code != 0:
            failed = WindowsPrecisionTimeActionResult(
                status=WindowsPrecisionTimeActionStatus.FAILED,
                detail="disable_sync",
                exit_code=stop_run.exit_code,
                stdout=stop_run.stdout,
                stderr=stop_run.stderr,
                command=list(command),
                server=None,
            )
            return failed

        config_command = [
            _SC, "config", service, "start=", "disabled",
        ]
        config_run = self._runner.run(
            list(config_command),
            elevated=self._config.elevated,
        )
        if config_run.exit_code == 0:
            mapped_status = WindowsPrecisionTimeActionStatus.SUCCESS
        else:
            mapped_status = WindowsPrecisionTimeActionStatus.FAILED

        rtnval = WindowsPrecisionTimeActionResult(
            status=mapped_status,
            detail="disable_sync",
            exit_code=config_run.exit_code,
            stdout=config_run.stdout,
            stderr=config_run.stderr,
            command=list(config_command),
            server=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_current_peers(self) -> list[str]:
        """
            Read the currently configured peer list from
            ``w32tm /query /peers``.  Never elevates.

            :returns: Configured peers in declaration order.
                      Empty list when the service is inactive.
        """
        command = [_W32TM, "/query", "/peers"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = []
            return rtnval

        sources = _parse_w32tm_peers(text=run_result.stdout)
        rtnval = [info.server for info in sources]
        return rtnval

    def _service_is_running(self) -> bool:
        """
            Probe whether the W32Time service is currently running
            via ``sc query <service>``.

            :returns: True when ``STATE`` reports ``RUNNING``.
        """
        service = self._config.service_name
        command = [_SC, "query", service]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        for raw in run_result.stdout.splitlines():
            line = raw.strip()
            if not line.startswith("STATE"):
                continue
            if "RUNNING" in line.upper():
                rtnval = True
                return rtnval
            rtnval = False
            return rtnval
        rtnval = False
        return rtnval

    def _apply_peer_list(
        self,
        *,
        peers: list[str],
        detail: str,
        server: Optional[str],
    ) -> WindowsPrecisionTimeActionResult:
        """
            Apply a peer list via ``w32tm /config`` and bounce the
            service.

            :param peers: Desired peer list.
            :param detail: ``detail`` value to echo on the result.
            :param server: ``server`` value to echo on the result.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        rendered = " ".join(peers)
        config_command = [
            _W32TM, "/config",
            "/manualpeerlist:{}".format(rendered),
            "/syncfromflags:manual",
            "/update",
        ]
        config_run = self._runner.run(
            list(config_command),
            elevated=self._config.elevated,
        )
        if config_run.exit_code != 0:
            failed = WindowsPrecisionTimeActionResult(
                status=WindowsPrecisionTimeActionStatus.FAILED,
                detail=detail,
                exit_code=config_run.exit_code,
                stdout=config_run.stdout,
                stderr=config_run.stderr,
                command=list(config_command),
                server=server,
            )
            return failed

        service = self._config.service_name
        stop_command = [_NET, "stop", service]
        stop_run = self._runner.run(
            list(stop_command),
            elevated=self._config.elevated,
        )
        # ``net stop`` returns non-zero when the service is already
        # stopped; that is an acceptable state for a fresh start.

        start_command = [_NET, "start", service]
        start_run = self._runner.run(
            list(start_command),
            elevated=self._config.elevated,
        )
        if start_run.exit_code != 0:
            failed = WindowsPrecisionTimeActionResult(
                status=WindowsPrecisionTimeActionStatus.FAILED,
                detail=detail,
                exit_code=start_run.exit_code,
                stdout=start_run.stdout,
                stderr=start_run.stderr,
                command=list(start_command),
                server=server,
            )
            return failed

        # Note: stop_run is intentionally inspected only via
        # ``stop_run.stdout``; an already-stopped service is normal.
        _ = stop_run.stdout

        rtnval = WindowsPrecisionTimeActionResult(
            status=WindowsPrecisionTimeActionStatus.SUCCESS,
            detail=detail,
            exit_code=start_run.exit_code,
            stdout=start_run.stdout,
            stderr=start_run.stderr,
            command=list(start_command),
            server=server,
        )
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        detail: str,
        server: Optional[str],
    ) -> WindowsPrecisionTimeActionResult:
        """
            Dispatch a single-shot write command and wrap the
            result.

            :param command: The full argv.
            :param detail: ``detail`` value to echo.
            :param server: ``server`` value to echo.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsPrecisionTimeActionStatus.DRY_RUN,
                detail=detail,
                command=command,
                server=server,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsPrecisionTimeActionStatus.SUCCESS
        else:
            mapped_status = WindowsPrecisionTimeActionStatus.FAILED

        rtnval = WindowsPrecisionTimeActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            server=server,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsPrecisionTimeActionStatus,
        detail: str,
        command: list[str],
        server: Optional[str],
    ) -> WindowsPrecisionTimeActionResult:
        """
            Construct a :class:`WindowsPrecisionTimeActionResult`
            without invoking the runner.

            :param status: Outcome status.
            :param detail: ``detail`` value to echo.
            :param command: ``command`` argv to record.
            :param server: ``server`` value to echo.

            :returns: The :class:`WindowsPrecisionTimeActionResult`.
        """
        rtnval = WindowsPrecisionTimeActionResult(
            status=status,
            detail=detail,
            command=list(command),
            server=server,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _stderr_indicates_inactive(*, stderr: str) -> bool:
    """
        Heuristic for "the W32Time service is not running".

        :param stderr: Captured stderr text.

        :returns: True when stderr matches a known inactive-service
                  signature.
    """
    haystack = (stderr or "").lower()
    needles = (
        "0x80070426",
        "the service has not been started",
        "service is not running",
        "could not connect to",
    )
    for needle in needles:
        if needle in haystack:
            rtnval = True
            return rtnval
    rtnval = False
    return rtnval


def _parse_keyed_lines(*, text: str) -> dict:
    """
        Split a multi-line ``Key: Value`` payload into a dict.

        :param text: Raw text.

        :returns: Mapping of key to value (last write wins).
    """
    rtnval: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if ":" not in line:
            continue
        head, _, tail = line.partition(":")
        key = head.strip()
        value = tail.strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _parse_w32tm_status(*, text: str) -> WindowsTimeStatus:
    """
        Parse ``w32tm /query /status /verbose`` output.

        :param text: Raw stdout.

        :returns: The :class:`WindowsTimeStatus`.
    """
    fields = _parse_keyed_lines(text=text)

    source_raw = fields.get("Source", "")
    current_server: Optional[str]
    if len(source_raw) > 0:
        current_server = source_raw
    else:
        current_server = None

    stratum = _parse_optional_int_first_token(
        value=fields.get("Stratum", ""),
    )

    last_sync_raw = fields.get("Last Successful Sync Time", "")
    last_sync: Optional[str]
    if len(last_sync_raw) > 0 and last_sync_raw.lower() != "unspecified":
        last_sync = last_sync_raw
    else:
        last_sync = None

    offset = _parse_optional_seconds_field(
        value=fields.get("Phase Offset", ""),
    )

    if last_sync is not None and stratum is not None and stratum > 0:
        synchronized = True
    else:
        synchronized = False

    rtnval = WindowsTimeStatus(
        enabled=True,
        synchronized=synchronized,
        current_server=current_server,
        stratum=stratum,
        last_sync=last_sync,
        offset_seconds=offset,
    )
    return rtnval


def _parse_w32tm_peers(*, text: str) -> list[WindowsTimeSourceInfo]:
    """
        Parse ``w32tm /query /peers`` output into per-peer records.

        ``w32tm`` emits one stanza per peer separated by a blank
        line, with lines like::

            #Peers: 2

            Peer: ntp1.example.com,0x1
            State: Active
            Time Remaining: 60.0000000s
            Mode: 1 (Symmetric Active)
            Stratum: 2 (secondary reference - syncd by (S)NTP)
            PeerPoll Interval: 10 (1024s)
            HostPoll Interval: 10 (1024s)
            Last Successful Sync Time: 4/24/2026 10:30:01 AM

        The parser splits on blank lines, then walks each stanza for
        the ``Peer:`` / ``State:`` / ``Stratum:`` /
        ``Last Successful Sync Time:`` fields.

        :param text: Raw stdout.

        :returns: One :class:`WindowsTimeSourceInfo` per peer.
    """
    rtnval: list[WindowsTimeSourceInfo] = []
    stanzas = _split_into_stanzas(text=text)
    for stanza in stanzas:
        fields = _parse_keyed_lines(text=stanza)

        peer_raw = fields.get("Peer", "")
        if len(peer_raw) == 0:
            continue
        # Strip the trailing flags after the comma (``,0x1``).
        head_token = peer_raw.split(",")[0].strip()
        if len(head_token) == 0:
            continue

        state_raw = fields.get("State", "")
        state = _classify_w32tm_state(value=state_raw)

        stratum = _parse_optional_int_first_token(
            value=fields.get("Stratum", ""),
        )

        last_sync = fields.get("Last Successful Sync Time", "")
        last_response: Optional[str]
        if len(last_sync) > 0:
            last_response = last_sync
        else:
            last_response = None

        info = WindowsTimeSourceInfo(
            server=head_token,
            state=state,
            stratum=stratum,
            offset_seconds=None,
            last_response=last_response,
        )
        rtnval.append(info)

    return rtnval


def _classify_w32tm_state(*, value: str) -> str:
    """
        Map a w32tm ``State:`` value onto the contract's
        ``selected``/``candidate``/``unreachable`` vocabulary.

        :param value: Raw state field.

        :returns: One of ``"selected"``, ``"candidate"``,
                  ``"unreachable"``.
    """
    lowered = value.strip().lower()
    if "active" in lowered:
        rtnval = "selected"
        return rtnval
    if "candidate" in lowered:
        rtnval = "candidate"
        return rtnval
    if "pending" in lowered:
        rtnval = "candidate"
        return rtnval
    if "rejected" in lowered:
        rtnval = "unreachable"
        return rtnval
    if "no" in lowered and "valid" in lowered:
        rtnval = "unreachable"
        return rtnval
    rtnval = "candidate"
    return rtnval


def _split_into_stanzas(*, text: str) -> list[str]:
    """
        Split a multi-stanza payload into a list of stanza bodies.

        Stanzas are separated by blank lines.  Trailing whitespace
        on each line is preserved.

        :param text: Raw text.

        :returns: List of stanza bodies.
    """
    rtnval: list[str] = []
    current_lines: list[str] = []
    for raw in text.splitlines():
        if len(raw.strip()) == 0:
            if len(current_lines) > 0:
                rtnval.append("\n".join(current_lines))
                current_lines = []
            continue
        current_lines.append(raw)
    if len(current_lines) > 0:
        rtnval.append("\n".join(current_lines))
    return rtnval


def _parse_optional_int_first_token(*, value: str) -> Optional[int]:
    """
        Parse the leading integer token out of a w32tm field that
        commonly contains both the integer and a parenthesised
        description (e.g. ``"2 (secondary reference)"``).

        :param value: Raw value.

        :returns: Integer or ``None``.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval: Optional[int] = None
        return rtnval
    head = stripped.split()[0]
    try:
        parsed = int(head)
    except ValueError:
        rtnval = None
        return rtnval
    rtnval = parsed
    return rtnval


def _parse_optional_seconds_field(*, value: str) -> Optional[float]:
    """
        Parse a w32tm ``"<num>s"`` seconds field into a float.

        ``Phase Offset`` and similar fields print values like
        ``"0.0001234s"`` or ``"-0.0007890s"``.

        :param value: Raw value.

        :returns: Offset in seconds, or ``None`` when unparseable.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval: Optional[float] = None
        return rtnval
    if stripped.endswith("s"):
        head = stripped[:-1]
    else:
        head = stripped
    try:
        parsed = float(head)
    except ValueError:
        rtnval = None
        return rtnval
    rtnval = parsed
    return rtnval


def _parse_w32tm_stripchart_offset(*, text: str) -> float:
    """
        Parse a single offset value out of
        ``w32tm /stripchart .. /samples:1 /dataonly`` output.

        Sample line::

            17:30:01, +00.0001234s

        :param text: Raw stdout.

        :returns: Offset in seconds (0.0 when unparseable).
    """
    rtnval = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        match = _STRIPCHART_OFFSET_PATTERN.search(line)
        if match is None:
            continue
        try:
            parsed = float(match.group(1))
        except ValueError:
            continue
        rtnval = parsed
        return rtnval
    return rtnval
